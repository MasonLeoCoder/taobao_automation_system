"""图文详情的完整列表、懒加载、数字命名、缓存和整批发布检查。"""
import hashlib
import io
import json
import os
import re
import struct
import tempfile
import unittest
import zlib
from contextlib import contextmanager
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.detail_description import collect_detail_images
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID='1050158363561'


def png(index=1,height=8):
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    pixels=b''.join(b'\x00'+bytes((index*17%255,80,120))*8 for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',8,height,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b''))


def url(index):
    return f'https://img.alicdn.com/description{index}.jpg'


def image(index,lazy=False):
    attributes=f'src="{url(index)}"' if not lazy else f'src="https://img.alicdn.com/loading.png" data-src="{url(index)}"'
    return f'<div class="descV8-singleImage"><img class="descV8-singleImage-image lazyload" {attributes} width="120" height="240"></div>'


def module(content):
    return '<section><p class="tabDetailItemTitle--fixture">图文详情</p><div id="imageTextInfo-container">'+content+'</div></section>'


class Response(io.BytesIO):
    def __init__(self,url,body):
        super().__init__(body)
        self.url,self.status=url,200
        self.headers=Message()
        self.headers['Content-Type']='image/png'
        self.headers['Content-Length']=str(len(body))


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用本地详情图检查')
class TaobaoDetailDescriptionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=sync_playwright().start()
        cls.browser=cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.runtime.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=replace(load_settings(root=Path(self.temp.name)).for_platform('taobao'),headless=True,timeout_ms=1400,
                              taobao_timing={key:(0,0) for key in DEFAULT_TAOBAO_TIMING})
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.body=module(''.join(image(i) for i in range(1,4)))
        self.long_images=set()
        def body_for(request_url):
            match=re.search(r'description([0-9]+)',request_url)
            i=int(match[1]) if match else 0
            return png(i,2500 if i in self.long_images else 8)
        self.body_for=body_for
        def route(route):
            if route.request.url.startswith('https://img.alicdn.com/'):
                route.fulfill(status=200,content_type='image/png',body=body_for(route.request.url))
            else:
                route.fulfill(status=200,content_type='text/html; charset=utf-8',body='<!doctype html><meta charset="utf-8"><span class="site-nav-login-info-nick">测试账号</span>'+self.body)
        self.context.route('**/*',route)
        self.page=self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id='+ITEM_ID)
        self.result=DetailPage(self.page,resolve_detail_input(ITEM_ID),'详情图测试商品')
        self.opener=MagicMock()
        self.opener.open.side_effect=lambda request,timeout:Response(request.full_url,body_for(request.full_url))

    def guard(self,attention=None):
        guard=TaobaoGuard(self.context,self.settings.log_dir,attention,timing=self.settings.taobao_timing)
        guard.login_mode=False
        return guard

    def collect(self,attention=None):
        with self.guard(attention),patch('ecommerce.modules.collection.detail_images.build_opener',return_value=self.opener):
            return collect_detail_images(self.settings,self.result,stable_ms=80)

    def reload(self,body):
        self.body=body;self.page.reload()

    def document(self):
        return json.loads((product_directory(self.settings,ITEM_ID)/(ITEM_ID+'.json')).read_text(encoding='utf-8'))

    def test_dynamic_count_order_actual_format_and_original_bytes(self):
        self.reload(module(''.join(image(i) for i in range(1,10))))
        record=self.collect()
        self.assertEqual(record['status'],'complete')
        self.assertEqual(record['summary']['recognized_count'],9)
        self.assertEqual(record['summary']['downloaded_count'],9)
        self.assertEqual([i['image_url'] for i in record['images']],[url(i) for i in range(1,10)])
        for i in record['images']:
            path=Path(i['image_path'])
            self.assertEqual(path.name,str(i['index'])+'.png')
            self.assertEqual(path.read_bytes(),self.body_for(i['image_url']))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),i['sha256'])
        self.assertGreater(record['scroll_steps'],1)
        raw=(product_directory(self.settings,ITEM_ID)/record['html_file']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),record['html_sha256'])

    def test_duplicate_urls_are_downloaded_once_and_saved_in_each_position(self):
        self.reload(module(image(1)+image(1)+image(2)))
        record=self.collect()
        self.assertEqual(self.opener.open.call_count,2)
        self.assertEqual([i['image_file'] for i in record['images']],['详情图/1.png','详情图/2.png','详情图/3.png'])
        self.assertEqual(record['images'][1]['reused_from_index'],1)

    def test_long_image_dimensions_and_bytes_are_preserved(self):
        self.long_images.add(1)
        self.reload(module(image(1)))
        record=self.collect()
        self.assertEqual(record['images'][0]['height'],2500)
        self.assertEqual(record['images'][0]['width'],8)
        self.assertEqual(Path(record['images'][0]['image_path']).read_bytes(),png(1,2500))

    def test_lazy_images_are_scrolled_and_actual_loaded_url_is_used(self):
        self.reload(module(''.join(image(i,lazy=True) for i in range(1,7)))+'''<script>
         const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){const e=entry.target;
           setTimeout(()=>{e.src=e.dataset.src;observer.unobserve(e)},50)}}));
         document.querySelectorAll('#imageTextInfo-container img').forEach(e=>observer.observe(e));
         </script>''')
        record=self.collect()
        self.assertEqual(record['summary']['recognized_count'],6)
        self.assertTrue(all('loading.png' not in i['image_url'] for i in record['images']))

    def test_more_images_appended_near_end_are_included_before_list_is_fixed(self):
        self.reload(module(''.join(image(i) for i in range(1,5)))+'''<script>
          window.added=false;window.addEventListener('scroll',()=>{const root=document.querySelector('#imageTextInfo-container');
           if(!added&&root.getBoundingClientRect().bottom<innerHeight+100){added=true;root.insertAdjacentHTML('beforeend',EXTRA)}});
          </script>'''.replace('EXTRA',json.dumps(image(5)+image(6))))
        record=self.collect()
        self.assertTrue(self.page.evaluate('added'))
        self.assertEqual(record['summary']['recognized_count'],6)

    def test_one_failed_image_does_not_shift_later_numbers_or_publish_partial_folder(self):
        first=self.collect()
        self.opener.open.reset_mock()
        def response(request,timeout):
            if request.full_url==url(2):raise HTTPError(request.full_url,404,'missing',None,None)
            return Response(request.full_url,self.body_for(request.full_url))
        self.opener.open.side_effect=response
        record=self.collect()
        self.assertEqual(record['status'],'partial')
        self.assertEqual(record['summary']['failed_count'],1)
        self.assertEqual(record['images'][1]['status'],'download_failed')
        self.assertEqual(record['images'][2]['image_file'],'详情图/3.png')
        self.assertEqual(self.document()['detail_images']['batch_id'],first['batch_id'])
        self.assertEqual(self.document()['detail_images_last_attempt']['batch_id'],record['batch_id'])

    def test_failed_duplicate_source_is_not_requested_again(self):
        self.reload(module(image(1)+image(1)+image(2)))
        def response(request,timeout):
            if request.full_url==url(1):raise HTTPError(request.full_url,404,'missing',None,None)
            return Response(request.full_url,png(2))
        self.opener.open.side_effect=response
        record=self.collect()
        self.assertEqual(self.opener.open.call_count,2)
        self.assertEqual(record['images'][1]['reused_failure_from_index'],1)
        self.assertEqual(record['summary']['failed_count'],2)

    def test_corrupt_file_is_not_accepted_or_published(self):
        self.opener.open.side_effect=lambda request,timeout:Response(request.full_url,b'not an image')
        record=self.collect()
        self.assertEqual(record['status'],'partial')
        self.assertEqual(record['summary']['downloaded_count'],0)
        self.assertNotIn('detail_images',self.document())
        self.assertFalse(list(product_directory(self.settings,ITEM_ID).rglob('*.part')))

    def test_oversized_image_requires_manual_feedback_without_compression_or_retry(self):
        self.reload(module(image(1)))
        def response(request,timeout):
            result=Response(request.full_url,png())
            result.headers.replace_header('Content-Length',str(33*1024*1024))
            return result
        self.opener.open.side_effect=response
        prompts=[]
        record=self.collect(lambda message:prompts.append(message) or '继续')
        self.assertEqual(len(prompts),1)
        self.assertEqual(self.opener.open.call_count,1)
        self.assertEqual(record['summary']['failed_count'],1)

    def test_http_403_pauses_and_does_not_retry_that_image(self):
        self.reload(module(image(1)))
        self.opener.open.side_effect=lambda request,timeout:(_ for _ in ()).throw(HTTPError(request.full_url,403,'blocked',None,None))
        prompts=[]
        record=self.collect(lambda message:prompts.append(message) or '继续')
        self.assertEqual(len(prompts),1)
        self.assertEqual(record['status'],'partial')
        self.assertEqual(self.opener.open.call_count,1)

    def test_stop_keeps_per_image_progress_and_previous_files(self):
        calls=0
        def response(request,timeout):
            nonlocal calls
            calls+=1
            if calls==2:raise TaskStopped('人工停止')
            return Response(request.full_url,self.body_for(request.full_url))
        self.opener.open.side_effect=response
        with self.assertRaises(TaskStopped):self.collect()
        folder=product_directory(self.settings,ITEM_ID)
        paths=list(folder.rglob('detail_images-progress.json'))
        self.assertEqual(len(paths),1)
        record=json.loads(paths[0].read_text(encoding='utf-8'))
        self.assertEqual(record['images'][0]['status'],'downloaded')
        self.assertTrue(Path(record['images'][0]['image_path']).is_file())
        self.assertFalse((folder/(ITEM_ID+'.json')).exists())
        self.assertFalse(list(folder.rglob('*.part')))

    def test_replacing_with_fewer_images_removes_stale_latest_files_and_preserves_history(self):
        first=self.collect()
        self.reload(module(image(4)))
        second=self.collect()
        folder=product_directory(self.settings,ITEM_ID)
        self.assertEqual([p.name for p in (folder/'详情图').iterdir()],['1.png'])
        old=json.loads(Path(first['history_record_path']).read_text(encoding='utf-8'))
        self.assertTrue(all(Path(i['image_path']).is_file() for i in old['images']))
        self.assertNotEqual(first['batch_id'],second['batch_id'])

    def test_metadata_failure_restores_previous_complete_folder_and_json(self):
        first=self.collect()
        path=Path(first['record_path']);original=path.read_bytes()
        with patch('ecommerce.modules.collection.detail_description.save_detail_section',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.collect()
        self.assertEqual(path.read_bytes(),original)
        self.assertTrue(all(Path(i['image_path']).is_file() for i in first['images']))
        self.assertFalse(list(product_directory(self.settings,ITEM_ID).glob('.detail-images-*')))

    def test_confirmed_no_images_replaces_latest_without_reusing_old_images(self):
        first=self.collect()
        self.reload(module('暂无图文详情'))
        record=self.collect()
        self.assertEqual(record['status'],'no_images')
        self.assertEqual(record['summary']['recognized_count'],0)
        self.assertEqual(list((product_directory(self.settings,ITEM_ID)/'详情图').iterdir()),[])
        self.assertTrue(Path(first['history_record_path']).is_file())

    def test_unknown_iframe_or_background_image_requires_review(self):
        for content in ('<iframe src="about:blank"></iframe>','<div style="height:40px;background-image:url('+url(1)+')"></div>'):
            with self.subTest(content=content):
                self.reload(module(content))
                with self.assertRaises(TaskStopped):self.collect(lambda message:'停止')
                self.assertEqual(self.opener.open.call_count,0)
                self.assertEqual(self.document()['detail_images_last_attempt']['state'],'unknown_structure')

    def test_empty_unloaded_module_is_not_reported_as_no_images(self):
        self.reload(module('<div style="height:30px"></div>'))
        with self.assertRaises(TaskStopped):self.collect(lambda message:'停止')
        self.assertNotIn('detail_images',self.document())

    def test_expand_and_verification_do_not_repeat_clicks(self):
        self.reload(module('<button id="expand">展开全部详情</button><div id="images" style="display:none">'+image(1)+'</div>')+'''<script>
          window.clicks=0;document.querySelector('#expand').onclick=()=>{clicks++;document.querySelector('#images').style.display='block';
          document.querySelector('#expand').remove();const popup=document.createElement('div');popup.className='J_MIDDLEWARE_FRAME_WIDGET';popup.textContent='请完成安全验证';document.body.append(popup)};
          </script>''')
        prompts=[]
        def attention(message):
            prompts.append(message);self.page.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate('e=>e.remove()');return '继续'
        record=self.collect(attention)
        self.assertEqual(record['status'],'complete')
        self.assertEqual(self.page.evaluate('clicks'),1)
        self.assertEqual(len(prompts),1)

    def test_picture_list_change_after_download_requires_restore_before_continuing(self):
        first=True
        def response(request,timeout):
            nonlocal first
            if first:
                first=False
                self.page.locator('#imageTextInfo-container').evaluate("e=>{const n=document.createElement('img');n.id='unexpected';n.src='"+url(9)+"';e.append(n)}")
            return Response(request.full_url,self.body_for(request.full_url))
        self.opener.open.side_effect=response
        prompts=[]
        def attention(message):
            prompts.append(message);self.page.locator('#unexpected').evaluate('e=>e.remove()');return '继续'
        record=self.collect(attention)
        self.assertEqual(len(prompts),1)
        self.assertEqual(record['summary']['downloaded_count'],3)
        self.assertEqual(self.opener.open.call_count,3)

    def test_other_sections_are_preserved_and_service_runs_only_description_images(self):
        from ecommerce.modules.collection.detail_service import open_detail_input
        from ecommerce.platforms.taobao import session
        folder=product_directory(self.settings,ITEM_ID)
        values={k:{'test':k} for k in ('video','main_images','product_info','sku_details','parameters')}
        for k,v in values.items():save_detail_section(folder,ITEM_ID,k,v)
        @contextmanager
        def browser(settings):yield self.context,self.page
        with patch.object(session,'base_browser_session',browser),patch('ecommerce.modules.collection.detail_images.build_opener',return_value=self.opener):
            message=open_detail_input(self.settings,ITEM_ID,wait=False,detail_images=True)
        self.assertIn('详情图采集完成',message)
        for k,v in values.items():self.assertEqual(self.document()[k],v)


if __name__=='__main__':unittest.main()
