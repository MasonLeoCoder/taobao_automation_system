"""SKU搜索主图、搜索标题及商品规格图片：逐规格匹配、保存并回读。"""
import mimetypes
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from logging import getLogger
from urllib.parse import urlsplit
from uuid import uuid4
from playwright.sync_api import Error as PlaywrightError, Page
from ecommerce.modules.product.assets import SkuImageInputs
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.shared.product_titles import SKU_TITLE_MAX, SKU_TITLE_MIN, validate_sku_title
from .product_sku import SKU_GROUP, SKU_ROWS, CREATED_NAMES, LAYERED_DIMENSIONS, layered_sku_rows
from .verification import (qianniu_operation, checkpoint, expect, page_step, choose_file_dialog,
                           intercept_file_dialogs, propagate_step_errors)

DRAWER = '.sku-component-sku-detail-drawer:visible'
BATCH = '.batch-fill-sku-image-dialog:visible'
PICKER = 'iframe[src*="sucai-selector-ng"]:visible'
TILE = 'div[class*="PicList_pic_background"]'
SEARCH_IMAGE = 'td[id$="-skuPicture"]'
SPEC_IMAGE = 'td[id$="-custom_-1"]'
TITLE = 'td[id$="-skuTitle"]'
WAIT_MS = 30000
IMAGE_FIELDS = ('SKU搜索主图', '商品规格图片')
IMAGE_REVIEW_SECONDS = 180
_THUMBNAIL = r'_\d+x\d+(?:[a-z]\d+)*(?:\.[a-z]+)?(?:_\.[a-z]+)?'
_CROP = r'~crop,\d+,\d+,\d+,\d+~'


@dataclass
class SkuMediaResult:
    """按规格返回已核对字段和逐字段跳过原因，不把缺少素材当作填写成功。"""
    filled: dict[str, list[str]] = field(default_factory=dict)
    skipped: dict[str, dict[str, str]] = field(default_factory=dict)
    optional_skips: dict[str, set[str]] = field(default_factory=dict)
    differences: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Asset:
    """本次唯一上传素材，复用于同一SKU的两个图片位。"""
    name: str
    source: str


class _ImageError(NeedsAttention):
    """单个可选图片位的失败。"""

    def __init__(self, message, rejected=None):
        super().__init__(message)
        self.rejected = rejected


def _skip(result, name, label, reason):
    """登记缺失；参数：结果、规格、字段、原因；返回：无，其他字段继续。"""
    result.skipped.setdefault(name, {})[label] = reason
    getLogger(__name__).warning('SKU补充信息跳过：规格=%s；字段=%s；原因=%s', name, label, reason)


def _reset_drawer(drawer):
    """将详情内层滚到顶部，避免表格复制的sticky单元格被固定标题遮住。"""
    drawer.evaluate('e=>{const body=Array.from(e.children).find(c=>getComputedStyle(c).overflowY==="auto");if(body)body.scrollTop=0}')


def _image_pattern(source):
    """按原素材的完整主机和路径匹配千牛预览，允许已知缩略及裁剪标记。"""
    location = urlsplit(source)
    path = re.sub(r'(\.[a-zA-Z]{3,4})' + _THUMBNAIL + '$', r'\1', location.path)
    crop = ''
    if location.hostname and (location.hostname == 'alicdn.com' or location.hostname.endswith('.alicdn.com')):
        path = re.sub(_CROP + r'(?=_!!\d+\.[a-zA-Z]+$)', '', path)
        seller = re.search(r'_!!\d+\.[a-zA-Z]+$', path)
        if seller:
            crop = f'(?:{_CROP})?' + re.escape(seller[0])
            path = path[:seller.start()]
    return re.compile(r'^(?:https?:)?//' + re.escape(location.netloc) + re.escape(path) + crop
                      + f'(?:{_THUMBNAIL})?(?:[?#].*)?$')


def _source(images):
    """即时读取单个图位的已选资源地址。"""
    if images.count() == 1:
        source = images.get_attribute('src', timeout=2000)
        if source and source.startswith(('https://', 'http://', '//')):
            return source
    return None


def _same_image(images, source):
    actual = _source(images)
    return bool(actual and _image_pattern(source).fullmatch(actual))


