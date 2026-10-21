"""通过淘宝首页表单搜索，仅确认首屏结果，不翻页或采集商品明细。"""
import time
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.login import is_authenticated
from ecommerce.platforms.taobao.cards import CARD_SELECTOR
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped

SEARCH_HOST = "s.taobao.com"
HOME_INPUT = '#J_TSearchForm input#q[name="q"]'
HOME_BUTTON = '#J_TSearchForm button.btn-search'

_PROBE = r"""() => {
 const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false;}return true;};
 const inputs=[...document.querySelectorAll('input[name="q"],input#q')].filter(visible).map(e=>e.value);
 const ids=new Set();
 for(const a of document.querySelectorAll('a[href]')){
   if(!visible(a))continue;
   const r=a.getBoundingClientRect();
   if(r.bottom<=0||r.right<=0||r.top>=innerHeight||r.left>=innerWidth)continue;
   try{
     const u=new URL(a.href,location.href),id=u.searchParams.get('id');
     if(['item.taobao.com','detail.tmall.com'].includes(u.hostname)&&u.pathname==='/item.htm'&&id&&/^\d+$/.test(id))ids.add(id);
   }catch{}
 }
 const empty=[...document.querySelectorAll('p,div,span,h1,h2,h3')].some(e=>{
   if(!visible(e)||e.children.length)return false;
   return /(?:没有找到|未找到|没有搜索到).{0,15}(?:相关|符合).{0,12}(?:宝贝|商品)|抱歉.{0,12}没有找到.{0,12}(?:宝贝|商品)/.test(e.innerText||'');
 });
 const anonymous=[...document.querySelectorAll('.site-nav-login a[href*="login.taobao.com"]')].some(visible);
 const visible_cards=[...document.querySelectorAll(__CARD_SELECTOR__)].filter(e=>{
   if(!visible(e))return false;const r=e.getBoundingClientRect();
   return r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth
    &&[...e.querySelectorAll('[class*="title--"]')].some(n=>visible(n)&&(n.innerText||'').trim())
    &&[...e.querySelectorAll('[class*="innerPriceWrapper--"]')].some(n=>visible(n)&&(n.innerText||'').trim());
 }).length;
 return {inputs,visible_items:ids.size,visible_cards,empty,anonymous};
}""".replace("__CARD_SELECTOR__", json.dumps(CARD_SELECTOR))


@dataclass(frozen=True)
class SearchPage:
    page: object
    keyword: str
    status: str
    visible_items: int


def validate_keyword(keyword):
    """统一项目输入限制，不静默截断或改写关键词。"""
    if not isinstance(keyword, str):
        raise ValueError("搜索关键词必须为文本")
    if any(ord(character) < 32 or ord(character) == 127 for character in keyword):
        raise ValueError("搜索关键词不能包含换行或控制字符")
    keyword = keyword.strip()
    if not keyword or len(keyword) > 100:
        raise ValueError("搜索关键词须为 1～100 个字符")
    return keyword


def matches_search_url(page, keyword):
    try:
        url = urlsplit(page.url)
        return (url.scheme == "https" and url.hostname == SEARCH_HOST and url.path.rstrip("/") == "/search"
                and parse_qs(url.query).get("q") == [keyword])
    except ValueError:
        return False


def canonical_search_url(keyword):
    """输出只保留本次关键词，不保存跟踪、授权或设备参数。"""
    return "https://s.taobao.com/search?" + urlencode({"q": keyword})


def inspect_search(page, keyword):
    """空白、旧关键词和隐藏模板都不能作为成功或无结果证据。"""
    if page.is_closed() or not matches_search_url(page, keyword):
        return None
    try:
        evidence = page.evaluate(_PROBE)
    except PlaywrightError:
        return None
    if not evidence["inputs"] or any(value != keyword for value in evidence["inputs"]) or evidence["anonymous"]:
        return None
    if evidence["visible_items"] or evidence.get("visible_cards", 0):
        return SearchPage(page, keyword, "results", evidence["visible_items"])
    if evidence["empty"]:
        return SearchPage(page, keyword, "empty", 0)
    return None


