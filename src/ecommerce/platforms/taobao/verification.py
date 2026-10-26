"""淘宝专用暂停守卫：只检测和交给人工，不自动处理安全验证。"""
from ecommerce.infrastructure.tasks.events import notify as print
import json
import math
import queue
import random
import re
import threading
import time
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.storage.database import now, update_task_progress
from ecommerce.infrastructure.tasks.runner import current_task
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped

_ACTIVE = ContextVar("taobao_verification_guard", default=None)
LOGIN_HOSTS = {"login.taobao.com", "www.taobao.com", "taobao.com"}

# 不扫描输入框值；短信登录中的“验证码”文字本身不属于阻断证据。
_SCAN = r"""loginMode => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'};
 const challenge='.J_MIDDLEWARE_FRAME_WIDGET,.nc-container .nc_scale,#nocaptcha,.baxia-dialog,.baxia-dialog-content,iframe[src*="captcha"],iframe[src*="punish"],iframe[src*="baxia"]';
 if([...document.querySelectorAll(challenge)].some(visible))return 'verification';
 if(!loginMode&&[...document.querySelectorAll('.site-nav-login a[href*="login.taobao.com"]')].some(visible))return 'login';
 const text=document.body?.innerText||'';
 if(/请拖动.{0,12}滑块|请完成安全验证|访问过于频繁|访问受限|操作过于频繁|请稍后再试.*验证/.test(text))return 'verification';
 const dialogs='[role="dialog"],[role="alertdialog"],dialog[open],.next-dialog,.ant-modal,.el-dialog';
 for(const e of document.querySelectorAll(dialogs)){
   if(!visible(e))continue;
   if(loginMode&&e.querySelector('#login,input[type="password"],iframe[src*="login.taobao.com"]'))continue;
   return 'popup';
 }
 const masks='.next-overlay-backdrop,.next-overlay-mask,.ant-modal-mask,.el-overlay,.modal-backdrop';
 for(const e of document.querySelectorAll(masks)){
   if(!visible(e)||getComputedStyle(e).pointerEvents==='none')continue;
   if(loginMode&&e.parentElement?.querySelector('#login,input[type="password"],iframe[src*="login.taobao.com"]'))continue;
   return 'popup';
 }
 return null;
}"""


def safe_location(url):
    """日志网址去掉账号、查询参数、片段，避免泄漏授权信息。"""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return parsed.scheme + ":" if parsed.scheme else "unknown"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"
    except ValueError:
        return "unknown"


def retry_after_seconds(value, *, at=None):
    """解析 HTTP 秒数或日期；无效值不猜测等待期限。"""
    value = (value or "").strip()
    if value.isdigit():
        try:
            seconds = float(value)
            return seconds if seconds < float("inf") else None
        except (ValueError, OverflowError):
            return None
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - (at or datetime.now(timezone.utc))).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass
class Blockage:
    kind: str
    page: object
    source: str = ""
    until: float = 0
    key: str = ""
    frame_location: str | None = None
    check_details: dict | None = None