def _verify_image(images, asset):
    """核对图片来源；参数：定位器、上传素材；返回：数量及原图地址核对后返回。"""
    try:
        expect(images).to_have_count(1, timeout=WAIT_MS)
        expect(images).to_have_attribute('src', _image_pattern(asset.source), timeout=WAIT_MS)
    except (AssertionError, PlaywrightError) as error:
        actual = _source(images) if not images.page.is_closed() else None
        raise _ImageError('页面图片与本次选中的素材不一致' if actual else '等待超时，未读到该图位的图片',
                          rejected=actual) from error


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
        raise _ImageError(f'素材库未提供已上传图片的地址：{file.name}')
    return _Asset(asset_name, source)


def _tile(page, asset):
    """按本次唯一素材名定位；参数：页面、素材；返回：唯一的素材卡片。"""
    picker = page.frame_locator(PICKER)
    tile = picker.locator(TILE).filter(has=picker.get_by_text(asset.name, exact=True))
    expect(tile).to_have_count(1, timeout=WAIT_MS)
    return tile


def _asset_for(page, file, uploaded_assets):
    """素材入库后立即记录，供同一次任务的其他图位复用。"""
    if file not in uploaded_assets:
        uploaded_assets[file] = _upload_asset(page, file)
    return uploaded_assets[file]


def _layered_media_name(drawer, name, expected, combinations):
    """核对详情中的独立属性，再取得搜索主图窗口使用的完整名称。"""
    values = []
    for label in LAYERED_DIMENSIONS:
        heading = drawer.page.locator('.sell-component-info-wrapper-label').filter(has_text=re.compile(f'^{re.escape(label)}$'))
        section = drawer.locator('.sell-component-info-wrapper-wrap:visible').filter(has=heading)
        cell = section.locator('td.is-sale-prop:visible')
        if section.count() != 1 or cell.count() != 1:
            raise NeedsAttention(f'SKU「{name}」详情无法唯一读取「{label}」')
        values.append(' '.join(cell.inner_text().split()))
    pair = tuple(' '.join(value.split()) for value in expected)
    if tuple(values) != pair:
        raise NeedsAttention(f'SKU「{name}」详情的颜色和尺码与 Excel 不一致，页面显示「{" × ".join(values)}」')
    platform_name = ' '.join(pair)
    if sum(' '.join(' '.join(combination).split()) == platform_name for combination in combinations.values()) != 1:
        raise NeedsAttention(f'SKU「{name}」与其他规格在搜索主图窗口显示同名，无法唯一选图')
    return platform_name


def _fill_search_image(page, drawer, name, file, uploaded_assets):
    """只为指定SKU选择搜索图；参数：页面、面板、规格、文件、可复用素材；返回：所用素材。"""
    _reset_drawer(drawer)
    batch = page.locator(BATCH)
    trigger = drawer.locator(SEARCH_IMAGE).locator('[class*="skuImagesHocContainer-"]')
    page_step(lambda: trigger.click(timeout=WAIT_MS), completed=lambda: batch.count() == 1)
    expect(batch).to_have_count(1, timeout=WAIT_MS)
    expect(batch.locator('.sku-item').first).to_be_visible(timeout=WAIT_MS)
    label = page.locator('.sku-text').and_(page.get_by_text(name, exact=True))
    item = batch.locator('.sku-item').filter(has=label)
    if item.count() != 1:
        raise _ImageError(f'搜索主图窗口无法唯一定位规格「{name}」')
    # 批量窗口可能默认勾选第一行，切换目标前先取消其他规格，避免同图误填多个SKU。
    for other in batch.locator('.sku-item').all():
        if other.locator(label).count():
            continue
        slot = other.locator('.sku-image-container')
        if 'selected' in (slot.get_attribute('class') or '').split():
            page_step(lambda: slot.click(timeout=WAIT_MS), completed=lambda: 'selected' not in (slot.get_attribute('class') or '').split())
    target = item.locator('.sku-image-container')
    if 'selected' not in (target.get_attribute('class') or '').split():
        page_step(lambda: target.click(timeout=WAIT_MS), completed=lambda: 'selected' in (target.get_attribute('class') or '').split())
    expect(batch.locator('.sku-image-container.selected')).to_have_count(1, timeout=WAIT_MS)
    expect(target).to_have_class(re.compile(r'\bselected\b'), timeout=WAIT_MS)
    asset = _asset_for(page, file, uploaded_assets)
    tile = _tile(page, asset)
    expect(batch.locator('.sku-image-container.selected')).to_have_count(1, timeout=WAIT_MS)
    expect(target).to_have_class(re.compile(r'\bselected\b'), timeout=WAIT_MS)
    preview = target.locator('img')
    if not _same_image(preview, asset.source):
        page_step(lambda: tile.locator('div[class*="PicList_pic_imgBox"]').click(timeout=WAIT_MS), completed=lambda: _same_image(preview, asset.source))
    _verify_image(preview, asset)
    page_step(lambda: batch.get_by_role('button', name='确定', exact=True).click(timeout=WAIT_MS), completed=lambda: batch.count() == 0)
    expect(batch).to_have_count(0, timeout=WAIT_MS)
    _verify_image(drawer.locator(SEARCH_IMAGE).locator('img'), asset)
    return asset


