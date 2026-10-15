"""千牛商品页 1:1 和 3:4 主图上传及顺序验证。"""
import logging
import re
from pathlib import Path
from uuid import uuid4
from collections.abc import Callable

from playwright.sync_api import Page, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from .verification import has_human_verification, wait_for_human_verification, qianniu_operation, expect, page_step, choose_file_dialog

MAIN_GROUP = "#sell-field-mainImagesGroup"
PICKER = 'iframe[src*="sucai-selector-ng"]:visible'
FIELD_LABEL = 'span[title="1:1主图"]'
TILE = 'div[class*="PicList_pic_background"]'
WAIT_MS = 30_000


@qianniu_operation
def open_main_image_picker(page: Page, position: int, aspect_ratio: str = "1:1") -> None:
    """打开空主图位；参数：商品页、位置（1～5）、比例；返回：无。"""
    if position not in range(1, 6):
        raise ValueError("主图位置必须在 1～5 之间")
    page_step(lambda: page.locator(f'span[title="{aspect_ratio}主图"]').click(), completed=lambda: page.locator(PICKER).count() == 0)
    expect(page.locator(PICKER)).to_have_count(0, timeout=WAIT_MS)
    slot = _image_group(page, aspect_ratio).locator(".drag-item").nth(position - 1)
    if slot.locator("img").count():
        raise ValueError(f"第 {position} 个主图位已有图片，停止以避免覆盖")
    page_step(lambda: slot.locator(".empty-container").click(), completed=lambda: page.locator(PICKER).count() == 1)
    expect(page.locator(PICKER)).to_have_count(1, timeout=WAIT_MS)


@qianniu_operation
def upload_main_images(page: Page, files: list[Path], aspect_ratio: str = "1:1", on_attention: Callable[[str], None] | None = None) -> None:
    """上传并填入五张主图；参数：商品页、有序的五张图片、比例；返回：无，失败抛错。"""
    # expect_file_chooser 的临时监听在获取文件请求后就结束了。
    # 在整个上传步骤保留监听，防止上传组件的额外请求弹出系统窗口。
    # 实际文件仍由下方捕获到的 FileChooser.set_files 提供。
    def intercept_file_chooser(chooser) -> None:
        """保持文件选择请求由 Playwright 接管；参数：请求；返回：无。"""
        logging.getLogger(__name__).debug("已拦截主图组件的文件选择请求")

    page.on("filechooser", intercept_file_chooser)
    try:
        if on_attention is not None:
            _upload_main_images(page, files, aspect_ratio, on_attention=on_attention)
        elif aspect_ratio == "1:1":
            _upload_main_images(page, files)
        else:
            _upload_main_images(page, files, aspect_ratio)
    finally:
        page.remove_listener("filechooser", intercept_file_chooser)


