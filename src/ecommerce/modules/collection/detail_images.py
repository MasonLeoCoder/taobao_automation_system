"""主图按图集顺序原样下载，保存到商品目录的 1-1主图，并合并详情 JSON。"""
import base64
import hashlib
import json
import shutil
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section, save_detail_sections
from ecommerce.modules.collection.detail_video import validate_media_url
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.platforms.taobao.detail_images import collect_main_image_links, confirm_main_image_links
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention

IMAGE_FOLDER = "1-1主图"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
FORMATS = {"jpeg":(".jpg","image/jpeg"),"png":(".png","image/png"),"webp":(".webp","image/webp"),
           "gif":(".gif","image/gif"),"avif":(".avif","image/avif"),"bmp":(".bmp","image/bmp")}


def image_format(head):
    if head.startswith(b'\xff\xd8\xff'): return "jpeg"
    if head.startswith(b'\x89PNG\r\n\x1a\n'): return "png"
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP': return "webp"
    if head[:6] in {b'GIF87a',b'GIF89a'}: return "gif"
    if head[4:8] == b'ftyp' and (b'avif' in head[8:64] or b'avis' in head[8:64]): return "avif"
    if head[:2] == b'BM': return "bmp"
    raise ValueError("响应不是当前支持的图片文件")


class _ImageRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        validate_media_url(newurl)
        return super().redirect_request(request,response,code,message,headers,newurl)


def download_image(image, directory, result, guard, *, file_stem=None, image_folder=IMAGE_FOLDER, label="主图"):
    """共用原图片下载与解码检查；SKU 调用方提供已清理的文件名。"""
    stem = str(image['index']) if file_stem is None else file_stem
    if not stem or any(c in stem for c in '/\\') or stem in {'.', '..'}:
        raise ValueError("图片文件名无效")
    url = validate_media_url(image["image_url"])
    page, item_id = result.page, result.target.item_id
    request = Request(url,headers={"Referer":resolve_detail_input(page.url).canonical_url,
                                  "User-Agent":page.evaluate("navigator.userAgent"),"Accept":"image/*"})
    opener = build_opener(ProxyHandler({}),_ImageRedirect())
    directory.mkdir(parents=True,exist_ok=True)
    temporary = directory / f".{image['index']}.part"
    size, digest = 0, hashlib.sha256()
    try:
        guard.checkpoint(f"下载{label}前检查")
        if not matches_detail_url(page,item_id): raise NeedsAttention(f"{label}下载前商品页面发生变化")
        with opener.open(request,timeout=15) as response:
            validate_media_url(response.url)
            if response.status != 200: raise ValueError(f"{label}响应未返回完整文件")
            content_type=response.headers.get_content_type()
            if not (content_type.startswith('image/') or content_type=='application/octet-stream'):
                raise ValueError(f"{label}响应不是图片")
            length=response.headers.get('Content-Length')
            expected=int(length) if length and length.isdigit() else None
            if expected is not None and not 0 < expected <= MAX_IMAGE_BYTES: raise ValueError(f"{label}长度无效或超过下载保护上限")
            head=response.read(64)
            actual_format=image_format(head)
            suffix,mime=FORMATS[actual_format]
            with temporary.open('xb') as output:
                chunk=head
                while chunk:
                    guard.checkpoint(f"下载第{image['index']}张{label}")
                    if not matches_detail_url(page,item_id): raise NeedsAttention(f"{label}下载期间商品页面发生变化")
                    size+=len(chunk)
                    if size>MAX_IMAGE_BYTES: raise ValueError(f"{label}超过下载保护上限")
                    output.write(chunk);digest.update(chunk)
                    chunk=response.read(1024*1024)
            if expected is not None and size!=expected: raise ValueError(f"{label}未完整接收")
        guard.checkpoint(f"核对{label}文件")
        try:
            dimensions=page.evaluate("""async ({data,mime})=>{
              const image=new Image();image.src='data:'+mime+';base64,'+data;await image.decode();
              return {width:image.naturalWidth,height:image.naturalHeight};
            }""",{"data":base64.b64encode(temporary.read_bytes()).decode('ascii'),"mime":mime})
        except PlaywrightError as error:
            guard.checkpoint(f"复核{label}解码状态")
            raise ValueError(f"{label}无法完整解码") from error
        if not dimensions['width'] or not dimensions['height']: raise ValueError(f"{label}尺寸无效")
        if not matches_detail_url(page,item_id): raise NeedsAttention(f"{label}保存前商品页面发生变化")
        destination=directory/f"{stem}{suffix}"
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {**image,"image_path":str(destination.resolve()),"image_file":f"{image_folder}/{destination.name}",
            "format":actual_format,"content_type":content_type,"size_bytes":size,"sha256":digest.hexdigest(),
            **dimensions,"is_square":dimensions['width']==dimensions['height']}