def _fill_spec_image(page, drawer, file, uploaded_assets):
    """填写当前规格的图片；参数：页面、面板、文件、可复用素材；返回：所用素材。"""
    _reset_drawer(drawer)
    target = drawer.locator(SPEC_IMAGE)
    picker = page.locator(PICKER)
    page_step(lambda: target.locator('.empty-container').click(timeout=WAIT_MS), completed=lambda: picker.count() == 1)
    expect(picker).to_have_count(1, timeout=WAIT_MS)
    asset = _asset_for(page, file, uploaded_assets)
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


def _cancel_image_picker(page):
    """退出这次选图，避免把未确认的选择应用到 SKU。"""
    batch = page.locator(BATCH)
    picker = page.locator(PICKER)
    cancel_name = re.compile(r'^(取消|关闭)$')
    with propagate_step_errors():
        for _ in range(3):
            batch_count, picker_count = batch.count(), picker.count()
            if not batch_count and not picker_count:
                return
            controls = []
            if picker_count == 1:
                frame = page.frame_locator(PICKER)
                controls.append(frame.locator('[class*="UploadDialog_dialog__"]:visible').get_by_role('button', name=cancel_name))
                if not batch_count:
                    controls.append(frame.get_by_role('button', name=cancel_name))
            if batch_count == 1:
                controls.append(batch.get_by_role('button', name=cancel_name))
            cancel = next((button for button in controls if button.count() == 1 and button.is_visible()), None)
            try:
                if cancel is not None:
                    page_step(lambda: cancel.click(timeout=1500))
                else:
                    page.keyboard.press('Escape')
            except PlaywrightError:
                if page.is_closed():
                    raise NeedsAttention('千牛商品编辑页已关闭，无法继续 SKU 填写') from None
                page.keyboard.press('Escape')
            page.wait_for_timeout(200)
    if batch.count() or picker.count():
        raise NeedsAttention('SKU 素材窗口未能安全退出，请在千牛关闭该窗口后核对已填写的商品')


def _review_image(page, name, label, images, expected, error, on_image_review, manual_selection, result, *, saved=False):
    """在同一图位等待手动完成或跳过，所有复核共用第一次提示的期限。"""
    reason = str(error) if isinstance(error, _ImageError) else '素材上传或选图未完成，请检查当前素材窗口'
    getLogger(__name__).warning('SKU 图片需要处理：规格=%s；图位=%s；%s', name, label, reason)
    initial = _source(images)
    rejected = error.rejected if isinstance(error, _ImageError) else None
    deadline = time.time() + IMAGE_REVIEW_SECONDS
    instructions = ('请在千牛打开这个 SKU 的详情，手动选图并保存详情，回网页点击「完成」。' if saved else
                    '请在千牛为这个图位手动选图并确认，保留 SKU 详情面板，再回网页点击「完成」。')
    hint = instructions
    while True:
        with manual_selection():
            action = on_image_review(page, f'SKU「{name}」的{label}：{reason}\n{hint}', deadline)
        if action != 'continue':
            _cancel_image_picker(page)
            reason = '等待 3 分钟未完成，自动跳过' if action == 'timeout' else '用户选择跳过此图片'
            _skip(result, name, label, reason)
            result.optional_skips.setdefault(name, set()).add(label)
            return None
        if not page.locator(BATCH).count() and not page.locator(PICKER).count():
            actual = _source(images)
            if actual and (expected and _image_pattern(expected.source).fullmatch(actual)
                           or not any(source and _image_pattern(source).fullmatch(actual) for source in (initial, rejected))):
                getLogger(__name__).info('SKU 图片已手动选择并回读：规格=%s；图位=%s', name, label)
                return _Asset('手动上传', actual)
        hint = f'还未读到当前图位已确认的图片。{instructions}'


