"""SKU搜索主图、搜索标题及商品规格图片：逐规格匹配、保存并回读。"""
import mimetypes
import re
from dataclasses import dataclass, field
from logging import getLogger
from uuid import uuid4
from playwright.sync_api import Page
from ecommerce.modules.product.assets import SkuImageInputs
from ecommerce.shared.exceptions import NeedsAttention
from .product_sku import SKU_GROUP, SKU_ROWS, CREATED_NAMES
from .verification import qianniu_operation, checkpoint, expect, page_step, choose_file_dialog, intercept_file_dialogs

DRAWER = '.sku-component-sku-detail-drawer:visible'
BATCH = '.batch-fill-sku-image-dialog:visible'
PICKER = 'iframe[src*="sucai-selector-ng"]:visible'
TILE = 'div[class*="PicList_pic_background"]'
SEARCH_IMAGE = 'td[id$="-skuPicture"]'
SPEC_IMAGE = 'td[id$="-custom_-1"]'
TITLE = 'td[id$="-skuTitle"]'
WAIT_MS = 30000
IMAGE_FIELDS = ('SKU搜索主图', '商品规格图片')


@dataclass
class SkuMediaResult:
    """按规格返回已核对字段和逐字段跳过原因，不把缺少素材当作填写成功。"""
    filled: dict[str, list[str]] = field(default_factory=dict)
    skipped: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Asset:
    """本次唯一上传素材，复用于同一SKU的两个图片位。"""
    name: str
    source: str


def _skip(result, name, label, reason):
    """登记缺失；参数：结果、规格、字段、原因；返回：无，其他字段继续。"""
    result.skipped.setdefault(name, {})[label] = reason
    getLogger(__name__).warning('SKU补充信息跳过：规格=%s；字段=%s；原因=%s', name, label, reason)


def _reset_drawer(drawer):
    """将详情内层滚到顶部，避免表格复制的sticky单元格被固定标题遮住。"""
    drawer.evaluate('e=>{const body=Array.from(e.children).find(c=>getComputedStyle(c).overflowY==="auto");if(body)body.scrollTop=0}')


def _same_image(images, source):
    """只读检查所选来源；参数：图片定位器、原图地址；返回：单张图片来源是否相同。"""
    return images.count() == 1 and source in (images.get_attribute('src') or '')


def _verify_image(images, asset):
    """核对图片来源；参数：定位器、上传素材；返回：数量及原图地址核对后返回。"""
    expect(images).to_have_count(1, timeout=WAIT_MS)
    expect(images).to_have_attribute('src', re.compile(re.escape(asset.source)), timeout=WAIT_MS)


def _upload_asset(page, file):
    """在已打开的素材选择器上传一次；参数：页面、图片；返回：唯一素材名与来源，不重放上传。"""
    picker = page.frame_locator(PICKER)
    dropzone = picker.get_by_text('点击/拖拽，批量导入文件', exact=True)
    if not dropzone.is_visible():
        page_step(lambda: picker.get_by_text('本地上传', exact=True).click(timeout=WAIT_MS), completed=dropzone.is_visible)
    chooser = choose_file_dialog(page, dropzone, timeout=WAIT_MS)
    asset_name = f'sku-{uuid4().hex[:12]}{file.suffix.lower()}'
    mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
    chooser.set_files({'name': asset_name, 'mimeType': mime, 'buffer': file.read_bytes()})
    getLogger(__name__).info('开始上传SKU图片：本地文件=%s；本次素材=%s', file.name, asset_name)
    expect(picker.get_by_text(re.compile(r'^1\s*个文件上传成功$'))).to_be_visible(timeout=180000)
    complete = picker.get_by_role('button', name='完成', exact=True)
    page_step(lambda: complete.click(timeout=WAIT_MS), completed=lambda: not complete.is_visible())
    tile = picker.locator(TILE).filter(has=picker.get_by_text(asset_name, exact=True))
    expect(tile).to_have_count(1, timeout=WAIT_MS)
    source = tile.locator('img').get_attribute('src')
    if not source:
        raise NeedsAttention(f'SKU图片上传后缺少可核对来源：{file.name}')
    return _Asset(asset_name, source.split('_320x320')[0])


def _tile(page, asset):
    """按本次唯一素材名定位；参数：页面、素材；返回：唯一的素材卡片。"""
    picker = page.frame_locator(PICKER)
    tile = picker.locator(TILE).filter(has=picker.get_by_text(asset.name, exact=True))
    expect(tile).to_have_count(1, timeout=WAIT_MS)
    return tile


