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
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section
from ecommerce.modules.collection.detail_video import validate_media_url
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.platforms.taobao.detail_images import collect_main_image_links
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
    raise ValueError("主图响应不是当前支持的图片文件")


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
        if not matches_detail_url(page,item_id): raise NeedsAttention("主图下载前商品页面发生变化")
        with opener.open(request,timeout=15) as response:
            validate_media_url(response.url)
            if response.status != 200: raise ValueError("主图响应未返回完整文件")
            content_type=response.headers.get_content_type()
            if not (content_type.startswith('image/') or content_type=='application/octet-stream'):
                raise ValueError("主图响应不是图片")
            length=response.headers.get('Content-Length')
            expected=int(length) if length and length.isdigit() else None
            if expected is not None and not 0 < expected <= MAX_IMAGE_BYTES: raise ValueError("主图长度无效或超过下载保护上限")
            head=response.read(64)
            actual_format=image_format(head)
            suffix,mime=FORMATS[actual_format]
            with temporary.open('xb') as output:
                chunk=head
                while chunk:
                    guard.checkpoint(f"下载第{image['index']}张{label}")
                    if not matches_detail_url(page,item_id): raise NeedsAttention("主图下载期间商品页面发生变化")
                    size+=len(chunk)
                    if size>MAX_IMAGE_BYTES: raise ValueError("主图超过下载保护上限")
                    output.write(chunk);digest.update(chunk)
                    chunk=response.read(1024*1024)
            if expected is not None and size!=expected: raise ValueError("主图未完整接收")
        guard.checkpoint("核对主图文件")
        try:
            dimensions=page.evaluate("""async ({data,mime})=>{
              const image=new Image();image.src='data:'+mime+';base64,'+data;await image.decode();
              return {width:image.naturalWidth,height:image.naturalHeight};
            }""",{"data":base64.b64encode(temporary.read_bytes()).decode('ascii'),"mime":mime})
        except PlaywrightError as error:
            guard.checkpoint("复核主图解码状态")
            raise ValueError("主图无法完整解码") from error
        if not dimensions['width'] or not dimensions['height']: raise ValueError("主图尺寸无效")
        if not matches_detail_url(page,item_id): raise NeedsAttention("主图保存前商品页面发生变化")
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


def save_images_result(directory,record):
    product_dir=directory.parent.parent
    item_id=record['product_id']
    document=read_detail_document(product_dir,item_id)
    destination=directory/'main_images.json'
    directory.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(destination)
    if record['status']=='download_failed':
        record_path=save_detail_section(product_dir,item_id,'main_images_last_attempt',record)
        return {**record,'record_path':str(record_path.resolve()),'history_record_path':str(destination.resolve())}
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
        record_path=save_detail_section(product_dir,item_id,'main_images',latest)
    except Exception:
        if installed: _remove_owned_tree(target,product_dir)
        if backup.exists(): backup.replace(target)
        raise
    finally:
        _remove_owned_tree(staging,product_dir)
    _remove_owned_tree(backup,product_dir)
    return {**latest,'record_path':str(record_path.resolve()),'history_record_path':str(destination.resolve())}


def collect_main_images(settings,result):
    guard=active_guard()
    if guard is None: raise RuntimeError("主图采集必须在淘宝专用守卫中运行")
    links=collect_main_image_links(result,settings.timeout_ms,count=5)
    batch_id=guard.task_id+'-'+uuid4().hex[:8]
    directory=product_directory(settings,result.target.item_id)/'history'/batch_id
    record={'schema_version':1,'batch_id':batch_id,'task_id':guard.task_id,'product_id':result.target.item_id,
            'source_url':resolve_detail_input(result.page.url).canonical_url,'captured_at':now(),
            'scope':'main_gallery_images','expected_count':5,'available_count':links['available_count'],
            'video_cover_skipped':links['video_cover_skipped'],'parameters_skipped':links['parameters_skipped'],
            'selector':links['selector'],'images':[],'status':'complete','error':None}
    for image in links['images']:
        try:
            record['images'].append(download_image(image,directory/IMAGE_FOLDER,result,guard))
        except HTTPError as error:
            record.update(status='download_failed',error=f"第{image['index']}张主图返回 HTTP {error.code}；未自动重试")
            break
        except (URLError,OSError,ValueError,IncompleteRead) as error:
            record.update(status='download_failed',error=f"第{image['index']}张主图下载或校验失败（{type(error).__name__}）；未自动重试")
            break
    record['collected_count']=len(record['images'])
    if record['status']=='complete' and len(record['images'])<5: record['status']='partial'
    saved=guard.perform('保存主图及详情记录',lambda:save_images_result(directory,record))
    message=(f"主图已保存：{record['collected_count']} 张；目录：{product_directory(settings,result.target.item_id)/IMAGE_FOLDER}"
             if record['status']!='download_failed' else record['error'])
    if record['status']=='partial': message+='；页面实际主图不足 5 张，已按实际数量记录'
    return {**saved,'message':message}