def _remove_owned_tree(path, product_dir):
    resolved=path.resolve()
    resolved.relative_to(product_dir.resolve())
    if resolved==product_dir.resolve(): raise ValueError("不能清理商品根目录")
    if path.exists(): shutil.rmtree(path)


def _write_images_history(directory, record):
    """原子保存本批清单及逐张状态；下载开始前和每张完成后均更新。"""
    destination=directory/'main_images.json'
    directory.mkdir(parents=True,exist_ok=True)
    temporary=directory/f'.main-images-{uuid4().hex}.json.tmp'
    try:
        temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def save_images_progress(directory, record):
    """保存进度及最后尝试，不替换已有完整主图文件夹或 main_images 分区。"""
    record.update(updated_at=now(), collected_count=len(record['images']),
                  reused_count=sum(bool(p.get('reused_from_batch_id')) for p in record['images']))
    record['downloaded_count']=record['collected_count']-record['reused_count']
    destination=_write_images_history(directory,record)
    record_path=save_detail_section(directory.parent.parent,record['product_id'],'main_images_last_attempt',record)
    return {**record,'record_path':str(record_path.resolve()),'history_record_path':str(destination.resolve())}


def save_images_result(directory,record):
    """完整批次整批发布；失败批次只保留历史与进度，发布失败恢复旧目录。"""
    product_dir=directory.parent.parent
    item_id=record['product_id']
    document=read_detail_document(product_dir,item_id)
    if record['status']!='complete':
        return save_images_progress(directory,record)
    destination=_write_images_history(directory,record)
    target=product_dir/IMAGE_FOLDER
    if target.exists():
        previous=document.get('main_images',{}).get('images',[])
        known={p['image_file'].split('/')[-1]:p['sha256'] for p in previous}
        if set(p.name for p in target.iterdir())!=set(known): raise ValueError("现有主图目录与登记信息不一致，未覆盖")
        for path in target.iterdir():
            with path.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=known[path.name]: raise ValueError("现有主图被修改，未覆盖")
    staging=product_dir/f".main-images-{record['batch_id']}.tmp"
    backup=product_dir/f".main-images-{record['batch_id']}.backup"
    latest={**record,'images':[{**p,'image_path':str((target/p['image_file'].split('/')[-1]).resolve())} for p in record['images']],
            'folder':IMAGE_FOLDER,'history_record_file':destination.relative_to(product_dir).as_posix()}
    installed=False
    try:
        shutil.copytree(directory/IMAGE_FOLDER,staging)
        for path in (target,staging,backup):
            path.resolve().relative_to(product_dir.resolve())
            if path.resolve()==product_dir.resolve(): raise ValueError("主图目录路径异常")
        if target.exists(): target.replace(backup)
        staging.replace(target);installed=True
        record_path=save_detail_sections(product_dir,item_id,{'main_images':latest,'main_images_last_attempt':record})
    except (Exception,KeyboardInterrupt):
        if installed: _remove_owned_tree(target,product_dir)
        if backup.exists(): backup.replace(target)
        raise
    finally:
        _remove_owned_tree(staging,product_dir)
    _remove_owned_tree(backup,product_dir)
    return {**latest,'record_path':str(record_path.resolve()),'history_record_path':str(destination.resolve())}


def _manifest_fingerprint(item_id, links):
    """恢复条件包括商品ID、完整有序地址和原始槽位；来源改变则新采，不混用。"""
    manifest={'product_id':item_id,'collection_method':links['collection_method'],'images':links['images']}
    return hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()