def _guard():
    guard = active_guard()
    if guard is None:
        raise RuntimeError("淘宝搜索必须在淘宝专用会话守卫中运行")
    return guard


def open_search(context, home, keyword, timeout_ms):
    keyword = validate_keyword(keyword)
    guard = _guard()
    guard.update_progress(keyword=keyword, page_number=1)
    guard.resume_check = lambda: is_authenticated(home)
    control = home.locator(HOME_INPUT)
    button = home.locator(HOME_BUTTON)
    if control.count() != 1 or button.count() != 1 or not control.is_visible() or not button.is_visible():
        guard.request_attention(home, "未能唯一识别淘宝首页搜索框或搜索按钮，请检查页面", step="定位淘宝搜索入口")
        if control.count() != 1 or button.count() != 1 or not control.is_visible() or not button.is_visible():
            raise NeedsAttention("淘宝首页搜索入口仍未确认，未提交搜索")
    guard.perform("填写淘宝搜索关键词", lambda: control.fill(keyword), completed=lambda: control.input_value() == keyword)
    if control.input_value() != keyword:
        raise NeedsAttention("搜索框关键词核对失败，未提交搜索")

    def ready_to_submit():
        try:
            return (control.count() == 1 and button.count() == 1 and control.is_visible() and control.is_enabled()
                    and button.is_visible() and button.is_enabled() and control.input_value() == keyword)
        except PlaywrightError:
            return False

    guard.resume_check = lambda: is_authenticated(home) and ready_to_submit()
    guard.checkpoint("搜索提交前等待")
    guard.pause_between("before_search")
    while not ready_to_submit():
        guard.request_attention(home, "等待期间搜索框或按钮发生变化，请核对关键词和页面状态；尚未提交搜索", step="复核搜索提交条件")
    prior_pages = set(context.pages)

    def candidates():
        return [page for page in context.pages if (page is home or page not in prior_pages)
                and not page.is_closed() and matches_search_url(page, keyword)]

    # 表单提交和验证恢复阶段只读核对搜索地址，随后另行等待首屏内容就绪。
    guard.resume_check = lambda: bool(candidates()) or (is_authenticated(home) and control.input_value() == keyword)
    with guard.expect_new_page(lambda page: matches_search_url(page, keyword), timeout_ms):
        guard.perform("提交淘宝关键词搜索", lambda: button.click(timeout=min(timeout_ms, 5000)), completed=lambda: bool(candidates()))
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            paused_before = guard.paused_seconds
            guard.checkpoint("等待淘宝搜索结果")
            deadline += guard.paused_seconds - paused_before
            pages = candidates()
            if len(pages) > 1:
                guard.request_attention(pages[-1], "检测到多个相同关键词的结果页，请只保留本次目标页", step="核对搜索结果页")
                continue
            if pages:
                result = inspect_search(pages[0], keyword)
                if result is not None:
                    guard.resume_check = lambda: inspect_search(result.page, keyword) is not None
                    guard.pause_between("search")
                    # 停顿期间页面可能变化，再次核对后才返回成功。
                    confirmed = inspect_search(result.page, keyword)
                    if confirmed is not None:
                        guard.checkpoint("搜索结果已确认")
                        return confirmed
            if time.monotonic() >= deadline:
                target = pages[0] if pages else home
                guard.request_attention(target, "尚未确认本次关键词的商品列表或明确无结果提示；不会自动重新搜索，请检查页面后回复继续", step="检查搜索结果加载")
                deadline = time.monotonic() + timeout_ms / 1000
            paused_before = guard.paused_seconds
            guard.wait(250)
            deadline += guard.paused_seconds - paused_before


def keep_search_open(context, result):
    guard = _guard()
    print(f"关键词“{result.keyword}”的搜索结果已确认并保存。浏览器保持打开，关闭搜索标签页即可结束本次任务。", flush=True)
    while not result.page.is_closed():
        try:
            guard.wait(300)
        except TaskStopped:
            if result.page.is_closed() and not guard.awaiting_manual:
                return
            raise
