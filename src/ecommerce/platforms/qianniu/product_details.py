"""新版千牛详情编辑器：全部详情图按编号上传、分批添加并核对顺序。"""
import logging
import re
from pathlib import Path
from uuid import uuid4
from playwright.sync_api import Page
from ecommerce.shared.exceptions import NeedsAttention
from .verification import checkpoint, qianniu_operation, expect, page_step, choose_file_dialog

DETAIL = "#sell-field-descRepublicOfSell"
CONTENT = f'{DETAIL} [class*="panel_edit_content"]'
IMAGES = f"{CONTENT} img.image-item"
PICKER = 'iframe[src*="sucai-selector-ng"]:visible'
TILE = 'div[class*="PicList_pic_background"]'
BATCH_SIZE = 50
WAIT_MS = 30000


def expected_segments(source: str, width: int, height: int) -> list[str]:
    """按平台原图分割规则生成顺序；参数：原图地址、宽高；返回：完整覆盖的分段地址。"""
    if width <= 0 or height <= 0:
        raise ValueError("图片宽高必须为正数")
    if height <= width * 2:
        return [source]
    if "_!!" not in source:
        raise NeedsAttention("长图地址格式改变，无法核对完整分割，请人工检查")
    prefix, suffix = source.split("_!!", 1)
    return [f"{prefix}~crop,0,{y},{width},{min(width * 2, height - y)}~_!!{suffix}"
            for y in range(0, height, width * 2)]


def verify_detail_order(page: Page, expected: list[str]) -> None:
    """核对详情模块总数、逐张来源和分割顺序；不一致时停止，避免误报成功。"""
    images = page.locator(IMAGES)
    expect(images).to_have_count(len(expected), timeout=WAIT_MS)
    for index, source in enumerate(expected):
        expect(images.nth(index)).to_have_attribute(
            "src", re.compile(r"^" + re.escape(source) + r"(?:_.*|\?.*)?$"), timeout=WAIT_MS)


@qianniu_operation
def upload_detail_images(page: Page, files: list[Path]) -> None:
    """按传入顺序上传全部详情图；要求编辑区为空；完成后逐张核对，不发布。"""
    if not files or any(not p.is_file() or not 0 < p.stat().st_size <= 6 * 1024 * 1024 for p in files):
        raise ValueError("详情图必须存在且每张大于0、不超过6MB")
    content = page.locator(CONTENT)
    expect(page.locator(DETAIL)).to_be_visible(timeout=WAIT_MS)
    expect(content).to_have_count(1, timeout=WAIT_MS)
    if content.locator(":scope > *").count():
        raise NeedsAttention("详情编辑区已有内容，请先核对；不会自动覆盖或重复添加")
    expected = []
    batch_id = uuid4().hex[:12]
    def intercept(chooser):
        """全程接管文件选择，防止额外系统文件窗口阻塞。"""
        logging.getLogger(__name__).debug("已拦截详情图文件选择请求")
    page.on("filechooser", intercept)
    try:
        for offset in range(0, len(files), BATCH_SIZE):
            checkpoint(f"上传详情图 {offset + 1}～{min(offset + BATCH_SIZE, len(files))}")
            expected.extend(_upload_batch(page, files[offset:offset+BATCH_SIZE], batch_id, offset))
            verify_detail_order(page, expected)
            logging.getLogger(__name__).info("详情图已填入 %s/%s 张源图，共 %s 个模块，顺序核对通过",
                                             min(offset+BATCH_SIZE, len(files)), len(files), len(expected))
    finally:
        page.remove_listener("filechooser", intercept)


def _upload_batch(page, files, batch_id, offset):
    """上传一个批次并核对选中序号；返回此批次预期的详情模块地址。"""
    names = [f"detail-{batch_id}-{offset+i:04d}{p.suffix.lower()}" for i,p in enumerate(files, 1)]
    page_step(lambda: page.locator(DETAIL).locator('[data-type="pic"]').click(), completed=lambda: page.locator(PICKER).count() == 1)
    expect(page.locator(PICKER)).to_have_count(1, timeout=WAIT_MS)
    picker = page.frame_locator(PICKER)
    page_step(lambda: picker.get_by_role("button", name="本地上传", exact=True).click(), completed=lambda: picker.get_by_text("点击/拖拽，批量导入文件", exact=True).is_visible())
    chooser = choose_file_dialog(page, picker.get_by_text("点击/拖拽，批量导入文件", exact=True))
    chooser.set_files([{"name": name, "mimeType": "image/png" if p.suffix.lower()==".png" else "image/jpeg",
                             "buffer": p.read_bytes()} for p,name in zip(files,names)])
    expect(picker.get_by_text(re.compile(rf"^{len(files)}\s*个文件上传成功$"))).to_be_visible(timeout=180000)
    page_step(lambda: picker.get_by_role("button", name="完成", exact=True).click(), completed=lambda: not picker.get_by_role("button", name="完成", exact=True).is_visible())
    expected = []
    for index, name in enumerate(names, 1):
        tile = picker.locator(TILE).filter(has=picker.get_by_text(name, exact=True))
        expect(tile).to_have_count(1, timeout=WAIT_MS)
        # 上传完成后自动选中的序号代表插入顺序；不能用素材列表的显示顺序。
        expect(tile.locator(".next-checkbox-label")).to_have_text(str(index), timeout=WAIT_MS)
        source = tile.locator("img").get_attribute("src")
        dimensions = tile.locator('[class*="PicList_spec"]').inner_text()
        match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", dimensions)
        if not source or not match:
            raise NeedsAttention(f"无法核对详情图尺寸或地址：{name}")
        expected.extend(expected_segments(source.split("_320x320")[0], *map(int, match.groups())))
    expect(picker.locator('input[type="checkbox"]:checked')).to_have_count(len(files), timeout=WAIT_MS)
    old_count = page.locator(IMAGES).count()
    page_step(lambda: picker.get_by_role("button", name=f"确定（{len(files)}）", exact=True).click(), completed=lambda: page.locator(PICKER).count() == 0 or page.get_by_role("button", name="确认全部分割", exact=True).is_visible() or page.locator(IMAGES).count() > old_count)
    if len(expected) > len(files):
        logging.getLogger(__name__).info("此批次含长图，使用平台分割保留全部内容：%s 张源图 → %s 个模块", len(files), len(expected))
        page_step(lambda: page.get_by_role("button", name="确认全部分割", exact=True).click(timeout=WAIT_MS), completed=lambda: page.locator(IMAGES).count() == old_count + len(expected))
    expect(page.locator(PICKER)).to_have_count(0, timeout=WAIT_MS)
    return expected
