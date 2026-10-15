"""千牛弹窗规则、现场记录与人工处理；不重放业务操作。"""
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from playwright.sync_api import Page, Frame, Error as PlaywrightError, expect
from ecommerce.shared.exceptions import NeedsAttention


def _location(url: str) -> str:
    """去掉查询参数和片段；参数：原地址；返回：日志地址。"""
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else f"{parsed.scheme}:{parsed.path}"


def _dismiss_known_sku(frame: Page | Frame) -> bool:
    """仅关闭已确认的SKU引导；参数：页面或框架；返回：是否关闭，未知窗口不作处理。"""
    notice = frame.get_by_role("dialog").filter(has=frame.locator('img[alt="SKU管理页引导"]:visible'))
    if notice.count() > 1:
        raise NeedsAttention("匹配到多个SKU管理页引导公告，请人工检查")
    if notice.count() == 0 or not notice.is_visible():
        return False
    close = notice.get_by_role("button", name="关闭", exact=True)
    expect(close).to_have_count(1, timeout=3000)
    close.click(timeout=3000)
    expect(notice).to_be_hidden(timeout=3000)
    return True


def _frame_visible(frame: Frame) -> bool:
    """检查嵌入框架的整条可见链；参数：框架；返回：祖先iframe均可见。"""
    current = frame
    while current.parent_frame is not None:
        if not current.frame_element().is_visible():
            return False
        current = current.parent_frame
    return True


def _business_rules(operation: str) -> dict:
    """按操作限定业务窗口；参数：模块.函数；返回：允许的结构特征，不按宽泛标题放行。"""
    module = operation.split(".")[0]
    rules = {"selectors": [], "frame_sources": [], "crop": False}
    if module in {"product_images", "product_details", "product_sku_media"}:
        rules["selectors"].append('iframe[src*="sucai-selector-ng"]')
        rules["frame_sources"].append("sucai-selector-ng")
        rules["crop"] = module == "product_details"
    if module == "product_sku_media":
        rules["selectors"].extend([".sku-component-sku-detail-drawer", ".batch-fill-sku-image-dialog"])
    if module == "product_video":
        rules["selectors"].append('iframe[src*="videoSelector.htm"]')
        rules["frame_sources"].append("videoSelector.htm")
    if operation == "product_sku.create_sales_specifications":
        rules["selectors"].append(".sku-decouple-drawer")
    return rules


# 使用可见的对话框和遮罩子元素，不能用可能为零尺寸的overlay外壳判断是否遮挡。
_SCAN = """rules => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'};
 const dialogs='[role="dialog"],[role="alertdialog"],dialog[open],.next-dialog,.ant-modal,.el-dialog,.sku-decouple-drawer';
 const masks='.next-overlay-backdrop,.next-overlay-mask,.ant-modal-mask,.el-overlay,.modal-backdrop';
 const matches=(e,sel)=>(e.matches(sel)&&visible(e))||Array.from(e.querySelectorAll(sel)).some(visible);
 const allowed=e=>rules.selectors.some(s=>matches(e,s)) ||
   (rules.crop&&Array.from(e.querySelectorAll('button')).some(b=>visible(b)&&b.innerText.trim()==='确认全部分割')) ||
   (rules.in_picker&&(!!e.querySelector('input[type="file"],[class*="PicList_pic_background"],[class*="VideoList--videoItem--"]')||
     Array.from(e.querySelectorAll('button')).some(b=>/^(本地上传|裁剪|确认全部分割)$/.test(b.innerText.trim()))));
 const result=[];
 for(const e of document.querySelectorAll(dialogs)){
   if(!visible(e))continue;
   if(e.parentElement?.closest(dialogs))continue;
   const nested=Array.from(e.querySelectorAll(dialogs)).filter(visible);
   const nodes=nested.length?[...nested,e]:[e];
   for(const node of nodes){
     if(allowed(node))continue;
     const known=Array.from(node.querySelectorAll('img[alt="SKU管理页引导"]')).some(visible);
     result.push({kind:known?'known':'unknown',title:node.getAttribute('aria-label')||node.querySelector('h1,h2,h3,.next-dialog-header')?.innerText||node.innerText.slice(0,220)||'无文字对话框',role:node.getAttribute('role'),classes:typeof node.className==='string'?node.className:''});
   }
 }
 for(const e of document.querySelectorAll(masks)){
   if(!visible(e)||getComputedStyle(e).pointerEvents==='none')continue;
   const shell=e.closest('.next-overlay-wrapper,.ant-modal-root,.el-overlay,.modal')||e.parentElement;
   if(shell&&Array.from(shell.querySelectorAll(dialogs)).some(visible))continue;
   if(shell&&allowed(shell))continue;
   result.push({kind:'unknown',title:'未识别的遮罩层',role:'mask',classes:e.className});
 }
 return result;
}"""


