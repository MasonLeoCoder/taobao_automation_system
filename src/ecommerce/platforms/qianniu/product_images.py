"""千牛商品页 1:1 和 3:4 主图上传及顺序验证。"""
import logging
import re
from time import monotonic
from pathlib import Path
from uuid import uuid4
from collections.abc import Callable

from playwright.sync_api import Page, Error as PlaywrightError
from playwright.sync_api import expect as playwright_expect
from ecommerce.shared.exceptions import NeedsAttention
from .verification import checkpoint, wait_for_human_verification, qianniu_operation, expect, page_step, choose_file_dialog, intercept_file_dialogs

MAIN_GROUP = "#sell-field-mainImagesGroup"
PICKER = 'iframe[src*="sucai-selector-ng"]:visible'
FIELD_LABEL = 'span[title="1:1主图"]'
TILE = 'div[class*="PicList_pic_background"]'
WAIT_MS = 30_000
DROPZONE = "点击/拖拽，批量导入文件"
SLOT_SETTLE_MS = 1800
SLOT_CLICK_MS = 5000


def _main_tile(picker, name):
    return picker.locator(TILE).filter(has=picker.get_by_text(name, exact=True))


def _watch_picker_owner(page):
    """记住素材窗口打开前的 iframe 和被点击的空图位，供人工返回后核对。"""
    return page.evaluate_handle("""() => {
        const state = {};
        const remember = event => {
            const trigger = event.target.closest?.('.empty-container');
            if (!trigger) return;
            state.slot = trigger.closest('.drag-item');
            state.previous = Array.from(document.querySelectorAll('iframe[src*="sucai-selector-ng"]'))
                .filter(frame => frame.getClientRects().length && getComputedStyle(frame).visibility === 'visible');
        };
        document.addEventListener('click', remember, true);
        state.dispose = () => document.removeEventListener('click', remember, true);
        return state;
    }""")


def _picker_matches_slot(page, slot, owner):
    picker = page.locator(PICKER)
    if picker.count() != 1:
        return False
    frame = picker.element_handle(timeout=WAIT_MS)
    try:
        if not owner.evaluate("(state, frame) => state.previous && !state.previous.includes(frame)", frame):
            return False
        return slot.evaluate("(element, state) => state.slot === element", owner)
    finally:
        frame.dispose()


def _fill_main_slot(page, group, owner, position, aspect_ratio, filename, name, source, review):
    """只在已核实的空位选择本批素材，复查图位后再决定如何恢复。"""
    slot = group.locator(".drag-item").nth(position - 1)
    image = slot.locator("img")
    picker = page.frame_locator(PICKER)
    progress = f"{aspect_ratio} 主图已核对 {position - 1}/5，正在填写第 {position} 格"
    target = f"目标文件「{filename}」，本批素材名「{name}」"
    verified_source = re.compile(re.escape(source))
    resuming = False
    while True:
        if page.is_closed():
            raise NeedsAttention(f"千牛发品页已关闭。{progress}")
        try:
            checkpoint(progress)
            if resuming:
                try:
                    playwright_expect(image).to_have_attribute("src", verified_source, timeout=SLOT_SETTLE_MS)
                    return
                except (PlaywrightError, AssertionError):
                    pass
            current = image.evaluate_all("images => images.map(image => image.getAttribute('src'))")
            if len(current) == 1 and source in (current[0] or ""):
                return
            if current:
                raise NeedsAttention("当前图位的图片与本批目标不符，请在千牛移除或更换这张图")
            if not resuming or not page.locator(PICKER).count():
                open_main_image_picker(page, position, aspect_ratio)
            if not _picker_matches_slot(page, slot, owner):
                raise NeedsAttention(f"无法确认素材窗口属于第 {position} 格，请从这个空位重新打开素材库")
            tile = _main_tile(picker, name)
            try:
                playwright_expect(tile).to_have_count(1, timeout=WAIT_MS)
                tile.locator('div[class*="PicList_pic_imgBox"]').click(timeout=SLOT_CLICK_MS)
            except (PlaywrightError, AssertionError):
                # 点击超时也可能已生效，先等待千牛回写图位，再决定是否请求人工。
                pass
            try:
                playwright_expect(image).to_have_attribute("src", verified_source, timeout=SLOT_SETTLE_MS)
                return
            except (PlaywrightError, AssertionError) as error:
                raise NeedsAttention("本次选择尚未在图位回读成功，请检查素材窗口或手动选择目标图片") from error
        except (NeedsAttention, PlaywrightError) as error:
            if page.is_closed():
                raise NeedsAttention(f"千牛发品页已关闭。{progress}") from error
            reason = str(error) if isinstance(error, NeedsAttention) else "素材窗口或图位发生变化，请核对当前页面"
            message = f"{progress}。{target}。{reason}。"
            if review is None:
                raise NeedsAttention(message) from error
            try:
                review(page, message)
            except NeedsAttention as feedback_error:
                if page.is_closed():
                    raise NeedsAttention(f"千牛发品页已关闭。{progress}") from feedback_error
                raise
            resuming = True


