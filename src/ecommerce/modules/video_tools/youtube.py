"""YouTube单视频工作台，复用缓存、真实任务及公共媒体处理。"""
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ecommerce.infrastructure.media import local, transfer
from ecommerce.infrastructure.tasks.events import current_events
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.infrastructure.tasks.runner import current_task, run_task
from ecommerce.platforms.youtube import video as platform
from ecommerce.platforms.youtube.links import parse_input
from ecommerce.shared.config import youtube_options
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped
from .cache import MediaCache, ACTIVE
from .media import image_file


def resource_key(video_id, kind, video='', audio='', image_format='original'):
    """标识当前视频及所选组合；参数：身份与规格；返回：24位资源键。"""
    return hashlib.sha256(json.dumps([video_id, kind, video, audio, image_format]).encode()).hexdigest()[:24]


def output_container(video, audio):
    """为实际编码选择无损封装；参数：规格；返回：格式、扩展名和MIME，不重编码视频。"""
    if video['codec'].startswith(('vp9', 'vp09', 'av01')) and audio and audio['codec'] in {'opus', 'vorbis'}:
        return 'webm', '.webm', 'video/webm'
    return 'mp4', '.mp4', 'video/mp4'


def filename(meta, kind, extension, video=None, audio=None):
    """生成保留视频编号和所选画质/语言的安全名称；参数：身份与规格；返回：名称。"""
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', meta['title']).strip(' .')[:85] or 'YouTube视频'
    parts = [title, meta['video_id']]
    if kind == 'video' and video:
        parts += [str(video['height']) + 'P', str(video.get('fps') or '').removesuffix('.0') + 'fps', video['codec'].split('.')[0]]
    else:
        parts.append('完整声音' if kind == 'audio' else '封面')
    if audio and audio.get('language'):
        parts.append(audio['language'])
    return '_'.join(parts) + extension


