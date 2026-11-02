"""解析优先的抖音素材工作台；继承旧缓存读取能力。"""
import json
import logging
import re
import threading
import time
from uuid import uuid4

from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.runner import current_task, run_task
from ecommerce.infrastructure.tasks.events import current_events
from ecommerce.platforms.douyin.links import parse_input, video_filename
from ecommerce.platforms.douyin.video import video_catalog
from ecommerce.shared.exceptions import TaskStopped
from . import service as legacy
from .media import fetch_asset, image_file, music_file, audio_file, fingerprint


class VideoTools(legacy.VideoTools):
    """按解析记录管理多规格视频、封面与音频，任务结果继续以数据库为准。"""
    def _success(self,task_id):
        """核对真实成功任务；参数：编号；返回：是否成功。"""
        if not task_id:
            return False
        store=self.settings.task_database
        if self.settings.task_backend=='mysql':
            task=store.get_task(task_id)
        else:
            with database.connect(store) as connection:
                task=connection.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        return bool(task and task['status']=='succeeded' and task['name'].startswith('tools.douyin.'))

    def _busy(self):
        """返回活动记录编号；参数：无；返回：编号或空值。"""
        self.workers={key:value for key,value in self.workers.items() if value[0].is_alive()}
        return next(iter(self.workers),None)

    def start(self,text,interactive=False):
        """仅解析资料与版本，暂不下载视频；参数：输入与登录模式；返回：记录编号。"""
        video=parse_input(text)
        with self.lock:
            busy=self._busy()
            if busy:
                if self._read(busy)['input']==video.url:
                    return busy
                raise ValueError('另一个素材任务正在执行，请等待或先取消')
            self.cleanup()
            identifier=uuid4().hex;self._path(identifier).mkdir()
            row={'version':2,'id':identifier,'input':video.url,'created':time.time(),'expires':time.time()+legacy.TTL,
                 'phase':'queued','message':'正在等待解析','items':[],'formats':[],'resources':{},
                 'task_id':None,'metadata_task_id':None}
            self._write(row)
            self._launch(row,'resolve',interactive=interactive)
            return identifier

    def _launch(self,row,action,*,format_id=None,interactive=False,output_format='original'):
        """派发独立素材任务；参数：记录和动作；返回：无。"""
        row.update(phase='queued',message='任务已提交',task_id=None,expires=time.time()+legacy.TTL,
                   operation={'action':action,'format_id':format_id,'output_format':output_format},loaded=0,total=0)
        self._write(row)
        stop=threading.Event()
        thread=threading.Thread(target=self._execute,args=(row,action,format_id,interactive,output_format,stop),daemon=True)
        self.workers[row['id']]=(thread,stop);thread.start()

    def _execute(self,row,action,format_id,interactive,output_format,stop):
        """在统一执行器执行资源动作；参数：派发上下文；返回：无。"""
        def checkpoint():
            if stop.is_set():
                raise TaskStopped('已取消素材任务')
            if current_events():
                current_events().checkpoint('抖音素材处理')
        def update(**values):
            with self.lock:
                row.update(values);self._write(row)
        def work():
            task_id=current_task()[1]
            update(task_id=task_id,phase='resolving' if action=='resolve' else 'downloading',message='正在解析资料与可选版本' if action=='resolve' else '正在准备所选素材')
            if action=='resolve':
                detail,sources=legacy.resolve_video(self.settings,parse_input(row['input']),checkpoint,
                                                  interactive=interactive,timeout=180 if interactive else 60)
                meta,formats,catalog=video_catalog(detail,sources)
                if not formats:
                    raise ValueError('没有可选择的视频版本')
                with self.lock:
                    legacy.atomic_json(self._path(row['id'])/'catalog.json',catalog)
                    row.update(items=[meta],formats=formats,metadata_task_id=task_id)
                    self._write(row)
                for kind in ('cover','avatar'):
                    checkpoint()
                    try:
                        self._image(row,kind,catalog[kind],checkpoint)
                    except (ValueError,OSError):
                        row.setdefault('notes',[]).append('封面暂不可用，可重试封面下载' if kind=='cover' else '头像暂不可用')
                current_events() and current_events().emit('douyin_catalog','已解析资料与可选版本，尚未下载视频',
                    details={'version':2,'cache_id':row['id'],'video_id':meta['video_id'],'format_count':len(formats)})
                return '资料解析完成，请选择版本或素材'
            catalog=self._catalog(row)
            if action=='video':
                if time.time()-catalog['created']>300:
                    catalog=self._refresh(row,checkpoint)
                if format_id not in catalog['formats']:
                    raise ValueError('所选版本已经变化，请重新选择')
                target=self._path(row['id'])/'video.part'
                def download(current):
                    return legacy.fetch_video(current['formats'][format_id]['sources'],target,checkpoint,
                        lambda loaded,total:update(loaded=loaded,total=total))
                try:
                    download(catalog)
                except ValueError:
                    checkpoint();catalog=self._refresh(row,checkpoint)
                    if format_id not in catalog['formats']:
                        raise ValueError('原视频已没有所选规格，请重新选择版本') from None
                    download(catalog)
                checkpoint()
                name=video_filename(row['items'][0]['title'],row['items'][0]['video_id'])
                self._register(row,'video_'+format_id,target,'video',name,'video/mp4',format_id)
            elif action=='cover':
                if not self._resource_valid(row,'cover'):
                    try:self._image(row,'cover',catalog['cover'],checkpoint)
                    except ValueError:
                        catalog=self._refresh(row,checkpoint);self._image(row,'cover',catalog['cover'],checkpoint)
                if output_format!='original':
                    target=self._path(row['id'])/'cover-convert.part'
                    ext,mime=image_file(self._asset_path(row['id'],'cover'),target,output_format)
                    self._register(row,'cover_'+output_format,target,'cover',self._name(row,'封面',ext),mime)
            elif action=='music':
                target=self._path(row['id'])/'music.part'
                try:fetch_asset(catalog['music'],'music',target,checkpoint)
                except ValueError:
                    catalog=self._refresh(row,checkpoint);fetch_asset(catalog['music'],'music',target,checkpoint)
                ext,mime=music_file(target,checkpoint)
                self._register(row,'music',target,'music',self._name(row,'背景音乐',ext),mime)
            elif action=='audio':
                key='video_'+format_id
                if not self._resource_valid(row,key):
                    raise ValueError('请先准备所选视频版本，再提取完整声音')
                target=self._path(row['id'])/'audio.part'
                audio_file(self._asset_path(row['id'],key),target,checkpoint)
                self._register(row,'audio_'+format_id,target,'audio',self._name(row,'完整声音','.m4a'),'audio/mp4',format_id)
            checkpoint()
            return '所选素材已准备，可以预览或另存为'
        try:
            result=run_task(self.settings,'tools.douyin.'+action,work)
            phase=('resolved' if action=='resolve' else 'prepared') if result.status=='succeeded' else 'cancelled' if result.status=='cancelled' else 'failed'
            update(task_id=result.task_id,phase=phase,message=result.message,outcome=result.status,expires=time.time()+legacy.TTL)
        except Exception:
            logging.getLogger(__name__).exception('抖音素材任务异常')
            update(phase='failed',message='素材任务异常，请查看任务日志后重试')
        finally:
            with self.lock:
                for path in self._path(row['id']).glob('*.part'):
                    path.unlink(missing_ok=True)

    def _name(self,row,suffix,extension):
        """生成素材建议名称；参数：记录、类型和扩展名；返回：安全文件名。"""
        item=row['items'][0]
        return video_filename(item['title'],item['video_id'])[:-4]+'_'+suffix+extension

    def _catalog(self,row):
        """读取私有来源目录；参数：记录；返回：目录。"""
        try:
            return json.loads((self._path(row['id'])/'catalog.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            raise ValueError('来源信息已过期或被清理，请重新解析') from None

    def _refresh(self,row,checkpoint):
        """刷新同视频的临时来源；参数：记录和停止回调；返回：新目录，不替换用户选择。"""
        detail,sources=legacy.resolve_video(self.settings,parse_input(row['items'][0]['url']),checkpoint,timeout=60)
        meta,formats,catalog=video_catalog(detail,sources)
        if meta['video_id']!=row['items'][0]['video_id']:
            raise ValueError('刷新来源的视频编号不一致')
        with self.lock:
            legacy.atomic_json(self._path(row['id'])/'catalog.json',catalog)
            row['formats']=formats;self._write(row)
        return catalog

    def _asset_path(self,identifier,key):
        """生成固定资源文件路径；参数：记录和资源编号；返回：路径。"""
        if not re.fullmatch('[a-z0-9_-]{1,80}',key):
            raise LookupError('素材编号无效')
        return self._path(identifier)/('asset_'+key+'.bin')

    def _image(self,row,kind,urls,checkpoint):
        """获取并解码图片，保留原格式；参数：记录、种类、来源和停止回调；返回：无。"""
        if not urls:
            raise ValueError('平台未提供该图片')
        source=self._path(row['id'])/(kind+'-raw.part')
        target=self._path(row['id'])/(kind+'.part')
        fetch_asset(urls,'image',source,checkpoint)
        ext,mime=image_file(source,target)
        self._register(row,kind,target,kind,self._name(row,'封面' if kind=='cover' else '头像',ext),mime)
        source.unlink(missing_ok=True)

    def _register(self,row,key,target,kind,name,mime,format_id=None):
        """原子发布资源文件并记录校验；参数：资源内容；返回：无。"""
        size,digest=fingerprint(target)
        with self.lock:
            target.replace(self._asset_path(row['id'],key))
            row['resources'][key]={'kind':kind,'filename':name,'mime':mime,'bytes':size,'sha256':digest,
                                   'task_id':current_task()[1],'format_id':format_id}
            if kind=='video':
                row.update(loaded=size,total=size)
            self._write(row)
        if current_events():
            current_events().emit('douyin_asset_prepared','素材文件已准备，尚未保存到用户选择的位置',
                details={'version':2,'cache_id':row['id'],'video_id':row['items'][0]['video_id'],'asset_id':key,
                         'kind':kind,'format_id':format_id,'bytes':size,'sha256':digest,
                         'review':'required' if kind=='video' else 'not_applicable'})

    def _resource_valid(self,row,key,*,check_hash=True):
        """核对资源及所属任务；参数：记录、资源编号、是否校验内容；返回：是否完整可用。"""
        asset=row.get('resources',{}).get(key)
        if not asset or row['expires']<time.time() or not self._success(asset.get('task_id')):
            return False
        path=self._asset_path(row['id'],key)
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=asset['bytes']:
            return False
        return not check_hash or fingerprint(path)[1]==asset['sha256']

    def prepare(self,identifier,kind,format_id=None,output_format='original'):
        """准备所选素材，已缓存时直接复用；参数：记录、类型、规格、封面格式；返回：资源编号。"""
        if not isinstance(kind,str) or kind not in {'video','music','cover','audio'} or not isinstance(output_format,str) or output_format not in {'original','png','jpg'}:
            raise ValueError('素材类型或封面格式无效')
        if kind!='cover' and output_format!='original' or kind not in {'video','audio'} and format_id is not None:
            raise ValueError('该素材不接受视频规格或封面格式参数')
        with self.lock:
            row=self._read(identifier)
            if row.get('version')!=2:
                raise ValueError('旧记录请重新解析，以选择素材版本')
            if not self._success(row.get('metadata_task_id')) or row['expires']<time.time() or not (self._path(identifier)/'catalog.json').is_file():
                raise ValueError('解析资料未完成或已过期，请重新解析')
            if kind in {'video','audio'} and (not isinstance(format_id,str) or format_id not in {f['id'] for f in row['formats']}):
                raise ValueError('请选择该视频实际提供的版本')
            key=kind+'_'+format_id if kind in {'video','audio'} else 'cover_'+output_format if kind=='cover' and output_format!='original' else kind
            if self._resource_valid(row,key):
                row['expires']=time.time()+legacy.TTL;self._write(row);return key
            busy=self._busy()
            if busy:
                operation=self._read(busy).get('operation',{})
                if busy==identifier and operation=={'action':kind,'format_id':format_id,'output_format':output_format}:
                    return key
                raise ValueError('另一个素材任务正在执行，请等待或先取消')
            if kind=='music' and not row['items'][0]['music']['available']:
                raise ValueError('平台未提供可下载的背景音乐')
            if kind=='audio' and not self._resource_valid(row,'video_'+format_id):
                raise ValueError('请先准备所选视频版本，再提取完整声音')
            self._launch(row,kind,format_id=format_id,output_format=output_format)
            return key

    def snapshot(self,identifier):
        """返回不含签名来源地址的展示快照；参数：编号；返回：状态字典。"""
        with self.lock:
            row=self._read(identifier)
            if row.get('version')!=2:
                return {**super().snapshot(identifier),'legacy':True}
            active=bool(self.workers.get(identifier) and self.workers[identifier][0].is_alive())
            expired=row['expires']<time.time()
            if row['phase'] in {'queued','resolving','downloading'} and not active:
                row.update(phase='interrupted',message='素材任务已中断，可重试未完成的步骤')
            if expired and row['phase']!='cleared':
                row.update(phase='expired',message='临时缓存已过期，请重新解析')
            row['metadata_ready']=self._success(row.get('metadata_task_id'))
            row['can_prepare']=row['metadata_ready'] and not expired and (self._path(identifier)/'catalog.json').is_file()
            row['resources']={key:{**asset,'ready':self._resource_valid(row,key,check_hash=False)} for key,asset in row['resources'].items()}
            row['cache_bytes']=sum(p.stat().st_size for p in self._path(identifier).iterdir() if p.is_file() and not p.is_symlink())
            return row

    def file(self,identifier,key=None):
        """校验完整资源并续期；参数：记录和资源编号；返回：路径、建议名称、MIME。"""
        with self.lock:
            row=self._read(identifier)
            if row.get('version')!=2:
                path,name=super().file(identifier);return path,name,'video/mp4'
            if not key or not self._resource_valid(row,key):
                raise ValueError('素材尚未准备完成、已过期或校验失败，请重新准备')
            row['expires']=time.time()+legacy.TTL;self._write(row)
            asset=row['resources'][key]
            return self._asset_path(identifier,key),asset['filename'],asset['mime']

    def delete_cache(self,identifier):
        """删除缓存内容保留历史摘要；参数：记录编号；返回：无。"""
        with self.lock:
            if self.workers.get(identifier) and self.workers[identifier][0].is_alive():
                raise ValueError('该记录仍在执行，请先取消或等待结束')
            row=self._read(identifier)
            paths=sorted(self._path(identifier).iterdir(),key=lambda p:p.name=='catalog.json')
            for path in paths:
                if path.name!='manifest.json' and (path.is_file() or path.is_symlink()):
                    try:path.unlink()
                    except OSError:
                        raise ValueError('缓存正在被预览或保存，请停止播放后重试删除') from None
            row.update(resources={},phase='cleared',expires=0,message='缓存已删除，历史摘要保留；需要下载时请重新解析')
            self._write(row)

    def cleanup(self):
        """惰性清理过期文件，保留活动任务和历史摘要；参数：无；返回：无。"""
        with self.lock:
            for path in self.root.glob('*/manifest.json'):
                try:
                    identifier=path.parent.name
                    row=self._read(identifier)
                    if row['expires']<time.time() and row.get('phase')!='cleared':
                        if not (self.workers.get(identifier) and self.workers[identifier][0].is_alive()):
                            self.delete_cache(identifier)
                except (LookupError,OSError,ValueError):
                    continue

    def usage(self):
        """统计缓存实际文件占用；参数：无；返回：字节数。"""
        with self.lock:
            total=0
            for manifest in self.root.glob('*/manifest.json'):
                try:
                    directory=self._path(manifest.parent.name)
                    total+=sum(p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.is_symlink())
                except (LookupError,OSError):
                    continue
            return total
