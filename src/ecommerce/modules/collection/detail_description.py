"""图文详情图片按页面顺序保存；逐图进度、链接缓存及整批发布。"""
import hashlib
import json
import shutil
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_images import MAX_IMAGE_BYTES, _remove_owned_tree, download_image
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section
from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.detail_description import collect_description_links, image_list_signature, read_description
from ecommerce.platforms.taobao.verification import active_guard

DETAIL_IMAGE_FOLDER = '详情图'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_description_result(directory, record, raw_html, *, attempt=None):
    product_dir=directory.parent.parent
    item_id=record['product_id']
    document=read_detail_document(product_dir,item_id)
    directory.mkdir(parents=True,exist_ok=True)
    stem='detail_images' if attempt is None else f'detail_images-attempt-{attempt}'
    destination=directory/(stem+'.json')
    saved={**record,'html_file':None,'html_sha256':None,'html_size_bytes':0,
           'history_record_file':destination.relative_to(product_dir).as_posix()}
    if raw_html is not None:
        path=directory/(stem+'.html')
        temporary=path.with_suffix('.html.tmp')
        data=raw_html.encode('utf-8')
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        saved.update(html_file=path.relative_to(product_dir).as_posix(),html_size_bytes=len(data),html_sha256=hashlib.sha256(data).hexdigest())
    atomic_json(destination,saved)
    if saved['status'] not in {'complete','no_images'}:
        record_path=save_detail_section(product_dir,item_id,'detail_images_last_attempt',saved)
        return {**saved,'record_path':str(record_path.resolve()),'history_record_path':str(destination.resolve())}
    target=product_dir/DETAIL_IMAGE_FOLDER
    staging=product_dir/f".detail-images-{saved['batch_id']}.tmp"
    backup=product_dir/f".detail-images-{saved['batch_id']}.backup"
    for path in (target,staging,backup):
        path.resolve().relative_to(product_dir.resolve())
        if path.resolve()==product_dir.resolve():
            raise ValueError('详情图目录路径无效')
    if target.exists():
        previous=document.get('detail_images',{}).get('images',[])
        known={i['image_file'].split('/')[-1]:i['sha256'] for i in previous if i.get('status')=='downloaded'}
        if set(p.name for p in target.iterdir())!=set(known):
            raise ValueError('现有详情图目录与登记信息不一致，未覆盖')
        for path in target.iterdir():
            if not path.is_file() or path.is_symlink():
                raise ValueError('现有详情图包含未登记内容，未覆盖')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=known[path.name]:
                    raise ValueError('现有详情图已修改，未覆盖')
    latest={**saved,'folder':DETAIL_IMAGE_FOLDER,'images':[{**i,'image_path':str((target/i['image_file'].split('/')[-1]).resolve())} for i in saved['images']]}
    installed=False
    try:
        shutil.copytree(directory/DETAIL_IMAGE_FOLDER,staging)
        if target.exists(): target.replace(backup)
        staging.replace(target)
        installed=True
        record_path=save_detail_section(product_dir,item_id,'detail_images',latest)
    except Exception:
        if installed: _remove_owned_tree(target,product_dir)
        if backup.exists(): backup.replace(target)
        raise
    finally:
        _remove_owned_tree(staging,product_dir)
    _remove_owned_tree(backup,product_dir)
    return {**latest,'record_path':str(record_path.resolve()),'history_record_path':str(destination.resolve())}


