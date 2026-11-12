"""本地模拟千牛主图素材选择和人工恢复，不连接店铺。"""
import os
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from ecommerce.platforms.qianniu import product_images
from ecommerce.platforms.qianniu.popups import PopupGuard
from ecommerce.platforms.qianniu.verification import VerificationGuard
from ecommerce.shared.exceptions import NeedsAttention


EDITOR = """
<div id="sell-field-mainImagesGroup"></div><div class="sell-component-info-wrapper-wrap"></div>
<script>
window.assets = [];
window.opens = [];
window.uploads = 0;
function closePicker(){ document.querySelector('iframe')?.remove(); }
function openPicker(ratio, slot){
    closePicker();
    window.currentSlot = slot;
    opens.push(ratio);
    const frame = document.createElement('iframe');
    frame.src = 'https://qianniu.test/sucai-selector-ng';
    frame.style = 'width:600px;height:240px';
    document.body.append(frame);
}
function renderGroup(group, ratio){
    const label = document.createElement('span');
    label.title = label.textContent = ratio + '主图';
    label.onclick = closePicker;
    group.append(label);
    for (let i = 0; i < 5; i++){
        const slot = document.createElement('div');
        slot.className = 'drag-item';
        slot.innerHTML = '<button class="empty-container">上传图片</button>';
        slot.firstChild.onclick = () => openPicker(ratio, slot);
        group.append(slot);
    }
}
function choose(name){
    const image = document.createElement('img');
    image.src = 'https://qianniu.test/images/' + name;
    currentSlot.replaceChildren(image);
    closePicker();
}
renderGroup(document.querySelector('#sell-field-mainImagesGroup'), '1:1');
renderGroup(document.querySelector('.sell-component-info-wrapper-wrap'), '3:4');
</script>
"""

PICKER = """
<main id="picker"></main>
<script>
const root = document.querySelector('#picker');
function gallery(){
    root.innerHTML = '<button onclick="openUpload()">本地上传</button>';
    for (const name of parent.assets){
        const tile = document.createElement('div');
        tile.className = 'PicList_pic_background-test';
        const button = document.createElement('div');
        button.className = 'PicList_pic_imgBox-test';
        button.innerHTML = '<img width="24" height="24">';
        button.firstChild.src = 'https://qianniu.test/images/' + name;
        button.onclick = () => parent.choose(name);
        const label = document.createElement('span');
        label.textContent = name;
        tile.append(button, label);
        root.append(tile);
    }
}
function openUpload(){
    root.innerHTML = '<div class="next-dialog UploadDialog_dialog__test" role="dialog">'
        + '<h2>上传素材</h2><button onclick="selectFiles()">点击/拖拽，批量导入文件</button></div>';
    if (parent.interruptBefore && parent.opens.length === 1)
        root.firstChild.insertAdjacentHTML('beforeend','<div role="alertdialog"><h3>测试遮挡</h3></div>');
}
function selectFiles(){
    const field = document.createElement('input');
    field.type = 'file';
    field.multiple = true;
    field.hidden = true;
    field.onchange = () => {
        parent.uploads++;
        parent.assets = Array.from(field.files, file => file.name);
        if (parent.partialBatch) parent.assets.pop();
        if (parent.closeAfterUpload) parent.closePicker();
        else root.firstChild.insertAdjacentHTML('beforeend','<p>5 个文件上传成功</p><button onclick="gallery()">完成</button>');
    };
    root.firstChild.append(field);
    field.click();
}
gallery();
</script>
"""


@unittest.skipUnless(os.environ.get('QIANNIU_BROWSER_TESTS') == '1', '显式启用本地浏览器测试')
class MainImageResumeBrowserTests(unittest.TestCase):
    def test_main_images_resume_and_upload_exactly_once(self):
        cases = [('1:1', False, False, False), ('1:1', True, False, False),
                 ('3:4', False, True, False), ('3:4', False, True, True)]
        with TemporaryDirectory() as folder, sync_playwright() as playwright, ExitStack() as cleanup:
            files = []
            for index in range(5):
                path = Path(folder) / f'{index}.png'
                path.write_bytes(b'fixture')
                files.append(path)
            browser = playwright.chromium.launch(headless=True)
            cleanup.callback(browser.close)
            for ratio, interrupt_before, close_after, partial in cases:
                with self.subTest(ratio=ratio, interrupt_before=interrupt_before, close_after=close_after, partial=partial):
                    context = browser.new_context()
                    context.route('**/editor', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=EDITOR))
                    context.route('**/sucai-selector-ng', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=PICKER))
                    context.route('**/images/**', lambda route: route.fulfill(status=204, body=''))
                    page = context.new_page()
                    page.goto('https://qianniu.test/editor')
                    page.evaluate('([before, after, partial]) => { window.interruptBefore = before; window.closeAfterUpload = after; window.partialBatch = partial; }',
                                  [interrupt_before, close_after, partial])
                    prompts = []

                    def feedback(message):
                        prompts.append(message)
                        self.assertIn('测试遮挡', message)
                        self.assertEqual(page.evaluate('uploads'), 0)
                        page.evaluate('closePicker()')
                        return '继续'

                    guard = PopupGuard(context, feedback, Path(folder))
                    with VerificationGuard(context, feedback, popup_guard=guard), patch.object(product_images, 'WAIT_MS', 1500):
                        if partial:
                            with self.assertRaisesRegex(NeedsAttention, '本批素材未能唯一核对'):
                                product_images.upload_main_images(page, files, aspect_ratio=ratio)
                        else:
                            product_images.upload_main_images(page, files, aspect_ratio=ratio)
                    self.assertEqual(len(prompts), int(interrupt_before))
                    self.assertEqual(page.evaluate('uploads'), 1)
                    opened = page.evaluate('opens')
                    self.assertEqual(set(opened), {ratio})
                    if (interrupt_before or close_after) and not partial:
                        self.assertGreaterEqual(len(opened), 7)
                    images = product_images._image_group(page, ratio).locator('img')
                    sources = images.evaluate_all('images => images.map(image => image.src.split("/").at(-1))')
                    self.assertEqual(sources, [] if partial else page.evaluate('assets'))
                    context.close()