def _fill_search_image(page, drawer, name, file, asset):
    """只为指定SKU选择搜索图；参数：页面、面板、规格、文件、可复用素材；返回：所用素材。"""
    _reset_drawer(drawer)
    batch = page.locator(BATCH)
    trigger = drawer.locator(SEARCH_IMAGE).locator('[class*="skuImagesHocContainer-"]')
    page_step(lambda: trigger.click(timeout=WAIT_MS), completed=lambda: batch.count() == 1)
    expect(batch).to_have_count(1, timeout=WAIT_MS)
    item = batch.locator('.sku-item').filter(has=page.get_by_text(name, exact=True))
    expect(item).to_have_count(1, timeout=WAIT_MS)
    # 批量窗口可能默认勾选第一行，切换目标前先取消其他规格，避免同图误填多个SKU。
    for other in batch.locator('.sku-item').all():
        if other.locator('.sku-text').inner_text() == name:
            continue
        slot = other.locator('.sku-image-container')
        if 'selected' in (slot.get_attribute('class') or '').split():
            page_step(lambda: slot.click(timeout=WAIT_MS), completed=lambda: 'selected' not in (slot.get_attribute('class') or '').split())
    target = item.locator('.sku-image-container')
    if 'selected' not in (target.get_attribute('class') or '').split():
        page_step(lambda: target.click(timeout=WAIT_MS), completed=lambda: 'selected' in (target.get_attribute('class') or '').split())
    expect(batch.locator('.sku-image-container.selected')).to_have_count(1, timeout=WAIT_MS)
    expect(target).to_have_class(re.compile(r'\bselected\b'), timeout=WAIT_MS)
    asset = asset or _upload_asset(page, file)
    tile = _tile(page, asset)
    preview = target.locator('img')
    if not _same_image(preview, asset.source):
        page_step(lambda: tile.locator('div[class*="PicList_pic_imgBox"]').click(timeout=WAIT_MS), completed=lambda: _same_image(preview, asset.source))
    _verify_image(preview, asset)
    page_step(lambda: batch.get_by_role('button', name='确定', exact=True).click(timeout=WAIT_MS), completed=lambda: batch.count() == 0)
    expect(batch).to_have_count(0, timeout=WAIT_MS)
    _verify_image(drawer.locator(SEARCH_IMAGE).locator('img'), asset)
    return asset


def _fill_spec_image(page, drawer, file, asset):
    """填写当前规格的图片；参数：页面、面板、文件、可复用素材；返回：所用素材。"""
    _reset_drawer(drawer)
    target = drawer.locator(SPEC_IMAGE)
    picker = page.locator(PICKER)
    page_step(lambda: target.locator('.empty-container').click(timeout=WAIT_MS), completed=lambda: picker.count() == 1)
    expect(picker).to_have_count(1, timeout=WAIT_MS)
    asset = asset or _upload_asset(page, file)
    tile = _tile(page, asset)
    checkbox = tile.locator('input[type="checkbox"]')
    if not checkbox.is_checked():
        page_step(lambda: tile.locator('div[class*="PicList_pic_imgBox"]').click(timeout=WAIT_MS), completed=checkbox.is_checked)
    expect(checkbox).to_be_checked(timeout=WAIT_MS)
    frame = page.frame_locator(PICKER)
    expect(frame.locator('input[type="checkbox"]:checked')).to_have_count(1, timeout=WAIT_MS)
    confirm = frame.get_by_role('button', name=re.compile(r'^确定[（(]\s*1\s*[）)]$'))
    page_step(lambda: confirm.click(timeout=WAIT_MS), completed=lambda: _same_image(target.locator('img'), asset.source))
    expect(picker).to_have_count(0, timeout=WAIT_MS)
    _verify_image(target.locator('img'), asset)
    return asset


def _core_values(row):
    """只读保存原有规格及数值；参数：SKU行；返回：本次功能不应改变的字段。"""
    return row.evaluate('''row=>({name:row.querySelector('textarea[data-real="true"]')?.value,
      values:Array.from(row.querySelectorAll('td:not([id$="-skuTitle"]) input:not([type="file"]):not([type="hidden"])')).map(e=>[e.closest('td')?.id,e.getAttribute('role'),e.value]),
      status:row.querySelector('[role="switch"]')?.getAttribute('aria-checked')})''')


@qianniu_operation
def fill_sku_media(page: Page, names: list[str], images: SkuImageInputs, guide_title=None) -> SkuMediaResult:
    """逐SKU填写两类图片及搜索标题；参数：页面、规格、图片匹配、Excel导购标题；返回：已核对及跳过字段。"""
    with intercept_file_dialogs(page):
        return _fill_sku_media(page, names, images, guide_title)


