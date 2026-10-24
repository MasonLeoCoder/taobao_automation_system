"""商品详情页入口：数字 ID 拼接地址，详情链接按输入直接打开。"""
from ecommerce.infrastructure.tasks.events import notify as print
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import TaskStopped

DETAIL_HOSTS = {"item.taobao.com", "detail.tmall.com"}
DETAIL_BASE_URL = "https://item.taobao.com/item.htm?id="


@dataclass(frozen=True)
class DetailTarget:
    item_id: str
    url: str
    input_type: str

    @property
    def canonical_url(self):
        """记录只保存商品入口，不保存输入链接里的跟踪或授权参数。"""
        return "https://" + urlsplit(self.url).hostname + "/item.htm?" + urlencode({"id": self.item_id})


@dataclass(frozen=True)
class DetailPage:
    page: object
    target: DetailTarget
    document_title: str


def _valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{1,25}", value) is not None and int(value) > 0


def resolve_detail_input(value):
    """区分商品 ID 与完整详情链接；链接通过校验后保留原地址。"""
    if not isinstance(value, str):
        raise ValueError("请输入商品 ID 或完整的淘宝/天猫详情链接")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("详情输入不能包含换行或控制字符")
    value = value.strip()
    if not value or len(value) > 8192:
        raise ValueError("请输入商品 ID 或完整的淘宝/天猫详情链接")
    if re.fullmatch(r"[0-9]+", value):
        if not _valid_id(value):
            raise ValueError("商品 ID 须为 1～25 位的正整数")
        return DetailTarget(value, DETAIL_BASE_URL + value, "id")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname in DETAIL_HOSTS
                 and parsed.path == "/item.htm" and parsed.username is None and parsed.password is None
                 and parsed.port in {None, 443 if parsed.scheme == "https" else 80})
        ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
    except ValueError:
        valid, ids = False, []
    if not valid or len(ids) != 1 or not _valid_id(ids[0]):
        raise ValueError("详情链接须为 item.taobao.com 或 detail.tmall.com 的 /item.htm 地址，且包含唯一有效 id")
    return DetailTarget(ids[0], value, "url")


def matches_detail_url(page, item_id):
    if page.is_closed():
        return False
    try:
        return resolve_detail_input(page.url).item_id == item_id
    except ValueError:
        return False


def inspect_detail_entry(page, target):
    """仅确认目标地址和文档已打开；不将入口就绪当作商品字段采集成功。"""
    if not matches_detail_url(page, target.item_id):
        return None
    try:
        value = page.evaluate("""() => ({ready:['interactive','complete'].includes(document.readyState),
          body_present:!!document.body&&document.body.childElementCount>0,title:document.title})""")
    except PlaywrightError:
        return None
    if value["ready"] and value["body_present"]:
        return DetailPage(page, target, value["title"])
    return None


def open_detail(page, value, timeout_ms):
    """在调用方提供的页面打开一个商品，可供独立入口及后续循环复用。"""
    target = resolve_detail_input(value)
    guard = active_guard()
    if guard is None:
        raise RuntimeError("详情页必须在淘宝专用会话守卫中打开")
    guard.checkpoint("打开商品详情前检查")
    guard.pause_between("page")
    guard.resume_check = lambda: inspect_detail_entry(page, target) is not None
    guard.perform("打开商品详情页", lambda: page.goto(target.url, wait_until="domcontentloaded", timeout=timeout_ms),
                  completed=lambda: inspect_detail_entry(page, target) is not None)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        paused = guard.paused_seconds
        guard.checkpoint("确认商品详情页入口")
        deadline += guard.paused_seconds - paused
        result = inspect_detail_entry(page, target)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            guard.request_attention(page, "尚未确认输入商品的详情页文档，请核对页面及商品 ID 后回复继续；不会自动重新打开", step="检查详情页入口")
            deadline = time.monotonic() + timeout_ms / 1000
        paused = guard.paused_seconds
        guard.wait(250)
        deadline += guard.paused_seconds - paused


def keep_detail_open(result):
    guard = active_guard()
    print(f"商品 {result.target.item_id} 的详情页保持打开，等待下一步采集要求。关闭该标签页结束任务。", flush=True)
    while not result.page.is_closed():
        try:
            guard.wait(300)
        except TaskStopped:
            if result.page.is_closed() and not guard.awaiting_manual:
                return
            raise