def _uploaded_sources(page, group, names, aspect_ratio, on_attention):
    """交付本地文件后只核对本批素材，支持素材窗口关闭后的恢复。"""
    step = f"核对已上传的 {aspect_ratio} 主图"
    picker = page.frame_locator(PICKER)
    success = picker.get_by_text(re.compile(r"^5\s*个文件上传成功$"))
    panel = picker.locator('[class*="UploadDialog_dialog__"]:visible')
    deadline = monotonic() + 120
    while True:
        checkpoint(step)
        if page.is_closed():
            raise NeedsAttention(f"千牛发品页已关闭，{aspect_ratio} 主图上传结果待核对")
        if not page.locator(PICKER).count():
            break
        if success.is_visible():
            complete = picker.get_by_role("button", name="完成", exact=True)
            wait_for_human_verification(page, on_attention, step=step)
            if success.is_visible():
                page_step(lambda: complete.click(timeout=WAIT_MS), completed=lambda: not success.is_visible())
            break
        if not panel.count() and not picker.get_by_text(DROPZONE, exact=True).is_visible() and (
            _main_tile(picker, names[0]).is_visible() or picker.get_by_text("本地上传", exact=True).is_visible()
        ):
            break
        if monotonic() >= deadline:
            raise NeedsAttention(f"{aspect_ratio} 主图已交给千牛，但未核实本批 5 张素材，请检查上传结果")
        page.wait_for_timeout(500)

    if not page.locator(PICKER).count():
        if group.locator(".drag-item img").count():
            raise NeedsAttention(f"{aspect_ratio} 主图栏已有图片，请人工核对本批素材和顺序")
        open_main_image_picker(page, 1, aspect_ratio)
    sources = []
    for name in names:
        tile = _main_tile(picker, name)
        try:
            playwright_expect(tile).to_have_count(1, timeout=WAIT_MS)
            source = tile.locator("img").get_attribute("src", timeout=WAIT_MS)
        except (PlaywrightError, AssertionError) as error:
            raise NeedsAttention(f"{aspect_ratio} 主图本批素材未能唯一核对，请检查上传结果：{name}") from error
        if not source:
            raise NeedsAttention(f"{aspect_ratio} 主图素材缺少可核对地址：{name}")
        sources.append(source.split("_320x320")[0])
    return sources


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
def upload_main_images(page: Page, files: list[Path], aspect_ratio: str = "1:1", on_attention: Callable[[str], None] | None = None,
                       *, on_slot_review: Callable[[Page, str], None] | None = None) -> None:
    """上传并填入五张主图；参数：商品页、有序的五张图片、比例；返回：无，失败抛错。"""
    with intercept_file_dialogs(page):
        if on_slot_review is not None:
            _upload_main_images(page, files, aspect_ratio, on_attention=on_attention, on_slot_review=on_slot_review)
        elif on_attention is not None:
            _upload_main_images(page, files, aspect_ratio, on_attention=on_attention)
        elif aspect_ratio == "1:1":
            _upload_main_images(page, files)
        else:
            _upload_main_images(page, files, aspect_ratio)


def _upload_main_images(page: Page, files: list[Path], aspect_ratio: str = "1:1", on_attention: Callable[[str], None] | None = None,
                        *, on_slot_review: Callable[[Page, str], None] | None = None) -> None:
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
    picker = page.frame_locator(PICKER)
    dropzone = picker.get_by_text(DROPZONE, exact=True)

    def prepare_picker():
        if page.is_closed():
            raise NeedsAttention("千牛发品页已关闭，请重新启动填写任务")
        if group.locator(".drag-item img").count():
            raise NeedsAttention(f"{aspect_ratio} 主图栏已有图片，请先核对当前主图")
        if not page.locator(PICKER).count():
            open_main_image_picker(page, 1, aspect_ratio)
        if not dropzone.is_visible():
            picker.get_by_text("本地上传", exact=True).click(timeout=WAIT_MS)

    chooser = choose_file_dialog(page, dropzone, timeout=WAIT_MS, prepare=prepare_picker)
    logger.info("开始上传 5 张 %s 主图，素材批次：%s", aspect_ratio, batch)
    try:
        chooser.set_files(payloads)
    except PlaywrightError:
        logger.warning("%s 主图交付结果待核对，检查本批素材", aspect_ratio)
    sources = _uploaded_sources(page, group, names, aspect_ratio, on_attention)
    logger.info("5 张素材上传成功，开始按文件编号填入主图位置")

    review = on_slot_review
    if review is None and on_attention is not None:
        def review(_, message):
            on_attention(f"{message}\n在千牛处理后输入“继续”。")
    owner = _watch_picker_owner(page)
    try:
        for position, (path, name, source) in enumerate(zip(files, names, sources), start=1):
            _fill_main_slot(page, group, owner, position, aspect_ratio, path.name, name, source, review)
            logger.info("%s 主图 %s/5 已填入并核对：%s", aspect_ratio, position, path.name)
    finally:
        if not page.is_closed():
            try:
                owner.evaluate("state => state.dispose()")
                owner.dispose()
            except PlaywrightError:
                logger.debug("素材窗口核对监听已随页面退出")

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
def upload_portrait_images(page: Page, files: list[Path], on_attention: Callable[[str], None] | None = None,
                           *, on_slot_review: Callable[[Page, str], None] | None = None) -> None:
    """上传五张3:4主图；参数：页面、有序图片路径；返回：无，失败抛错。"""
    if on_slot_review is not None:
        upload_main_images(page, files, aspect_ratio="3:4", on_attention=on_attention, on_slot_review=on_slot_review)
    elif on_attention is None:
        upload_main_images(page, files, aspect_ratio="3:4")
    else:
        upload_main_images(page, files, aspect_ratio="3:4", on_attention=on_attention)
