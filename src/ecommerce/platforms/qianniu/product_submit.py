"""商品提交与页面结果核对；每次调用只点击一次，不自动重放提交。"""
import re
import time
from dataclasses import dataclass
from logging import getLogger
from playwright.sync_api import Page, Error as PlaywrightError, expect
from .verification import checkpoint, qianniu_operation

EDITOR = "#sell-field-title"
WAREHOUSE_GROUP = "#sell-field-startTime"
ERRORS = ".sell-component-info-wrapper-msg-content.error:visible, .sell-o-message.error:visible, .sell-o-message[class*='errorMessage-']:visible"
SUCCESS = re.compile(r"^(?:商品|宝贝)?(?:发布成功|提交成功|成功放入仓库|已放入仓库)(?:[，,。！!\s].*)?$")
SUCCESS_ISSUES = re.compile(r"创建失败|提交失败|发布失败|流量受限|存在异常")
DETAIL_INSTRUCTION = re.compile(r"^宝贝详情图【高/宽(?:≤|<=)2】[，,]超出将被裁剪[。.]建议宽度(?:≥|>=)1440像素以确保清晰[。.]?$")
ALERT_ERRORS = re.compile(r"失败|错误|不能为空|未通过|不符合|不能超过|不得超过|超出.*(?:限制|上限|范围|最大)|超过.*(?:限制|上限|最大)")
WAIT_MS = 30000


@dataclass(frozen=True)
class SubmissionResult:
    """保留明确成功、明确失败或未确认状态，以及人工检查所需的页面。"""
    status: str
    message: str
    page: Page
    issues: tuple[str, ...] = ()
    item_id: str | None = None
    submit_attempted: bool = True


def _read_validation_error(page: Page) -> str | None:
    """读取真实字段或提交错误；参数：当前页；返回：去除已知说明文字的错误详情，无错误返回None。"""
    def is_instruction(text: str) -> bool:
        return bool(DETAIL_INSTRUCTION.fullmatch(re.sub(r"\s+", "", text)))

    errors = page.locator(ERRORS).evaluate_all("""els => els.map(e => {
        const group=e.closest('.sell-component-info-wrapper-wrap');
        const label=group?.querySelector('.sell-component-info-wrapper-label');
        const name=label?.getAttribute('title') || label?.innerText || '';
        return {name:name.trim(), message:e.innerText.trim()};
    }).filter(e => e.message)""")
    texts = [(f"{error['name']}：" if error['name'] else "") + error['message']
             for error in errors if not is_instruction(error['message'])]
    # role=alert只表示提示需要被读出，提示框也会包含裁剪说明，不能直接当作错误。
    alerts = page.get_by_role("alert")
    for index in range(alerts.count()):
        alert = alerts.nth(index)
        if alert.is_visible():
            text = alert.inner_text().strip()
            if not is_instruction(text) and ALERT_ERRORS.search(text):
                texts.append(text)
    return "；".join(dict.fromkeys(texts)) if texts else None


def _read_result(page: Page) -> SubmissionResult | None:
    """读取可见结果；参数：候选页面；返回：明确结果，尚无结果时返回None。"""
    if page.is_closed():
        return None
    # 素材上传也有“发布成功”，只有商品编辑区已离开才接受成功提示。
    if page.locator(EDITOR).count() == 0:
        messages = page.get_by_text(SUCCESS)
        for index in range(messages.count()):
            message = messages.nth(index)
            if message.is_visible():
                body = page.locator("body").inner_text()
                item = re.search(r"商品\s*ID\s*[：:]\s*(\d+)", body, re.IGNORECASE)
                issues = page.get_by_text(SUCCESS_ISSUES)
                visible_issues = tuple(dict.fromkeys(
                    issues.nth(i).inner_text().strip()
                    for i in range(issues.count()) if issues.nth(i).is_visible()
                ))
                return SubmissionResult("succeeded", message.inner_text().strip(), page,
                                        visible_issues, item.group(1) if item else None)
    error = _read_validation_error(page)
    if error:
        return SubmissionResult("failed", error, page)
    return None


def _wait_result(page: Page, baseline_pages: tuple[Page, ...], wait_ms: int) -> SubmissionResult:
    """等待返回页或字段错误；参数：原页面、提交前页面、等待时长；返回：核对结果，不以跳转判定成功。"""
    deadline = time.monotonic() + wait_ms / 1000
    context = page.context
    while True:
        checkpoint("等待商品提交结果")
        candidates = [current for current in context.pages if current == page or current not in baseline_pages]
        for current in reversed(candidates):
            try:
                result = _read_result(current)
                if result is not None:
                    return result
            except PlaywrightError:
                # 导航过程中上下文短暂不可用，继续只读核对，不重复点击。
                if time.monotonic() >= deadline:
                    break
        if time.monotonic() >= deadline:
            current = candidates[-1] if candidates else page
            return SubmissionResult("unknown", "未能确认商品提交结果，请检查当前页面及仓库中的宝贝；程序未重复提交", current)
        if not candidates:
            return SubmissionResult("unknown", "提交页面已关闭且没有可核对的结果页，请人工检查仓库中的宝贝", page)
        candidates[-1].wait_for_timeout(250)


@qianniu_operation
def inspect_submission_result(page: Page, baseline_pages: tuple[Page, ...], wait_ms: int = 3000) -> SubmissionResult:
    """人工反馈后只读复核；参数：原页面、提交前页面、等待时长；返回：结果，不点击提交。"""
    return _wait_result(page, baseline_pages, wait_ms)


@qianniu_operation
def submit_product_to_warehouse(page: Page, wait_ms: int = WAIT_MS) -> SubmissionResult:
    """提交宝贝信息；参数：商品页、结果等待时长；返回：成功、失败或未确认，只点击一次。"""
    group = page.locator(WAREHOUSE_GROUP)
    warehouse = group.get_by_role("radio", name="放入仓库", exact=True)
    if warehouse.count() != 1 or not warehouse.is_checked():
        return SubmissionResult("failed", "上架时间尚未选中“放入仓库”，提交尚未执行", page, submit_attempted=False)
    if group.get_by_role("radio").evaluate_all("els => els.filter(e => e.checked).length") != 1:
        return SubmissionResult("failed", "上架时间选中状态不唯一，提交尚未执行", page, submit_attempted=False)
    error = _read_validation_error(page)
    if error:
        return SubmissionResult("failed", error, page, submit_attempted=False)
    baseline_pages = tuple(page.context.pages)
    button = page.get_by_role("button", name="提交宝贝信息", exact=True)
    try:
        expect(button).to_have_count(1, timeout=WAIT_MS)
    except (PlaywrightError, AssertionError) as error:
        return SubmissionResult("unknown", f"提交按钮尚未确认，未点击提交：{error}", page, submit_attempted=False)
    getLogger(__name__).info("提交前检查通过，点击一次“提交宝贝信息”；等待平台结果")
    try:
        button.click(timeout=WAIT_MS)
    except (PlaywrightError, AssertionError) as error:
        return SubmissionResult("unknown", f"提交操作未确认：{error}；请核对页面结果，程序未重放提交", page)
    return _wait_result(page, baseline_pages, wait_ms)
