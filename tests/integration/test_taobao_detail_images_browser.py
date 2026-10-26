"""页面主图列表、图集核对、逐张进度恢复及整批发布的本地浏览器检查。"""
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
from ecommerce.platforms.taobao.detail_images import DOM_IMAGES_SOURCE, collect_main_image_links
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
    return f'https://img.alicdn.com/main{index}.png_q50.jpg_.webp'


def gallery_html(video=True,count=5,duplicate=False):
    urls=[photo_url(1 if duplicate else i) for i in range(1,count+1)]
    entries=([{'type':'video','url':urls[0]}] if video else [])+[{'type':'image','url':url} for url in urls]+[{'type':'parameters'}]
    thumbs=''
    for index,entry in enumerate(entries):
        content=f'<img class="thumbnailPic--fixture" src="{entry["url"]}" width=30 height=30>' if 'url' in entry else '参数'
        thumbs+=f'<div class="thumbnail--fixture {"thumbnailActive--fixture" if index==0 else ""}">{content}</div>'
    main='<div id="mainPicVideoEl">视频封面</div>' if video else f'<img id="mainPicImageEl" src="{urls[0]}" width=200 height=200>'
    tabs=('<div class="switchTabsItem--fixture">视频</div>' if video else '')+'<div class="switchTabsItem--fixture">图集</div><div class="switchTabsItem--fixture">参数</div>'
    return f'''<div id="picGalleryEle"><div class="thumbnails--fixture">{thumbs}</div>
      <div class="mainPicWrap--fixture">{main}</div><div class="bottomSwitchTabsWrap--fixture">{tabs}</div></div>
      <script>
      window.__ICE_APP_CONTEXT__={{loaderData:{{home:{{data:{{ssrItemId:'{ITEM_ID}',res:{{
        item:{{itemId:'{ITEM_ID}',images:{json.dumps(urls)}}},componentsVO:{{headImageVO:{{images:{json.dumps(urls)}}}}}
      }}}}}}}}}};
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

    def test_six_dom_images_deduplicate_first_pair_and_number_five_images(self):
        record=self.collect()
        self.assertEqual(record['status'],'complete')
        self.assertEqual(record['collected_count'],5)
        self.assertEqual(record['video_cover_skipped'],0)
        self.assertEqual((record['raw_image_count'],record['duplicate_count']),(6,1))
        self.assertEqual(record['schema_version'],3)
        self.assertEqual(record['parameters_skipped'],1)
        self.assertEqual([p['slot_index'] for p in record['images']],[1,3,4,5,6])
        self.assertEqual(self.page.evaluate('imageClicks'),[])
        self.assertEqual(record['collection_method'],'dom_thumbnails')
        self.assertEqual(record['source_path'],DOM_IMAGES_SOURCE)
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

    def test_user_provided_dom_six_images_become_five_ordered_files(self):
        """复现用户给出的DOM地址与节点结构，去重后按首次出现编号；参数：无；返回：无。"""
        urls=[
            '//img.alicdn.com/imgextra/i1/2218637507912/O1CN01hcQDq628JkpRRe2Ox_!!4611686018427387208-0-item_pic.jpg_q50.jpg_.webp',
            '//img.alicdn.com/imgextra/i3/2218637507912/O1CN01kdxMSS28JkpT7pwcj_!!2218637507912.jpg_q50.jpg_.webp',
            '//img.alicdn.com/imgextra/i1/2218637507912/O1CN01GwdOjM28JkpRf5CdK_!!2218637507912.jpg_q50.jpg_.webp',
            '//img.alicdn.com/imgextra/i4/2218637507912/O1CN01ONie7x28JkpRinhv5_!!2218637507912.jpg_q50.jpg_.webp',
            '//img.alicdn.com/imgextra/i2/2218637507912/O1CN01I3LKlS28JkpT7poKa_!!2218637507912.jpg_q50.jpg_.webp']
        entries=''.join(f'<div class="thumbnail--TxeB1sWz"><div class="thumbnailItem--WQyauvvr"><img src="{url}" class="thumbnailPic--QasTmWDm"></div></div>' for url in [urls[0],*urls])
        self.html='<div id="picGalleryEle"><div class="thumbnails--v976to2t">'+entries+'<div class="thumbnail--TxeB1sWz"><div class="thumbnailItem--WQyauvvr thumbnailItemOther--sDO8U6H8"><div>参数</div></div></div></div></div>'
        self.page.reload()
        absolute=['https:'+url for url in urls]
        self.opener.open.side_effect=lambda request,timeout:Response(request.full_url,png(absolute.index(request.full_url)+1))
        record=self.collect()
        self.assertEqual(record['collected_count'],5)
        self.assertEqual([image['image_url'] for image in record['images']],absolute)
        self.assertEqual([image['index'] for image in record['images']],[1,2,3,4,5])
        self.assertEqual([image['slot_index'] for image in record['images']],[1,3,4,5,6])
        self.assertEqual(record['duplicate_count'],1)
        self.assertEqual([Path(image['image_path']).name for image in record['images']],['1.png','2.png','3.png','4.png','5.png'])
        self.assertEqual(self.opener.open.call_count,5)

    def test_conflicting_page_data_never_overrides_dom_download_addresses(self):
        """两套页面数据不同仍只下载DOM实际地址；参数：无；返回：无。"""
        self.page.evaluate('''()=>{
          const res=window.__ICE_APP_CONTEXT__.loaderData.home.data.res;
          res.item.images=['https://img.alicdn.com/unrelated.png'];res.componentsVO.headImageVO.images=[];
        }''')
        record=self.collect()
        self.assertEqual(record['status'],'complete');self.assertEqual(record['source_path'],DOM_IMAGES_SOURCE)
        self.assertEqual([row['image_url'] for row in record['images']],[photo_url(i) for i in range(1,6)])
        self.assertEqual(self.page.evaluate('imageClicks'),[]);self.assertEqual(self.opener.open.call_count,5)

    def test_invalid_dom_resume_keeps_specific_reason_until_address_is_restored(self):
        """DOM无效地址恢复前保持暂停并记录位置；参数：无；返回：无。"""
        self.page.evaluate("document.querySelectorAll('.thumbnailPic--fixture')[3].src='https://example.com/other.png'")
        messages=[]
        def attention(message):
            messages.append(message)
            if len(messages)==2:
                self.assertTrue(message.startswith('【检查尚未通过】'));self.assertIn('DOM',message)
                self.assertNotIn('登录状态',message)
                self.page.evaluate("url=>document.querySelectorAll('.thumbnailPic--fixture')[3].src=url",photo_url(3))
            return '继续'
        with self.guard(attention) as guard:
            manifest=collect_main_image_links(self.result,100)
            events=[json.loads(row) for row in guard.event_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(manifest['images']),5);self.assertEqual(len(messages),2)
        failures=[row for row in events if row.get('event')=='blocked' and row.get('check_details')]
        self.assertTrue(failures)
        self.assertTrue(any(image['slot_index']==4 and image['image_url'] is None for row in failures for image in row['check_details']['dom_images']))
        self.assertEqual(self.page.evaluate('imageClicks'),[])

    def test_all_duplicate_dom_urls_save_only_first_image(self):
        self.replace_gallery(duplicate=True)
        record=self.collect()
        self.assertEqual(record['collected_count'],1)
        self.assertEqual([p['index'] for p in record['images']],[1])
        self.assertEqual(len(set(p['image_url'] for p in record['images'])),1)
        self.assertEqual(self.opener.open.call_count,1)

    def test_current_large_image_can_be_wrong_or_loading_without_affecting_list(self):
        self.page.evaluate('''()=>{window.imageDelay=150;
          document.querySelector('.mainPicWrap--fixture').innerHTML='<img id="mainPicImageEl" src="https://img.alicdn.com/other.png">'}''')
        record=self.collect()
        self.assertEqual([p['image_url'] for p in record['images']],[photo_url(i) for i in range(1,6)])
        self.assertEqual(self.page.evaluate('imageClicks'),[])

    def test_video_json_is_preserved_and_old_image_batch_remains(self):
        folder=product_directory(self.settings,ITEM_ID)
        save_detail_section(folder,ITEM_ID,'video',{'video_file':ITEM_ID+'.mp4','video_url':'original-video-url'})
        first=self.collect();second=self.collect()
        document=json.loads(Path(second['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['video']['video_url'],'original-video-url')
        self.assertEqual(document['main_images']['collected_count'],5)
        self.assertNotEqual(first['history_record_path'],second['history_record_path'])
        self.assertEqual(second['reused_count'],0)
        self.assertEqual(self.opener.open.call_count,10)
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

    def test_three_images_are_complete_when_all_available_images_are_saved(self):
        self.replace_gallery(count=3)
        record=self.collect()
        self.assertEqual(record['status'],'complete')
        self.assertEqual(record['collected_count'],3)
        self.assertEqual(record['available_count'],3)
        self.assertEqual(record['expected_count'],3)
        self.assertEqual(len(list((product_directory(self.settings,ITEM_ID)/'1-1主图').iterdir())),3)

    def test_single_image_is_complete_with_or_without_video(self):
        for video in (False,True):
            with self.subTest(video=video):
                self.replace_gallery(count=1,video=video)
                record=self.collect()
                self.assertEqual(record['status'],'complete')
                self.assertEqual((record['expected_count'],record['available_count'],record['collected_count']),(1,1,1))
                self.assertEqual(record['images'][0]['slot_index'],1)

    def test_more_than_five_images_are_not_truncated(self):
        self.replace_gallery(count=8)
        record=self.collect()
        self.assertEqual(record['status'],'complete')
        self.assertEqual((record['expected_count'],record['available_count'],record['collected_count']),(8,8,8))
        self.assertEqual([p['image_url'] for p in record['images']],[photo_url(i) for i in range(1,9)])
        self.assertEqual(self.opener.open.call_count,8)

    def test_shorter_gallery_replaces_latest_but_preserves_longer_history(self):
        self.replace_gallery(count=7)
        first=self.collect()
        self.replace_gallery(count=3)
        second=self.collect()
        folder=product_directory(self.settings,ITEM_ID)/'1-1主图'
        self.assertEqual(sorted(p.name for p in folder.iterdir()),['1.png','2.png','3.png'])
        self.assertEqual(second['status'],'complete')
        self.assertEqual(second['expected_count'],3)
        self.assertTrue(all(Path(p['image_path']).is_file() for p in json.loads(Path(first['history_record_path']).read_text(encoding='utf-8'))['images']))

    def test_empty_or_unloaded_gallery_is_not_published_as_complete(self):
        self.html='<div id="picGalleryEle"><div class="thumbnails--fixture"><div class="thumbnail--fixture">参数</div></div><div class="mainPicWrap--fixture"></div></div>'
        self.page.reload()
        with self.guard(lambda _:'停止'),self.assertRaises(TaskStopped):
            collect_main_image_links(self.result,100)
        self.assertFalse(list(self.settings.workspace.rglob('main_images.json')))

    def test_parameter_panel_and_hover_handlers_are_not_used_to_get_images(self):
        self.replace_gallery(count=3,video=False)
        self.page.evaluate('''()=>{window.parameterSwitches=0;const thumbs=[...document.querySelectorAll('.thumbnail--fixture')];
          const last=thumbs[thumbs.length-2],params=thumbs[thumbs.length-1];let timer;
          last.onmouseenter=()=>{timer=setTimeout(()=>{if(!last.matches(':hover'))return;window.parameterSwitches++;
            thumbs.forEach(t=>t.classList.remove('thumbnailActive--fixture'));params.classList.add('thumbnailActive--fixture');
            document.querySelector('.mainPicWrap--fixture').innerHTML='参数'},100)};
          last.onmouseleave=()=>clearTimeout(timer)}''')
        self.page.evaluate('''()=>{document.querySelector('.mainPicWrap--fixture').innerHTML='参数';
          const thumbs=[...document.querySelectorAll('.thumbnail--fixture')];
          thumbs.forEach(t=>t.classList.remove('thumbnailActive--fixture'));thumbs.at(-1).classList.add('thumbnailActive--fixture')}''')
        record=self.collect()
        self.assertEqual(record['status'],'complete')
        self.assertEqual(record['collected_count'],3)
        self.assertEqual(self.page.evaluate('window.parameterSwitches'),0)
        self.assertEqual(self.page.evaluate('imageClicks'),[])
        self.assertEqual(self.page.locator('.mainPicWrap--fixture').inner_text(),'参数')

    def test_main_image_uses_provided_list_instead_of_click_driven_transformed_urls(self):
        self.replace_gallery(count=2)
        self.settings=replace(self.settings,timeout_ms=1800)
        self.page.evaluate('''()=>document.querySelectorAll('.thumbnail--fixture').forEach((thumb,i)=>{
          const click=thumb.onclick;thumb.onclick=()=>{click();const entry=window.entries[i];if(entry.type==='image')
            setTimeout(()=>document.querySelector('#mainPicImageEl').src=entry.url+'?quality=final',350)}})''')
        record=self.collect()
        self.assertEqual([p['image_url'] for p in record['images']],[photo_url(i) for i in (1,2)])
        self.assertEqual(self.page.evaluate('imageClicks'),[])

    def test_wrong_product_url_requires_manual_review(self):
        self.page.goto('https://item.taobao.com/item.htm?id=999')
        with self.guard(lambda message:'停止'):
            with self.assertRaises(TaskStopped):
                collect_main_image_links(self.result,100)
        self.assertFalse(list(self.settings.workspace.rglob('main_images.json')))

    def test_metadata_failure_restores_existing_folder_and_json(self):
        first=self.collect()
        original=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))['main_images']
        with self.guard(),patch('ecommerce.modules.collection.detail_images.build_opener',return_value=self.opener),\
                patch('ecommerce.modules.collection.detail_images.save_detail_sections',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):collect_main_images(self.settings,self.result)
        document=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['main_images'],original)
        self.assertEqual(document['main_images_last_attempt']['status'],'interrupted')
        self.assertTrue(all(Path(p['image_path']).read_bytes()==png(p['index']) for p in first['images']))
        folder=product_directory(self.settings,ITEM_ID)
        self.assertFalse(list(folder.glob('.main-images-*')))

    def test_dom_collection_does_not_require_page_data_or_video_tabs(self):
        self.page.evaluate("delete window.__ICE_APP_CONTEXT__;document.querySelector('.bottomSwitchTabsWrap--fixture').remove()")
        record=self.collect()
        self.assertEqual(record['source_path'],DOM_IMAGES_SOURCE)
        self.assertEqual(record['collected_count'],5)
        self.assertEqual(self.page.evaluate('imageClicks'),[])

    def test_missing_ambiguous_or_invalid_dom_pauses_without_click_or_download(self):
        cases=[
            ("document.querySelector('#picGalleryEle').remove()",'画廊'),
            ("document.querySelector('.thumbnails--fixture').remove()",'结构'),
            ("document.querySelector('.thumbnails--fixture').innerHTML='参数'",'尚未提供图片'),
            ("document.querySelectorAll('.thumbnailPic--fixture')[0].src='https://example.com/main1.png'",'图片地址'),
            ("document.querySelectorAll('.thumbnailPic--fixture')[0].removeAttribute('src')",'图片地址'),
            ("document.querySelector('#picGalleryEle').append(document.querySelector('.thumbnails--fixture').cloneNode(true))",'结构'),
        ]
        for expression,reason in cases:
            with self.subTest(reason=reason,expression=expression):
                self.page.reload();self.page.evaluate(expression)
                messages=[]
                with self.guard(lambda message:messages.append(message) or '停止'),self.assertRaises(TaskStopped):
                    collect_main_image_links(self.result,50)
                self.assertTrue(any(reason in message for message in messages))
                self.assertEqual(self.page.evaluate('imageClicks'),[])
        self.opener.open.assert_not_called()

    def test_thumbnail_bytes_do_not_need_to_load_for_source_order_validation(self):
        self.context.route('**/*_q50.jpg_.webp',lambda route:route.abort())
        self.page.reload()
        self.assertTrue(self.page.locator('.thumbnailPic--fixture').first.evaluate('e=>e.complete&&!e.naturalWidth'))
        record=self.collect()
        self.assertEqual(record['status'],'complete')
        self.assertEqual(self.page.evaluate('imageClicks'),[])

    def fail_third_download(self):
        """制造下载中断样例，返回已成功保存两张的批次。"""
        original=self.opener.open.side_effect
        def response(request,timeout):
            if request.full_url==photo_url(3):
                return Response(request.full_url,png(3)[:40])
            return original(request,timeout)
        self.opener.open.side_effect=response
        first=self.collect()
        self.opener.open.side_effect=original
        self.opener.open.reset_mock()
        return first

    def test_failed_download_persists_each_status_and_resumes_only_unfinished_images(self):
        first=self.fail_third_download()
        progress=json.loads(Path(first['history_record_path']).read_text(encoding='utf-8'))
        self.assertEqual(progress['collected_count'],2)
        self.assertEqual([p['status'] for p in progress['image_results']],['downloaded','downloaded','failed','pending','pending'])
        second=self.collect()
        self.assertEqual(second['status'],'complete')
        self.assertEqual((second['collected_count'],second['reused_count'],second['downloaded_count']),(5,2,3))
        self.assertEqual(second['resumed_from_batch_id'],first['batch_id'])
        self.assertEqual([call.args[0].full_url for call in self.opener.open.call_args_list],[photo_url(i) for i in (3,4,5)])
        self.assertTrue(all(Path(p['image_path']).is_file() for p in first['images']))

    def test_modified_history_image_is_redownloaded_instead_of_reused(self):
        first=self.fail_third_download()
        Path(first['images'][0]['image_path']).write_bytes(b'damaged')
        second=self.collect()
        self.assertEqual(second['reused_count'],1)
        self.assertEqual([call.args[0].full_url for call in self.opener.open.call_args_list],[photo_url(i) for i in (1,3,4,5)])

    def test_changed_source_address_does_not_reuse_old_batch(self):
        self.fail_third_download()
        self.page.evaluate("document.querySelectorAll('.thumbnailPic--fixture').forEach(img=>img.src=img.getAttribute('src')+'?revision=2')")
        record=self.collect()
        self.assertIsNone(record['resumed_from_batch_id'])
        self.assertEqual(record['reused_count'],0)
        self.assertEqual(self.opener.open.call_count,5)

    def test_cancel_mid_file_keeps_completed_progress_and_cleans_partial_file(self):
        original=self.opener.open.side_effect
        class InterruptedResponse(Response):
            def read(self,size=-1):
                if self.tell():
                    raise TaskStopped('用户停止')
                return super().read(size)
        def response(request,timeout):
            return InterruptedResponse(request.full_url,png(3)) if request.full_url==photo_url(3) else original(request,timeout)
        self.opener.open.side_effect=response
        with self.assertRaises(TaskStopped):
            self.collect()
        document=json.loads((product_directory(self.settings,ITEM_ID)/f'{ITEM_ID}.json').read_text(encoding='utf-8'))
        self.assertEqual(document['main_images_last_attempt']['status'],'interrupted')
        self.assertEqual(document['main_images_last_attempt']['collected_count'],2)
        self.assertEqual(document['main_images_last_attempt']['image_results'][2]['status'],'interrupted')
        self.assertFalse(list(self.settings.workspace.rglob('*.part')))
        self.opener.open.side_effect=original;self.opener.open.reset_mock()
        record=self.collect()
        self.assertEqual(record['reused_count'],2)
        self.assertEqual(self.opener.open.call_count,3)

    def test_cancel_during_resume_keeps_access_to_successful_images_from_earlier_batch(self):
        from ecommerce.modules.collection import detail_images
        first=self.fail_third_download()
        original=detail_images._reuse_image
        def interrupt(image,previous,directory):
            if image['index']==2:
                raise TaskStopped('补采中再次停止')
            return original(image,previous,directory)
        with patch.object(detail_images,'_reuse_image',side_effect=interrupt),self.assertRaises(TaskStopped):
            self.collect()
        document=json.loads((product_directory(self.settings,ITEM_ID)/f'{ITEM_ID}.json').read_text(encoding='utf-8'))
        second=document['main_images_last_attempt']
        self.assertEqual(second['collected_count'],1)
        self.assertEqual(second['resumed_from_batch_id'],first['batch_id'])
        self.opener.open.assert_not_called()
        third=self.collect()
        self.assertEqual(third['reused_count'],2)
        self.assertEqual([p.get('reused_from_batch_id') for p in third['images'][:2]],[second['batch_id'],first['batch_id']])
        self.assertEqual([call.args[0].full_url for call in self.opener.open.call_args_list],[photo_url(i) for i in (3,4,5)])

    def test_keyboard_interrupt_during_publish_restores_existing_files(self):
        first=self.collect()
        original=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))['main_images']
        with patch('ecommerce.modules.collection.detail_images.save_detail_sections',side_effect=KeyboardInterrupt),self.assertRaises(KeyboardInterrupt):
            self.collect()
        document=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['main_images'],original)
        self.assertTrue(all(Path(p['image_path']).read_bytes()==png(p['index']) for p in first['images']))
        self.assertFalse(list(product_directory(self.settings,ITEM_ID).glob('.main-images-*')))

    def test_partial_new_batch_preserves_previously_published_images(self):
        first=self.collect()
        self.fail_third_download()
        document=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['main_images']['batch_id'],first['batch_id'])
        self.assertEqual(document['main_images_last_attempt']['status'],'download_failed')
        self.assertTrue(all(Path(p['image_path']).read_bytes()==png(p['index']) for p in first['images']))

    def test_source_change_during_download_waits_for_restoration_without_replaying_download(self):
        original=self.opener.open.side_effect
        def response(request,timeout):
            if request.full_url==photo_url(1):
                self.page.evaluate("document.querySelectorAll('.thumbnailPic--fixture')[3].src='https://img.alicdn.com/other.png'")
            return original(request,timeout)
        self.opener.open.side_effect=response
        messages=[]
        def restore(message):
            messages.append(message)
            self.page.evaluate("url=>document.querySelectorAll('.thumbnailPic--fixture')[3].src=url",photo_url(3))
            return '继续'
        with self.guard(restore),patch('ecommerce.modules.collection.detail_images.build_opener',return_value=self.opener):
            record=collect_main_images(self.settings,self.result)
        self.assertEqual(record['status'],'complete')
        self.assertEqual(self.opener.open.call_count,5)
        self.assertTrue(any('顺序' in message for message in messages))
        self.assertEqual(self.page.evaluate('imageClicks'),[])


if __name__=='__main__':
    unittest.main()
