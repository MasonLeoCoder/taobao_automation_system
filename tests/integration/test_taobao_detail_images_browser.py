"""图集顺序、视频封面排除、大图下载及商品资料合并的本地浏览器检查。"""
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.detail_images import collect_main_images
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.detail_images import collect_main_image_links
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID='1050158363561'


def png(index):
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    pixels=b''.join(b'\x00'+bytes((index*30,80,120))*8 for _ in range(8))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',8,8,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')


def photo_url(index):
    return f'https://img.alicdn.com/main{index}.png'


def gallery_html(video=True,count=5,duplicate=False):
    urls=[photo_url(1 if duplicate else i) for i in range(1,count+1)]
    entries=([{'type':'video','url':urls[0]}] if video else [])+[{'type':'image','url':url} for url in urls]+[{'type':'parameters'}]
    thumbs=''
    for index,entry in enumerate(entries):
        content=f'<img class="thumbnailPic--fixture" src="{entry["url"]}_q50.jpg_.webp" width=30 height=30>' if 'url' in entry else '参数'
        thumbs+=f'<div class="thumbnail--fixture {"thumbnailActive--fixture" if index==0 else ""}">{content}</div>'
    main='<div id="mainPicVideoEl">视频封面</div>' if video else f'<img id="mainPicImageEl" src="{urls[0]}" width=200 height=200>'
    tabs=('<div class="switchTabsItem--fixture">视频</div>' if video else '')+'<div class="switchTabsItem--fixture">图集</div><div class="switchTabsItem--fixture">参数</div>'
    return f'''<div id="picGalleryEle"><div class="thumbnails--fixture">{thumbs}</div>
      <div class="mainPicWrap--fixture">{main}</div><div class="bottomSwitchTabsWrap--fixture">{tabs}</div></div>
      <script>
      window.imageClicks=[];window.entries={json.dumps(entries)};
      document.querySelectorAll('.thumbnail--fixture').forEach((thumb,index)=>thumb.onclick=()=>{{
        window.imageClicks.push(index+1);
        document.querySelectorAll('.thumbnail--fixture').forEach(t=>t.classList.remove('thumbnailActive--fixture'));
        thumb.classList.add('thumbnailActive--fixture');
        const entry=window.entries[index];
        if(entry.type==='image'){{
          const update=()=>document.querySelector('.mainPicWrap--fixture').innerHTML='<img id="mainPicImageEl" src="'+(window.wrongSource||entry.url)+'" width=200 height=200>';
          if(window.imageDelay)setTimeout(update,window.imageDelay);else update();
        }}
      }});
      </script>'''


