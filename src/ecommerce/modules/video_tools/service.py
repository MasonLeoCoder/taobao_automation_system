"""抖音单条准备任务与短期文件缓存；任务终态以统一任务存储为准。"""
import hashlib
import json
import re
import threading
import time
import urllib.request
from dataclasses import replace
from uuid import uuid4

from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.infrastructure.tasks.runner import current_task, run_task
from ecommerce.platforms.douyin.links import parse_input, video_filename
from ecommerce.platforms.douyin.video import resolve_video, safe_playback_url
from ecommerce.shared.exceptions import TaskStopped

TTL = 3600
MAX_BYTES = 1024 * 1024 * 1024


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """仅跟随抖音视频来源域名；每次重定向重新检查水印参数。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """验证跳转；参数：urllib跳转上下文；返回：新请求。"""
        if not safe_playback_url(newurl):
            raise ValueError('视频地址跳转到未允许的来源或水印版本')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_video(sources, target, checkpoint, progress):
    """流式获取候选视频；参数：候选、临时目标、停止和进度回调；返回：大小与SHA256。"""
    opener = urllib.request.build_opener(SafeRedirect())
    for source in sources[:3]:
        checkpoint()
        if not safe_playback_url(source['url']):
            continue
        try:
            request = urllib.request.Request(source['url'], headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.douyin.com/'})
            with opener.open(request, timeout=30) as response:
                if not safe_playback_url(response.url):
                    raise ValueError('最终视频来源未通过校验')
                total = int(response.headers.get('Content-Length') or 0)
                if total > MAX_BYTES:
                    raise ValueError('视频超过第一版1 GiB上限')
                digest, size, last = hashlib.sha256(), 0, 0
                with target.open('wb') as output:
                    while chunk := response.read(256 * 1024):
                        checkpoint()
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError('视频超过第一版1 GiB上限')
                        if size == len(chunk) and b'ftyp' not in chunk[:64]:
                            raise ValueError('来源返回的内容不是MP4视频')
                        output.write(chunk)
                        digest.update(chunk)
                        if time.monotonic() - last > .5:
                            progress(size, total)
                            last = time.monotonic()
                if not size or (total and size != total) or (source.get('bytes') and size != source['bytes']):
                    raise ValueError('视频文件不完整，请重新解析')
                return size, digest.hexdigest()
        except TaskStopped:
            target.unlink(missing_ok=True)
            raise
        except (OSError, ValueError):
            target.unlink(missing_ok=True)
    raise ValueError('视频下载失败或地址已过期，请重新解析；未采用平台水印下载源')


class VideoTools:
    """本机单任务执行器；manifest只存缓存信息，不替代数据库任务状态。"""
    def __init__(self, settings):
        """设置独立浏览器和缓存；参数：项目配置；返回：无。"""
        profile = (settings.workspace/'storage/browser_profiles/douyin').resolve()
        if settings.active_platform != 'douyin':
            for name in ('qianniu', 'taobao'):
                other=settings.for_platform(name).profile_path.resolve()
                if profile==other or profile in other.parents or other in profile.parents:
                    raise ValueError('抖音与现有平台必须使用独立且不嵌套的浏览器资料目录')
        self.settings = replace(settings, profile_path=settings.workspace/'storage/browser_profiles/douyin',
                                session_path=settings.workspace/'storage/sessions/douyin.json', active_platform='douyin')
        scope = f'{settings.mysql.host}:{settings.mysql.port}/{settings.mysql.database}' if settings.task_backend=='mysql' else str(settings.database)
        self.root = settings.workspace/'output/temp/douyin'/hashlib.sha256(scope.encode()).hexdigest()[:12]
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.workers = {}

    def _path(self, identifier):
        """限制缓存编号和目录；参数：缓存编号；返回：目录。"""
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise LookupError('下载记录不存在')
        path = self.root/identifier
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise LookupError('下载目录无效')
        return path

    def _read(self, identifier):
        """读取缓存信息；参数：缓存编号；返回：字典。"""
        try:
            return json.loads((self._path(identifier)/'manifest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise LookupError('下载记录不存在或缓存损坏') from None

    def _write(self, row):
        """原子保存缓存信息；参数：缓存内容；返回：无。"""
        atomic_json(self._path(row['id'])/'manifest.json', row)

    def start(self, text, interactive=False):
        """去重并派发后台准备任务；参数：输入和登录模式；返回：缓存编号。"""
        video = parse_input(text)
        with self.lock:
            self.cleanup()
            self.workers = {key:value for key,value in self.workers.items() if value[0].is_alive()}
            for identifier, (thread, stop) in self.workers.items():
                if thread.is_alive():
                    row = self._read(identifier)
                    if row['input'] == video.url:
                        return identifier
                    raise ValueError('另一个视频正在准备，请等待或先取消')
            identifier = uuid4().hex
            self._path(identifier).mkdir()
            row = {'id': identifier, 'input': video.url, 'created': time.time(), 'expires': time.time()+TTL,
                   'phase': 'queued', 'message': '等待准备视频', 'items': [], 'task_id': None}
            self._write(row)
            stop = threading.Event()
            thread = threading.Thread(target=self._run, args=(row, video, stop, interactive), daemon=True)
            self.workers[identifier] = (thread, stop)
            thread.start()
            return identifier

    def _run(self, row, video, stop, interactive):
        """统一执行器内解析与下载；参数：缓存、输入、停止和登录模式；返回：无。"""
        from ecommerce.infrastructure.tasks.events import current_events
        def checkpoint():
            if stop.is_set():
                raise TaskStopped('已取消视频准备')
            session = current_events()
            if session:
                session.checkpoint('准备抖音视频')
        def update(**values):
            with self.lock:
                row.update(values)
                self._write(row)
        def action():
            update(task_id=current_task()[1], phase='resolving', message='正在解析视频；登录模式可在抖音窗口中完成验证')
            detail, sources = resolve_video(self.settings, video, checkpoint, interactive=interactive,
                                           timeout=180 if interactive else 60)
            identifier = str(detail['aweme_id'])
            title = detail.get('desc') or '抖音视频'
            update(phase='downloading', message='正在下载待核对的视频', items=[{'video_id': identifier,
                   'title': title, 'author': (detail.get('author') or {}).get('nickname') or '未提供',
                   'duration': (detail.get('video') or {}).get('duration', 0)/1000,
                   'filename': video_filename(title, identifier), 'review': 'required'}])
            target = self._path(row['id'])/'video.part'
            size, digest = fetch_video(sources, target, checkpoint,
                lambda loaded,total: update(loaded=loaded, total=total))
            checkpoint()
            target.replace(target.with_name('video.mp4'))
            update(phase='prepared', message='视频已准备，请预览并核对水印', bytes=size, sha256=digest, loaded=size, total=size,
                   expires=time.time()+TTL)
            session=current_events()
            if session:
                session.emit('douyin_prepared', '视频已准备，画面待用户核对，尚未保存到用户选择的位置',
                             details={'cache_id':row['id'],'video_id':identifier,'bytes':size,'sha256':digest,
                                      'review':'required'})
            return '抖音视频已准备；等待预览核对和另存为'
        try:
            result = run_task(self.settings, 'tools.douyin.prepare', action)
            update(task_id=result.task_id, outcome=result.status,
                   message=row['message'] if result.status=='succeeded' else result.message,
                   phase='prepared' if result.status=='succeeded' else 'cancelled' if result.status=='cancelled' else 'failed')
        except Exception:
            update(phase='failed', message='视频准备异常，请查看任务日志后重新解析')

    def snapshot(self, identifier):
        """按统一任务状态核对缓存；参数：缓存编号；返回：不含来源地址的页面状态。"""
        with self.lock:
            row = self._read(identifier)
            worker = self.workers.get(identifier)
            active = bool(worker and worker[0].is_alive())
            ready = False
            if row.get('task_id'):
                store = self.settings.task_database
                if self.settings.task_backend == 'mysql':
                    task = store.get_task(row['task_id'])
                else:
                    with database.connect(store) as connection:
                        task = connection.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
                ready = bool(task and task['name']=='tools.douyin.prepare' and task['status']=='succeeded')
            expired = row['expires'] < time.time()
            if row['phase']=='prepared' and not ready:
                row.update(phase='waiting' if active else 'interrupted',
                           message='正在核对任务结果，尚不能保存' if active else '准备任务未确认成功，请重新解析')
            if not active and row['phase'] in {'queued','resolving','downloading'}:
                row.update(phase='interrupted', message='视频准备已中断，请重新解析')
            if expired:
                row.update(phase='expired', message='临时视频已过期，请重新解析')
            row['ready'] = ready and not expired and row['phase']=='prepared'
            return {key:value for key,value in row.items() if key not in {'sha256'}}

    def file(self, identifier):
        """校验缓存完整性并续期；参数：缓存编号；返回：文件和建议名称。"""
        with self.lock:
            row = self.snapshot(identifier)
            if not row['ready']:
                raise ValueError('视频尚未准备完成或缓存已过期，请重新解析')
            path = self._path(identifier)/'video.mp4'
            manifest = self._read(identifier)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != manifest['bytes']:
                raise ValueError('视频缓存丢失或大小不符，请重新解析')
            digest=hashlib.sha256()
            with path.open('rb') as source:
                while chunk:=source.read(1024*1024):
                    digest.update(chunk)
            if digest.hexdigest()!=manifest['sha256']:
                raise ValueError('视频缓存校验失败，请重新解析')
            manifest['expires']=time.time()+TTL
            self._write(manifest)
            return path, manifest['items'][0]['filename']

    def cancel(self, identifier):
        """请求取消本服务创建的在途任务；参数：缓存编号；返回：无。"""
        with self.lock:
            self._read(identifier)
            worker=self.workers.get(identifier)
            if worker and worker[0].is_alive():
                worker[1].set()

    def recent(self):
        """列出最近20次本机缓存记录；参数：无；返回：状态列表。"""
        self.cleanup()
        rows=[]
        for path in sorted(self.root.glob('*/manifest.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]:
            try:
                rows.append(self.snapshot(path.parent.name))
            except LookupError:
                continue
        return rows

    def cleanup(self):
        """清理闲置过期视频，保留小型缓存索引；参数：无；返回：无。"""
        with self.lock:
            for path in self.root.glob('*/manifest.json'):
                worker=self.workers.get(path.parent.name)
                if worker and worker[0].is_alive():
                    continue
                try:
                    row=self._read(path.parent.name)
                    if row['expires'] < time.time():
                        for name in ('video.mp4','video.part'):
                            (self._path(row['id'])/name).unlink(missing_ok=True)
                except (LookupError, OSError):
                    continue