def _core_values(row):
    """只读保存原有规格及数值；参数：SKU行；返回：本次功能不应改变的字段。"""
    return row.evaluate('''row=>({name:row.querySelector('textarea[data-real="true"]')?.value,
      values:Array.from(row.querySelectorAll('td:not([id$="-skuTitle"]) input:not([type="file"]):not([type="hidden"])')).map(e=>[e.closest('td')?.id,e.getAttribute('role'),e.value]),
      status:row.querySelector('[role="switch"]')?.getAttribute('aria-checked')})''')


@qianniu_operation
def fill_sku_media(page: Page, names: list[str], images: SkuImageInputs, guide_title=None, *,
                   sku_titles=None, combinations=None, on_image_review=None) -> SkuMediaResult:
    """逐 SKU 填写图片和搜索标题，缺少专属标题时取导购标题。"""
    with intercept_file_dialogs(page) as manual_selection:
        return _fill_sku_media(page, names, images, guide_title,
                               **({'on_image_review': on_image_review, 'manual_selection': manual_selection}
                                  if on_image_review is not None else {}),
                               **({'sku_titles': sku_titles} if sku_titles is not None else {}),
                               **({'combinations': combinations} if combinations else {}))


def _fill_sku_media(page: Page, names: list[str], images: SkuImageInputs, guide_title=None, *,
                    sku_titles=None, combinations=None, on_image_review=None, manual_selection=None) -> SkuMediaResult:
    """执行逐SKU填写；参数及返回同公开入口；调用方负责全程接管文件选择请求。"""
    if not names or len(set(names)) != len(names) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError('SKU补充信息需要非空、唯一的销售规格名称')
    result = SkuMediaResult()
    guide = guide_title.strip() if isinstance(guide_title, str) else ''
    titles = sku_titles or {}
    missing_title = 'Excel缺少导购标题或值为空' if sku_titles is None else 'SKU 搜索标题和导购标题均为空'
    group = page.locator(SKU_GROUP)
    rows = group.locator(SKU_ROWS)
    image_fields = IMAGE_FIELDS[:1] if combinations else IMAGE_FIELDS
    uploaded_assets = {}
    image_steps = propagate_step_errors if on_image_review else nullcontext
    for name in names:
        try:
            title = validate_sku_title(titles.get(name) or guide)
        except ValueError as error:
            raise NeedsAttention(f'SKU「{name}」的{error}') from None
        title_reason = None if title else missing_title
        checkpoint(f'填写SKU图片及搜索标题：{name}')
        matching = [layered_sku_rows(group, combinations, only=name)[name]] if combinations else [
            rows.nth(i) for i in range(rows.count()) if rows.nth(i).locator(CREATED_NAMES).input_value() == name]
        if len(matching) != 1:
            for label in (*image_fields, 'SKU搜索标题'):
                _skip(result, name, label, f'页面SKU行未唯一匹配，实际匹配{len(matching)}行')
            continue
        row = matching[0]
        file = images.files.get(name)
        reason = images.missing.get(name, '未找到与销售规格同名的SKU图片')
        if file is not None and not file.is_file():
            file, reason = None, f'SKU图片已不存在：{file}'
        if file is None:
            for label in image_fields:
                _skip(result, name, label, reason)
        if title_reason:
            _skip(result, name, 'SKU搜索标题', title_reason)
        if file is None and title_reason:
            continue
        drawer = page.locator(DRAWER)
        if combinations and drawer.count():
            raise NeedsAttention('请先关闭已有的 SKU 详情面板，再核对分层规格')
        if not drawer.count():
            buttons = row.locator(TITLE).get_by_role('button')
            if buttons.count() != 1:
                for label in (*image_fields, 'SKU搜索标题'):
                    if label not in result.skipped.get(name, {}):
                        _skip(result, name, label, '缺少唯一的SKU详情填写入口')
                continue
            page_step(lambda: buttons.click(timeout=WAIT_MS), completed=lambda: drawer.count() == 1)
        expect(drawer).to_have_count(1, timeout=WAIT_MS)
        platform_name = name
        if combinations:
            platform_name = _layered_media_name(drawer, name, combinations[name], combinations)
        elif drawer.locator(CREATED_NAMES).input_value() != name:
            raise NeedsAttention(f'已打开的SKU详情不是“{name}”，未填写或覆盖其他规格')
        original = _core_values(row)
        filled, selected = [], {}
        if file:
            writers = [(IMAGE_FIELDS[0], SEARCH_IMAGE, _fill_search_image),
                       (IMAGE_FIELDS[1], SPEC_IMAGE, _fill_spec_image)]
            if combinations:
                writers = writers[:1]
            for label, selector, writer in writers:
                cell = drawer.locator(selector)
                if cell.count() != 1:
                    _skip(result, name, label, '页面缺少唯一的图片填写区域')
                    continue
                current_images = cell.locator('img')
                if current_images.count():
                    asset = uploaded_assets.get(file)
                    if combinations and asset and _same_image(current_images, asset.source):
                        selected[selector] = (label, asset)
                        filled.append(label)
                    else:
                        _skip(result, name, label, '页面已有图片，未覆盖；请核对是否对应当前素材')
                    continue
                try:
                    with image_steps():
                        asset = (writer(page, drawer, platform_name, file, uploaded_assets) if selector == SEARCH_IMAGE
                                 else writer(page, drawer, file, uploaded_assets))
                except (_ImageError, PlaywrightError, AssertionError) as error:
                    if on_image_review is None or page.is_closed():
                        raise
                    asset = _review_image(page, name, label, current_images, uploaded_assets.get(file),
                                          error, on_image_review, manual_selection, result)
                    if not drawer.count() or (not combinations and drawer.locator(CREATED_NAMES).input_value() != name):
                        raise NeedsAttention(f'请核对 SKU「{name}」的详情面板，无法确认当前填写位置')
                    if combinations:
                        _layered_media_name(drawer, name, combinations[name], combinations)
                if asset:
                    selected[selector] = (label, asset)
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
                expect(counter).to_have_text(re.compile(fr'^\d+/{SKU_TITLE_MAX}$'), timeout=WAIT_MS)
                count = int(counter.inner_text().split('/')[0])
                if not SKU_TITLE_MIN <= count <= SKU_TITLE_MAX:
                    raise NeedsAttention(f'SKU「{name}」搜索标题为 {count} 字符，千牛要求 {SKU_TITLE_MIN}～{SKU_TITLE_MAX} 字符')
                filled.append('SKU搜索标题')
        errors = drawer.locator('.sell-o-message.error:visible, .sell-o-message[class*="errorMessage-"]:visible, .sell-component-info-wrapper-msg-content.error:visible')
        if errors.count():
            raise NeedsAttention(f'SKU“{name}”详情有平台校验错误：' + '；'.join(errors.all_text_contents()))
        page_step(lambda: drawer.get_by_role('button', name='确定', exact=True).click(timeout=WAIT_MS), completed=lambda: drawer.count() == 0)
        expect(drawer).to_have_count(0, timeout=WAIT_MS)
        if combinations:
            row = layered_sku_rows(group, combinations, only=name)[name]
        if _core_values(row) != original:
            raise NeedsAttention(f'SKU“{name}”详情保存后原有规格、价格、数量或编码发生变化，请人工核对')
        for selector, (label, uploaded) in selected.items():
            current_images = row.locator(selector).locator('img')
            try:
                with image_steps():
                    _verify_image(current_images, uploaded)
            except _ImageError as error:
                if on_image_review is None or page.is_closed():
                    raise
                if not _review_image(page, name, label, current_images, uploaded, error,
                                     on_image_review, manual_selection, result, saved=True):
                    filled.remove(label)
        for label, selector in zip(IMAGE_FIELDS, (SEARCH_IMAGE, SPEC_IMAGE)):
            if label in result.optional_skips.get(name, ()) and row.locator(selector).locator('img').count():
                result.differences.append(f'SKU「{name}」的{label}已跳过，但页面仍有未确认的图片，请在入仓前核对。')
        if 'SKU搜索标题' in filled:
            expect(row.locator(TITLE)).to_contain_text(title, timeout=WAIT_MS)
        result.filled[name] = filled
        getLogger(__name__).info('SKU补充信息已保存并回读：规格=%s；完成字段=%s；跳过=%s', name, filled, result.skipped.get(name, {}))
    return result
