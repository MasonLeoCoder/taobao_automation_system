"""直接读取主图缩略图DOM，按完整地址去重并保留首次出现的顺序。"""
import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import Blockage,active_guard

IMAGE_SELECTOR = "#picGalleryEle"
DOM_IMAGES_SOURCE = '#picGalleryEle [class*="thumbnails--"] img'
GALLERY_SNAPSHOT = r"""() => {
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>[...n.classList].some(c=>c.startsWith(p+'--')));
 const url=raw=>{if(typeof raw!=='string'||!raw.trim())return null;try{const u=new URL(raw,location.href);
  return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password&&(!u.port||u.port===(u.protocol==='https:'?'443':'80'))
   &&['alicdn.com','taobao.com','tmall.com'].some(d=>u.hostname===d||u.hostname.endsWith('.'+d))?u.href:null;}catch{return null;}};
 const asset=e=>url(e?.getAttribute('src'));
 const roots=[...document.querySelectorAll('#picGalleryEle')];
 if(roots.length!==1)return {ready:false,reason:'主图画廊未唯一确认',photos:[]};
 const root=roots[0],lists=parts(root,'thumbnails');
 if(lists.length!==1)return {ready:false,reason:'缩略图区域结构未确认',photos:[]};
 const entries=[...lists[0].children],photos=[];let parametersSkipped=0;
 entries.forEach((entry,i)=>{
  const imgs=[...entry.querySelectorAll('img')];
  if(!imgs.length&&(entry.innerText||'').trim()==='参数')parametersSkipped++;
  imgs.forEach(img=>photos.push({slot_index:i+1,thumbnail_url:asset(img)}));
 });
 return {ready:photos.length>0&&photos.every(p=>p.thumbnail_url),
  reason:photos.length?'主图DOM内存在尚未提供或不支持的图片地址':'主图DOM尚未提供图片',
  photos,video_cover_skipped:0,parameters_skipped:parametersSkipped};
}"""


def image_asset_key(url):
    """仅用文件名核对缩略图与大图是同一资源，不改写或拼接下载地址。"""
    if not isinstance(url, str):
        return None
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    match = re.fullmatch(r"(.+?\.(?:jpe?g|png|webp|gif|avif|bmp))(?:_.*)?", name, re.I)
    return match[1] if match else name or None


def read_gallery(page, item_id):
    if not matches_detail_url(page, item_id):
        raise ValueError("当前商品 ID 与主图采集目标不一致")
    return page.evaluate(GALLERY_SNAPSHOT)


def gallery_signature(value):
    return [(p["slot_index"], p["thumbnail_url"]) for p in value.get("photos", [])]


def _dom_image_url(url):
    """规范化DOM完整地址并检查下载域名；参数：图片地址；返回：保留查询和压缩后缀的地址。"""
    if not isinstance(url,str) or not url or any(ord(c)<32 or ord(c)==127 for c in url):
        raise ValueError('主图DOM包含无效图片地址')
    parsed=urlsplit('https:'+url if url.startswith('//') else url)
    host=parsed.hostname or ''
    if (parsed.scheme not in {'https','http'} or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None,443 if parsed.scheme=='https' else 80}
            or not any(host==domain or host.endswith('.'+domain) for domain in ('alicdn.com','taobao.com','tmall.com'))):
        raise ValueError('主图DOM包含不支持的图片地址')
    return parsed._replace(netloc=host,fragment='').geturl()


def gallery_diagnostics(value,item_id):
    """记录DOM图片位置与地址；参数：画廊快照和目标商品；返回：去除查询凭证的诊断字典。"""
    def address(url):
        """隐藏地址中的查询凭证；参数：来源地址；返回：无查询参数和片段的地址。"""
        if not isinstance(url,str): return None
        parsed=urlsplit(url)
        return parsed._replace(netloc=parsed.hostname or '',query='',fragment='').geturl()
    photos=value.get('photos') or []
    return {'target_item_id':item_id,'collection_method':'dom_thumbnails','source_path':DOM_IMAGES_SOURCE,
            'raw_image_count':len(photos),'dom_images':[{'slot_index':p.get('slot_index'),
            'image_url':address(p.get('thumbnail_url'))} for p in photos]}


class MainImageCheckError(ValueError):
    """主图复核失败时保留结构化差异，供暂停及恢复检查共用。"""
    def __init__(self,reason,details):
        """保存具体原因与诊断；参数：原因、差异；返回：异常对象。"""
        super().__init__(reason)
        self.check_details=details