def _upload_main_images(page: Page, files: list[Path], aspect_ratio: str = "1:1", on_attention: Callable[[str], None] | None = None) -> None:
    """执行主图上传；参数：页面、五张图片；返回：无，外层负责请求拦截。"""
    if len(files) != 5 or any(not path.is_file() for path in files):
        raise ValueError("必须提供五张存在的主图文件")
    logger = logging.getLogger(__name__)
    group = _image_group(page, aspect_ratio)
    page_step(lambda: group.wait_for(state="visible", timeout=WAIT_MS))
    if group.locator("img").count():
        raise ValueError("主图栏已有图片，请先核对；本步骤不自动覆盖或重复添加")

    # 只改变上传时的素材名称，不修改本地文件。避免同名旧素材被误选。
    batch = uuid4().hex[:12]
    names = [f"main-{batch}-{index}{path.suffix.lower()}"
             for index, path in enumerate(files, start=1)]
    payloads = [{"name": name,
                 "mimeType": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
                 "buffer": path.read_bytes()}
                for path, name in zip(files, names)]
    open_main_image_picker(page, 1, aspect_ratio)
    picker = page.frame_locator(PICKER)
    page_step(lambda: picker.get_by_text("本地上传", exact=True).click(), completed=lambda: picker.get_by_text("点击/拖拽，批量导入文件", exact=True).is_visible())
    chooser = choose_file_dialog(page, picker.get_by_text("点击/拖拽，批量导入文件", exact=True))
    logger.info("开始上传 5 张 %s 主图，素材批次：%s", aspect_ratio, batch)
    chooser.set_files(payloads)
    page_step(lambda: picker.get_by_text(re.compile(r"^5\s*个文件上传成功$")).wait_for(
        state="visible", timeout=120_000))
    page_step(lambda: page.locator(PICKER).scroll_into_view_if_needed())
    complete_button = picker.get_by_role("button", name="完成", exact=True)
    page_step(complete_button.scroll_into_view_if_needed)
    wait_for_human_verification(page, on_attention, step=f"结束 {aspect_ratio} 主图素材上传")
    page_step(lambda: complete_button.click(timeout=WAIT_MS), completed=lambda: not complete_button.is_visible())
    logger.info("5 张素材上传成功，开始按文件编号填入主图位置")

    sources = []
    for position, (path, name) in enumerate(zip(files, names), start=1):
        step = f"选择第 {position} 张 {aspect_ratio} 主图（{path.name}）"
        wait_for_human_verification(page, on_attention, step=step)
        open_main_image_picker(page, position, aspect_ratio)
        picker = page.frame_locator(PICKER)
        tile = picker.locator(TILE).filter(has=picker.get_by_text(name, exact=True))
        expect(tile).to_have_count(1, timeout=WAIT_MS)
        source = tile.locator("img").get_attribute("src")
        if not source:
            raise ValueError(f"素材没有图片地址：{path.name}")
        source = source.split("_320x320")[0]
        slot_image = group.locator(".drag-item").nth(position - 1).locator("img")
        for attempt in range(3):
            wait_for_human_verification(page, on_attention, step=step)
            # 如果之前的点击已生效，不再重复点击，避免取消已选图片。
            if slot_image.count() and source in (slot_image.get_attribute("src") or ""):
                break
            try:
                page_step(lambda: tile.locator('div[class*="PicList_pic_imgBox"]').click(timeout=5_000), completed=lambda: slot_image.count() == 1 and source in (slot_image.get_attribute("src") or ""))
                wait_for_human_verification(page, on_attention, step=step)
                break
            except PlaywrightTimeoutError:
                if not has_human_verification(page) or attempt == 2:
                    raise
                wait_for_human_verification(page, on_attention, step=step)
        expect(slot_image).to_have_attribute("src", re.compile(re.escape(source)), timeout=WAIT_MS)
        sources.append(source)
        logger.info("%s 主图 %s/5 已填入并核对：%s", aspect_ratio, position, path.name)

    page_step(lambda: page.locator(f'span[title="{aspect_ratio}主图"]').click(), completed=lambda: page.locator(PICKER).count() == 0)
    expect(page.locator(PICKER)).to_have_count(0, timeout=WAIT_MS)
    page.mouse.move(0, 0)
    expect(group.locator("img")).to_have_count(5, timeout=WAIT_MS)
    for index, source in enumerate(sources):
        expect(group.locator(".drag-item").nth(index).locator("img")).to_have_attribute(
            "src", re.compile(re.escape(source)), timeout=WAIT_MS
        )
    logger.info("%s 主图完成：5 张图片及顺序核对通过，停留等待下一步", aspect_ratio)


def _image_group(page: Page, aspect_ratio: str):
    """定位独立主图区域；参数：页面、比例；返回：字段区域定位器。"""
    if aspect_ratio == "1:1":
        return page.locator(MAIN_GROUP)
    if aspect_ratio != "3:4":
        raise ValueError("主图比例只支持 1:1 或 3:4")
    return page.locator(".sell-component-info-wrapper-wrap").filter(
        has=page.locator('span[title="3:4主图"]')
    )


@qianniu_operation
def upload_portrait_images(page: Page, files: list[Path], on_attention: Callable[[str], None] | None = None) -> None:
    """上传五张3:4主图；参数：页面、有序图片路径；返回：无，失败抛错。"""
    if on_attention is None:
        upload_main_images(page, files, aspect_ratio="3:4")
    else:
        upload_main_images(page, files, aspect_ratio="3:4", on_attention=on_attention)
