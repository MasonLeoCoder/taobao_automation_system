"""复现视频上传后窗口未关闭；人工反馈后接着填表，不重复上传或确认。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from ecommerce.platforms.qianniu import product_video
from ecommerce.platforms.qianniu.popups import PopupGuard
from ecommerce.platforms.qianniu.verification import VerificationGuard


PICKER_HTML = """
<div role="dialog"><input type="file" onchange="uploaded(this)">
<div id="status"></div><button id="complete" hidden onclick="showList()">完成</button>
<div id="list" hidden><div class="VideoList--videoItem--test">
<img src="https://qianniu.test/poster.png"><span id="name"></span>
<label><input type="checkbox">选择</label></div>
<button onclick="confirmed()">确认上传 (1)</button></div></div>
<script>
function uploaded(input){
 parent.uploadCount++; document.querySelector('#name').textContent=input.files[0].name;
 document.querySelector('#status').innerHTML='<span>1 个文件已上传</span><span>发布成功</span>';
 document.querySelector('#complete').hidden=false;
}
function showList(){document.querySelector('#complete').hidden=true; document.querySelector('#list').hidden=false}
function confirmed(){
 parent.confirmCount++;
 parent.document.querySelector('#sell-field-auctionVideos').insertAdjacentHTML('beforeend','<img class="cover" src="https://qianniu.test/poster.png">');
 // 模拟平台进入视频优化窗口；商品已收到视频，但iframe仍在。
 document.body.insertAdjacentHTML('beforeend','<p>比例不符，请人工检查</p>');
}
</script>
"""


@unittest.skipUnless(os.environ.get('QIANNIU_BROWSER_TESTS') == '1', '显式启用本地浏览器测试')
class VideoResumeBrowserTests(unittest.TestCase):
    def test_manual_close_continues_after_video_and_does_not_reupload(self):
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=PICKER_HTML))
                page = context.new_page()
                page.goto('https://qianniu.test/editor')
                page.set_content('''<div id="sell-field-auctionVideos"><button onclick="openPicker()">上传视频</button></div>
                    <input id="next" placeholder="后续字段"><script>
                    window.uploadCount=0; window.confirmCount=0;
                    function openPicker(){let frame=document.createElement('iframe'); frame.id='picker';
                      frame.src='https://qianniu.test/videoSelector.htm'; document.body.append(frame)}
                    </script>''')
                file = Path(folder)/'local.mp4'
                file.write_bytes(b'isolated-video-fixture')
                messages = []
                def feedback(message):
                    messages.append(message)
                    self.assertIn('等待视频窗口关闭', message)
                    self.assertEqual(page.evaluate('uploadCount'), 1)
                    self.assertEqual(page.evaluate('confirmCount'), 1)
                    time.sleep(0.3)
                    page.evaluate('document.querySelector("#picker").remove()')
                popups = PopupGuard(context, feedback, Path(folder))
                with VerificationGuard(context, feedback, popup_guard=popups), patch.object(product_video, 'WAIT_MS', 500):
                    product_video.upload_product_video(page, file)
                    page.locator('#next').fill('继续执行成功')
                self.assertEqual(len(messages), 1)
                self.assertEqual(page.locator('#next').input_value(), '继续执行成功')
                self.assertEqual(page.evaluate('uploadCount'), 1)
                self.assertEqual(page.evaluate('confirmCount'), 1)
                self.assertFalse(page.is_closed())
            finally:
                browser.close()
