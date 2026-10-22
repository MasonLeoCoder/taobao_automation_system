"""读取页面已有主图列表，与缩略图顺序核对；不点击或切换主图。"""
import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import active_guard

IMAGE_SELECTOR = "#picGalleryEle"
ITEM_IMAGES_SOURCE = "window.__ICE_APP_CONTEXT__.loaderData.home.data.res.item.images"
HEAD_IMAGES_SOURCE = "window.__ICE_APP_CONTEXT__.loaderData.home.data.res.componentsVO.headImageVO.images"
GALLERY_SNAPSHOT = r"""() => {
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>[...n.classList].some(c=>c.startsWith(p+'--')));
 const url=raw=>{if(typeof raw!=='string'||!raw.trim())return null;try{const u=new URL(raw,location.href);
  return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password&&(!u.port||u.port===(u.protocol==='https:'?'443':'80'))
   &&['alicdn.com','taobao.com','tmall.com'].some(d=>u.hostname===d||u.hostname.endsWith('.'+d))?u.href:null;}catch{return null;}};
 const asset=e=>url(e?.currentSrc||e?.getAttribute('src'));
 const roots=[...document.querySelectorAll('#picGalleryEle')];
 if(roots.length!==1)return {ready:false,reason:'主图画廊未唯一确认',photos:[]};
 const root=roots[0],lists=parts(root,'thumbnails');
 if(lists.length!==1)return {ready:false,reason:'缩略图区域结构未确认',photos:[]};
 const thumbs=[...lists[0].children].filter(e=>[...e.classList].some(c=>c.startsWith('thumbnail--')));
 const videoTabs=parts(root,'switchTabsItem').filter(e=>(e.innerText||'').trim()==='视频');
 if(videoTabs.length>1)return {ready:false,reason:'视频入口不唯一',photos:[]};
 const photos=[],unknown=[];let videoSkipped=0,parametersSkipped=0;
 thumbs.forEach((e,i)=>{
  const imgs=parts(e,'thumbnailPic').filter(n=>n.tagName==='IMG'),text=(e.innerText||'').trim();
  if(i===0&&videoTabs.length===1&&imgs.length===1){videoSkipped++;return;}
  if(imgs.length===0&&text==='参数'){parametersSkipped++;return;}
  if(imgs.length!==1){unknown.push(i+1);return;}
  photos.push({slot_index:i+1,thumbnail_url:asset(imgs[0]),loaded:imgs[0].complete&&imgs[0].naturalWidth>0,
   active:[...e.classList].some(c=>c.startsWith('thumbnailActive--'))});
 });
 const data=window.__ICE_APP_CONTEXT__?.loaderData?.home?.data,res=data?.res;
 const list=v=>v==null?null:Array.isArray(v)?v.map(url):false;
 const id=v=>typeof v==='string'||typeof v==='number'?String(v):null;
 return {ready:thumbs.length>0&&photos.length>0&&!unknown.length&&(!videoTabs.length||videoSkipped===1)&&photos.every(p=>p.thumbnail_url),
  reason:unknown.length?'存在未知缩略图入口':'主图缩略图地址尚未提供',photos,unknown_slots:unknown,
  video_cover_skipped:videoSkipped,parameters_skipped:parametersSkipped,
  data_item_id:id(res?.item?.itemId),ssr_item_id:id(data?.ssrItemId),
  item_images:list(res?.item?.images),head_images:list(res?.componentsVO?.headImageVO?.images)};
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


def _asset_identity(url):
    """核对图片所在目录与资源文件名，不修改实际下载地址。"""
    parsed = urlsplit(url)
    return parsed.hostname, parsed.path.rsplit("/", 1)[0], image_asset_key(url)


def main_image_manifest(value, item_id):
    """将同次快照内的商品数据与图集核对，返回有序下载清单；不一致抛出 ValueError。"""
    if not value.get("ready"):
        raise ValueError(value.get("reason", "主图画廊尚未确认"))
    if value.get("data_item_id") != item_id:
        raise ValueError("页面主图数据的商品ID与目标不一致或尚未提供")
    if value.get("ssr_item_id") not in {None, item_id}:
        raise ValueError("页面初始数据的商品ID与当前商品不一致")
    sources = [(ITEM_IMAGES_SOURCE, value.get("item_images")), (HEAD_IMAGES_SOURCE, value.get("head_images"))]
    available = [(path, images) for path, images in sources if images is not None]
    if not available:
        raise ValueError("页面尚未提供已适配的主图列表，不会点击缩略图或拼接地址")
    photos = value["photos"]
    expected = [_asset_identity(p["thumbnail_url"]) for p in photos]
    for path, images in available:
        label = "商品主图" if path == ITEM_IMAGES_SOURCE else "图集主图"
        if not isinstance(images, list) or not images or not all(isinstance(u, str) and u for u in images):
            raise ValueError(f"页面{label}列表为空、格式未确认或包含无效地址")
        if len(images) != len(photos):
            raise ValueError(f"页面{label}数量与缩略图数量不一致（{len(images)}/{len(photos)}）")
        if [_asset_identity(u) for u in images] != expected:
            raise ValueError(f"页面{label}来源或顺序与缩略图不一致")
    source, urls = available[0]
    return {"images": [{"index": index, "slot_index": photo["slot_index"],
                        "thumbnail_url": photo["thumbnail_url"], "image_url": url}
                       for index, (photo, url) in enumerate(zip(photos, urls), 1)],
            "expected_count": len(photos), "available_count": len(photos),
            "video_cover_skipped": value["video_cover_skipped"], "parameters_skipped": value["parameters_skipped"],
            "selector": IMAGE_SELECTOR, "collection_method": "page_data", "source_path": source,
            "verified_source_paths": [path for path, _ in available], "gallery_signature": gallery_signature(value)}


def _read_manifest(result):
    return main_image_manifest(read_gallery(result.page, result.target.item_id), result.target.item_id)


def _matches_manifest(result, expected=None):
    try:
        current = _read_manifest(result)
        return expected is None or current == expected
    except (ValueError, PlaywrightError):
        return False


def collect_main_image_links(result, timeout_ms):
    """等待页面提供可核对的完整主图列表，返回清单；缺失时按具体原因暂停人工检查。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("主图采集必须在淘宝专用守卫中运行")
    guard.resume_check = lambda: _matches_manifest(result)
    guard.pause_between("page")
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        paused = guard.paused_seconds
        guard.checkpoint("读取页面主图列表")
        deadline += guard.paused_seconds - paused
        try:
            manifest = _read_manifest(result)
            guard.resume_check = lambda: _matches_manifest(result, manifest)
            return manifest
        except ValueError as error:
            reason = str(error)
        except PlaywrightError:
            reason = "当前页面暂时无法读取主图数据"
        if time.monotonic() >= deadline:
            guard.request_attention(result.page, reason + "；请处理后回复继续，不会重开商品或点击缩略图", step="复核主图列表")
            deadline = time.monotonic() + timeout_ms / 1000
        paused = guard.paused_seconds
        guard.wait(250)
        deadline += guard.paused_seconds - paused


def confirm_main_image_links(result, manifest):
    """下载及发布前复核清单仍属于当前商品；改变时保留现场等待明确人工反馈。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("主图复核必须在淘宝专用守卫中运行")
    guard.resume_check = lambda: _matches_manifest(result, manifest)
    guard.checkpoint("复核主图来源及顺序")
    while not _matches_manifest(result, manifest):
        guard.request_attention(result.page, "主图数据、数量或顺序与本批开始时不一致，请恢复当前商品图集后回复继续", step="复核主图列表")
