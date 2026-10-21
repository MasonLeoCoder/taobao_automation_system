"""主图视频检测与流式下载边界，页面和媒体响应全部在本地构造。"""
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.detail_video import collect_main_video, validate_media_url
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.detail_video import detect_main_video
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID = "1050158363561"
VIDEO_URL = "https://video-sh.cloudvideocdn.taobao.com/sample.mp4?auth_key=local-test"
IMAGE = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="2" height="2"%3E%3C/svg%3E'
MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 4096


def gallery(video=True):
    media = f'<div id="mainPicVideoEl" class="mainPicVideo--fixture"><video src="{VIDEO_URL}"></video></div>' if video else ''
    return f'''<div id="picGalleryEle"><div class="thumbnails--fixture">
      <div class="thumbnail--fixture thumbnailActive--fixture"><img src='{IMAGE}' width=20 height=20></div>
      <div class="thumbnail--fixture"><img src='{IMAGE}' width=20 height=20></div></div>
      <div class="mainPicWrap--fixture">{media}<img src='{IMAGE}' width=200 height=200></div></div>'''


class MediaResponse(io.BytesIO):
    def __init__(self, body=MP4, content_type="video/mp4", length=None):
        super().__init__(body)
        self.url = VIDEO_URL
        self.status = 200
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        self.headers['Content-Length'] = str(len(body) if length is None else length)


