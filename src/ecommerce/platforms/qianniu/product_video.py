"""千牛商品视频：本地上传、唯一素材选择及封面核对。"""
import logging
import re
from pathlib import Path
from uuid import uuid4
from playwright.sync_api import Page
from ecommerce.shared.exceptions import NeedsAttention
from .verification import checkpoint, qianniu_operation, expect, page_step

VIDEO_GROUP = "#sell-field-auctionVideos"
VIDEO_PICKER = 'iframe[src*="videoSelector.htm"]:visible'
VIDEO_TILE = 'div[class*="VideoList--videoItem--"]'
WAIT_MS = 30000


def _open_picker(page: Page) -> None:
    """展开可选项并打开视频选择器；参数：编辑页；返回：无。"""
    group = page.locator(VIDEO_GROUP)
    if not group.is_visible():
        toggle = page.get_by_text("展开收起项", exact=True).locator("..").get_by_role("switch").first
        if toggle.get_attribute("aria-checked") != "true":
            page_step(lambda: toggle.click(timeout=WAIT_MS), completed=lambda: toggle.get_attribute("aria-checked") == "true")
    expect(group).to_be_visible(timeout=WAIT_MS)
    if group.locator("img.cover").count():
        raise NeedsAttention("商品视频栏已有视频，请先核对；不会自动覆盖或重复添加")
    page_step(lambda: group.get_by_text("上传视频", exact=True).click(timeout=WAIT_MS), completed=lambda: page.locator(VIDEO_PICKER).count() == 1)
    expect(page.locator(VIDEO_PICKER)).to_have_count(1, timeout=WAIT_MS)


def _select_video(page: Page, asset_name: str) -> None:
    """选择本次唯一素材并核对商品封面；参数：编辑页、素材名；返回：无。"""
    picker = page.frame_locator(VIDEO_PICKER)
    tile = picker.locator(VIDEO_TILE).filter(has=picker.get_by_text(asset_name, exact=True))
    expect(tile).to_have_count(1, timeout=WAIT_MS)
    poster = tile.locator("img").get_attribute("src")
    if not poster:
        raise NeedsAttention("视频没有生成封面，请等待素材处理完成后再核对")
    checkbox = tile.locator('input[type="checkbox"]')
    if not checkbox.is_checked():
        # 此选择框只在鼠标悬停时显示，先悬停，再正常点击。
        page_step(lambda: tile.hover(timeout=WAIT_MS))
        page_step(lambda: tile.locator("label").click(timeout=WAIT_MS), completed=checkbox.is_checked)
    expect(checkbox).to_be_checked(timeout=WAIT_MS)
    expect(picker.locator('input[type="checkbox"]:checked')).to_have_count(1, timeout=WAIT_MS)
    page_step(lambda: picker.get_by_role("button", name=re.compile(r"^确认上传\s*\(\s*1\s*\)$")).click(timeout=WAIT_MS), completed=lambda: page.locator(VIDEO_PICKER).count() == 0 or page.locator(VIDEO_GROUP).locator("img.cover").count() == 1)
    expect(page.locator(VIDEO_PICKER), "等待视频窗口关闭").to_have_count(0, timeout=WAIT_MS)
    cover = page.locator(VIDEO_GROUP).locator("img.cover")
    expect(cover).to_have_count(1, timeout=WAIT_MS)
    expect(cover).to_have_attribute("src", poster, timeout=WAIT_MS)
    group = page.locator(VIDEO_GROUP)
    if group.get_by_text("详情页不展示", exact=True).count():
        logging.getLogger(__name__).warning("视频已填入，但平台提示当前比例在详情页不展示")
    logging.getLogger(__name__).info("商品视频已填入并核对封面：%s", asset_name)


@qianniu_operation
def upload_product_video(page: Page, file: Path) -> None:
    """上传一个MP4视频并加入商品页；已有视频则停止，不重复上传或发布商品。"""
    if not file.is_file() or file.suffix.lower() != ".mp4" or not file.stat().st_size:
        raise ValueError("请提供存在且非空的MP4商品视频")
    _open_picker(page)
    picker = page.frame_locator(VIDEO_PICKER)
    # 视频平台会截短过长名称，使用17字符名称，保留完整编号和扩展名。
    asset_name = f"v{uuid4().hex[:12]}.mp4"
    checkpoint("提交本地商品视频")
    picker.locator('input[type="file"]').set_input_files(
        {"name": asset_name, "mimeType": "video/mp4", "buffer": file.read_bytes()}, timeout=WAIT_MS)
    logging.getLogger(__name__).info("开始上传商品视频：%s，素材名：%s", file.name, asset_name)
    expect(picker.get_by_text("1 个文件已上传", exact=True)).to_be_visible(timeout=180000)
    expect(picker.get_by_text("发布成功", exact=True)).to_be_visible(timeout=WAIT_MS)
    # 此处“发布成功”是素材上传组件状态，商品仍未提交。
    page_step(lambda: picker.get_by_role("button", name="完成", exact=True).click(timeout=WAIT_MS), completed=lambda: not picker.get_by_role("button", name="完成", exact=True).is_visible())
    checkpoint("选择已上传商品视频")
    _select_video(page, asset_name)