@dataclass(frozen=True)
class Popup:
    """待处理现场：页面、框架、类型及不含输入框值的摘要。"""
    page: Page
    frame: Frame | None
    kind: str
    details: dict


class PopupGuard:
    """由验证守卫统一调度，覆盖DOM弹窗、新标签页及原生确认框。"""
    NAVIGATION_OPERATIONS = {
        "product.click_product_nav", "product.click_publish_product", "product.select_brand_and_continue",
        "product_submit.submit_product_to_warehouse", "login.check_login", "login.login_with_qrcode",
    }
    NAVIGATION_HOSTS = {"myseller.taobao.com", "item.upload.taobao.com", "upload.taobao.com", "loginmyseller.taobao.com", "login.taobao.com"}

    def __init__(self, context, on_attention, log_dir: Path):
        """初始化；参数：任务上下文、人工回调、日志目录；返回：守卫。"""
        self.context = context
        self.on_attention = on_attention
        self.log_dir = Path(log_dir)
        self.allowed_pages = set(context.pages)
        self.pending_pages = {}
        self.listeners = {}
        self.handled_count = 0
        self.feedback_count = 0
        self.native_handling = False

    def attach(self, page, operation, fail):
        """登记新页和原生事件；参数：页面、当前操作获取函数、失败回调；返回：无。"""
        if page not in self.allowed_pages:
            self.pending_pages[page] = operation() in self.NAVIGATION_OPERATIONS
        def native(dialog):
            self.native_handling = True
            try:
                self._handle_native(page, dialog, operation())
            except Exception as error:
                fail(error)
            finally:
                self.native_handling = False
        page.on("dialog", native)
        self.listeners[page] = native

    def _scan(self, operation):
        """扫描实际可见的结构；参数：当前操作；返回：候选，优先未知弹窗。"""
        found = []
        rules = _business_rules(operation)
        for page in self.context.pages:
            if page.is_closed():
                continue
            if page in self.pending_pages:
                expected = self.pending_pages[page]
                if expected and urlsplit(page.url).hostname in self.NAVIGATION_HOSTS:
                    self.allowed_pages.add(page)
                    del self.pending_pages[page]
                elif not (expected and page.url == "about:blank"):
                    found.append(Popup(page, None, "tab", {"title": "未预期的新标签页", "page": _location(page.url)}))
            for frame in page.frames:
                try:
                    if not _frame_visible(frame):
                        continue
                    in_picker = any(source in frame.url for source in rules["frame_sources"])
                    if frame != page.main_frame and not in_picker:
                        src = frame.frame_element().get_attribute("src") or ""
                        in_picker = any(source in src for source in rules["frame_sources"])
                    for details in frame.evaluate(_SCAN, dict(rules, in_picker=in_picker)):
                        found.append(Popup(page, frame, details["kind"], details))
                except PlaywrightError as error:
                    if page.is_closed() or frame.is_detached() or "Execution context was destroyed" in str(error):
                        # 导航销毁旧文档时由下一次操作检查新文档，不在旧框架上反复扫描。
                        continue
                    raise
        return sorted(found, key=lambda item: item.kind == "known")

    def _record(self, popup, step, error=None):
        """保存一次现场；参数：候选、步骤、可选错误；返回：供人工查看的文件说明。"""
        folder = self.log_dir / "screenshots"
        stem = folder / f"popup-{uuid4().hex}"
        metadata = dict(popup.details, kind=popup.kind, step=step, page=_location(popup.page.url), frame=_location(popup.frame.url) if popup.frame else None)
        if error:
            metadata["error"] = re.sub(r"https?://[^\s\"'<>]+", lambda match: _location(match.group()), str(error))[:1200]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stem.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logging.getLogger(__name__).exception("弹窗现场记录无法写入；仍须等待人工处理")
            return "现场记录保存失败，请直接检查当前浏览器，并检查日志目录权限及磁盘空间。"
        note = f"现场记录：{stem.with_suffix('.json')}"
        if popup.kind != "native":
            try:
                popup.page.screenshot(path=str(stem.with_suffix('.png')), timeout=2000)
                note += f"\n截图：{stem.with_suffix('.png')}"
            except (PlaywrightError, OSError):
                logging.getLogger(__name__).warning("弹窗截图未保存，结构记录已保留")
        logging.getLogger(__name__).error("需要人工处理：步骤=%s；页面=%s；弹窗=%s；%s", step, metadata["page"], popup.details.get("title"), note)
        return note

    def checkpoint(self, operation, step):
        """等待遮挡消失；参数：操作及步骤；返回：无，不点击未知窗口、不重放业务。"""
        seen = set()
        auto_closed = 0
        while True:
            found = self._scan(operation)
            if not found:
                return
            popup = found[0]
            if popup.kind == "known" and auto_closed < 3:
                self.handled_count += 1
                try:
                    if _dismiss_known_sku(popup.frame):
                        auto_closed += 1
                        logging.getLogger(__name__).info("全局守卫已关闭SKU管理页引导公告：%s", step)
                        continue
                except (PlaywrightError, AssertionError, NeedsAttention):
                    logging.getLogger(__name__).warning("已知公告无法安全关闭，转人工检查")
            key = (id(popup.page), id(popup.frame), popup.kind, json.dumps(popup.details, ensure_ascii=False, sort_keys=True))
            first = key not in seen
            if first:
                # 必须在截图等Playwright调用前登记，原动作可能在截图期间已经到达时限。
                self.handled_count += 1
            note = self._record(popup, step) if first else "上次现场记录已保存"
            seen.add(key)
            prefix = "【需要人工处理弹窗】" if first else "【弹窗尚未处理】"
            instruction = "请检查并关闭这个未知标签页" if popup.kind == "tab" else "请手动检查并关闭弹窗，不要盲目确认业务操作"
            message = f"{prefix}\n当前步骤：{step}\n页面：{_location(popup.page.url)}\n提示：{popup.details.get('title')}\n{note}\n{instruction}，完成后输入“继续”；没有反馈将一直暂停。"
            if self.on_attention is None:
                raise NeedsAttention(message)
            self.on_attention(message)
            self.feedback_count += 1

    def _handle_native(self, page, dialog, operation):
        """等待原生弹窗的明确选择；参数：页面、原生弹窗、操作；返回：仅按人工选择接受或取消。"""
        popup = Popup(page, None, "native", {"title": dialog.message[:500], "dialog_type": dialog.type})
        self.handled_count += 1
        note = self._record(popup, operation)
        message = f"【需要人工决定原生弹窗】\n当前步骤：{operation}\n页面：{_location(page.url)}\n类型：{dialog.type}\n内容：{dialog.message[:500]}\n{note}\n输入“确认”接受此弹窗，输入“取消”拒绝；不会自动选择。"
        if self.on_attention is None:
            raise NeedsAttention(message)
        response = self.on_attention(message)
        self.feedback_count += 1
        if response == "确认":
            dialog.accept()
        elif response == "取消":
            dialog.dismiss()
        else:
            raise NeedsAttention("未收到原生弹窗的明确决定，已停止当前操作")

    def review_error(self, operation, step, error):
        """记录子步骤暂停；参数：操作、步骤、错误；返回：明确反馈后返回，由调用方核对恢复条件。"""
        pages = [page for page in self.context.pages if not page.is_closed()]
        if not pages:
            raise NeedsAttention("浏览器已关闭，无法恢复当前步骤；请核对之前的操作结果") from error
        popup = Popup(pages[-1], None, "interrupted", {"title": "页面操作中断，原因尚未确认"})
        note = self._record(popup, step, error)
        message = f"【需要人工处理弹窗】\n当前步骤：{step}\n当前子步骤已暂停，可能有尚未识别的遮挡或页面变化。\n{note}\n请手动关闭未知弹窗，完成后输入“继续”。程序将留在当前浏览器，重新核对当前子步骤并继续；没有反馈将一直暂停。"
        if self.on_attention is None:
            raise NeedsAttention(message) from error
        self.on_attention(message)
        self.feedback_count += 1

    def close(self):
        """清理事件监听；参数：无；返回：无。"""
        for page, listener in self.listeners.items():
            if not page.is_closed():
                page.remove_listener("dialog", listener)
        self.listeners.clear()
