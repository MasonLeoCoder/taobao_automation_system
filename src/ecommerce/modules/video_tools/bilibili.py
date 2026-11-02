"""B站下载工作台：解析、显式范围、逐集任务与可核验的中断恢复。"""
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

from ecommerce.infrastructure.media import local, transfer
from ecommerce.infrastructure.tasks.events import current_events
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.infrastructure.tasks.runner import current_task, run_task
from ecommerce.platforms.bilibili import video as platform
from ecommerce.platforms.bilibili.links import parse_input
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped
from .media import image_file

from .cache import MediaCache


def resource_key(episode, kind, video_id='', audio_id='', output_format='original'):
    """稳定标识所选组合；参数：集、类型、轨道及封面格式；返回：资源编号。"""
    return hashlib.sha256(json.dumps([episode, kind, video_id, audio_id, output_format]).encode()).hexdigest()[:24]


def filename(episode, kind, track=None, extension=None):
    """生成保留BV、分P及规格的安全名称；参数：元数据、类型和扩展名；返回：文件名。"""
    def clean(value, length):
        """去掉Windows保留字符；参数：原文和长度；返回：文件名片段。"""
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:length] or '视频'
    parts = [clean(episode['submission_title'], 55), episode['bvid'], f'P{episode["page"]:02d}', clean(episode['title'], 45)]
    if episode.get('collection_index'):
        parts.insert(0, f'E{episode["collection_index"]:02d}')
    if kind == 'video' and track:
        codec = track['codec'].split('.')[0]
        codec = 'H264' if codec.startswith('avc') else 'H265' if codec.startswith(('hvc', 'hev')) else 'AV1' if codec.startswith('av01') else codec
        parts += [str(track.get('height') or track.get('quality') or '原画') + 'P', clean(codec, 12)]
    elif kind != 'video':
        parts.append('完整声音' if kind == 'audio' else '封面')
    return '_'.join(parts) + (extension or ('.mp4' if kind == 'video' else '.m4a'))


