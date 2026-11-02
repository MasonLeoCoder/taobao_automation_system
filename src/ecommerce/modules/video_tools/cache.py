"""各视频工具共用的独立缓存、任务核对、磁盘设置与历史生命周期。"""
import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path
from dataclasses import replace
from uuid import uuid4
from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.ownership import atomic_json, owner_alive
from ecommerce.infrastructure.media import transfer
from ecommerce.shared.config import bilibili_options

ACTIVE = {'queued', 'resolving', 'downloading', 'merging'}

class MediaCache:
    """按平台与数据库隔离的媒体缓存基类，不负责平台解析或下载流程。"""
    site = 'bilibili'
    label = 'B站'
    options_validator = staticmethod(bilibili_options)

    def __init__(self, settings):
        """绑定配置和数据库命名空间；参数：应用配置；返回：无。"""
        self.original = settings
        profile = settings.workspace / ('storage/browser_profiles/' + self.site)
        for name in ('qianniu', 'taobao'):
            other = settings.for_platform(name).profile_path.resolve()
            if profile.resolve() == other or other in profile.resolve().parents or profile.resolve() in other.parents:
                raise ValueError(self.label + '浏览器资料必须与现有平台独立')
        self.settings = replace(settings, profile_path=profile, session_path=settings.workspace / ('storage/sessions/' + self.site + '.json'), active_platform=self.site)
        scope = f'{settings.mysql.host}:{settings.mysql.port}/{settings.mysql.database}' if settings.task_backend == 'mysql' else str(settings.database)
        self.scope = hashlib.sha256(scope.encode()).hexdigest()[:12]
        self.root = settings.workspace / ('storage/video_tools/' + self.site) / self.scope
        self.root.mkdir(parents=True, exist_ok=True)
        self.options = dict(getattr(settings, self.site))
        try:
            self.options = self.options_validator(json.loads((self.root / 'options.json').read_text(encoding='utf-8')), settings.root)
        except (OSError, ValueError):
            pass
        self.lock = threading.RLock()
        self.workers = {}


    def _path(self, identifier):
        """检查记录目录；参数：编号；返回：路径。"""
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise LookupError('下载记录不存在')
        path = self.root / identifier
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise LookupError('记录目录不合法')
        return path


    def _read(self, identifier):
        """读取摘要；参数：编号；返回：记录字典。"""
        try:
            return json.loads((self._path(identifier) / 'manifest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise LookupError('下载记录不存在或损坏') from None


    def _write(self, row):
        """原子保存摘要；参数：记录；返回：无。"""
        atomic_json(self._path(row['id']) / 'manifest.json', row)


    def _cache(self, row):
        """返回固定命名空间内的缓存；参数：记录；返回：路径，不跟随符号链接。"""
        path = Path(row['cache_dir'])
        if path.name != row['id'] or path.parent.name != self.scope or path.parent.parent.name != self.site:
            raise ValueError('缓存目录身份不合法')
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('缓存目录不允许符号链接')
        return path


    def _task(self, task_id):
        """只读核对真实任务；参数：编号；返回：任务或空。"""
        if not task_id:
            return None
        store = self.settings.task_database
        if self.settings.task_backend == 'mysql':
            return store.get_task(task_id)
        with database.connect(store) as connection:
            return connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()


    def _success(self, task_id):
        """判断输出所属任务成功；参数：编号；返回：布尔值。"""
        task = self._task(task_id)
        return bool(task and task['name'].startswith(('tools.' + self.site + '.')) and task['status'] == 'succeeded')


    def _busy(self):
        """清理已退出线程并查找工作；参数：无；返回：活动编号。"""
        self.workers = {k: v for k, v in self.workers.items() if v[0].is_alive()}
        return next(iter(self.workers), None)


    def _idle(self):
        """要求当前服务无活动任务；参数：无；返回：无。"""
        if self._busy():
            raise ValueError('另一个' + self.label + '任务正在执行，请先暂停或等待完成')
        if self.settings.task_backend == 'mysql':
            for path in self.root.glob('*/manifest.json'):
                row = self._read(path.parent.name)
                if row['phase'] not in ACTIVE:
                    continue
                task = self._task(row.get('task_id'))
                if task and task['status'] == 'running' and owner_alive(task.get('owner_pid'), task.get('owner_process_started_at')) is not False:
                    raise ValueError('另一个进程中的' + self.label + '任务仍在执行或存活状态待核对，请等待任务退出')


    def configure(self, values):
        """保存本库高级设置；参数：缓存目录及提醒/期限；返回：有效选项。"""
        with self.lock:
            self._idle()
            options = self.options_validator(values, self.settings.root)
            base = Path(options['temp_dir']) if options['temp_dir'] else self.settings.workspace / 'output/temp'
            destination = base / self.site / self.scope
            if any(p.is_symlink() for p in [destination, *destination.parents]):
                raise ValueError('临时目录不能通过符号链接指向其他位置')
            destination.mkdir(parents=True, exist_ok=True)
            probe = destination / ('.write-' + uuid4().hex)
            try:
                probe.write_bytes(b'')
            finally:
                probe.unlink(missing_ok=True)
            atomic_json(self.root / 'options.json', options)
            self.options = options
            return options


    def _valid(self, row, key, hash_check=True):
        """核对成功任务和实际文件；参数：资源编号；返回：可用性。"""
        asset = row.get('resources', {}).get(key)
        if not asset or not self._success(asset.get('task_id')):
            return False
        path = self._cache(row) / ('asset_' + key + '.bin')
        if path.is_symlink() or not path.is_file() or path.stat().st_size != asset['bytes']:
            return False
        return not hash_check or transfer.fingerprint(path)[1] == asset['sha256']


    def file(self, identifier, key):
        """只返回已完成且摘要一致的资源；参数：记录/资源编号；返回：路径、名称、MIME。"""
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{24}|cover|avatar', key):
            raise LookupError('资源编号无效')
        with self.lock:
            row = self._read(identifier)
            if not self._valid(row, key):
                raise ValueError('资源已清理、未完整准备或校验失败，请重新准备')
            row['touched'] = time.time()
            self._write(row)
            asset = row['resources'][key]
            return self._cache(row) / ('asset_' + key + '.bin'), asset['filename'], asset['mime']


    def snapshot(self, identifier):
        """读取可展示摘要；参数：编号；返回：无来源地址与本地路径的状态。"""
        with self.lock:
            row = self._read(identifier)
            public = {k: v for k, v in row.items() if k != 'cache_dir'}
            if row['phase'] in ACTIVE and identifier not in self.workers:
                task = self._task(row.get('task_id'))
                public.update(phase='interrupted', message='上次处理已中断，请确认任务退出后继续或重新选择规格')
                if task and task['status'] == 'running' and (self.settings.task_backend != 'mysql' or owner_alive(task.get('owner_pid'), task.get('owner_process_started_at')) is not False):
                    public.update(phase='waiting', message='上次任务仍标记运行中，新的执行会先核对资源锁')
            public['resources'] = {k: {**v, 'ready': self._valid(row, k, False)} for k, v in row['resources'].items()}
            public['metadata_ready'] = bool(row.get('metadata') and self._success(row.get('metadata_task_id')))
            return public


    def recent(self):
        """读取最近记录；参数：无；返回：最近30条。"""
        paths = sorted(self.root.glob('*/manifest.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        rows = []
        for path in paths[:30]:
            try:
                rows.append(self.snapshot(path.parent.name))
            except LookupError:
                continue
        return rows


    def usage(self):
        """统计本库全部已知缓存；参数：无；返回：占用字节。"""
        total = 0
        for path in self.root.glob('*/manifest.json'):
            row = self._read(path.parent.name)
            cache = self._cache(row)
            if cache.is_dir():
                total += sum(p.stat().st_size for p in cache.iterdir() if p.is_file() and not p.is_symlink())
        return total


    def disk(self):
        """显示当前临时目录空间和提醒；参数：无；返回：磁盘摘要。"""
        base = Path(self.options['temp_dir']) if self.options['temp_dir'] else self.settings.workspace / 'output/temp'
        probe = base
        while not probe.exists():
            probe = probe.parent
        used = self.usage()
        limit = self.options['cache_warn_gib'] * 1024 ** 3
        return {'cache_bytes': used, 'free_bytes': shutil.disk_usage(probe).free, 'temp_dir': str(base),
                'warning': bool(limit and used > limit), 'options': self.options}


    def _discard_parts(self, row):
        """只删除本记录未完成分段；参数：记录；返回：无，完整轨道与输出保留。"""
        for path in self._cache(row).glob('*'):
            if path.is_file() and (path.name.endswith('.part') or path.name.endswith('.part.json')):
                path.unlink(missing_ok=True)


    def delete_cache(self, identifier):
        """清理临时文件保留摘要；参数：记录编号；返回：无。"""
        with self.lock:
            if identifier == self._busy():
                raise ValueError('该记录正在使用，请先暂停或取消')
            row = self._read(identifier)
            task = self._task(row.get('task_id'))
            if task and task['status'] == 'running':
                raise ValueError('原任务仍在运行，不能删除其缓存')
            cache = self._cache(row)
            if cache.is_dir():
                for path in cache.iterdir():
                    if path.is_symlink() or path.is_dir():
                        raise ValueError('缓存存在异常目录，未执行递归删除')
                    try:
                        path.unlink()
                    except OSError:
                        raise ValueError('缓存文件正在使用，请关闭预览或保存后重试') from None
            for path in self._path(identifier).glob('sources_*.json'):
                path.unlink(missing_ok=True)
            row.update(phase='cleared', message='缓存已清理，摘要与任务记录保留')
            self._write(row)


    def cleanup(self):
        """清理闲置终态缓存，保护暂停、失败与进行中记录；参数：无；返回：清理数量。"""
        count = 0
        with self.lock:
            for path in self.root.glob('*/manifest.json'):
                row = self._read(path.parent.name)
                if row['phase'] not in {'prepared', 'resolved', 'cancelled', 'cleared'} or row.get('touched', row['created']) + self.options['idle_hours'] * 3600 > time.time():
                    continue
                if row['phase'] == 'cleared':
                    continue
                try:
                    self.delete_cache(row['id'])
                    count += 1
                except ValueError:
                    continue
        return count