class TaobaoGuard:
    """后续业务必须通过 checkpoint、perform 和 wait 执行，不在事件中重试业务。"""
    def __init__(self, context, log_dir, on_attention=None, *, timing=None):
        self.context = context
        self.log_dir = Path(log_dir)
        self.on_attention = on_attention
        self.timing = {**DEFAULT_TAOBAO_TIMING, **(timing or {})}
        self.state = "running"
        self.step = "淘宝登录"
        self.progress = {}
        self.paused_seconds = 0.0
        self.failure = None
        self.login_mode = True
        self.resume_check = None
        self.watched_responses = []
        self._pending_documents = {}
        self._deferred_network = []
        self._pages = set(context.pages)
        self._listeners = {}
        self._page_times = {}
        self._expected_rule = None
        self._expected_pages = {}
        self._pending = []
        self._feedback_queue = queue.Queue()
        self._feedback_thread = None
        self.awaiting_manual = False
        self._handling = False
        self._token = None
        self._task = current_task()
        self.task_id = self._task[1] if self._task else uuid4().hex
        self.control_path = self.log_dir.parent / "storage/task_controls" / f"{self.task_id}.json"
        self.control_state_path = self.control_path.with_suffix(".state.json")
        self._prompt_id = None
        self._prompt_allowed = set()
        self._prompt_message = ''
        self.feedback_file_only = False
        self._file_replies = {}
        self._stdin_unavailable = False
        self.event_path = self.log_dir / "taobao" / f"{self.task_id}.jsonl"

    def __enter__(self):
        self._token = _ACTIVE.set(self)
        self.context.on("page", self._attach)
        self.context.on("request", self._document_requested)
        self.context.on("response", self._response)
        self.context.on("requestfailed", self._request_failed)
        for page in self.context.pages:
            self._attach(page)
        self._record("started")
        return self

    def __exit__(self, *exc):
        self.context.remove_listener("page", self._attach)
        self.context.remove_listener("request", self._document_requested)
        self.context.remove_listener("response", self._response)
        self.context.remove_listener("requestfailed", self._request_failed)
        for page, listener in self._listeners.items():
            page.remove_listener("dialog", listener)
        self._listeners.clear()
        if self._token is not None:
            _ACTIVE.reset(self._token)
        self.control_state_path.unlink(missing_ok=True)
        if self.state != "stopped":
            self._record("session_ended")

    def _record(self, event, blockage=None, **details):
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"time": now(), "task_id": self.task_id, "event": event, "state": self.state,
                  "step": self.step, "progress": self.progress, **details}
        if blockage:
            record.update(kind=blockage.kind, location=safe_location(blockage.page.url), source=blockage.source)
            if blockage.check_details is not None:
                record['check_details']=blockage.check_details
            if blockage.frame_location is not None:
                record["frame_location"] = blockage.frame_location
        from ecommerce.infrastructure.tasks.events import sanitize
        record=sanitize(record)
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        from ecommerce.infrastructure.tasks.events import emit_event
        emit_event(event,state=self.state,step=self.step,details=record)

    def _transition(self, state, blockage=None):
        self.state = state
        if state in {"needs_attention", "resuming"}:
            self.awaiting_manual = True
        elif state == "running":
            self.awaiting_manual = False
        self._record("state_changed", blockage)
        if self._task and state in {"running", "needs_attention", "resuming"}:
            status = "running" if state == "running" else "needs_attention"
            update_task_progress(*self._task, status, f"淘宝：{self.step}；" + ("等待人工处理" if status == "needs_attention" else "继续执行"))

    def update_progress(self, *, keyword=None, page_number=None, collected=None, target=None):
        """只记录明确的业务进度字段，不记录页面输入框或登录凭证。"""
        for key, value in {"keyword": keyword, "page_number": page_number, "collected": collected, "target": target}.items():
            if value is not None:
                self.progress[key] = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", str(value)) if key == "keyword" else int(value)

    def _attach(self, page):
        if page in self._listeners:
            return
        listener = lambda dialog: self._native(page, dialog)
        page.on("dialog", listener)
        self._listeners[page] = listener
        self._page_times[page] = time.monotonic()
        if self._expected_rule is not None and page not in self._pages and not self._expected_rule["claimed"]:
            self._expected_rule["claimed"] = True
            self._expected_pages[page] = self._expected_rule
        elif not self.login_mode and page not in self._pages:
            self._queue(Blockage("tab", page, "未预期的新标签页，请人工检查并关闭", key="tab:" + uuid4().hex))
        self._flush_network()

    @contextmanager
    def expect_new_page(self, predicate, timeout_ms=60_000):
        """仅登记下一个预期新页，必须核对目标地址；不宽泛放行同域名页面。"""
        if self._expected_rule is not None:
            raise ValueError("不能嵌套登记预期新标签页")
        rule = {"matches": predicate, "started": time.monotonic(), "paused": self.paused_seconds,
                "timeout": timeout_ms / 1000, "claimed": False}
        self._expected_rule = rule
        try:
            yield
        finally:
            self._expected_rule = None
            for page, registered in list(self._expected_pages.items()):
                if registered is rule:
                    del self._expected_pages[page]
                    if not page.is_closed() and page not in self._pages:
                        self._queue(Blockage("tab", page, "预期页面未通过搜索条件核对", key="tab:" + uuid4().hex))

    def watch_responses(self, predicate):
        """由后续搜索适配登记商品请求规则；只监听，不额外请求或改写响应。"""
        self.watched_responses.append(predicate)

    def _document_requested(self, request):
        """被动记录实际主页面导航；不记录请求参数、Cookie 或正文。"""
        host = urlsplit(request.url).hostname or ""
        if not (host == "taobao.com" or host.endswith(".taobao.com") or host == "tmall.com" or host.endswith(".tmall.com")):
            return
        if request.resource_type == "document" and request.is_navigation_request():
            self._pending_documents[request] = now()
            self._flush_document(request)

    def _flush_document(self, request):
        """新标签页首个请求可能早于框架创建，在响应/失败事件里补核对来源。"""
        if request not in self._pending_documents:
            return
        try:
            frame = request.frame
            requested_at = self._pending_documents.pop(request)
            if frame == frame.page.main_frame:
                self._record("document_requested", location=safe_location(request.url), method=request.method, requested_at=requested_at)
        except PlaywrightError:
            return

    def _relevant(self, request):
        host = urlsplit(request.url).hostname or ""
        if not (host == "taobao.com" or host.endswith(".taobao.com") or host == "tmall.com" or host.endswith(".tmall.com")):
            return False
        if request.is_navigation_request() and request.frame == request.frame.page.main_frame:
            return True
        return any(predicate(request) for predicate in self.watched_responses)

    def _response(self, response):
        self._flush_document(response.request)
        if response.status not in {401, 403, 429, 503}:
            return
        try:
            self._response_signal(response)
        except PlaywrightError:
            if response.request.is_navigation_request():
                self._deferred_network.append(("response", response))

    def _response_signal(self, response):
        if not self._relevant(response.request):
            return
        page = response.request.frame.page
        delay = retry_after_seconds(response.headers.get("retry-after")) or 0
        self._queue(Blockage("network", page, f"HTTP {response.status}；{safe_location(response.url)}", time.monotonic() + delay,
                             f"http:{response.status}:{safe_location(response.url)}"))

    def _request_failed(self, request):
        self._flush_document(request)
        try:
            self._failed_signal(request)
        except PlaywrightError:
            if request.is_navigation_request():
                self._deferred_network.append(("failed", request))

    def _failed_signal(self, request):
        if self._relevant(request):
            self._queue(Blockage("network", request.frame.page, "页面或已登记商品请求加载失败", key="request_failed:" + safe_location(request.url)))

    def _flush_network(self):
        for request in list(self._pending_documents):
            self._flush_document(request)
        for kind, item in list(self._deferred_network):
            try:
                self._response_signal(item) if kind == "response" else self._failed_signal(item)
            except PlaywrightError:
                continue
            self._deferred_network.remove((kind, item))

    def _queue(self, blockage):
        existing = next((item for item in self._pending if item.key == blockage.key), None)
        if existing:
            existing.until = max(existing.until, blockage.until)
        else:
            self._pending.append(blockage)

    def _scan(self):
        if self.failure:
            raise self.failure
        pages = [page for page in self.context.pages if not page.is_closed()]
        if not pages:
            raise TaskStopped("淘宝浏览器已关闭，未继续后续操作")
        self._check_control(pages[-1])
        self._flush_network()
        if self._pending:
            return self._pending[0]
        for page in pages:
            if page in self._expected_pages:
                rule = self._expected_pages[page]
                if rule["matches"](page):
                    self._pages.add(page)
                    del self._expected_pages[page]
                elif urlsplit(page.url).hostname == "login.taobao.com":
                    return Blockage("login", page, "搜索页面需要登录，请人工处理后返回本次搜索结果")
                elif page.url == "about:blank" and time.monotonic() - rule["started"] - (self.paused_seconds - rule["paused"]) < rule["timeout"]:
                    continue
                else:
                    return Blockage("tab", page, "新页面与本次搜索条件不一致，请人工检查")
            if page not in self._pages:
                if self.login_mode and urlsplit(page.url).hostname in LOGIN_HOSTS:
                    self._pages.add(page)
                elif self.login_mode and page.url == "about:blank" and time.monotonic() - self._page_times.get(page, 0) < 2:
                    continue  # 登录跳转的新页短暂为空，下一次检查再确认来源。
                else:
                    return Blockage("tab", page, "未预期的新标签页，请人工检查并关闭")
            if not self.login_mode and urlsplit(page.url).hostname == "login.taobao.com":
                return Blockage("login", page, "登录状态失效，请人工完成登录")
            for frame in page.frames:
                try:
                    current = frame
                    visible = True
                    while current.parent_frame is not None:
                        if not current.frame_element().is_visible():
                            visible = False
                            break
                        current = current.parent_frame
                    if not visible:
                        continue
                    kind = frame.evaluate(_SCAN, self.login_mode)
                    if kind:
                        label = {"verification": "检测到安全验证", "login": "登录状态失效，请人工登录", "popup": "未识别的弹窗或遮罩"}[kind]
                        return Blockage(kind, page, label, frame_location=safe_location(frame.url))
                except PlaywrightError:
                    if page.is_closed() or frame.is_detached():
                        continue
                    return Blockage("inspection", page, "页面暂时无法检查，请确认页面加载和验证状态")
        if self.failure:
            raise self.failure
        return self._pending[0] if self._pending else None

    def _capture(self, blockage, *, native=False):
        name = f"taobao-{self.task_id}-{uuid4().hex}.png"
        screenshot = self.log_dir / "screenshots" / name
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        error_type = None
        if not native:
            try:
                blockage.page.screenshot(path=str(screenshot), timeout=2000)
                self._record("blocked", blockage, screenshot=str(screenshot))
                return str(screenshot)
            except PlaywrightError as error:
                error_type = type(error).__name__
        self._record("blocked", blockage, screenshot=None, screenshot_note="原生弹窗或页面状态导致无法截图", screenshot_error_type=error_type)
        return "未能截图，已记录事件"

    def _reply(self, message, allowed):
        if self.on_attention is None:
            raise NeedsAttention(message)
        while True:
            reply = self.on_attention(message)
            if reply == "停止":
                raise TaskStopped("用户已停止淘宝任务，未继续后续操作")
            if reply in allowed:
                return reply
            # 空回车、回调未返回明确指令都不能放行；后续不重复响铃。
            title = ("【原生弹窗尚未决定】" if allowed == {"确认", "取消"} else
                     "【检查尚未通过】" if message.startswith(("【需要人工检查】", "【检查尚未通过】")) else "【验证尚未通过】")
            message = title + "\n未收到有效指令，保持暂停。"

    def _check_control(self, page):
        if not self.control_path.exists():
            return
        try:
            request = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise NeedsAttention("任务控制文件无法读取，已暂停后续操作") from error
        self.control_path.unlink()
        if request.get("action") == "stop":
            self._transition("stopped")
            raise TaskStopped("用户已停止淘宝任务，未继续后续操作")
        if request.get("action") == "pause":
            self._queue(Blockage("manual", page, "用户主动暂停；确认页面后回复继续，或输入停止", key="manual_pause"))
        reply = {"continue": "继续", "confirm": "确认", "cancel": "取消"}.get(request.get("action"))
        if reply is not None:
            if request.get("prompt_id") == self._prompt_id and self._prompt_id and reply in self._prompt_allowed:
                self._file_replies[self._prompt_id] = reply
            else:
                self._record("stale_feedback_ignored")

    def _publish_prompt(self):
        self.control_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.control_state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"task_id": self.task_id, "state": self.state, "step": self.step,
                                           "prompt_id": self._prompt_id, "allowed": sorted(self._prompt_allowed),
                                           "message": self._prompt_message}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.control_state_path)

    def read_feedback(self, prompt, allowed=None):
        """终端和跨进程反馈共用提示编号；旧指令不能放行新的验证。"""
        from ecommerce.infrastructure.tasks.events import sanitize
        previous = self._prompt_id, self._prompt_allowed, self._prompt_message
        self._prompt_id, self._prompt_allowed = uuid4().hex, set(allowed or {"继续"})
        self._prompt_message = sanitize(prompt)
        self._publish_prompt()
        try:
            return self._read_feedback(prompt)
        finally:
            self._file_replies.pop(self._prompt_id, None)
            self._prompt_id, self._prompt_allowed, self._prompt_message = previous
            self._publish_prompt()

    def _read_feedback(self, prompt):
        """仅输入线程读终端；主线程继续派发浏览器事件及检查停止指令。"""
        requested_at = time.monotonic()
        if self._feedback_thread is None and not self.feedback_file_only:
            def read_lines():
                while True:
                    try:
                        line = input()
                        self._feedback_queue.put((time.monotonic(), line))
                    except (EOFError, OSError):
                        self._feedback_queue.put(None)
                        return
            self._feedback_thread = threading.Thread(target=read_lines, daemon=True)
            self._feedback_thread.start()
        print(prompt, end="", flush=True)
        while True:
            if self.failure:
                raise self.failure
            pages = [page for page in self.context.pages if not page.is_closed()]
            if not pages:
                raise TaskStopped("人工等待期间淘宝浏览器已关闭，任务已停止")
            from ecommerce.infrastructure.tasks.events import storage_checkpoint
            storage_checkpoint(self.step,lambda:pages[-1].wait_for_timeout(100))
            self._check_control(pages[-1])
            if self._prompt_id in self._file_replies:
                return self._file_replies.pop(self._prompt_id)
            try:
                value = self._feedback_queue.get_nowait()
                if value is None:
                    if not self._stdin_unavailable:
                        self._stdin_unavailable = True
                        self._record("terminal_input_unavailable")
                        print("终端输入已关闭，浏览器保持暂停。可在聊天中回复“继续”，或在另一个终端执行 taobao continue；也可执行 taobao stop 结束。", flush=True)
                    continue
                if isinstance(value, tuple):
                    received_at, value = value
                    if received_at < requested_at:
                        print("已忽略上一次提示前的旧反馈，请针对当前提示重新输入。", flush=True)
                        continue
                return value.strip()
            except queue.Empty:
                try:
                    pages[-1].wait_for_timeout(100)
                except PlaywrightError:
                    if not pages[-1].is_closed():
                        raise

    def _native(self, page, dialog):
        blockage = Blockage("native", page, f"浏览器原生弹窗：{dialog.type}", key=uuid4().hex)
        started = time.monotonic()
        try:
            self._transition("needs_attention", blockage)
            self._capture(blockage, native=True)
            answer = self._reply("【需要人工决定原生弹窗】\n当前步骤：" + self.step +
                                 "\n弹窗内容：" + dialog.message + "\n请明确选择确认或取消；不自动作出决定。", {"确认", "取消"})
            dialog.accept() if answer == "确认" else dialog.dismiss()
            self._queue(Blockage("native_done", page, "原生弹窗已由人工决定，请回复继续后恢复后续操作", key=blockage.key))
        except (NeedsAttention, TaskStopped, KeyboardInterrupt) as error:
            self.failure = error
            if isinstance(error, (TaskStopped, KeyboardInterrupt)):
                self._transition("stopped", blockage)
                try:
                    dialog.dismiss()
                except PlaywrightError:
                    pass
        except PlaywrightError:
            self.failure = NeedsAttention("原生弹窗已变化，未能确认处理结果")
        finally:
            self.paused_seconds += time.monotonic() - started

    def checkpoint(self, step=None):
        """反馈与页面复核同时通过才放行；不重试、不重放业务。"""
        if step:
            self.step = step
        if self.failure:
            raise self.failure
        if self._handling:
            return
        self._handling = True
        first = True
        captured = set()
        started = None
        paused_before = self.paused_seconds
        try:
            from ecommerce.infrastructure.tasks.events import storage_checkpoint
            def pump_storage():
                pages=[page for page in self.context.pages if not page.is_closed()]
                if not pages:
                    raise TaskStopped('等待数据库恢复期间浏览器已关闭，任务停止')
                pages[-1].wait_for_timeout(100)
            storage_checkpoint(self.step,pump_storage)
            blockage = self._scan()
            if blockage is not None and blockage.kind == "native_done":
                first = False  # 原生弹窗已经提示过，本次只等待继续，不重复响铃。
            while blockage:
                if started is None:
                    started = time.monotonic()
                self._transition("needs_attention", blockage)
                incident = (blockage.kind, blockage.page, blockage.source)
                evidence = self._capture(blockage) if incident not in captured and blockage.kind != "native_done" else str(self.event_path)
                captured.add(incident)
                if blockage.kind == "business":
                    title = "【需要人工检查】" if first else "【检查尚未通过】"
                else:
                    title = "【需要人工验证】" if first else "【验证尚未通过】"
                remaining = max(0, blockage.until - time.monotonic())
                message = (f"{title}\n当前步骤：{self.step}\n原因：{blockage.source}\n记录：{evidence}\n"
                           + (f"服务器要求至少再等待 {math.ceil(remaining)} 秒。\n" if remaining else "")
                           + "请在当前淘宝浏览器中处理；处理完成后回复“继续”。没有反馈一直暂停。")
                self._reply(message, {"继续"})
                first = False
                self._transition("resuming", blockage)
                if self.failure:
                    raise self.failure
                if blockage in self._pending and time.monotonic() >= blockage.until:
                    self._pending.remove(blockage)
                # 先派发暂停期间的导航、网络及新标签页事件，再复查所有页面。
                pages = [page for page in self.context.pages if not page.is_closed()]
                if pages:
                    pages[-1].wait_for_timeout(1)
                if self.failure:
                    raise self.failure
                blockage = self._scan()
                if blockage is None and self.resume_check is not None:
                    try:
                        resumed = self.resume_check()
                    except PlaywrightError:
                        resumed = False
                    if isinstance(resumed,Blockage):
                        blockage=resumed
                    elif not resumed:
                        if incident[0]=='business':
                            blockage=Blockage('business',incident[1],incident[2])
                        else:
                            blockage = Blockage("resume_check", pages[-1], "登录状态或当前业务页面未通过复核")
            if not first:
                self._transition("running")
        except (TaskStopped, KeyboardInterrupt):
            self._transition("stopped")
            raise
        except NeedsAttention as error:
            self.failure = error
            raise
        finally:
            if started is not None:
                self.paused_seconds += max(0, time.monotonic() - started - (self.paused_seconds - paused_before))
            self._handling = False

    def wait(self, milliseconds):
        """可中断的分段等待；人工处理时间不消耗业务等待预算。"""
        remaining = milliseconds / 1000
        self.checkpoint()
        while remaining > 0:
            pages = [page for page in self.context.pages if not page.is_closed()]
            if not pages:
                raise TaskStopped("淘宝浏览器已关闭，未继续后续操作")
            before, paused = time.monotonic(), self.paused_seconds
            try:
                pages[-1].wait_for_timeout(min(250, remaining * 1000))
            except PlaywrightError:
                if not pages[-1].is_closed():
                    raise
            remaining -= max(0, time.monotonic() - before - (self.paused_seconds - paused))
            self.checkpoint()

    def pause_between(self, action):
        """按动作读取间隔，提交前和结果加载后的等待分别配置。"""
        low, high = self.timing[action]
        seconds = random.uniform(low, high)
        self._record("paced", action=action, seconds=round(seconds, 3))
        self.wait(seconds * 1000)

    def request_attention(self, page, reason, *, step=None,check_details=None):
        """页面结构或业务结果无法确认时交给人工，不自动刷新或重放。"""
        self._queue(Blockage("business", page, reason, key="business:" + uuid4().hex,check_details=check_details))
        self.checkpoint(step)

    def perform(self, step, action, *, completed=None):
        """单个业务动作的前后屏障；动作失败后复核阻断，但绝不自动重放。"""
        def confirmed():
            try:
                return completed is not None and bool(completed())
            except PlaywrightError:
                return False
        self.checkpoint(step)
        self._record("operation_started")
        try:
            result = action()
        except PlaywrightError as error:
            self._record("operation_failed", error_type=type(error).__name__)
            self.checkpoint(step)
            if confirmed():
                self._record("operation_verified_after_error")
                return None
            pages = [page for page in self.context.pages if not page.is_closed()]
            if not pages:
                raise TaskStopped("操作期间淘宝浏览器已关闭") from None
            self._queue(Blockage("operation", pages[-1], "当前操作超时或中断；不会重放，请人工确认页面结果", key="operation_error"))
            self.checkpoint(step)
            if confirmed():
                return None
            self.failure = NeedsAttention("当前操作结果仍未确认，已阻止后续步骤，请关闭浏览器后重新启动")
            self._transition("needs_attention")
            raise self.failure from None
        self._record("operation_finished")
        self.checkpoint(step)
        return result

    def hold_without_input(self, *, reason=None):
        """中断后保留现场及真实原因，只派发浏览器事件，不恢复已退出的业务。"""
        self._transition("needs_attention")
        if reason:
            print("任务已中断，原因："+reason+"；当前保留浏览器现场，请关闭窗口结束任务。", flush=True)
            self._record("session_halted", reason=reason)
        else:
            print("人工输入不可用，任务保持暂停。请关闭淘宝浏览器结束任务，之后重新启动。", flush=True)
            self._record("input_unavailable")
        while True:
            pages = [page for page in self.context.pages if not page.is_closed()]
            if not pages:
                return
            self._check_control(pages[-1])
            try:
                pages[-1].wait_for_timeout(250)
            except PlaywrightError:
                if not pages[-1].is_closed():
                    return


def active_guard():
    return _ACTIVE.get()


def checkpoint(step=None):
    """兼容登录轮询，返回本次人工等待秒数，用于延长页面检查预算。"""
    guard = active_guard()
    if guard is None:
        return 0.0
    before = guard.paused_seconds
    guard.checkpoint(step)
    return guard.paused_seconds - before


def wait(page, milliseconds):
    guard = active_guard()
    if guard is None:
        page.wait_for_timeout(milliseconds)
        return 0.0
    before = guard.paused_seconds
    guard.wait(milliseconds)
    return guard.paused_seconds - before
