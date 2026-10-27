"""真实浏览器上的本地 SKU 页面检查；所有网络由本地夹具接管。"""
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
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.detail_skus import collect_skus
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.detail_skus import OUT_OF_STOCK_PRICE, collect_sku_data
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID = '1050158363561'


def png(index=1):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)
    pixels = b''.join(b'\x00' + bytes((index*20, 80, 120))*8 for _ in range(8))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


def html(config):
    return '''<!doctype html><meta charset="utf-8"><style>
      .valueItem--fixture{display:inline-block;padding:8px;border:1px solid #ddd}
      .valueItemImg--fixture{width:32px;height:32px} [aria-busy=true]{min-height:20px}
      </style><span class="site-nav-login-info-nick">测试账号</span>
      <div class="ItemTitle--fixture"><div class="MainTitle--fixture"><span class="mainTitle--fixture">测试完整商品标题</span></div>
      <div>已售 10万+</div><div>312人评价"有价值"</div><div>超1万回头客</div><div>超7千人加购</div></div>
      <div class="priceWrap--fixture"><div class="highlightPrice--fixture"><span class="title--fixture">优惠前</span><span class="symbol--fixture">￥</span><span class="text--fixture">3.35</span></div></div>
      <div id="skuOptionsArea"></div><input class="countValue--fixture" value="1">
      <div id="picGalleryEle"><img id="mainPicImageEl" width="80" height="80"></div>
      <div class="valueItem--recommendation">其他商品，不能计入</div>
      <script>window.config=CONFIG;window.clicks=[];
      window.selected=config.selected||config.groups.map(()=>0);
      const root=document.querySelector('#skuOptionsArea');
      function disabled(g,o){return !!config.groups[g].options[o].disabled||
        (config.lockFirstColor&&g===0&&o===1&&selected[1]===0)}
      function render(){
       root.innerHTML=config.groups.map((g,gi)=>'<div class="skuItem--fixture"><div class="ItemLabel--fixture">'+g.name+'</div><div>'+g.options.map((o,oi)=>
        '<div class="valueItem--fixture '+(selected[gi]===oi?'isSelected--fixture':'')+'" data-vid="'+(o.valueId??gi+'-'+oi)+'" '+(o.skuId?'data-sku-id="'+o.skuId+'" ':'')+'data-disabled="'+disabled(gi,oi)+'" '+(o.outOfStock?'data-stock="0"':'')+'>'+
        (o.noImage?'':'<img class="valueItemImg--fixture" src="https://img.alicdn.com/sku'+(o.image||oi+1)+'.png">')+
        '<span class="valueItemText--fixture" title="'+o.name.replaceAll('"','&quot;')+'">'+o.name.replaceAll('&','&amp;').replaceAll('<','&lt;')+'</span></div>').join('')+'</div></div>').join('');
       root.querySelectorAll('.skuItem--fixture').forEach((g,gi)=>g.querySelectorAll('.valueItem--fixture').forEach((e,oi)=>e.onclick=()=>{
        clicks.push([gi,oi]);if(disabled(gi,oi))throw Error('Disabled option clicked');
        selected[gi]=selected[gi]===oi?null:oi;render();
        const wrap=document.querySelector('.priceWrap--fixture');wrap.setAttribute('aria-busy','true');
        const finish=()=>{if(selected.every(i=>i!==null)){
          const key=selected.join(',');document.querySelector('.text--fixture').textContent=(config.prices||{})[key]||'3.35';
          const option=config.groups.at(-1).options[selected.at(-1)];
          document.querySelector('#mainPicImageEl').src='https://img.alicdn.com/sku'+(option.image||selected.at(-1)+1)+'.png';
         }wrap.removeAttribute('aria-busy')};
        setTimeout(finish,config.delay||20);
        if(config.failedRequestOnClick&&!window.failedOnce){window.failedOnce=true;fetch('https://h5api.m.taobao.com/h5/mtop.sku.test/1.0/').catch(()=>{})}
        if(config.verificationOnClick&&!window.verified){window.verified=true;const modal=document.createElement('div');modal.className='J_MIDDLEWARE_FRAME_WIDGET';modal.textContent='请完成安全验证';root.append(modal)}
       }));
      }
      render();document.querySelector('#mainPicImageEl').src='https://img.alicdn.com/sku1.png';
      </script>'''.replace('CONFIG', json.dumps(config, ensure_ascii=False))