class BilibiliTools(MediaCache):
    """独立平台任务与缓存，复用统一任务存储和通用媒体能力。"""


    def start(self, text, interactive=False):
        """创建解析记录，不下载视频；参数：单个入口和登录模式；返回：记录编号。"""
        video = parse_input(text)
        with self.lock:
            self._idle()
            self.cleanup()
            identifier = uuid4().hex
            self._path(identifier).mkdir()
            base = Path(self.options['temp_dir']) if self.options['temp_dir'] else self.settings.workspace / 'output/temp'
            row = {'version': 1, 'id': identifier, 'input': video.url, 'created': time.time(), 'touched': time.time(),
                   'cache_dir': str(base / 'bilibili' / self.scope / identifier), 'phase': 'queued', 'message': '等待解析',
                   'task_id': None, 'attempts': [], 'metadata_task_id': None, 'metadata': None, 'catalogs': {}, 'resources': {}, 'plan': []}
            self._cache(row).mkdir(parents=True, exist_ok=True)
            self._write(row)
            self._launch(row, 'resolve', {'interactive': interactive})
            return identifier

    def _episodes(self, row):
        """合并可选集身份；参数：记录；返回：编号到元数据的映射。"""
        meta = row.get('metadata') or {}
        result = {}
        for part in [*(meta.get('collection') or {}).get('items', []), *meta.get('parts', [])]:
            result[part['id']] = {**result.get(part['id'], {}), **part}
        return result

    def select(self, identifier, episode_id):
        """获取某一集实际规格；参数：记录和集编号；返回：无。"""
        with self.lock:
            self._idle()
            row = self._read(identifier)
            if episode_id not in self._episodes(row):
                raise ValueError('请选择该记录中的分集')
            if not self._success(row.get('metadata_task_id')):
                raise ValueError('原解析任务没有成功，请重新解析')
            self._launch(row, 'formats', {'episode_id': episode_id})

    def prepare(self, identifier, *, episode_id, video_id, audio_id='', kind='video', scope='single', output_format='original'):
        """确认范围后派发逐集任务；参数：固定集/轨道与范围；返回：资源编号。"""
        with self.lock:
            self._idle()
            row = self._read(identifier)
            if kind not in {'video', 'audio', 'cover'} or output_format not in {'original', 'png', 'jpg'} or scope not in {'single', 'parts', 'collection'}:
                raise ValueError('素材种类、范围或封面格式无效')
            if kind != 'video' and scope != 'single' or kind != 'cover' and output_format != 'original':
                raise ValueError('声音与封面请逐集准备')
            episodes = self._episodes(row)
            if episode_id not in episodes:
                raise ValueError('所选分集不存在')
            catalog = row.get('catalogs', {}).get(episode_id)
            if not catalog or not self._success(row.get('metadata_task_id')):
                raise ValueError('请先成功解析所选集的规格')
            chosen_v = next((v for v in catalog['videos'] if v['id'] == video_id), None)
            chosen_a = next((a for a in catalog['audios'] if a['id'] == audio_id), None)
            if kind != 'cover' and (not chosen_v or not chosen_v.get('combined') and not chosen_a):
                raise ValueError('请选择实际提供的视频和音频规格')
            if kind == 'cover':
                video_id = audio_id = ''
            key = resource_key(episode_id, kind, video_id, audio_id, output_format)
            if scope == 'single' and self._valid(row, key):
                return key
            selected = [episodes[episode_id]]
            if scope == 'parts':
                selected = row['metadata']['parts']
            elif scope == 'collection':
                collection = row['metadata']['collection']
                selected = collection['items']
                if collection.get('declared_count') and collection['declared_count'] != len(selected):
                    raise ValueError('平台返回的合集列表不完整，不能确认全部范围，请逐集下载')
            if not selected:
                raise ValueError('没有可下载的分集')
            operation = {'kind': kind, 'scope': scope, 'episode_id': episode_id, 'video_id': video_id, 'audio_id': audio_id,
                         'output_format': output_format, 'preference_video': chosen_v, 'preference_audio': chosen_a}
            row['plan'] = [{'episode_id': p['id'], 'title': p['title'], 'state': 'pending', 'message': '等待准备'} for p in selected]
            row['operation'] = operation
            self._write(row)
            self._launch(row, 'prepare', operation)
            return key

    def control(self, identifier, action):
        """暂停/取消/继续或重试未完成集；参数：编号及动作；返回：无。"""
        if action not in {'pause', 'cancel', 'resume'}:
            raise ValueError('控制动作无效')
        with self.lock:
            row = self._read(identifier)
            worker = self.workers.get(identifier)
            if action in {'pause', 'cancel'}:
                if worker and worker[0].is_alive():
                    worker[2]['action'] = action
                    worker[1].set()
                    return
                if action == 'cancel' and row['phase'] in {'paused', 'failed', 'interrupted'}:
                    self._discard_parts(row)
                    row.update(phase='cancelled', message='已取消，未完成分段已删除，完整资源仍保留')
                    self._write(row)
                    return
                raise ValueError('当前没有可以暂停或取消的任务')
            self._idle()
            if not row.get('operation') or not row.get('plan'):
                raise ValueError('请重新选择素材和下载范围')
            for entry in row['plan']:
                if entry['state'] != 'prepared' or not self._valid(row, entry.get('resource_key', '')):
                    entry.update(state='pending', message='等待继续')
            self._launch(row, 'prepare', row['operation'])

    def _launch(self, row, action, options):
        """启动后台工作；参数：记录和动作；返回：无。"""
        row.update(phase='queued', message='任务已提交', loaded=0, total=0, speed=0, eta=None,
                   stage=None, processed_seconds=0, duration=None, touched=time.time())
        self._write(row)
        stop, control = threading.Event(), {'action': 'pause'}
        thread = threading.Thread(target=self._execute, args=(row, action, options, stop, control), daemon=True)
        self.workers[row['id']] = (thread, stop, control)
        thread.start()

    def _execute(self, row, action, options, stop, control):
        """每集由独立统一任务审计，暂停作为需要处理；参数：执行上下文；返回：无。"""
        def checkpoint():
            if stop.is_set():
                if control['action'] == 'pause':
                    raise NeedsAttention('用户已暂停，保留完整轨道与可核验分段')
                raise TaskStopped('用户已取消，完整资源保留')
            events = current_events()
            if events:
                events.checkpoint('B站素材下载')
        def update(**values):
            with self.lock:
                row.update(values, touched=time.time())
                self._write(row)
        def task(action_name, work):
            def body():
                checkpoint()
                task_id = current_task()[1]
                row['attempts'].append({'task_id': task_id, 'action': action_name, 'episode_id': row.get('active_episode'), 'created': time.time()})
                update(task_id=task_id)
                events = current_events()
                if events:
                    events.emit('bilibili_operation', 'B站素材操作已开始', details={
                        'version': 1, 'cache_id': row['id'], 'action': action_name,
                        'episode_id': row.get('active_episode') or options.get('episode_id'),
                        'scope': options.get('scope', 'single'), 'video_track': options.get('video_id'),
                        'audio_track': options.get('audio_id')})
                return work()
            result = run_task(self.settings, 'tools.bilibili.' + action_name, body)
            update(task_id=result.task_id)
            return result
        try:
            if action in {'resolve', 'formats'}:
                def resolve_work():
                    update(phase='resolving', message='正在解析资料与当前会话可播放的规格')
                    if action == 'resolve':
                        meta, images = platform.resolve(self.settings, parse_input(row['input']), checkpoint, options.get('interactive', False))
                        row['metadata'] = meta
                        episode_id = meta['current']
                        row['selected'] = episode_id
                        row['metadata_task_id'] = current_task()[1]
                        self._write(row)
                        for kind, source in images.items():
                            if source:
                                try:
                                    self._image(row, kind, source, checkpoint)
                                except (ValueError, OSError):
                                    row.setdefault('notes', []).append('封面或头像暂不可用，可稍后重试')
                    else:
                        episode_id = options['episode_id']
                        row['selected'] = episode_id
                    self._catalog(row, episode_id, checkpoint)
                    events = current_events()
                    if events:
                        events.emit('bilibili_catalog', '已解析资料，等待用户确认下载范围', details={
                            'version': 1, 'cache_id': row['id'], 'bvid': row['metadata']['bvid'], 'episode_id': episode_id,
                            'part_count': len(row['metadata']['parts']), 'collection_count': len(row['metadata']['collection']['items'])})
                    return '解析完成，请选择单集或全部以及素材规格'
                result = task(action, resolve_work)
                phase = 'resolved' if result.status == 'succeeded' else 'cancelled' if result.status == 'cancelled' else 'failed'
                if stop.is_set() and control['action'] == 'pause':
                    phase = 'paused'
                update(phase=phase, message='解析已暂停，请重新解析，尚未下载视频' if phase == 'paused' else result.message)
                return
            failed = False
            for entry in row['plan']:
                checkpoint()
                if entry['state'] == 'prepared' and self._valid(row, entry.get('resource_key', '')):
                    continue
                row['active_episode'] = entry['episode_id']
                entry.update(state='running', message='正在准备')
                def prepare_work():
                    update(phase='downloading', message='正在准备：' + entry['title'], loaded=0, total=0, speed=0, eta=None,
                           stage=None, processed_seconds=0, duration=None)
                    return self._prepare_one(row, entry, options, checkpoint, update)
                result = task(options['kind'], prepare_work)
                if result.status == 'succeeded':
                    entry.update(state='prepared', message='已准备，可预览或保存')
                else:
                    failed = True
                    entry.update(state='paused' if stop.is_set() and control['action'] == 'pause' else 'failed', message=result.message)
                self._write(row)
                if stop.is_set() or result.status in {'cancelled', 'needs_attention'}:
                    break
            if stop.is_set():
                if control['action'] == 'cancel':
                    self._discard_parts(row)
                update(phase='paused' if control['action'] == 'pause' else 'cancelled', message='已暂停，可继续未完成的分集' if control['action'] == 'pause' else '已取消，完整资源保留')
            else:
                update(phase='partial' if failed else 'prepared', message='部分集数未完成，可重试未完成项；规格不匹配时请逐集选择' if failed else '所选范围已准备，预览确认后可保存')
        except (NeedsAttention, TaskStopped):
            if control['action'] == 'cancel':
                self._discard_parts(row)
            update(phase='paused' if control['action'] == 'pause' else 'cancelled', message='已暂停，可继续' if control['action'] == 'pause' else '已取消')
        except Exception:
            logging.getLogger(__name__).exception('B站素材任务异常')
            update(phase='failed', message='任务异常，完整资源与可恢复分段保留，请查看任务日志')
        finally:
            # 不完整封装文件不能当作续传输入；完整视频轨和音轨仍保留。
            for path in self._cache(row).glob('*.output.part'):
                path.unlink(missing_ok=True)

    def _catalog(self, row, episode_id, checkpoint):
        """获取新来源，公开规格与私有地址分开；参数：集和回调；返回：两种目录。"""
        episode = self._episodes(row)[episode_id]
        catalog, sources = platform.tracks(self.settings, episode, checkpoint)
        row['catalogs'][episode_id] = catalog
        atomic_json(self._path(row['id']) / ('sources_' + episode_id + '.json'), sources)
        self._write(row)
        return catalog, sources

    def _image(self, row, kind, url, checkpoint):
        """获取投稿封面或头像并验证图片；参数：元数据及地址；返回：无。"""
        if isinstance(url, str) and url.startswith('http://'):
            url = 'https://' + url[7:]
        target = self._cache(row) / (kind + '.raw.part')
        transfer.download([url], target, identity=kind + ':' + row['metadata']['bvid'], allowed=platform.media_url,
                          checkpoint=checkpoint, progress=lambda *args: None, referer='https://www.bilibili.com/', max_bytes=10 * 1024 ** 2)
        output = self._cache(row) / (kind + '.output.part')
        ext, mime = image_file(target, output)
        self._publish(row, kind, output, kind, kind + ext, mime, row['metadata']['current'])
        target.unlink(missing_ok=True)
        target.with_suffix(target.suffix + '.json').unlink(missing_ok=True)

    def _choice(self, catalog, options, episode_id):
        """只匹配用户选择的规格，不静默降画质；参数：目录与偏好；返回：视频/音频轨。"""
        if episode_id == options['episode_id']:
            v = next((v for v in catalog['videos'] if v['id'] == options['video_id']), None)
            a = next((a for a in catalog['audios'] if a['id'] == options['audio_id']), None)
        else:
            pref = options['preference_video']
            v = next((v for v in catalog['videos'] if v['quality'] == pref['quality'] and v['codec'].split('.')[0] == pref['codec'].split('.')[0]), None)
            audio = options['preference_audio']
            a = next((a for a in catalog['audios'] if audio and a['quality'] == audio['quality'] and a['codec'].split('.')[0] == audio['codec'].split('.')[0]), None)
        if not v or not v.get('combined') and not a:
            raise ValueError('这集没有所选清晰度或编码，请单独选择该集的实际规格')
        return v, a

    def _track(self, row, episode, spec, sources, checkpoint, update):
        """复用已核验完整轨道或执行Range下载；参数：轨道和回调；返回：本地路径。"""
        identity = json.dumps([episode['bvid'], episode['cid'], spec['id']], separators=(',', ':'))
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        path = self._cache(row) / (key + '.track')
        proof_path = path.with_suffix('.proof.json')
        try:
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            if proof.get('identity') == identity and path.is_file() and list(transfer.fingerprint(path)) == [proof['bytes'], proof['sha256']]:
                update(message='复用已校验的完整轨道', loaded=proof['bytes'], total=proof['bytes'], speed=0, eta=None)
                return path
        except (OSError, ValueError, KeyError):
            pass
        target = path.with_suffix('.part')
        def progress(loaded, total, speed, note):
            update(loaded=loaded, total=total, speed=speed, eta=(total - loaded) / speed if total and speed else None,
                   stage='音频轨' if spec['id'].startswith('a') else '视频轨', **({'message': note} if note else {}))
        size, digest = transfer.download(sources, target, identity=identity, allowed=platform.media_url,
                                         checkpoint=checkpoint, progress=progress, referer='https://www.bilibili.com/')
        local.inspect_media(target, checkpoint, duration=row['catalogs'][episode['id']]['duration'],
                            video=not spec['id'].startswith('a'), audio=spec['id'].startswith('a') or spec.get('combined', False))
        target.replace(path)
        target.with_suffix(target.suffix + '.json').unlink(missing_ok=True)
        atomic_json(proof_path, {'identity': identity, 'bytes': size, 'sha256': digest, 'task_id': current_task()[1]})
        return path

    def _prepare_one(self, row, entry, options, checkpoint, update):
        """处理一集，结束前核对音画时长并发布；参数：选项和回调；返回：结果说明。"""
        episode_id, kind = entry['episode_id'], options['kind']
        episode = self._episodes(row)[episode_id]
        if kind == 'cover':
            source = episode.get('cover') or row.get('catalogs', {}).get(episode_id, {}).get('episode', {}).get('cover')
            if not source:
                raise ValueError('平台没有提供该投稿的封面；分P共用投稿封面')
            raw = self._cache(row) / 'selected-cover.raw.part'
            if source.startswith('http://'):
                source = 'https://' + source[7:]
            transfer.download([source], raw, identity=episode_id + ':cover', allowed=platform.media_url,
                              checkpoint=checkpoint, progress=lambda *args: None, referer='https://www.bilibili.com/', max_bytes=10 * 1024 ** 2)
            output = self._cache(row) / 'cover.output.part'
            ext, mime = image_file(raw, output, options['output_format'])
            raw.unlink(missing_ok=True)
            raw.with_suffix(raw.suffix + '.json').unlink(missing_ok=True)
            key = resource_key(episode_id, kind, output_format=options['output_format'])
            self._publish(row, key, output, kind, filename(episode, kind, extension=ext), mime, episode_id)
        else:
            # 新解析仍使用同集/同规格，旧完整轨道只在身份与摘要相符时复用。
            catalog, sources = self._catalog(row, episode_id, checkpoint)
            v, a = self._choice(catalog, options, episode_id)
            key = resource_key(episode_id, kind, v['id'], a['id'] if a else '')
            if self._valid(row, key):
                entry['resource_key'] = key
                return '已存在同一集同规格的完整资源'
            duration = catalog['duration']
            estimated = ((v.get('bytes') or v.get('estimated_bytes') or 0) if kind == 'video' or not a else 0) + (a.get('bytes') or a.get('estimated_bytes') or 0 if a else 0)
            existing = 0
            for spec in ([v, a] if kind == 'video' else [a or v]):
                if spec:
                    identity = json.dumps([episode['bvid'], episode['cid'], spec['id']], separators=(',', ':'))
                    track_key = hashlib.sha256(identity.encode()).hexdigest()[:24]
                    existing += max((p.stat().st_size for suffix in ('.track', '.part') if (p := self._cache(row) / (track_key + suffix)).is_file()), default=0)
            transfer.ensure_space(self._cache(row), max(0, estimated * 2 - existing))
            # 一次安全刷新重试；不得换规格，只能更新相同身份轨道的来源。
            def obtain(spec):
                try:
                    return self._track(row, episode, spec, sources[spec['id']][:2], checkpoint, update)
                except ValueError:
                    checkpoint()
                    fresh, renewed = self._catalog(row, episode_id, checkpoint)
                    self._choice(fresh, options, episode_id)
                    if spec['id'] not in renewed:
                        raise ValueError('原规格已不可用，请重新选择') from None
                    return self._track(row, episode, spec, renewed[spec['id']][:1], checkpoint, update)
            video_path = obtain(v) if kind == 'video' or not a else None
            audio_path = obtain(a) if a else None
            output = self._cache(row) / (key + '.output.part')
            required = sum(p.stat().st_size for p in (video_path, audio_path) if p)
            transfer.ensure_space(self._cache(row), required)
            update(phase='merging', message='正在合并音画' if kind == 'video' else '正在准备完整声音', stage='合并' if kind == 'video' else '音频封装', loaded=0, total=0, speed=0, eta=None)
            def media_progress(seconds):
                checkpoint()
                transfer.ensure_space(self._cache(row), 0)
                update(processed_seconds=seconds, duration=duration)
            if kind == 'video':
                local.merge(video_path, audio_path, output, checkpoint, duration, media_progress)
                mime = 'video/mp4'
            else:
                local.extract_audio(audio_path or video_path, output, checkpoint, duration,
                                    copy=bool(a and a['codec'].startswith('mp4a')), progress=media_progress)
                mime = 'audio/mp4'
            checkpoint()
            self._publish(row, key, output, kind, filename(episode, kind, v), mime, episode_id, v['id'], a['id'] if a else '')
        entry['resource_key'] = key
        self._write(row)
        return '所选分集素材已准备，尚未保存到用户选择的位置'

    def _publish(self, row, key, source, kind, name, mime, episode_id, video_id='', audio_id=''):
        """发布可校验资源并记录真实任务事件；参数：文件身份；返回：无。"""
        size, digest = transfer.fingerprint(source)
        checkpoint = current_events()
        if checkpoint:
            checkpoint.emit('bilibili_asset_prepared', '分集素材已准备，尚未保存到电脑', details={
                'version': 1, 'cache_id': row['id'], 'episode_id': episode_id, 'asset_id': key, 'kind': kind,
                'video_track': video_id or None, 'audio_track': audio_id or None, 'bytes': size, 'sha256': digest})
        source.replace(self._cache(row) / ('asset_' + key + '.bin'))
        row['resources'][key] = {'kind': kind, 'filename': name, 'mime': mime, 'episode_id': episode_id,
                                 'video_id': video_id, 'audio_id': audio_id, 'bytes': size, 'sha256': digest,
                                 'task_id': current_task()[1]}
        self._write(row)
