"""读取主图图集，按缩略图位置选择实际大图，排除视频封面和参数入口。"""
import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.detail_video import FIRST_THUMB_SELECTOR
from ecommerce.platforms.taobao.verification import active_guard

IMAGE_SELECTOR = "#picGalleryEle #mainPicImageEl"
GALLERY_SNAPSHOT = r"""() => {
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>[...n.classList].some(c=>c.startsWith(p+'--')));
 const asset=e=>{const raw=e?.currentSrc||e?.getAttribute('src');if(!raw)return null;try{return new URL(raw,location.href).href;}catch{return null;}};
 const roots=[...document.querySelectorAll('#picGalleryEle')];
 if(roots.length!==1)return {ready:false,reason:'主图画廊未唯一确认',photos:[]};
 const root=roots[0],lists=parts(root,'thumbnails'),mains=parts(root,'mainPicWrap');
 if(lists.length!==1||mains.length!==1)return {ready:false,reason:'主图或缩略图区域结构未确认',photos:[]};
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
 const images=[...mains[0].querySelectorAll('#mainPicImageEl')],image=images.length===1?images[0]:null;
 return {ready:thumbs.length>0&&photos.length>0&&!unknown.length&&(!videoTabs.length||videoSkipped===1)&&photos.every(p=>p.loaded&&p.thumbnail_url),
  reason:unknown.length?'存在未知缩略图入口':'主图缩略图尚未加载完成',photos,unknown_slots:unknown,
  video_cover_skipped:videoSkipped,parameters_skipped:parametersSkipped,
  current_image:image?{url:asset(image),loaded:image.complete&&image.naturalWidth>0,width:image.naturalWidth,height:image.naturalHeight}:null};
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


def collect_main_image_links(result, timeout_ms, count=5):
    guard = active_guard()
    if guard is None:
        raise RuntimeError("主图采集必须在淘宝专用守卫中运行")
    page, item_id = result.page, result.target.item_id

    def read():
        return read_gallery(page, item_id)

    def ready():
        try:
            return bool(read().get("ready"))
        except (ValueError, PlaywrightError):
            return False

    def wait_for(check, reason):
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            paused = guard.paused_seconds
            guard.checkpoint("等待主图加载")
            deadline += guard.paused_seconds - paused
            try:
                value = read()
                if check(value):
                    return value
            except (ValueError, PlaywrightError):
                pass
            if time.monotonic() >= deadline:
                guard.request_attention(page, reason + "；请人工核对后回复继续，不会重开商品或重复点击", step="复核主图采集")
                deadline = time.monotonic() + timeout_ms / 1000
            paused = guard.paused_seconds
            guard.wait(250)
            deadline += guard.paused_seconds - paused

    guard.resume_check = ready
    guard.pause_between("page")
    initial = wait_for(lambda value: value.get("ready"), "主图画廊或缩略图尚未确认")
    signature = gallery_signature(initial)
    selected = initial["photos"][:count]
    images = []
    for index, photo in enumerate(selected, 1):
        slot = photo["slot_index"]

        def selected_ready(value):
            current = value.get("current_image") or {}
            active = [p for p in value.get("photos", []) if p["active"]]
            return (value.get("ready") and gallery_signature(value) == signature and len(active) == 1
                    and active[0]["slot_index"] == slot and current.get("loaded") and current.get("url")
                    and image_asset_key(current["url"]) == image_asset_key(photo["thumbnail_url"]))

        def selected_check():
            try:
                return selected_ready(read())
            except (ValueError, PlaywrightError):
                return False

        current = wait_for(lambda value: value.get("ready"), "主图画廊尚未就绪")
        if gallery_signature(current) != signature:
            guard.request_attention(page, "主图列表顺序或来源发生变化，请恢复原商品图集后继续", step="复核主图顺序")
            current = wait_for(lambda value: value.get("ready") and gallery_signature(value) == signature, "主图列表与开始采集时不一致")
        if not any(p["active"] and p["slot_index"] == slot for p in current["photos"]):
            guard.pause_between("page")
            guard.resume_check = lambda: ready()
            current = wait_for(lambda value: value.get("ready") and gallery_signature(value) == signature, "等待期间主图列表发生变化")
            if not any(p["active"] and p["slot_index"] == slot for p in current["photos"]):
                guard.perform(f"选择第{index}张主图", lambda: page.locator(FIRST_THUMB_SELECTOR).nth(slot-1).click(timeout=min(timeout_ms,5000)),
                              completed=selected_check)
        guard.resume_check = selected_check
        confirmed = wait_for(selected_ready, f"第{index}张主图的大图尚未与选中缩略图对应")
        guard.wait(300)
        after = wait_for(lambda value: selected_ready(value) and value["current_image"]["url"] == confirmed["current_image"]["url"],
                         f"第{index}张主图仍在切换")
        images.append({"index": index, "slot_index": slot, "thumbnail_url": photo["thumbnail_url"],
                       "image_url": after["current_image"]["url"], "page_width": after["current_image"]["width"],
                       "page_height": after["current_image"]["height"]})
    def signature_ready():
        try:
            value = read()
            return value.get("ready") and gallery_signature(value) == signature
        except (ValueError, PlaywrightError):
            return False

    guard.resume_check = signature_ready
    return {"images": images, "expected_count": count, "available_count": len(initial["photos"]),
            "video_cover_skipped": initial["video_cover_skipped"], "parameters_skipped": initial["parameters_skipped"],
            "selector": IMAGE_SELECTOR, "gallery_signature": signature}