class Response(io.BytesIO):
    def __init__(self, url, body):
        super().__init__(body)
        self.url, self.status = url, 200
        self.headers = Message()
        self.headers['Content-Type'] = 'image/png'
        self.headers['Content-Length'] = str(len(body))


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用本地 SKU 浏览器检查')
class TaobaoDetailSkusBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(load_settings(root=Path(self.temp.name)).for_platform('taobao'), headless=True, timeout_ms=1800,
                                taobao_timing={k: (0, 0) for k in DEFAULT_TAOBAO_TIMING})
        save_detail_section(product_directory(self.settings,ITEM_ID),ITEM_ID,'product_info',{'title':'独立已有基本信息','sales_text':'已售 99件'})
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.config = {'groups': [{'name': '商品规格', 'options': [{'name': '1卷-替换纸'}, {'name': '3卷替换纸'}, {'name': '5卷替换纸'}]}],
                       'prices': {'0': '3.35', '1': '7.90', '2': '11.80'}}
        def route(route):
            if 'mtop.sku.test' in route.request.url:
                route.fulfill(status=503, content_type='application/json', body='{}')
            elif route.request.url.startswith('https://img.alicdn.com/'):
                route.fulfill(status=200, content_type='image/png', body=png())
            else:
                route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html(self.config))
        self.context.route('**/*', route)
        self.page = self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id=' + ITEM_ID)
        self.result = DetailPage(self.page, resolve_detail_input(ITEM_ID), '测试商品')
        self.opener = MagicMock()
        self.opener.open.side_effect = lambda request, timeout: Response(request.full_url, png())

    def reload(self):
        self.page.reload()

    def guard(self, attention=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def collect(self):
        with self.guard(), patch('ecommerce.modules.collection.detail_images.build_opener', return_value=self.opener):
            return collect_skus(self.settings, self.result, settle_ms=100)

    def test_variable_count_prices_and_named_images(self):
        self.config['groups'][0]['options'] = [{'name': str(i)+'卷替换纸'} for i in range(1, 9)]
        self.config['prices'] = {str(i): str(i+1)+'.50' for i in range(8)}
        self.reload()
        record = self.collect()
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['summary']['recognized_count'], 8)
        self.assertEqual(record['summary']['image_downloaded_count'], 8)
        self.assertEqual([s['price_value'] for s in record['skus']][1:], [str(i+1)+'.50' for i in range(1, 8)])
        self.assertEqual(len(self.page.evaluate('clicks')), 7)
        self.assertTrue(all(Path(s['image']['image_path']).name == s['name']+'.png' for s in record['skus']))
        document = json.loads(Path(record['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['product_info'], {'title':'独立已有基本信息','sales_text':'已售 99件'})
        self.assertNotIn('product_info',record)

    def test_delayed_price_does_not_reuse_previous_value(self):
        self.config['delay'] = 600
        self.reload()
        record = self.collect()
        self.assertEqual([s['price_value'] for s in record['skus']], ['3.35', '7.90', '11.80'])
        self.assertEqual([s['price_label'] for s in record['skus']], ['优惠前']*3)

    def test_same_price_can_belong_to_different_skus(self):
        self.config['prices'] = {'0': '3.35', '1': '3.35', '2': '3.35'}
        self.reload()
        self.assertEqual([s['price_value'] for s in self.collect()['skus']], ['3.35']*3)

    def test_explicit_out_of_stock_is_not_clicked_and_still_has_image(self):
        self.config['groups'][0]['options'][1].update(disabled=True, outOfStock=True)
        self.reload()
        record = self.collect()
        sku = record['skus'][1]
        self.assertEqual(sku['price'], OUT_OF_STOCK_PRICE)
        self.assertIsNone(sku['price_value'])
        self.assertEqual(sku['stock_status'], 'out_of_stock')
        self.assertEqual(sku['image_status'], 'downloaded')
        self.assertNotIn([0, 1], self.page.evaluate('clicks'))
        self.assertEqual(record['summary']['out_of_stock_count'], 1)
        self.assertEqual(record['status'], 'complete')

    def test_explicit_disabled_option_is_out_of_stock_and_complete(self):
        self.config['groups'][0]['options'][1]['disabled'] = True
        self.reload()
        record = self.collect()
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['skus'][1]['stock_status'], 'out_of_stock')
        self.assertEqual(record['skus'][1]['price_status'], 'out_of_stock')
        self.assertIsNone(record['skus'][1]['price'])
        self.assertIsNone(record['skus'][1]['price_value'])
        self.assertNotIn([0, 1], self.page.evaluate('clicks'))
        self.assertTrue(record['continuation_allowed'])
        self.assertEqual(record['warnings'],[])
        self.assertEqual(record['blocking_issues'],[])
        self.assertEqual(record['skus'][1]['unavailability_evidence'],['data-disabled=true'])
        self.assertIn('页面规格禁用',record['skus'][1]['stock_evidence'])
        self.assertEqual(record['summary']['out_of_stock_count'],1)
        self.assertEqual(record['summary']['unavailable_unconfirmed_count'],0)
        self.assertEqual(record['summary']['option_count'],3)
        self.assertEqual(record['summary']['explicit_sku_id_count'],0)
        self.assertEqual(record['result_section'],'sku_details')
        self.assertTrue(Path(record['image_directory']).is_dir())

    def test_multiple_groups_reset_old_selection_before_linked_options(self):
        self.config.update(groups=[{'name': '颜色', 'options': [{'name': '红'}, {'name': '蓝'}]},
                                   {'name': '尺寸', 'options': [{'name': '小'}, {'name': '大'}]}],
                           lockFirstColor=True, prices={'0,0':'1.00', '0,1':'2.00', '1,0':'3.00', '1,1':'4.00'})
        self.reload()
        record = self.collect()
        self.assertEqual(record['status'], 'complete')
        self.assertEqual([s['name'] for s in record['skus']], ['颜色-红_尺寸-小', '颜色-红_尺寸-大', '颜色-蓝_尺寸-小', '颜色-蓝_尺寸-大'])
        self.assertEqual([s['price_value'] for s in record['skus']][1:], ['2.00', '3.00', '4.00'])

    def test_shared_images_are_saved_with_each_sku_name(self):
        for option in self.config['groups'][0]['options']:
            option['image'] = 1
        self.reload()
        record = self.collect()
        self.assertEqual(len({s['image_url'] for s in record['skus']}), 1)
        self.assertEqual(len({s['image']['image_file'] for s in record['skus']}), 3)

    def test_missing_image_is_recorded_without_using_other_gallery_picture(self):
        self.config['groups'][0]['options'][1]['noImage'] = True
        self.reload()
        record = self.collect()
        self.assertEqual(record['skus'][1]['image_status'], 'no_image')
        self.assertIsNone(record['skus'][1]['image'])
        self.assertEqual(record['summary']['no_image_count'], 1)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_failed_download_preserves_previous_complete_results(self):
        first = self.collect()
        old = Path(first['record_path']).read_bytes()
        self.opener.open.side_effect = lambda request, timeout: Response(request.full_url, b'not an image')
        second = self.collect()
        self.assertEqual(second['status'], 'partial')
        self.assertFalse(second['continuation_allowed'])
        document = json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['sku_details']['batch_id'], first['batch_id'])
        self.assertEqual(document['sku_details_last_attempt']['summary']['image_failed_count'], 3)
        self.assertTrue(all(Path(s['image']['image_path']).read_bytes() == png() for s in first['skus']))
        self.assertFalse(list(product_directory(self.settings, ITEM_ID).rglob('*.part')))

    def test_publish_failure_restores_images_and_json(self):
        first = self.collect()
        original = json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        with self.guard(), patch('ecommerce.modules.collection.detail_images.build_opener', return_value=self.opener), \
                patch('ecommerce.modules.collection.detail_skus.save_detail_sections', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                collect_skus(self.settings, self.result, settle_ms=100)
        current=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(current['sku_details'],original['sku_details'])
        self.assertEqual(current['product_info'],original['product_info'])
        self.assertEqual(current['sku_details_last_attempt']['status'],'interrupted')
        self.assertTrue(all(Path(s['image']['image_path']).exists() for s in first['skus']))
        self.assertFalse(list(product_directory(self.settings, ITEM_ID).glob('.sku-images-*')))

    def test_video_main_images_and_history_are_preserved(self):
        folder = product_directory(self.settings, ITEM_ID)
        save_detail_section(folder, ITEM_ID, 'video', {'video_file':ITEM_ID+'.mp4'})
        save_detail_section(folder, ITEM_ID, 'main_images', {'images':[{'index':1}]})
        first = self.collect()
        second = self.collect()
        document = json.loads(Path(second['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['video'], {'video_file':ITEM_ID+'.mp4'})
        self.assertEqual(document['main_images'], {'images':[{'index':1}]})
        self.assertNotEqual(first['history_record_path'], second['history_record_path'])
        archive = json.loads(Path(first['history_record_path']).read_text(encoding='utf-8'))
        self.assertTrue(all(Path(s['image']['image_path']).exists() for s in archive['skus']))

    def test_verification_requires_explicit_continue_and_does_not_replay_click(self):
        self.config['verificationOnClick'] = True
        self.reload()
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate('e=>e.remove()')
            return '继续'
        with self.guard(attention):
            data = collect_sku_data(self.result, 1800, settle_ms=100)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(len(data['skus']), 3)
        self.assertEqual(self.page.evaluate('clicks'), [[0,1], [0,2]])

    def test_wrong_quantity_waits_for_manual_correction(self):
        self.page.locator('input').fill('2')
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('input').fill('1')
            return '继续'
        with self.guard(attention):
            data = collect_sku_data(self.result, 1800, settle_ms=100)
        self.assertEqual(len(prompts), 1)
        self.assertTrue(all(s['quantity'] == 1 for s in data['skus']))

    def test_unknown_structure_stops_instead_of_reporting_zero_skus(self):
        self.page.locator('#skuOptionsArea').evaluate('e=>e.remove()')
        with self.guard(lambda message:'停止'):
            with self.assertRaises(TaskStopped):
                collect_sku_data(self.result, 100, settle_ms=100)

    def test_stopping_on_later_sku_keeps_previous_progress_without_publishing_complete(self):
        self.config['prices']['1'] = '价格待确认'
        self.reload()
        self.settings = replace(self.settings, timeout_ms=500)
        with self.guard(lambda message:'停止'):
            with self.assertRaises(TaskStopped):
                collect_skus(self.settings, self.result, settle_ms=100)
        folder = product_directory(self.settings, ITEM_ID)
        paths = list(folder.rglob('sku_progress.json'))
        self.assertEqual(len(paths), 1)
        progress = json.loads(paths[0].read_text(encoding='utf-8'))
        self.assertEqual(progress['status'], 'interrupted')
        self.assertEqual(progress['processed_count'], 1)
        self.assertEqual(progress['skus'][0]['price_value'], '3.35')
        document=json.loads((folder/(ITEM_ID+'.json')).read_text(encoding='utf-8'))
        self.assertNotIn('sku_details',document)
        self.assertEqual(document['sku_details_last_attempt']['status'],'interrupted')

    def test_hidden_duplicate_options_are_excluded_from_count_and_clicks(self):
        self.page.locator('#skuOptionsArea').evaluate("e=>{const c=e.firstElementChild.cloneNode(true);c.style.display='none';e.prepend(c)}")
        record = self.collect()
        self.assertEqual(record['summary']['recognized_count'], 3)
        self.assertEqual([s['price'] for s in record['skus']], ['￥3.35', '￥7.90', '￥11.80'])

    def test_missing_selected_price_requires_manual_review_without_replaying_click(self):
        self.config['prices']['1'] = '价格待确认'
        self.reload()
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('.text--fixture').evaluate("e=>e.textContent='7.90'")
            return '继续'
        with self.guard(attention):
            # 保留缺失价格触发人工检查，给正常DOM稳定核对足够的本地执行时间。
            data = collect_sku_data(self.result, 1500, settle_ms=100)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(data['skus'][1]['price_value'], '7.90')
        self.assertEqual(self.page.evaluate('clicks'), [[0,1], [0,2]])

    def test_failed_price_request_requires_manual_review_even_if_old_price_remains_visible(self):
        self.config['failedRequestOnClick'] = True
        self.config['prices']['1'] = '3.35'
        self.reload()
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('.text--fixture').evaluate("e=>e.textContent='7.90'")
            return '继续'
        with self.guard(attention):
            data = collect_sku_data(self.result, 700, settle_ms=100)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(data['skus'][1]['price_value'], '7.90')
        self.assertEqual(self.page.evaluate('clicks'), [[0,1], [0,2]])

    def interrupt_images(self,index=2):
        """保留前面的真实下载结果，在指定图片开始时模拟用户中断。"""
        from ecommerce.modules.collection import detail_skus
        original=detail_skus.download_image
        def download(image,*args,**kwargs):
            if image['index']==index:
                raise TaskStopped('测试中断图片阶段')
            return original(image,*args,**kwargs)
        with patch.object(detail_skus,'download_image',side_effect=download),self.assertRaises(TaskStopped):
            self.collect()
        document=json.loads((product_directory(self.settings,ITEM_ID)/f'{ITEM_ID}.json').read_text(encoding='utf-8'))
        self.opener.open.reset_mock()
        return document['sku_details_last_attempt']

    def test_new_batch_redownloads_all_images_and_refreshes_every_price(self):
        first=self.interrupt_images()
        old_path=Path(first['skus'][0]['image']['image_path'])
        self.config['prices'].update({'0':'8.88','1':'6.60','2':'8.80'})
        self.reload()
        self.page.locator('.text--fixture').evaluate("e=>e.textContent='8.88'")
        second=self.collect()
        self.assertEqual(second['status'],'complete')
        self.assertEqual([s['price_value'] for s in second['skus']],['8.88','6.60','8.80'])
        self.assertEqual(second['price_refresh_policy'],'always_current_run')
        self.assertTrue(all(s['price_checked_at'] for s in second['skus']))
        self.assertEqual(second['summary']['image_reused_count'],0)
        self.assertEqual(self.opener.open.call_count,3)
        self.assertNotIn('reused_from_batch_id',second['skus'][0]['image'])
        self.assertEqual(old_path.read_bytes(),png())
        self.assertNotEqual(old_path,Path(second['skus'][0]['image']['image_path']))

    def test_cancelled_price_refresh_does_not_reuse_earlier_images(self):
        first=self.interrupt_images()
        self.config['prices']['1']='价格待确认'
        self.reload();self.settings=replace(self.settings,timeout_ms=300)
        with self.guard(lambda _:'停止'),self.assertRaises(TaskStopped):
            collect_skus(self.settings,self.result,settle_ms=100)
        self.config['prices']['1']='7.90'
        self.reload();self.settings=replace(self.settings,timeout_ms=1800)
        record=self.collect()
        self.assertEqual(record['summary']['image_reused_count'],0)
        self.assertNotIn('reused_from_batch_id',record['skus'][0]['image'])
        self.assertEqual(self.opener.open.call_count,3)

    def test_corrupt_history_and_other_partial_images_are_all_redownloaded(self):
        first=self.interrupt_images(index=3)
        Path(first['skus'][0]['image']['image_path']).write_bytes(b'damaged')
        record=self.collect()
        self.assertEqual(record['summary']['image_reused_count'],0)
        self.assertEqual(self.opener.open.call_count,3)
        self.assertEqual(record['skus'][0]['image_status'],'downloaded')

    def test_changed_inventory_disables_reuse(self):
        self.interrupt_images(index=3)
        self.config['groups'][0]['options'][0]['name']='另一种商品规格'
        self.reload()
        record=self.collect()
        self.assertIsNone(record['resumed_from_batch_id'])
        self.assertEqual(record['summary']['image_reused_count'],0)
        self.assertEqual(self.opener.open.call_count,3)

    def test_changed_image_address_is_not_reused_even_with_same_option_names(self):
        self.interrupt_images(index=3)
        self.config['groups'][0]['options'][0]['image']=6
        self.reload()
        record=self.collect()
        self.assertEqual(record['summary']['image_reused_count'],0)
        self.assertIn('sku6.png',record['skus'][0]['image_url'])
        self.assertEqual(self.opener.open.call_count,3)

    def test_service_names_negative_value_ids_and_disabled_options_are_not_filtered(self):
        options=self.config['groups'][0]['options']
        options[0].update(name='运费险正常可售规格',valueId='-1',skuId='998877')
        options[1].update(name='赠退货宝【运费险】',valueId='-2',disabled=True)
        options[2].update(valueId='-3',skuId='-3')
        self.reload()
        record=self.collect()
        self.assertEqual(record['candidate_count'],3)
        self.assertTrue(record['continuation_allowed'])
        self.assertEqual(record['summary']['explicit_sku_id_count'],1)
        self.assertEqual(record['skus'][0]['sku_identity_status'],'explicit_dom_id')
        self.assertEqual(record['skus'][1]['properties'][0]['value_id'],'-2')
        self.assertIsNone(record['skus'][2]['sku_id'])
        self.assertEqual(record['skus'][2]['sku_identity_status'],'not_provided')

    def test_unconfirmed_multi_group_combination_remains_blocking(self):
        self.config.update(groups=[{'name':'颜色','options':[{'name':'红'},{'name':'蓝','disabled':True}]},
                                   {'name':'尺寸','options':[{'name':'小'},{'name':'大'}]}])
        self.reload()
        record=self.collect()
        self.assertEqual(record['status'],'partial')
        self.assertFalse(record['continuation_allowed'])
        self.assertTrue(any(i['code']=='combination_unconfirmed' for i in record['blocking_issues']))

    def test_http_blocking_failure_is_saved_before_manual_feedback_without_retry(self):
        original=self.opener.open.side_effect
        def download(request,timeout):
            if 'sku2.png' in request.full_url:
                raise HTTPError(request.full_url,403,'Forbidden',{},None)
            return original(request,timeout)
        self.opener.open.side_effect=download
        messages=[]
        def attention(message):
            messages.append(message)
            doc=json.loads((product_directory(self.settings,ITEM_ID)/f'{ITEM_ID}.json').read_text(encoding='utf-8'))
            self.assertEqual(doc['sku_details_last_attempt']['skus'][1]['image_status'],'download_failed')
            return '继续'
        with self.guard(attention),patch('ecommerce.modules.collection.detail_images.build_opener',return_value=self.opener):
            record=collect_skus(self.settings,self.result,settle_ms=100)
        self.assertEqual(len(messages),1)
        self.assertEqual(self.opener.open.call_count,3)
        self.assertFalse(record['continuation_allowed'])
        self.assertEqual(record['summary']['image_failed_count'],1)

    def test_user_interrupt_while_publishing_restores_old_complete_result(self):
        first=self.collect()
        original=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        with patch('ecommerce.modules.collection.detail_skus.save_detail_sections',side_effect=KeyboardInterrupt),self.assertRaises(KeyboardInterrupt):
            self.collect()
        document=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(document['sku_details'],original['sku_details'])
        self.assertEqual(document['product_info'],original['product_info'])
        self.assertEqual(document['sku_details_last_attempt']['status'],'interrupted')
        self.assertFalse(list(product_directory(self.settings,ITEM_ID).glob('.sku-images-*')))
        self.assertTrue(all(Path(s['image']['image_path']).is_file() for s in first['skus']))


if __name__ == '__main__':
    unittest.main()