def _previous_progress(product_dir, item_id, fingerprint):
    """沿已登记的恢复关系读取同来源未完成批次，避免补采再次中断后遗失早先进度。"""
    attempt=read_detail_document(product_dir,item_id).get('main_images_last_attempt',{})
    if not isinstance(attempt,dict):
        return []
    batch=attempt.get('batch_id')
    records,seen=[],set()
    while isinstance(batch,str) and batch and batch not in seen:
        if any(c in batch for c in '/\\') or batch in {'.','..'}:
            break
        seen.add(batch)
        path=product_dir/'history'/batch/'main_images.json'
        try:
            path.resolve().relative_to(product_dir.resolve())
            previous=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            break
        if (not isinstance(previous,dict) or previous.get('product_id')!=item_id or previous.get('batch_id')!=batch
                or previous.get('status') not in {'in_progress','interrupted','download_failed'}
                or previous.get('manifest_fingerprint')!=fingerprint or not isinstance(previous.get('images'),list)):
            break
        records.append(previous)
        batch=previous.get('resumed_from_batch_id')
    return records


def _valid_saved_image(path, image):
    """恢复前验证既有文件的格式、长度、尺寸登记及哈希，损坏项重新下载。"""
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES or path.stat().st_size!=image['size_bytes']:
            return False
        if not image['width']>0 or not image['height']>0:
            return False
        with path.open('rb') as source:
            if image_format(source.read(64))!=image['format']:
                return False
            source.seek(0)
            return hashlib.file_digest(source,'sha256').hexdigest()==image['sha256']
    except (OSError,ValueError,KeyError,TypeError):
        return False


def _reuse_image(image, previous, directory):
    """将校验通过的历史原件复制到新批次，保持旧批次不变；无效项返回 None。"""
    if previous is None:
        return None
    candidates=[p for p in previous['images'] if isinstance(p,dict) and p.get('index')==image['index']]
    if len(candidates)!=1:
        return None
    old=candidates[0]
    if any(old.get(k)!=v for k,v in image.items()) or old.get('format') not in FORMATS:
        return None
    filename=f"{image['index']}{FORMATS[old['format']][0]}"
    if old.get('image_file')!=f'{IMAGE_FOLDER}/{filename}':
        return None
    source=directory.parent/previous['batch_id']/IMAGE_FOLDER/filename
    try:
        source.resolve().relative_to(directory.parent.parent.resolve())
    except ValueError:
        return None
    if not _valid_saved_image(source,old):
        return None
    folder=directory/IMAGE_FOLDER
    folder.mkdir(parents=True,exist_ok=True)
    temporary=folder/f'.{image["index"]}.reuse.part'
    destination=folder/filename
    try:
        with source.open('rb') as incoming, temporary.open('xb') as output:
            shutil.copyfileobj(incoming,output)
        if not _valid_saved_image(temporary,old):
            return None
        temporary.replace(destination)
    except OSError:
        return None
    finally:
        temporary.unlink(missing_ok=True)
    return {**old,**image,'image_path':str(destination.resolve()),'reused_from_batch_id':previous['batch_id']}


def reuse_image_file(image, previous_image, source, directory, *, file_stem, image_folder, source_batch_id):
    """校验并复制历史图片到新批次；由调用方核对所属商品、组合、来源及源路径范围。"""
    if (not file_stem or file_stem in {'.','..'} or any(c in file_stem for c in '/\\')
            or not isinstance(previous_image,dict) or previous_image.get('image_url')!=image['image_url']
            or previous_image.get('format') not in FORMATS or not _valid_saved_image(source,previous_image)):
        return None
    directory.mkdir(parents=True,exist_ok=True)
    destination=directory/(file_stem+FORMATS[previous_image['format']][0])
    temporary=directory/f'.{image["index"]}-{uuid4().hex}.reuse.part'
    try:
        with source.open('rb') as incoming,temporary.open('xb') as output:
            shutil.copyfileobj(incoming,output)
        if not _valid_saved_image(temporary,previous_image):
            return None
        temporary.replace(destination)
    except OSError:
        return None
    finally:
        temporary.unlink(missing_ok=True)
    return {**previous_image,**image,'image_file':f'{image_folder}/{destination.name}',
            'image_path':str(destination.resolve()),'reused_from_batch_id':source_batch_id}