class YouTubeTools(MediaCache):
    """当前链接仅一个视频，所有规格与临时来源绑定明确视频身份。"""
    site = 'youtube'
    label = 'YouTube'
    options_validator = staticmethod(youtube_options)

    def _network(self):
        """取得即时生效的本平台网络配置；参数：无；返回：独立配置。"""
        return replace(self.settings, youtube=dict(self.options))

    def start(self, text, interactive=False):
        """仅解析单视频，去除列表和时间参数；参数：输入与登录模式；返回：记录编号。"""
        video = parse_input(text)
        with self.lock:
            self._idle()
            self.cleanup()
            identifier = uuid4().hex
            self._path(identifier).mkdir()
            base = Path(self.options['temp_dir']) if self.options['temp_dir'] else self.settings.workspace / 'output/temp'
            row = {'version': 1, 'id': identifier, 'input': video.url, 'created': time.time(), 'touched': time.time(),
                   'cache_dir': str(base / self.site / self.scope / identifier), 'phase': 'queued', 'message': '等待解析当前视频',
                   'task_id': None, 'metadata_task_id': None, 'metadata': None, 'catalog': {}, 'resources': {}, 'attempts': []}
            self._cache(row).mkdir(parents=True, exist_ok=True)
            self._write(row)
            self._launch(row, 'resolve', {'interactive': interactive})
            return identifier

    def _catalog(self, row, checkpoint, interactive=False):
        """刷新同一视频的私有来源；参数：记录与回调；返回：私有来源。"""
        meta, catalog, sources = platform.extract(self._network(), parse_input(row['input']), checkpoint, interactive)
        if row.get('metadata') and meta['video_id'] != row['metadata']['video_id']:
            raise ValueError('刷新结果与当前视频不一致')
        atomic_json(self._path(row['id']) / 'sources_video.json', sources)
        with self.lock:
            row.update(metadata=meta, catalog=catalog)
            self._write(row)
        return sources

    def prepare(self, identifier, kind, video_id='', audio_id='', output_format='original'):
        """仅准备用户明确选择的规格；参数：资源种类及实际轨道键；返回：稳定资源键。"""
        if kind not in {'video', 'audio', 'cover'} or output_format not in {'original', 'png', 'jpg'} or kind != 'cover' and output_format != 'original':
            raise ValueError('素材类型或封面格式无效')
        with self.lock:
            self._idle()
            row = self._read(identifier)
            if not self._success(row.get('metadata_task_id')) or not row.get('metadata'):
                raise ValueError('请先成功解析当前视频')
            v = next((s for s in row['catalog']['videos'] if s['id'] == video_id), None)
            a = next((s for s in row['catalog']['audios'] if s['id'] == audio_id), None)
            if kind == 'video' and (not v or not v.get('combined') and not a) or kind == 'audio' and not a and not (v and v.get('combined')):
                raise ValueError('请选择当前视频实际提供的画质与音轨')
            if kind == 'cover':
                video_id = audio_id = ''
            elif v and v.get('combined'):
                audio_id = ''
            key = resource_key(row['metadata']['video_id'], kind, video_id, audio_id, output_format)
            operation = {'kind': kind, 'video_id': video_id, 'audio_id': audio_id, 'output_format': output_format, 'key': key}
            if kind != 'cover':
                row['selection'] = {'video_id': video_id, 'audio_id': audio_id}
            if self._valid(row, key):
                row.update(touched=time.time(), phase='prepared', operation=operation, message='已复用所选规格的完整缓存，可预览或保存')
                self._write(row)
                return key
            row['operation'] = operation
            self._write(row)
            self._launch(row, kind, operation)
            return key

    def control(self, identifier, action):
        """控制当前资源准备；参数：pause/cancel/resume；返回：无。"""
        if action not in {'pause', 'cancel', 'resume'}:
            raise ValueError('控制动作无效')
        with self.lock:
            row = self._read(identifier)
            worker = self.workers.get(identifier)
            if action != 'resume':
                if worker and worker[0].is_alive():
                    worker[2]['action'] = action; worker[1].set(); return
                if action == 'cancel' and row['phase'] in {'paused', 'failed', 'interrupted'}:
                    self._discard_parts(row)
                    row.update(phase='cancelled', message='已取消，未完成分段已删除，完整资源保留')
                    self._write(row); return
                raise ValueError('当前没有可暂停或取消的任务')
            self._idle()
            if not row.get('operation'):
                raise ValueError('资料解析中断，请重新解析')
            self._launch(row, row['operation']['kind'], row['operation'])

    def _launch(self, row, action, operation):
        """后台执行，页面关闭不撤销任务；参数：记录与动作；返回：无。"""
        row.update(phase='queued', message='任务已提交', loaded=0, total=0, speed=0, eta=None,
                   stage=None, processed_seconds=0, touched=time.time())
        self._write(row)
        stop, control = threading.Event(), {'action': 'pause'}
        thread = threading.Thread(target=self._execute, args=(row, action, operation, stop, control), daemon=True)
        self.workers[row['id']] = (thread, stop, control); thread.start()

    def _execute(self, row, action, operation, stop, control):
        """由统一执行器审计每个动作；参数：派发上下文；返回：无。"""
        def checkpoint():
            if stop.is_set():
                if control['action'] == 'pause':
                    raise NeedsAttention('用户暂停，完整输入与可核验分段已保留')
                raise TaskStopped('用户取消，完整资源保留')
            if current_events():
                current_events().checkpoint('YouTube素材下载')
        def update(**values):
            with self.lock:
                row.update(values, touched=time.time()); self._write(row)
        def work():
            checkpoint()
            task_id = current_task()[1]
            row['attempts'].append({'task_id': task_id, 'action': action, 'created': time.time()})
            update(task_id=task_id, phase='resolving' if action == 'resolve' else 'downloading', message='正在读取当前视频与实际可用规格' if action == 'resolve' else '正在准备所选素材')
            events = current_events()
            if events:
                events.emit('youtube_operation', 'YouTube单视频操作开始', details={'version': 1, 'cache_id': row['id'],
                    'video_id': parse_input(row['input']).video_id, 'action': action,
                    'video_track': operation.get('video_id') or None, 'audio_track': operation.get('audio_id') or None})
            if action == 'resolve':
                sources = self._catalog(row, checkpoint, operation.get('interactive', False))
                row['metadata_task_id'] = task_id; self._write(row)
                if sources.get('cover'):
                    try:
                        self._cover(row, sources['cover'], 'original', 'cover', checkpoint)
                    except (ValueError, OSError):
                        row.setdefault('notes', []).append('封面暂不可用，可稍后重试')
                if events:
                    events.emit('youtube_catalog', '当前视频规格已解析，尚未下载视频', details={'version': 1, 'cache_id': row['id'],
                        'video_id': row['metadata']['video_id'], 'video_formats': len(row['catalog']['videos']), 'audio_formats': len(row['catalog']['audios'])})
                return '解析完成，请选择画质和音轨，准备当前视频'
            self._prepare_one(row, operation, checkpoint, update)
            checkpoint()
            return '所选素材已准备，预览核对后可保存'
        try:
            result = run_task(self.settings, 'tools.youtube.' + action, work)
            phase = ('resolved' if action == 'resolve' else 'prepared') if result.status == 'succeeded' else 'cancelled' if result.status == 'cancelled' else 'failed'
            if stop.is_set():
                phase = 'paused' if control['action'] == 'pause' else 'cancelled'
            if phase == 'cancelled':
                self._discard_parts(row)
            update(task_id=result.task_id, phase=phase, message=result.message)
        except Exception:
            logging.getLogger(__name__).exception('YouTube任务异常')
            update(phase='failed', message='任务异常，完整资源与可核验分段保留，请查看任务日志')
        finally:
            for path in self._cache(row).glob('*.output.part'):
                path.unlink(missing_ok=True)

    def _sources(self, row, checkpoint):
        """读取未过期来源，否则刷新同视频；参数：记录与回调；返回：私有源。"""
        try:
            sources = json.loads((self._path(row['id']) / 'sources_video.json').read_text(encoding='utf-8'))
            if time.time() - sources['created'] <= 300:
                return sources
        except (OSError, ValueError, KeyError):
            pass
        return self._catalog(row, checkpoint)

    def _cover(self, row, url, output_format, key, checkpoint):
        """下载并转换封面；参数：私有源和目标格式；返回：无。"""
        source = self._cache(row) / 'cover.raw.part'
        transfer.download([url], source, identity=row['metadata']['video_id'] + ':cover', allowed=platform.media_url,
                          checkpoint=checkpoint, progress=lambda *args: None, referer='https://www.youtube.com/',
                          opener=platform.opener(self._network()), max_bytes=10 * 1024 ** 2)
        output = self._cache(row) / 'cover.output.part'
        extension, mime = image_file(source, output, output_format)
        self._publish(row, key, output, 'cover', filename(row['metadata'], 'cover', extension), mime)
        source.unlink(missing_ok=True); source.with_suffix(source.suffix + '.json').unlink(missing_ok=True)

    def _track(self, row, spec, source, checkpoint, update):
        """复用明确身份完整轨道或安全Range下载；参数：规格、来源；返回：已校验本地文件。"""
        identity = json.dumps([row['metadata']['video_id'], spec['id']])
        path = self._cache(row) / (spec['id'] + '.track')
        proof_path = path.with_suffix('.proof.json')
        try:
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            if proof['identity'] == identity and path.is_file() and list(transfer.fingerprint(path)) == [proof['bytes'], proof['sha256']]:
                checkpoint(); return path
        except (OSError, ValueError, KeyError):
            pass
        target = path.with_suffix('.part')
        is_audio = spec in row['catalog']['audios']
        def progress(loaded, total, speed, note):
            update(loaded=loaded, total=total, speed=speed, eta=(total-loaded)/speed if speed and total else None,
                   stage='音频轨' if is_audio else '视频轨', **({'message': note} if note else {}))
        size, digest = transfer.download([source['url']], target, identity=identity, allowed=platform.media_url,
            checkpoint=checkpoint, progress=progress, referer='https://www.youtube.com/', opener=platform.opener(self._network()), request_headers=source.get('headers'), request_timeout=60)
        if spec.get('bytes') and size != spec['bytes']:
            target.unlink(missing_ok=True)
            raise ValueError('轨道大小与平台规格不一致，请重新解析，未发布不完整文件')
        local.inspect_media(target, checkpoint, duration=row['metadata']['duration'], video=not is_audio, audio=is_audio or spec['combined'])
        target.replace(path); target.with_suffix(target.suffix + '.json').unlink(missing_ok=True)
        atomic_json(proof_path, {'identity': identity, 'bytes': size, 'sha256': digest, 'task_id': current_task()[1]})
        return path

    def _prepare_one(self, row, operation, checkpoint, update):
        """处理明确所选视频/声音/封面；参数：规格与回调；返回：无。"""
        sources = self._sources(row, checkpoint)
        kind = operation['kind']
        if kind == 'cover':
            if not sources.get('cover'):
                raise ValueError('平台未提供可用封面')
            self._cover(row, sources['cover'], operation['output_format'], operation['key'], checkpoint)
            return
        def selected():
            """要求刷新后同一画质与语言仍存在；参数：无；返回：两轨。"""
            v = next((s for s in row['catalog']['videos'] if s['id'] == operation['video_id']), None)
            a = next((s for s in row['catalog']['audios'] if s['id'] == operation['audio_id']), None)
            if kind == 'video' and not v or not a and not (v and v['combined']):
                raise ValueError('原画质或音轨已不可用，请重新选择；未自动降低画质或切换语言')
            return v, a
        v, a = selected()
        specs = ([v] if v else []) + ([a] if a else []) if kind == 'video' else [a or v]
        estimated = sum(s.get('bytes') or s.get('estimated_bytes') or 0 for s in specs)
        existing = sum(max((p.stat().st_size for ext in ('.track', '.part') if (p := self._cache(row)/(s['id']+ext)).is_file()), default=0) for s in specs)
        transfer.ensure_space(self._cache(row), max(0, estimated * 2 - existing))
        def obtain(spec):
            """来源失效时只刷新一次相同规格；参数：规格；返回：本地轨道。"""
            nonlocal sources
            try:
                return self._track(row, spec, sources['tracks'][spec['id']], checkpoint, update)
            except (ValueError, KeyError):
                checkpoint(); update(message='正在刷新同一视频的所选规格来源')
                sources = self._catalog(row, checkpoint); selected()
                if spec['id'] not in sources['tracks']:
                    raise ValueError('原规格已不可用，请重新选择') from None
                return self._track(row, spec, sources['tracks'][spec['id']], checkpoint, update)
        video_path = obtain(v) if kind == 'video' or not a else None
        audio_path = obtain(a) if a else None
        output = self._cache(row) / (operation['key'] + '.output.part')
        transfer.ensure_space(self._cache(row), sum(p.stat().st_size for p in (video_path, audio_path) if p))
        update(phase='merging', stage='音画合并' if kind == 'video' else '音频封装', message='正在合并所选画质与音轨' if kind == 'video' else '正在准备完整声音', loaded=0, total=0, speed=0, eta=None)
        def progress(seconds):
            checkpoint(); transfer.ensure_space(self._cache(row), 0); update(processed_seconds=seconds)
        if kind == 'video':
            container, extension, mime = output_container(v, a)
            local.merge(video_path, audio_path, output, checkpoint, row['metadata']['duration'], progress, container=container)
        else:
            extension, mime = '.m4a', 'audio/mp4'
            local.extract_audio(audio_path or video_path, output, checkpoint, row['metadata']['duration'],
                                copy=bool(a and a['codec'].startswith('mp4a')), progress=progress)
        checkpoint()
        self._publish(row, operation['key'], output, kind, filename(row['metadata'], kind, extension, v, a), mime, operation['video_id'], operation['audio_id'])

    def _publish(self, row, key, source, kind, name, mime, video_id='', audio_id=''):
        """发布校验文件并审计，数据库任务成功后才能访问；参数：资源身份；返回：无。"""
        size, digest = transfer.fingerprint(source)
        events = current_events()
        if events:
            events.emit('youtube_asset_prepared', 'YouTube素材已准备，尚未保存到电脑', details={'version': 1,
                'cache_id': row['id'], 'video_id': row['metadata']['video_id'], 'asset_id': key, 'kind': kind,
                'video_track': video_id or None, 'audio_track': audio_id or None, 'bytes': size, 'sha256': digest})
        source.replace(self._cache(row) / ('asset_' + key + '.bin'))
        with self.lock:
            row['resources'][key] = {'kind': kind, 'filename': name, 'mime': mime, 'video_id': video_id, 'audio_id': audio_id,
                                     'bytes': size, 'sha256': digest, 'task_id': current_task()[1]}
            self._write(row)
