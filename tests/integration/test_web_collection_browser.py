"""隔离浏览器验证网页采集交互；请求转入临时 Flask，禁止访问真实平台。"""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright,expect
from ecommerce.infrastructure.tasks import web_jobs as jobs
from ecommerce.infrastructure.tasks.ownership import atomic_json
from tests.unit import test_collection_browser as fixtures


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用隔离浏览器验证')
class WebCollectionBrowserTests(unittest.TestCase):
    def setUp(self):
        """临时数据和浏览器；参数：无；返回：无，所有采集派发均模拟。"""
        self.fixture=fixtures.CollectionBrowserTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.settings=replace(self.fixture.settings,task_backend='mysql')
        self.fixture.app.config['SETTINGS']=self.settings
        for target,kwargs in [
            ('ecommerce.infrastructure.tasks.web_jobs.launch',{'return_value':SimpleNamespace(pid=9999)}),
            ('ecommerce.infrastructure.tasks.web_jobs.alive',{'return_value':True}),
            ('ecommerce.infrastructure.tasks.ownership.active_local_tasks',{'side_effect':self.active}),
            ('ecommerce.modules.collection.web_collection.active_local_tasks',{'side_effect':self.active})]:
            mock=patch(target,**kwargs);mock.start();self.addCleanup(mock.stop)
        self.playwright=sync_playwright().start();self.addCleanup(self.playwright.stop)
        self.browser=self.playwright.chromium.launch(headless=True);self.addCleanup(self.browser.close)
        self.context=self.browser.new_context(viewport={'width':1440,'height':1000})
        self.context.route('**/*',self.route)
        self.page=self.context.new_page()
        self.errors=[];self.page.on('pageerror',lambda error:self.errors.append(str(error)))

    def active(self,settings):
        """模拟已由统一执行器启动的本地任务；参数：配置；返回：当前活动记录。"""
        return [{'id':row['id'],'name':'collection.taobao.detail.open','storage_pending':False}
                for row in jobs.rows(self.settings) if row['state']=='running']

    def route(self,route):
        """所有网络请求仅交给临时服务；参数：浏览器路由；返回：本地响应。"""
        request=route.request;url=urlsplit(request.url)
        if url.hostname!='127.0.0.1':route.abort();return
        response=self.fixture.client.open(url.path+('?' + url.query if url.query else ''),method=request.method,
                    headers={key:value for key,value in request.headers.items() if key.lower() not in {'host','content-length'}},
                    data=request.post_data_buffer,base_url='http://127.0.0.1')
        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
        response.close()

    def launch(self):
        """点击视频模块并模拟执行器就绪；参数：无；返回：派发。"""
        self.page.goto('http://127.0.0.1/collections/products/123')
        video=self.page.locator('[data-collection-module="video"]')
        expect(video.get_by_role('button',name='采集数据',exact=True)).to_be_visible()
        expect(self.page.locator('[data-collection-module="parameters"]').get_by_role('link',name='查看资料 →')).to_be_visible()
        video.get_by_role('button',name='采集数据',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('启动中')
        row=jobs.rows(self.settings)[0];row['state']='running';jobs.save(self.settings,row)
        return row

    def publish(self,row,prompt='b'*32,allowed=None):
        """模拟守卫人工提示；参数：派发、提示编号、允许值；返回：无。"""
        atomic_json(self.settings.workspace/'storage/task_controls'/f"{row['id']}.state.json",
                    {'task_id':row['id'],'prompt_id':prompt,'allowed':allowed or ['继续'],
                     'message':'请手动扫码登录 <script>不能执行</script>','step':'等待人工登录淘宝'})

    def test_batch_counts_and_status_are_consistent_across_pages(self):
        """概况、历史、模块和批次页统一显示真实数量，不把空记录显示为0。"""
        for path in ['/collections/products/123','/collections/products/123?module=history',
                     '/collections/products/123?module=parameters','/collections/batches/20']:
            self.page.goto('http://127.0.0.1'+path)
            expect(self.page.locator('main')).to_contain_text('2项参数')
            expect(self.page.locator('main')).not_to_contain_text('未确认覆盖范围')
        self.page.goto('http://127.0.0.1/collections/batches/21')
        expect(self.page.locator('main')).to_contain_text('部分完成')
        expect(self.page.locator('main')).not_to_contain_text('0项参数')
        self.assertEqual(self.errors,[])

    def test_batch_counts_and_status_are_consistent_across_pages(self):
        """概况、历史、模块和批次页统一显示真实数量，不把空记录显示为0。"""
        for path in ['/collections/products/123','/collections/products/123?module=history',
                     '/collections/products/123?module=parameters','/collections/batches/20']:
            self.page.goto('http://127.0.0.1'+path)
            expect(self.page.locator('main')).to_contain_text('2项参数')
            expect(self.page.locator('main')).not_to_contain_text('未确认覆盖范围')
        self.page.goto('http://127.0.0.1/collections/batches/21')
        expect(self.page.locator('main')).to_contain_text('部分完成')
        expect(self.page.locator('main')).not_to_contain_text('0项参数')
        self.assertEqual(self.errors,[])

    def test_continue_completion_and_new_result_link(self):
        row=self.launch();self.publish(row)
        modal=self.page.locator('#collection-attention');expect(modal).to_be_visible(timeout=7000)
        expect(modal).to_contain_text('等待人工登录淘宝')
        modal.get_by_role('button',name='继续',exact=True).click()
        expect(modal).not_to_be_visible()
        control=self.settings.workspace/'storage/task_controls'/f"{row['id']}.json"
        command=json.loads(control.read_text(encoding='utf-8'))
        self.assertEqual(command,{'action':'continue','prompt_id':'b'*32})
        control.unlink();(control.with_suffix('.state.json')).unlink()
        row.update(state='succeeded',message='已确认无视频',result={'batch_id':20,'valid':True,'label':'查看采集结果'})
        jobs.save(self.settings,row)
        video=self.page.locator('[data-collection-module="video"]')
        expect(video.get_by_role('link',name='查看采集结果 →')).to_be_visible(timeout=7000)
        expect(video.locator('.module-collect')).to_have_count(0)
        self.assertIn('batch=20',video.get_by_role('link',name='查看采集结果 →').get_attribute('href'))
        self.assertEqual(self.errors,[])

    def test_refresh_and_other_page_restore_native_dialog_and_stop(self):
        row=self.launch();self.publish(row,allowed=['确认','取消'])
        self.page.goto('http://127.0.0.1/tasks')
        modal=self.page.locator('#collection-attention');expect(modal).to_be_visible(timeout=7000)
        expect(modal.get_by_role('button',name='确认',exact=True)).to_be_visible()
        expect(modal.get_by_role('button',name='取消',exact=True)).to_be_visible()
        expect(modal.get_by_role('button',name='继续',exact=True)).to_have_count(0)
        self.page.reload();expect(modal).to_be_visible(timeout=7000)
        modal.get_by_role('button',name='停止',exact=True).click()
        path=self.settings.workspace/'storage/task_controls'/f"{row['id']}.json"
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['action'],'stop')
        self.assertEqual(self.errors,[])

    def test_partial_keeps_attempt_and_retry(self):
        row=self.launch()
        row.update(state='needs_attention',message='仅保存部分资料',result={'batch_id':21,'valid':False})
        jobs.save(self.settings,row)
        video=self.page.locator('[data-collection-module="video"]')
        expect(video.get_by_role('link',name='查看本次采集尝试')).to_be_visible(timeout=7000)
        expect(video.get_by_role('button',name='重新采集')).to_be_enabled()
        expect(video).to_contain_text('仅保存部分资料')
        self.assertEqual(self.errors,[])

    def test_basic_info_button_dispatches_separately_and_refresh_displays_own_data(self):
        """网页基本信息入口不采SKU，完成后从独立表读取。"""
        self.page.goto('http://127.0.0.1/collections/products/123')
        basic=self.page.locator('[data-collection-module="basic_info"]')
        basic.get_by_role('button',name='采集基本信息',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('基本信息')
        row=jobs.rows(self.settings)[0];self.assertEqual(row['module'],'basic_info')
        self.fixture.batch(80,'basic_info',product_id=1)
        self.fixture.put('product_info_observations',id=80,batch_id=80,product_id=1,module='basic_info',
                         title='独立保存的详情标题',shop_name='详情店铺',sales_text='已售 20+',price_text='¥9 起',captured_at=self.fixture.now,field_states={})
        self.fixture.put('product_current_sections',product_id=1,section='product_info',module='basic_info',batch_id=80)
        row.update(state='succeeded',message='基本信息已保存',result={'batch_id':80,'valid':True,'label':'查看资料'})
        jobs.save(self.settings,row)
        expect(basic.get_by_role('link',name='查看资料 →')).to_be_visible(timeout=7000)
        self.page.reload()
        expect(basic).to_contain_text('独立保存的详情标题')
        expect(basic).to_contain_text('详情店铺')
        expect(basic.get_by_role('link',name='查看资料 →')).to_have_count(1)
        expect(self.page.locator('[data-collection-module="skus"]').get_by_role('button',name='采集 SKU',exact=True)).to_be_visible()
        self.assertEqual(self.errors,[])

    def test_collect_remaining_skips_global_valid_recovers_progress_and_retry_skips_saved_module(self):
        """旧来源页面仍按商品全局跳过，部分完成后重新点击只补剩余模块。"""
        self.page.goto('http://127.0.0.1/collections/products/123?observation=10')
        button=self.page.get_by_role('button',name='采集所有（剩余 8 项）',exact=True)
        button.click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('采集所有')
        row=jobs.rows(self.settings)[0]
        self.assertEqual(row['mode'],'remaining');self.assertNotIn('parameters',row['modules'])
        row.update(state='running',progress={'current_module':'basic_info','started_modules':['basic_info'],'results':[]})
        self.fixture.batch(80,'basic_info',product_id=1)
        self.fixture.put('product_info_observations',id=80,batch_id=80,product_id=1,module='basic_info',title='补采标题',captured_at=self.fixture.now,field_states={})
        self.fixture.put('product_current_sections',product_id=1,section='product_info',module='basic_info',batch_id=80)
        saved={'module':'basic_info','batch_id':80,'valid':True,'label':'查看资料'}
        row['progress'].update(current_module='skus',started_modules=['basic_info','skus'],results=[saved]);jobs.save(self.settings,row)
        expect(self.page.locator('#collection-task-bar')).to_contain_text('已完成 1/8 项',timeout=7000)
        expect(self.page.locator('.collect-remaining')).to_be_disabled()
        self.page.reload()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('当前：SKU',timeout=7000)
        self.publish(row)
        modal=self.page.locator('#collection-attention');expect(modal).to_be_visible(timeout=7000)
        expect(modal).to_contain_text('采集所有')
        modal.get_by_role('button',name='停止',exact=True).click()
        path=self.settings.workspace/'storage/task_controls'/f"{row['id']}.json"
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['action'],'stop')
        path.unlink();path.with_suffix('.state.json').unlink()
        row.update(state='cancelled',message='已停止，基本信息保留',result={'valid':False,'modules':[saved]});jobs.save(self.settings,row)
        expect(self.page.get_by_role('button',name='采集所有（剩余 7 项）',exact=True)).to_be_enabled(timeout=7000)
        self.page.get_by_role('button',name='采集所有（剩余 7 项）',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('跳过 2 项',timeout=7000)
        latest=jobs.rows(self.settings)[0]
        self.assertNotIn('basic_info',latest['modules']);self.assertNotIn('parameters',latest['modules'])
        self.assertEqual(self.errors,[])

    def test_reclassified_sku_is_viewable_skipped_and_not_overwritten_by_old_job(self):
        """旧部分任务不覆盖新完成批次，缺货显示和补采计划使用同一有效结果。"""
        from ecommerce.modules.collection.web_collection import start
        job=start(self.settings,'123','skus');row=jobs.read(self.settings,job['id'])
        row.update(state='needs_attention',message='旧任务未确认有效资料',result={'batch_id':30,'valid':False})
        jobs.save(self.settings,row)
        self.fixture.batch(30,'skus',product_id=1,status='partial')
        self.fixture.batch(31,'skus',product_id=1,derivation_kind='sku_stock_rule',derived_from_batch_id=30)
        self.fixture.put('product_current_sections',product_id=1,section='sku_details',module='skus',batch_id=31)
        self.fixture.put('sku_observations',id=31,batch_id=31,position=1,sku_name='替换装',
                         stock_status='out_of_stock',price_status='out_of_stock',price_text=None,price_value=None)
        self.page.goto('http://127.0.0.1/collections/products/123')
        sku=self.page.locator('[data-collection-module="skus"]')
        expect(sku.get_by_role('link',name='查看资料 →',exact=True)).to_be_visible()
        self.page.wait_for_function("document.querySelector('[data-collection-module=skus]').dataset.latestBatch==='31'")
        self.page.wait_for_timeout(2200)
        expect(sku.locator('.module-job-status')).to_be_empty()
        response=self.fixture.client.get('/api/collections/products/123/jobs').get_json()
        self.assertIn('skus',response['plan']['skipped_modules'])
        self.assertNotIn('skus',response['plan']['modules'])
        sku.get_by_role('link',name='查看资料 →',exact=True).click()
        expect(self.page.locator('.batch-summary')).to_contain_text('从 SKU 批次 30 重新归类')
        expect(self.page.locator('table')).to_contain_text('缺货，未取得价格')
        expect(self.page.locator('table')).to_contain_text('库存状态：明确缺货')
        self.assertEqual(self.errors,[])

    def test_sku_images_stay_in_rows_when_sorting_paging_and_opening_original(self):
        """图片按规格外键跟随列表，统一20条分页并能打开本地原件。"""
        import hashlib
        from tests.integration.test_taobao_detail_skus_browser import png
        self.fixture.batch(70,'skus',product_id=1)
        self.fixture.put('product_current_sections',product_id=1,section='sku_details',module='skus',batch_id=70)
        root=self.settings.workspace/'output/sku-list-browser';root.mkdir(parents=True)
        for i in range(1,22):
            path=root/f'{i}.png';path.write_bytes(png(i%12+1))
            self.fixture.put('sku_observations',id=700+i,batch_id=70,position=i,sku_name=f'规格{i}',price_value=i,
                price_text=f'¥{i}',image_status='downloaded',stock_status='selectable',price_status='collected')
            self.fixture.put('file_assets',id=800+i,root_key='workspace',relative_path=path.relative_to(self.settings.workspace).as_posix(),
                availability='present',size_bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            self.fixture.put('media_references',id=900+i,batch_id=70,role='sku_image',position=100-i,
                sku_observation_id=700+i,file_asset_id=800+i,download_status='downloaded')
        self.page.goto('http://127.0.0.1/collections/products/123?module=skus&sort=price&order=desc')
        rows=self.page.locator('tbody tr');expect(rows).to_have_count(20)
        expect(rows.first.locator('.sku-thumbnail')).to_have_attribute('src','/collections/files/821')
        expect(self.page.locator('.media-grid,.media-sort')).to_have_count(0)
        expect(self.page.locator('.numbered-pagination')).to_have_count(1)
        self.page.get_by_role('link',name='价格 / 来源信息').click()
        expect(rows.first.locator('.sku-thumbnail')).to_have_attribute('src','/collections/files/801')
        self.page.wait_for_function("document.querySelector('.sku-thumbnail').naturalWidth > 0")
        size=rows.first.locator('.sku-thumbnail').evaluate("e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,fit:getComputedStyle(e).objectFit})")
        self.assertEqual(size,{'width':96,'height':96,'fit':'contain'})
        rows.first.get_by_role('link',name='查看规格图：规格1',exact=True).click()
        preview=self.page.locator('#image-preview');expect(preview).to_be_visible()
        with self.page.expect_popup() as opened:
            preview.get_by_role('link',name='打开原图').click()
        original=opened.value;original.wait_for_load_state()
        self.assertTrue(original.url.endswith('/collections/files/801'));original.close()
        preview.get_by_role('button',name='关闭图片预览').click()
        self.page.get_by_role('link',name='下一页',exact=True).click()
        expect(rows).to_have_count(1)
        expect(rows.first.locator('.sku-thumbnail')).to_have_attribute('src','/collections/files/821')
        expect(self.page.get_by_text('第 2 / 2 页 · 共 21 条 · 每页 20 条')).to_be_visible()
        self.assertEqual(self.errors,[])

    def test_all_nine_complete_disables_bulk_button_without_dispatch(self):
        """九个有效模块时按钮置灰，不生成后台派发。"""
        from ecommerce.modules.collection.web_collection import MODULES
        for index,module in enumerate(MODULES):
            identifier=100+index
            self.fixture.batch(identifier,module,product_id=1)
            self.fixture.put('product_current_sections',product_id=1,section='product_info' if module=='basic_info' else 'sku_details' if module=='skus' else module,
                             module=module,batch_id=identifier)
        self.page.goto('http://127.0.0.1/collections/products/123')
        expect(self.page.get_by_role('button',name='已全部采集',exact=True)).to_be_disabled()
        self.assertEqual(jobs.rows(self.settings),[])
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