def collect_main_images(settings,result):
    """读取已核对的主图清单，逐张保存进度，恢复相同来源的未完成图片并整批发布。"""
    guard=active_guard()
    if guard is None: raise RuntimeError("主图采集必须在淘宝专用守卫中运行")
    links=collect_main_image_links(result,settings.timeout_ms)
    product_dir=product_directory(settings,result.target.item_id)
    fingerprint=_manifest_fingerprint(result.target.item_id,links)
    previous_batches=_previous_progress(product_dir,result.target.item_id,fingerprint)
    batch_id=guard.task_id+'-'+uuid4().hex[:8]
    directory=product_dir/'history'/batch_id
    record={'schema_version':2,'batch_id':batch_id,'task_id':guard.task_id,'product_id':result.target.item_id,
            'source_url':resolve_detail_input(result.page.url).canonical_url,'captured_at':now(),
            'scope':'main_gallery_images','expected_count':links['expected_count'],'available_count':links['available_count'],
            'video_cover_skipped':links['video_cover_skipped'],'parameters_skipped':links['parameters_skipped'],
            'selector':links['selector'],'collection_method':links['collection_method'],'source_path':links['source_path'],
            'verified_source_paths':links['verified_source_paths'],'manifest_fingerprint':fingerprint,
            'history_record_file':f'history/{batch_id}/main_images.json',
            'resumed_from_batch_id':previous_batches[0]['batch_id'] if previous_batches else None,
            'image_results':[{**p,'status':'pending','error':None} for p in links['images']],
            'images':[],'status':'in_progress','error':None}
    save_images_progress(directory,record)

    def completed(image, entry):
        record['images'].append(image)
        record['images'].sort(key=lambda p:p['index'])
        entry.update(status='reused' if image.get('reused_from_batch_id') else 'downloaded',error=None)
        save_images_progress(directory,record)
        print(f"主图进度：已保存{record['collected_count']}/{record['expected_count']}张，复用{record['reused_count']}张",flush=True)

    try:
        # 先保留所有可复用项，即使后面的网络下载失败，也不会丢掉已确认的历史进度。
        if previous_batches:
            for image,entry in zip(links['images'],record['image_results']):
                confirm_main_image_links(result,links)
                for previous in previous_batches:
                    reused=_reuse_image(image,previous,directory)
                    if reused is not None:
                        completed(reused,entry)
                        break
        for image,entry in zip(links['images'],record['image_results']):
            if entry['status']=='reused':
                continue
            guard.pause_between('page')
            confirm_main_image_links(result,links)
            entry['status']='downloading'
            save_images_progress(directory,record)
            try:
                downloaded=download_image(image,directory/IMAGE_FOLDER,result,guard)
            except HTTPError as error:
                message=f"第{image['index']}张主图返回 HTTP {error.code}；已保存进度，未自动重试"
            except (URLError,OSError,ValueError,IncompleteRead) as error:
                message=f"第{image['index']}张主图下载或校验失败（{type(error).__name__}）；已保存进度，未自动重试"
            else:
                completed(downloaded,entry)
                continue
            entry.update(status='failed',error=message)
            record.update(status='download_failed',error=message)
            save_images_progress(directory,record)
            break
        else:
            confirm_main_image_links(result,links)
            if len(record['images'])==record['expected_count']:
                record.update(status='complete',error=None)
            else:
                record.update(status='download_failed',error='主图保存数量与本次确认的图集数量不一致，未发布为完整结果')
        saved=guard.perform('保存主图及详情记录',lambda:save_images_result(directory,record))
    except (Exception,KeyboardInterrupt):
        record.update(status='interrupted',error='主图任务中断；已完成的图片保留，下次相同来源可复用')
        for entry in record['image_results']:
            if entry['status']=='downloading':
                entry.update(status='interrupted',error='本张下载中断，未记为成功')
        try:
            save_images_progress(directory,record)
        except (OSError,ValueError):
            pass  # 保留最后一次成功写入的历史进度，不覆盖原始中断原因。
        raise
    message=(f"主图已保存：{record['collected_count']} 张；目录：{product_directory(settings,result.target.item_id)/IMAGE_FOLDER}"
             if record['status']!='download_failed' else record['error'])
    return {**saved,'message':message}