def _fill_sku_media(page: Page, names: list[str], images: SkuImageInputs, guide_title=None) -> SkuMediaResult:
    """执行逐SKU填写；参数及返回同公开入口；调用方负责全程接管文件选择请求。"""
    if not names or len(set(names)) != len(names) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError('SKU补充信息需要非空、唯一的销售规格名称')
    result = SkuMediaResult()
    title = guide_title.strip() if isinstance(guide_title, str) else ''
    title_reason = None if title else 'Excel缺少导购标题或值为空'
    if title and sum(1 if ord(char) < 128 else 2 for char in title) > 30:
        title_reason = 'Excel导购标题超过SKU搜索标题30字符限制'
    rows = page.locator(SKU_GROUP).locator(SKU_ROWS)
    for name in names:
        checkpoint(f'填写SKU图片及搜索标题：{name}')
        matching = [rows.nth(i) for i in range(rows.count()) if rows.nth(i).locator(CREATED_NAMES).input_value() == name]
        if len(matching) != 1:
            for label in (*IMAGE_FIELDS, 'SKU搜索标题'):
                _skip(result, name, label, f'页面SKU行未唯一匹配，实际匹配{len(matching)}行')
            continue
        row = matching[0]
        file = images.files.get(name)
        reason = images.missing.get(name, '未找到与销售规格同名的SKU图片')
        if file is not None and not file.is_file():
            file, reason = None, f'SKU图片已不存在：{file}'
        if file is None:
            for label in IMAGE_FIELDS:
                _skip(result, name, label, reason)
        if title_reason:
            _skip(result, name, 'SKU搜索标题', title_reason)
        if file is None and title_reason:
            continue
        drawer = page.locator(DRAWER)
        if not drawer.count():
            buttons = row.locator(TITLE).get_by_role('button')
            if buttons.count() != 1:
                for label in (*IMAGE_FIELDS, 'SKU搜索标题'):
                    if label not in result.skipped.get(name, {}):
                        _skip(result, name, label, '缺少唯一的SKU详情填写入口')
                continue
            page_step(lambda: buttons.click(timeout=WAIT_MS), completed=lambda: drawer.count() == 1)
        expect(drawer).to_have_count(1, timeout=WAIT_MS)
        if drawer.locator(CREATED_NAMES).input_value() != name:
            raise NeedsAttention(f'已打开的SKU详情不是“{name}”，未填写或覆盖其他规格')
        original = _core_values(row)
        filled, selected = [], {}
        asset = None
        if file:
            for label, selector, writer in [('SKU搜索主图', SEARCH_IMAGE, _fill_search_image), ('商品规格图片', SPEC_IMAGE, _fill_spec_image)]:
                cell = drawer.locator(selector)
                if cell.count() != 1:
                    _skip(result, name, label, '页面缺少唯一的图片填写区域')
                    continue
                if cell.locator('img').count():
                    _skip(result, name, label, '页面已有图片，未覆盖；请核对是否对应当前素材')
                    continue
                asset = writer(page, drawer, name, file, asset) if label == 'SKU搜索主图' else writer(page, drawer, file, asset)
                selected[selector] = asset
                filled.append(label)
        if not title_reason:
            field = drawer.locator(TITLE).get_by_role('textbox')
            if field.count() != 1:
                _skip(result, name, 'SKU搜索标题', '页面缺少唯一的SKU搜索标题文本框')
            else:
                if field.input_value() != title:
                    page_step(lambda: field.fill(title, timeout=WAIT_MS))
                page_step(field.blur)
                expect(field).to_have_value(title, timeout=WAIT_MS)
                counter = drawer.locator(TITLE).locator('.content-after > span')
                expect(counter).to_have_text(re.compile(r'^\d+/30$'), timeout=WAIT_MS)
                if int(counter.inner_text().split('/')[0]) > 30:
                    raise NeedsAttention(f'SKU“{name}”搜索标题超长，请核对Excel导购标题')
                filled.append('SKU搜索标题')
        errors = drawer.locator('.sell-o-message.error:visible, .sell-o-message[class*="errorMessage-"]:visible, .sell-component-info-wrapper-msg-content.error:visible')
        if errors.count():
            raise NeedsAttention(f'SKU“{name}”详情有平台校验错误：' + '；'.join(errors.all_text_contents()))
        page_step(lambda: drawer.get_by_role('button', name='确定', exact=True).click(timeout=WAIT_MS), completed=lambda: drawer.count() == 0)
        expect(drawer).to_have_count(0, timeout=WAIT_MS)
        if _core_values(row) != original:
            raise NeedsAttention(f'SKU“{name}”详情保存后原有规格、价格、数量或编码发生变化，请人工核对')
        for selector, uploaded in selected.items():
            _verify_image(row.locator(selector).locator('img'), uploaded)
        if 'SKU搜索标题' in filled:
            expect(row.locator(TITLE)).to_contain_text(title, timeout=WAIT_MS)
        result.filled[name] = filled
        getLogger(__name__).info('SKU补充信息已保存并回读：规格=%s；完成字段=%s；跳过=%s', name, filled, result.skipped.get(name, {}))
    return result