def main_image_manifest(value, item_id):
    """仅用DOM图片按完整地址去重编号；参数：同次快照、目标商品；返回：有序实际下载清单。"""
    if not value.get("ready"):
        raise ValueError(value.get("reason", "主图画廊尚未确认"))
    photos=value.get('photos')
    if not isinstance(photos,list) or not photos:
        raise ValueError('主图DOM尚未提供图片')
    seen=set();images=[]
    for photo in photos:
        if not isinstance(photo,dict) or type(photo.get('slot_index')) is not int or photo['slot_index']<1:
            raise ValueError('主图DOM图片位置无效')
        url=_dom_image_url(photo.get('thumbnail_url'))
        if url in seen:
            continue
        seen.add(url)
        images.append({'index':len(images)+1,'slot_index':photo['slot_index'],'thumbnail_url':url,'image_url':url})
    return {'images':images,'expected_count':len(images),'available_count':len(images),
            'raw_image_count':len(photos),'duplicate_count':len(photos)-len(images),'deduplication':'normalized_url_first',
            'video_cover_skipped':0,'parameters_skipped':value.get('parameters_skipped',0),
            'selector':IMAGE_SELECTOR,'collection_method':'dom_thumbnails','source_path':DOM_IMAGES_SOURCE,
            'verified_source_paths':[DOM_IMAGES_SOURCE],'gallery_signature':gallery_signature(value)}


def _read_manifest(result):
    value=read_gallery(result.page,result.target.item_id)
    try:
        return main_image_manifest(value,result.target.item_id)
    except ValueError as error:
        raise MainImageCheckError(str(error),gallery_diagnostics(value,result.target.item_id)) from error


def _resume_manifest(result,expected=None):
    """恢复时返回具体业务阻断；参数：当前商品和可选原清单；返回：True或保留差异的阻断。"""
    try:
        current=_read_manifest(result)
        if expected is not None and [p['image_url'] for p in current['images']]!=[p['image_url'] for p in expected['images']]:
            return Blockage('business',result.page,'主图DOM去重后的图片地址、数量或顺序与本批开始时不一致',
                check_details={'target_item_id':result.target.item_id,
                    'expected_images':gallery_diagnostics({'photos':expected['images']},result.target.item_id)['dom_images'],
                    'current_images':gallery_diagnostics({'photos':current['images']},result.target.item_id)['dom_images']})
        return True
    except MainImageCheckError as error:
        return Blockage('business',result.page,str(error),check_details=error.check_details)
    except (ValueError,PlaywrightError):
        return Blockage('business',result.page,'当前商品页面或主图数据无法复核')


def collect_main_image_links(result, timeout_ms):
    """等待页面提供可核对的完整主图列表，返回清单；缺失时按具体原因暂停人工检查。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("主图采集必须在淘宝专用守卫中运行")
    guard.resume_check = lambda: _resume_manifest(result)
    guard.pause_between("page")
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        paused = guard.paused_seconds
        guard.checkpoint("读取页面主图列表")
        deadline += guard.paused_seconds - paused
        try:
            manifest = _read_manifest(result)
            guard.resume_check = lambda: _resume_manifest(result, manifest)
            return manifest
        except (ValueError,PlaywrightError):
            pass
        if time.monotonic() >= deadline:
            blockage=_resume_manifest(result)
            if blockage is not True:
                guard.request_attention(result.page, blockage.source + "；请处理后回复继续，不会重开商品或点击缩略图",
                    step="复核主图列表",check_details=blockage.check_details)
            deadline = time.monotonic() + timeout_ms / 1000
        paused = guard.paused_seconds
        guard.wait(250)
        deadline += guard.paused_seconds - paused


def confirm_main_image_links(result, manifest):
    """下载及发布前复核清单仍属于当前商品；改变时保留现场等待明确人工反馈。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("主图复核必须在淘宝专用守卫中运行")
    guard.resume_check = lambda: _resume_manifest(result, manifest)
    guard.checkpoint("复核主图来源及顺序")
    while True:
        blockage=_resume_manifest(result,manifest)
        if blockage is True:
            return
        guard.request_attention(result.page,blockage.source+'；请恢复当前商品图集后回复继续',
            step='复核主图列表',check_details=blockage.check_details)