def collect_detail_images(settings,result,*,stable_ms=700):
    guard=active_guard()
    if guard is None:
        raise RuntimeError('详情图采集必须在淘宝专用守卫中运行')
    item_id=result.target.item_id
    batch_id=guard.task_id+'-'+uuid4().hex[:8]
    from ecommerce.modules.collection.recovery import register_attempt
    register_attempt(settings,result.target.item_id,'detail_images',batch_id,guard.task_id)
    directory=product_directory(settings,item_id)/'history'/batch_id
    image_dir=directory/DETAIL_IMAGE_FOLDER
    image_dir.mkdir(parents=True,exist_ok=True)
    base={'schema_version':1,'product_id':item_id,'batch_id':batch_id,'task_id':guard.task_id,
          'source_url':result.target.canonical_url,'scope':'description_images'}
    def diagnostic(value,attempt):
        if matches_detail_url(result.page,item_id):
            save_description_result(directory,{**base,'captured_at':now(),'status':'partial','state':value['state'],
                'images':value['images'],'reason':value.get('reason'),'unknown':value['unknown'],
                'list_confirmed':False},value.get('raw_html'),attempt=attempt)
    links=collect_description_links(result,settings.timeout_ms,on_diagnostic=diagnostic,stable_ms=stable_ms)
    pinned=image_list_signature(links)
    def unchanged():
        try:
            current=read_description(result.page,item_id)
            return current['state'] in {'ready','no_images'} and image_list_signature(current)==pinned
        except (ValueError,PlaywrightError):
            return False
    guard.resume_check=unchanged
    def verify():
        while True:
            guard.checkpoint('核对已固定的详情图顺序')
            if unchanged(): return
            guard.request_attention(result.page,'详情图片列表或商品已变化，请恢复采集开始时的列表后继续；也可停止后重新采集，不会混用新旧列表',step='复核详情图来源')
    record={**base,'captured_at':now(),'status':'in_progress','list_confirmed':True,'images':[
        {**i,'status':'pending','error':None} for i in links['images']],
        'scroll_steps':links['scroll_steps'],'expansion_count':links['expansion_count'],
        'selector':links['selector'],'list_sha256':hashlib.sha256(json.dumps(pinned,ensure_ascii=False).encode('utf-8')).hexdigest()}
    progress=directory/'detail_images-progress.json'
    raw_path=directory/'detail_images.html'
    raw_path.write_text(links['raw_html'],encoding='utf-8')
    saved_count=[-1]
    def save_progress():
        guard.perform('保存逐张详情图进度',lambda:atomic_json(progress,record))
        count=sum(image['status']=='downloaded' for image in record['images'])
        if count>saved_count[0] and hasattr(guard,'note_module_progress'):
            guard.note_module_progress(counts={'images':count})
            saved_count[0]=count
    save_progress()
    cache={}
    for index,image in enumerate(list(record['images'])):
        verify()
        url=image['image_url']
        try:
            if url in cache:
                cached=cache[url]
                if cached['status']=='downloaded':
                    source=image_dir/cached['image_file'].split('/')[-1]
                    source.resolve().relative_to(image_dir.resolve())
                    if hashlib.sha256(source.read_bytes()).hexdigest()!=cached['sha256']:
                        raise ValueError('已缓存详情图片校验失败')
                    destination=image_dir/(str(image['index'])+source.suffix)
                    guard.perform('复用同链接详情图片',lambda:shutil.copy2(source,destination))
                    record['images'][index]={**cached,**image,'status':'downloaded','image_path':str(destination.resolve()),
                        'image_file':DETAIL_IMAGE_FOLDER+'/'+destination.name,'reused_from_index':cached['index']}
                else:
                    record['images'][index]={**image,'status':'download_failed','error':cached['error'],
                                             'reused_failure_from_index':cached['index']}
            else:
                saved=download_image(image,image_dir,result,guard,image_folder=DETAIL_IMAGE_FOLDER,label='详情图')
                record['images'][index]={**saved,'status':'downloaded','error':None,'reused_from_index':None}
        except HTTPError as error:
            error.close()
            record['images'][index]={**image,'status':'download_failed','error':f'HTTP {error.code}；未自动重试'}
            if error.code in {401,403,429,503}:
                save_progress()
                guard.request_network_attention(result.page,f'详情图片返回 HTTP {error.code}，请人工检查访问状态后继续；本图不自动重试',step='检查详情图片访问',retry_after=error.headers.get('Retry-After') if error.headers else None)
        except (URLError,OSError,ValueError,IncompleteRead) as error:
            reason=str(error) if isinstance(error,ValueError) else type(error).__name__
            record['images'][index]={**image,'status':'download_failed','error':f'{reason}；未自动重试'}
            if isinstance(error,ValueError) and '保护上限' in str(error):
                save_progress()
                guard.request_attention(result.page,f'详情图超过单文件{MAX_IMAGE_BYTES//1024//1024}MiB保护上限，已记录失败；请核对后继续，不压缩或自动重试',step='检查超大详情图片')
        cache.setdefault(url,record['images'][index])
        save_progress()
        verify()
    verify()
    success=sum(i['status']=='downloaded' for i in record['images'])
    record.update(status=links['status'] if success==len(record['images']) else 'partial',
                  summary={'recognized_count':len(record['images']),'downloaded_count':success,
                           'failed_count':len(record['images'])-success,'unique_source_count':len(cache)})
    def publish():
        verify()
        return save_description_result(directory,record,links['raw_html'])
    saved=guard.perform('发布详情图完整批次',publish)
    progress.unlink(missing_ok=True)
    message=f'详情图采集{"完成" if saved["status"] in {"complete","no_images"} else "部分完成"}：识别{len(record["images"])}张，保存{success}张，失败{len(record["images"])-success}张'
    return {**saved,'message':message}