@unittest.skipUnless(os.environ.get("TAOBAO_BROWSER_TESTS") == "1", "显式启用本地主图视频测试")
class TaobaoDetailVideoBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(load_settings(root=Path(self.temp.name)).for_platform("taobao"), headless=True,
                                timeout_ms=1000, taobao_timing={key:(0,0) for key in DEFAULT_TAOBAO_TIMING})
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='video/mp4', body=MP4)
                           if url_is_media(route.request.url) else route.fulfill(status=200, content_type='text/html', body='<main>详情页</main>'))
        self.page = self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id=' + ITEM_ID)
        self.result = DetailPage(self.page, resolve_detail_input(ITEM_ID), '测试商品')

    def body(self, html):
        self.page.locator('body').evaluate('(e,html)=>e.innerHTML=html', html)
        self.page.wait_for_function('''() => [...document.querySelectorAll('img')].every(i=>i.complete&&i.naturalWidth>0)''')

    def guard(self, attention=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def test_video_download_saves_file_and_original_url(self):
        self.body(gallery())
        opener = MagicMock()
        opener.open.return_value = MediaResponse()
        with self.guard(), patch('ecommerce.modules.collection.detail_video.build_opener', return_value=opener):
            record = collect_main_video(self.settings, self.result)
        self.assertEqual(record['video_status'], 'downloaded')
        self.assertEqual(record['video_url'], VIDEO_URL)
        self.assertEqual(Path(record['video_path']).read_bytes(), MP4)
        self.assertEqual(record['video_size_bytes'], len(MP4))
        self.assertFalse(record['skipped'])
        self.assertEqual(Path(record['video_path']).name, ITEM_ID+'.mp4')
        self.assertEqual(Path(record['video_path']).parent.name, ITEM_ID)
        self.assertEqual(Path(record['record_path']).name, ITEM_ID+'.json')
        self.assertEqual(json.loads(Path(record['record_path']).read_text(encoding='utf-8'))['video']['video_status'], 'downloaded')
        self.assertEqual(Path(record['history_record_path']).parent.parent.name, 'history')
        self.assertFalse(list(self.settings.workspace.rglob('*.part')))
        opener.open.assert_called_once()

    def test_repeated_collection_keeps_history_and_other_detail_sections(self):
        self.body(gallery())
        records=[]
        with self.guard(), patch('ecommerce.modules.collection.detail_video.build_opener') as factory:
            for index, body in enumerate((MP4, MP4+b'new-version')):
                factory.return_value.open.return_value=MediaResponse(body)
                record=collect_main_video(self.settings,self.result)
                records.append(record)
                if index==0:
                    document=json.loads(Path(record['record_path']).read_text(encoding='utf-8'))
                    document['attributes']={'材质':'塑料'}
                    Path(record['record_path']).write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
        current=json.loads(Path(records[-1]['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(current['attributes'],{'材质':'塑料'})
        self.assertEqual(current['video']['batch_id'],records[-1]['batch_id'])
        self.assertEqual(Path(records[-1]['video_path']).read_bytes(),MP4+b'new-version')
        old=json.loads(Path(records[0]['history_record_path']).read_text(encoding='utf-8'))
        self.assertEqual(Path(old['video_path']).read_bytes(),MP4)
        self.assertNotEqual(records[0]['history_record_path'],records[1]['history_record_path'])

    def test_failed_metadata_update_restores_previous_video(self):
        self.body(gallery())
        with self.guard(), patch('ecommerce.modules.collection.detail_video.build_opener') as factory:
            factory.return_value.open.return_value=MediaResponse()
            first=collect_main_video(self.settings,self.result)
            original=Path(first['record_path']).read_bytes()
            factory.return_value.open.return_value=MediaResponse(MP4+b'new-version')
            with patch('ecommerce.modules.collection.detail_video.save_detail_section',side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    collect_main_video(self.settings,self.result)
        self.assertEqual(Path(first['video_path']).read_bytes(),MP4)
        self.assertEqual(Path(first['record_path']).read_bytes(),original)
        self.assertFalse(list(self.settings.workspace.rglob('*.backup')))

    def test_later_no_video_retains_old_history_without_stale_latest_file(self):
        self.body(gallery())
        with self.guard(), patch('ecommerce.modules.collection.detail_video.build_opener') as factory:
            factory.return_value.open.return_value=MediaResponse()
            first=collect_main_video(self.settings,self.result)
            self.body(gallery(False))
            second=collect_main_video(self.settings,self.result)
        self.assertEqual(second['video_status'],'no_video')
        self.assertFalse(Path(first['video_path']).exists())
        old=json.loads(Path(first['history_record_path']).read_text(encoding='utf-8'))
        self.assertEqual(Path(old['video_path']).read_bytes(),MP4)
        current=json.loads(Path(second['record_path']).read_text(encoding='utf-8'))
        self.assertFalse(current['video']['video_present'])

    def test_no_video_skips_download_and_ignores_video_elsewhere_on_page(self):
        self.body(gallery(False) + f'<section>推荐商品<video src="{VIDEO_URL}"></video></section>')
        with self.guard(), patch('ecommerce.modules.collection.detail_video.build_opener') as opener:
            record = collect_main_video(self.settings, self.result)
        self.assertEqual(record['video_status'], 'no_video')
        self.assertFalse(record['video_present'])
        self.assertTrue(record['skipped'])
        self.assertIsNone(record['video_path'])
        self.assertEqual(record['message'], '无视频，已跳过主图视频采集')
        opener.assert_not_called()

    def test_source_loading_is_not_reported_as_no_video(self):
        self.body(gallery().replace(f'src="{VIDEO_URL}"', ''))
        messages = []

        def attention(message):
            messages.append(message)
            self.page.locator('video').evaluate('(e,url)=>e.src=url', VIDEO_URL)
            return '继续'

        with self.guard(attention):
            value = detect_main_video(self.result, 100)
        self.assertEqual(value['status'], 'found')
        self.assertEqual(value['url'], VIDEO_URL)
        self.assertEqual(len(messages), 1)
        self.assertIn('尚未取得', messages[0])

    def test_first_thumbnail_is_selected_once_when_another_image_is_active(self):
        self.body(gallery(False))
        self.page.evaluate('''url=>{
          const thumbs=document.querySelectorAll('.thumbnail--fixture');
          thumbs[0].classList.remove('thumbnailActive--fixture');thumbs[1].classList.add('thumbnailActive--fixture');
          window.thumbClicks=0;thumbs[0].onclick=()=>{
            window.thumbClicks++;thumbs[0].classList.add('thumbnailActive--fixture');thumbs[1].classList.remove('thumbnailActive--fixture');
            document.querySelector('.mainPicWrap--fixture').insertAdjacentHTML('afterbegin','<div id="mainPicVideoEl"><video src="'+url+'"></video></div>');
          };
        }''', VIDEO_URL)
        with self.guard():
            value = detect_main_video(self.result, 1000)
        self.assertEqual(value['status'], 'found')
        self.assertEqual(self.page.evaluate('thumbClicks'), 1)

    def test_unknown_gallery_pauses_instead_of_skipping(self):
        with self.guard(lambda message:'停止'):
            with self.assertRaises(TaskStopped):
                collect_main_video(replace(self.settings,timeout_ms=100), self.result)
        self.assertFalse(list(self.settings.workspace.rglob('video.json')))

    def test_blob_source_remains_video_and_is_not_reported_as_absent(self):
        self.body(gallery().replace(VIDEO_URL, 'blob:https://item.taobao.com/local-id'))
        with self.guard(), patch('ecommerce.modules.collection.detail_video.build_opener') as opener:
            record = collect_main_video(self.settings, self.result)
        self.assertTrue(record['video_present'])
        self.assertFalse(record['skipped'])
        self.assertEqual(record['video_status'], 'unsupported_source')
        opener.assert_not_called()

    def test_html_or_truncated_download_is_not_a_success_or_no_video(self):
        self.body(gallery())
        for response in (MediaResponse(b'<html>verify</html>', content_type='text/html'), MediaResponse(length=len(MP4)+1)):
            with self.subTest(response=response), self.guard(), patch('ecommerce.modules.collection.detail_video.build_opener') as factory:
                factory.return_value.open.return_value = response
                record = collect_main_video(self.settings, self.result)
                self.assertEqual(record['video_status'], 'download_failed')
                self.assertTrue(record['video_present'])
                self.assertFalse(record['skipped'])
                self.assertIsNone(record['video_path'])
                factory.return_value.open.assert_called_once()
                self.assertFalse(list(self.settings.workspace.rglob('*.part')))

    def test_stop_during_download_cleans_partial_file(self):
        self.body(gallery())
        with self.guard() as guard, patch('ecommerce.modules.collection.detail_video.build_opener') as factory:
            factory.return_value.open.return_value = MediaResponse()
            original = guard.checkpoint
            calls = 0

            def checkpoint(step=None):
                nonlocal calls
                if step == '下载主图视频':
                    calls += 1
                    if calls == 2:
                        raise TaskStopped('用户停止')
                return original(step)

            with patch.object(guard,'checkpoint',side_effect=checkpoint), self.assertRaises(TaskStopped):
                collect_main_video(self.settings,self.result)
        self.assertFalse(list(self.settings.workspace.rglob('*.part')))
        self.assertFalse(list(self.settings.workspace.rglob(ITEM_ID+'.mp4')))

    def test_media_urls_reject_nonmedia_origins(self):
        self.assertEqual(validate_media_url(VIDEO_URL), VIDEO_URL)
        for url in ('file:///a.mp4','http://127.0.0.1/a.mp4','https://taobao.com.evil.example/a.mp4','blob:https://item.taobao.com/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_media_url(url)


def url_is_media(url):
    return url.startswith('https://video-sh.cloudvideocdn.taobao.com/')


if __name__ == '__main__':
    unittest.main()
