"""仅检测详情主图画廊的视频，未知结构或未加载完成不判为无视频。"""
import time

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import active_guard

GALLERY_SELECTOR = "#picGalleryEle"
FIRST_THUMB_SELECTOR = '#picGalleryEle [class*="thumbnails--"] > [class*="thumbnail--"]'
VIDEO_SELECTOR = '#picGalleryEle #mainPicVideoEl video,#picGalleryEle [class*="mainPicVideo--"] video'

VIDEO_SNAPSHOT = r"""() => {
 const shown=e=>{if(!e)return false;const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
  for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden')return false;}return true;};
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>[...n.classList].some(c=>c.startsWith(p+'--')));
 const roots=[...document.querySelectorAll('#picGalleryEle')];
 if(roots.length!==1)return {gallery_count:roots.length,ready:false,video_present:null,video_count:0,url:null};
 const root=roots[0],main=parts(root,'mainPicWrap'),thumbLists=parts(root,'thumbnails');
 const thumbs=thumbLists.length===1?[...thumbLists[0].children].filter(n=>[...n.classList].some(c=>c.startsWith('thumbnail--'))):[];
 const first=thumbs[0],firstImage=first?.querySelector('img');
 const firstActive=!!first&&[...first.classList].some(c=>c.startsWith('thumbnailActive--'));
 const containers=[...root.querySelectorAll('#mainPicVideoEl,[class*="mainPicVideo--"]')];
 const videos=[...new Set(containers.flatMap(n=>[...n.querySelectorAll('video')]))];
 const marker=containers.length>0||parts(root,'videoPicWrap').length>0;
 const present=marker||videos.length>0;
 const imageReady=main.length===1&&[...main[0].querySelectorAll('img')].some(i=>i.complete&&i.naturalWidth>0);
 const busy=[...root.querySelectorAll('[aria-busy="true"],.next-loading-indicator')].some(shown);
 const video=videos.length===1?videos[0]:null;
 const raw=video?(video.currentSrc||video.getAttribute('src')||video.querySelector('source[src]')?.getAttribute('src')):null;
 let url=null;try{if(raw)url=new URL(raw,location.href).href;}catch{}
 return {gallery_count:1,main_count:main.length,thumbnail_count:thumbs.length,first_active:firstActive,
  ready:shown(root)&&main.length===1&&thumbLists.length===1&&thumbs.length>0&&!!firstImage?.complete&&firstImage.naturalWidth>0&&!busy&&(present||imageReady),
  video_present:present,video_count:videos.length,url,
  duration_seconds:video&&Number.isFinite(video.duration)&&video.duration>0?video.duration:null};
}"""


def inspect_main_video(page, item_id):
    if not matches_detail_url(page, item_id):
        raise ValueError("当前页面的商品 ID 与视频采集目标不一致")
    return page.evaluate(VIDEO_SNAPSHOT)


def detect_main_video(result, timeout_ms):
    """确认首个主图槽位；有视频时等待资源地址，无视频时复核后立即跳过。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("主图视频检测必须在淘宝专用守卫中运行")
    page, item_id = result.page, result.target.item_id

    def read():
        return inspect_main_video(page, item_id)

    def ready():
        try:
            value = read()
            return value.get("ready") and value.get("first_active") and value.get("video_count", 0) <= 1
        except (ValueError, PlaywrightError):
            return False

    guard.resume_check = ready
    guard.checkpoint("检查主图视频前等待")
    guard.pause_between("page")
    deadline = time.monotonic() + timeout_ms / 1000
    activated = False
    absent_confirmed = False
    while True:
        paused = guard.paused_seconds
        guard.checkpoint("检查详情主图视频")
        deadline += guard.paused_seconds - paused
        try:
            value = read()
        except (ValueError, PlaywrightError):
            value = {}
        if value.get("gallery_count") == 1 and value.get("main_count") == 1 and value.get("thumbnail_count", 0) > 0:
            if not value.get("first_active") and not activated:
                activated = True
                guard.perform("选择主图首个缩略图", lambda: page.locator(FIRST_THUMB_SELECTOR).first.click(timeout=min(timeout_ms, 5000)),
                              completed=lambda: read().get("first_active"))
                continue
            if value.get("ready") and value.get("first_active"):
                if value.get("video_count", 0) <= 1 and value.get("video_present") and value.get("url"):
                    return {**value, "status": "found"}
                if value.get("video_present") is False:
                    if absent_confirmed:
                        return {**value, "status": "no_video"}
                    absent_confirmed = True
                    paused = guard.paused_seconds
                    guard.wait(750)
                    deadline += guard.paused_seconds - paused
                    continue
        if value.get("video_present") is not False or not value.get("ready") or not value.get("first_active"):
            absent_confirmed = False
        if time.monotonic() >= deadline:
            reason = ("已检测到主图视频，但尚未取得唯一视频地址" if value.get("video_present")
                      else "主图画廊结构或加载状态尚未确认，不能判定为无视频")
            guard.request_attention(page, reason + "；请核对首个主图视频或手动播放后回复继续，不会刷新或重新打开商品", step="复核主图视频")
            deadline = time.monotonic() + timeout_ms / 1000
        paused = guard.paused_seconds
        guard.wait(250)
        deadline += guard.paused_seconds - paused
