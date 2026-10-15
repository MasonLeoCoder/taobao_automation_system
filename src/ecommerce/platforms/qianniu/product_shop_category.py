"""千牛店铺分类多选：匹配项勾选，缺失或有歧义项记录并跳过。"""
from collections.abc import Sequence
from dataclasses import dataclass
from logging import getLogger
from playwright.sync_api import Page
from .verification import qianniu_operation, checkpoint, expect, page_step

SHOP_CATEGORY = "#sell-field-shopcat"
CATEGORY_TREE = 'ul.sell-shop-category-tree-select[role="tree"]:visible'
WAIT_MS = 30000
MAX_CATEGORIES = 20


@dataclass(frozen=True)
class ShopCategoryResult:
    """本次请求结果；selected包含已勾选及本次新增，skipped记录名称与原因。"""
    selected: list[str]
    skipped: dict[str, str]


@qianniu_operation
def select_shop_categories(page: Page, names: Sequence[str]) -> ShopCategoryResult:
    """多选店铺分类；参数：商品页、有序分类名称；返回：选择及跳过结果，普通操作错误仍抛出。"""
    if isinstance(names, str) or not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("店铺分类需要非空名称列表")
    names = list(dict.fromkeys(name.strip() for name in names))
    logger = getLogger(__name__)
    group = page.locator(SHOP_CATEGORY)
    if not group.is_visible():
        toggle = page.get_by_text("展开收起项", exact=True).locator("..").get_by_role("switch").first
        if toggle.get_attribute("aria-checked") != "true":
            page_step(lambda: toggle.click(timeout=WAIT_MS), completed=lambda: toggle.get_attribute("aria-checked") == "true")
    expect(group).to_be_visible(timeout=WAIT_MS)
    trigger = group.get_by_role("combobox")
    if trigger.get_attribute("aria-expanded") != "true":
        page_step(lambda: trigger.click(timeout=WAIT_MS), completed=lambda: trigger.get_attribute("aria-expanded") == "true")
    tree = page.locator(CATEGORY_TREE)
    expect(tree).to_have_count(1, timeout=WAIT_MS)
    selected = []
    skipped = {}
    try:
        for name in names:
            checkpoint(f"选择店铺分类：{name}")
            item = tree.get_by_role("treeitem", name=name, exact=True)
            count = item.count()
            if count == 0:
                reason = "未匹配到同名店铺分类"
            elif count > 1:
                reason = f"匹配到{count}项，名称有歧义"
            else:
                checkbox = item.get_by_role("checkbox", name=name, exact=True)
                if checkbox.is_checked():
                    selected.append(name)
                    logger.info("店铺分类已勾选，保持选择：%s", name)
                    continue
                if tree.locator('input[type="checkbox"]:checked').count() >= MAX_CATEGORIES:
                    reason = "已达20项选择上限（包含原已选项）"
                elif not checkbox.is_enabled():
                    reason = "分类当前不可选择"
                else:
                    page_step(lambda: item.locator("label").click(timeout=WAIT_MS), completed=checkbox.is_checked)
                    expect(checkbox).to_be_checked(timeout=WAIT_MS)
                    selected.append(name)
                    logger.info("店铺分类已选择并核对：%s", name)
                    continue
            skipped[name] = reason
            logger.warning("店铺分类跳过：%s；原因：%s", name, reason)
        # 多选过多时输入框会折叠标签，因此在关闭前逐项复查真实勾选状态。
        for name in selected:
            expect(tree.get_by_role("treeitem", name=name, exact=True).get_by_role("checkbox", name=name, exact=True)).to_be_checked(timeout=WAIT_MS)
    finally:
        # 不删除页面原有分类；关闭下拉框方便后续步骤继续。
        page_step(lambda: trigger.press("Escape"), completed=lambda: trigger.get_attribute("aria-expanded") == "false")
    expect(trigger).to_have_attribute("aria-expanded", "false", timeout=WAIT_MS)
    logger.info("店铺分类匹配完成：请求%s项，已选%s项，跳过%s项；已选=%s；跳过=%s",
                len(names), len(selected), len(skipped), selected, skipped)
    return ShopCategoryResult(selected, skipped)


@qianniu_operation
def select_shop_category(page: Page, name: str) -> ShopCategoryResult:
    """兼容原单分类调用；参数：页面、名称；返回：同多选结果，未匹配只记录并跳过。"""
    return select_shop_categories(page, [name])