class Response(io.BytesIO):
    def __init__(self,url,body,content_type='image/png'):
        super().__init__(body)
        self.url=url;self.status=200;self.headers=Message()
        self.headers['Content-Type']=content_type
        self.headers['Content-Length']=str(len(body))


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用本地主图采集测试')
class TaobaoDetailImagesBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=sync_playwright().start();cls.browser=cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.runtime.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=replace(load_settings(root=Path(self.temp.name)).for_platform('taobao'),headless=True,timeout_ms=1000,
                              taobao_timing={key:(0,0) for key in DEFAULT_TAOBAO_TIMING})
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.html=gallery_html()

        def route(request):
            if request.request.url.startswith('https://img.alicdn.com/'):
                request.fulfill(status=200,content_type='image/png',body=png(1))
            else:
                request.fulfill(status=200,content_type='text/html; charset=utf-8',body=self.html)

        self.context.route('**/*',route)
        self.page=self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id='+ITEM_ID)
        self.result=DetailPage(self.page,resolve_detail_input(ITEM_ID),'测试商品')
        self.opener=MagicMock()
        self.opener.open.side_effect=lambda request,timeout:Response(request.full_url,png(int(request.full_url.rsplit('main',1)[-1].split('.')[0])))

    def guard(self,attention=None):
        guard=TaobaoGuard(self.context,self.settings.log_dir,attention,timing=self.settings.taobao_timing);guard.login_mode=False
        return guard

    def collect(self):
        with self.guard(),patch('ecommerce.modules.collection.detail_images.build_opener',return_value=self.opener):
            return collect_main_images(self.settings,self.result)

    def replace_gallery(self,**options):
        self.html=gallery_html(**options)
        self.page.reload()

    def test_five_images_exclude_video_cover_and_use_actual_full_image_urls(self):
        record=self.collect()
        self.assertEqual(record['status'],'complete')
        self.assertEqual(record['collected_count'],5)
        self.assertEqual(record['video_cover_skipped'],1)
        self.assertEqual(record['parameters_skipped'],1)
        self.assertEqual([p['slot_index'] for p in record['images']],[2,3,4,5,6])
        self.assertEqual(self.page.evaluate('imageClicks'),[2,3,4,5,6])
        folder=product_directory(self.settings,ITEM_ID)/'1-1主图'
        self.assertEqual(sorted(p.name for p in folder.iterdir()),['1.png','2.png','3.png','4.png','5.png'])
        for index,image in enumerate(record['images'],1):
            self.assertEqual(image['image_url'],photo_url(index))
            self.assertEqual(Path(image['image_path']).read_bytes(),png(index))
            self.assertEqual((image['width'],image['height']),(8,8))
        self.assertEqual(self.opener.open.call_count,5)
        self.assertFalse(list(self.settings.workspace.rglob('*.part')))

    def test_without_video_keeps_first_image(self):
        self.replace_gallery(video=False)
        record=self.collect()
        self.assertEqual(record['video_cover_skipped'],0)
        self.assertEqual([p['slot_index'] for p in record['images']],[1,2,3,4,5])
        self.assertEqual(record['images'][0]['image_url'],photo_url(1))

    def test_repeated_source_urls_keep_all_five_positions(self):
        self.replace_gallery(duplicate=True)
        record=self.collect()
        self.assertEqual(record['collected_count'],5)
        self.assertEqual([p['index'] for p in record['images']],[1,2,3,4,5])
        self.assertEqual(len(set(p['image_url'] for p in record['images'])),1)

    def test_delayed_main_image_does_not_capture_previous_image(self):
        self.page.evaluate('window.imageDelay=150')
        record=self.collect()
        self.assertEqual([p['image_url'] for p in record['images']],[photo_url(i) for i in range(1,6)])

    def test_video_json_is_preserved_and_old_image_batch_remains(self):
        folder=product_directory(self.settings,ITEM_ID)
        save_detail_section(folder,ITEM_ID,'video',{'video_file':ITEM_ID+'.mp4','video_url':'original-video-url'})
        first=self.collect();second=self.collect()
        document=json.loads(Path(second['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['video']['video_url'],'original-video-url')
        self.assertEqual(document['main_images']['collected_count'],5)
        self.assertNotEqual(first['history_record_path'],second['history_record_path'])
        old=json.loads(Path(first['history_record_path']).read_text(encoding='utf-8'))
        self.assertTrue(all(Path(p['image_path']).exists() for p in old['images']))

    def test_corrupt_download_is_not_published_as_five_images(self):
        calls=0
        def response(request,timeout):
            nonlocal calls
            calls+=1
            return Response(request.full_url,png(1)[:40] if calls==3 else png(calls))
        self.opener.open.side_effect=response
        record=self.collect()
        self.assertEqual(record['status'],'download_failed')
        self.assertEqual(record['collected_count'],2)
        self.assertEqual(self.opener.open.call_count,3)
        self.assertFalse((product_directory(self.settings,ITEM_ID)/'1-1主图').exists())
        document=json.loads(Path(record['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['main_images_last_attempt']['status'],'download_failed')

    def test_insufficient_images_are_recorded_as_partial(self):
        self.replace_gallery(count=3)
        record=self.collect()
        self.assertEqual(record['status'],'partial')
        self.assertEqual(record['collected_count'],3)
        self.assertEqual(record['available_count'],3)
        self.assertEqual(len(list((product_directory(self.settings,ITEM_ID)/'1-1主图').iterdir())),3)

    def test_wrong_image_source_requires_manual_review(self):
        self.page.evaluate("window.wrongSource='https://img.alicdn.com/other.png'")
        with self.guard(lambda message:'停止'):
            with self.assertRaises(TaskStopped):
                collect_main_image_links(self.result,100)
        self.assertFalse(list(self.settings.workspace.rglob('main_images.json')))

    def test_metadata_failure_restores_existing_folder_and_json(self):
        first=self.collect()
        original=Path(first['record_path']).read_bytes()
        with self.guard(),patch('ecommerce.modules.collection.detail_images.build_opener',return_value=self.opener),\
                patch('ecommerce.modules.collection.detail_images.save_detail_section',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):collect_main_images(self.settings,self.result)
        self.assertEqual(Path(first['record_path']).read_bytes(),original)
        self.assertTrue(all(Path(p['image_path']).read_bytes()==png(p['index']) for p in first['images']))
        folder=product_directory(self.settings,ITEM_ID)
        self.assertFalse(list(folder.glob('.main-images-*')))


if __name__=='__main__':
    unittest.main()
