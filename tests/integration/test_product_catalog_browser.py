"""商品库的隔离浏览器验证；采集派发模拟，不访问真实平台。"""
import os
import unittest

from playwright.sync_api import expect
from ecommerce.infrastructure.tasks import web_jobs as jobs
from tests.integration import test_web_collection_browser as fixtures


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用商品库浏览器验证')
class ProductCatalogBrowserTests(unittest.TestCase):
    def setUp(self):
        """复用隔离服务与模拟派发；参数：无；返回：无。"""
        self.host=fixtures.WebCollectionBrowserTests();self.host.setUp();self.addCleanup(self.host.doCleanups)
        self.f=self.host.fixture;self.page=self.host.page
        table=self.f.store.tables['list_observations']
        with self.f.engine.begin() as c:
            c.execute(table.update().where(table.c.id==11).values(price_text='¥9.9',price_value=9.9,
                sales_text='1000+人收货',sales_count=1000,sales_precision='lower_bound',shop_name='最新店铺',location_text='浙江'))

    def goto(self,path='/collections/details'):
        """打开隔离商品库；参数：路径；返回：无。"""
        self.page.goto('http://127.0.0.1'+path)
        expect(self.page.get_by_role('heading',name='商品库',exact=True)).to_be_visible()

    def test_rows_sources_and_registered_product_navigation(self):
        """八列内容、搜索词追溯、返回状态及仅登记商品详情入口可用。"""
        self.goto('/collections/details?sort=sales&order=desc')
        expect(self.page.locator('.catalog-table th')).to_have_count(8)
        first=self.page.locator('[data-catalog-product="123"]')
        expect(first).to_contain_text('¥9.9');expect(first).to_contain_text('1000+人收货')
        expect(first).to_contain_text('最新店铺');expect(first).to_contain_text('1/9')
        expect(first.locator('.catalog-title')).to_have_count(1)
        expect(first.locator('.catalog-product a[href*="/collections/products/"]')).to_have_count(0)
        first.locator('.catalog-keyword').click()
        self.assertIn('product=123',self.page.url);self.assertIn('mode=records',self.page.url)
        expect(self.page.locator('tbody tr')).to_have_count(2)
        self.page.get_by_role('link',name='← 返回来源列表',exact=True).click()
        expect(self.page.get_by_role('heading',name='商品库',exact=True)).to_be_visible()
        self.assertIn('sort=sales',self.page.url)
        row=self.page.locator('[data-catalog-product="456"]')
        expect(row).to_contain_text('仅登记商品')
        row.get_by_role('link',name='查看详情',exact=True).click()
        expect(self.page.locator('[data-collection-product="456"]')).to_be_visible()
        expect(self.page.locator('.collect-remaining')).to_have_text('采集所有（剩余 9 项）')
        self.page.get_by_role('link',name='← 返回上一列表',exact=True).click()
        expect(self.page.get_by_role('heading',name='商品库',exact=True)).to_be_visible()
        self.assertIn('sort=sales',self.page.url)
        self.assertEqual(self.host.errors,[])

    def test_sales_filter_sort_pagination_and_narrow_table(self):
        """数字排序及区间在分页前生效，未知销量不当0，窄屏表格可横向查看。"""
        for identifier in range(3,27):
            self.f.put('products',id=identifier,platform='taobao',platform_product_id=str(1000+identifier),display_title='仅登记商品')
        self.goto()
        self.page.get_by_role('columnheader',name='销量',exact=False).get_by_role('link').click()
        expect(self.page.locator('.catalog-table tbody tr').first).to_have_attribute('data-catalog-product','123')
        expect(self.page.locator('.catalog-table tbody tr')).to_have_count(20)
        self.page.get_by_role('link',name='下一页',exact=True).click()
        expect(self.page.locator('.catalog-table tbody tr')).to_have_count(6)
        expect(self.page.get_by_text('下一页',exact=True)).to_have_attribute('aria-disabled','true')
        self.page.get_by_label('销量最小值',exact=True).fill('1000')
        self.page.get_by_label('销量最大值',exact=True).fill('1000')
        self.page.get_by_role('button',name='查询',exact=True).click()
        expect(self.page.locator('.catalog-table tbody tr')).to_have_count(1)
        expect(self.page.locator('.catalog-table tbody tr')).to_have_attribute('data-catalog-product','123')
        self.assertNotIn('page=2',self.page.url)
        self.page.set_viewport_size({'width':900,'height':1000})
        dimensions=self.page.locator('.table-wrap').evaluate('(n)=>({scroll:n.scrollWidth,width:n.clientWidth})')
        self.assertGreater(dimensions['scroll'],dimensions['width'])
        wrapper=self.page.locator('.table-wrap')
        header=self.page.locator('.catalog-table .actions-heading')
        action=self.page.locator('[data-catalog-product="123"] .actions-cell')
        # 不依赖实现方式：初始、中间、末尾及不同窗口宽度都能直接看到同一操作列。
        for width in [900,1320,390,2048]:
            self.page.set_viewport_size({'width':width,'height':1000})
            for fraction in [0,.5,1]:
                wrapper.evaluate('(n,f)=>n.scrollLeft=(n.scrollWidth-n.clientWidth)*f',fraction)
                bounds=wrapper.bounding_box();head=header.bounding_box();cell=action.bounding_box()
                for box in [head,cell]:
                    self.assertLess(abs(box['x']+box['width']-(bounds['x']+bounds['width']-1)),2)
                    self.assertGreaterEqual(box['x'],bounds['x'])
        self.page.set_viewport_size({'width':900,'height':1000})
        wrapper.evaluate('(n)=>n.scrollLeft=0')
        original=self.page.locator('.catalog-title').bounding_box()['x']
        wrapper.evaluate('(n)=>n.scrollLeft=300')
        self.assertAlmostEqual(self.page.locator('.catalog-title').bounding_box()['x'],original-300,delta=2)
        action.hover()
        self.page.wait_for_function("() => getComputedStyle(document.querySelector('.catalog-table .actions-cell')).backgroundColor===getComputedStyle(document.querySelector('.catalog-table tbody td')).backgroundColor")
        wrapper.evaluate('(n)=>n.scrollLeft=n.scrollWidth')
        action.get_by_role('link',name='查看详情',exact=True).click()
        expect(self.page.locator('[data-collection-product="123"]')).to_be_visible()
        self.assertEqual(self.host.errors,[])

    def test_collect_targets_row_and_manual_continue_stop(self):
        """多商品补采绑定所点行，沿用人工继续停止，既有模块会跳过。"""
        self.goto()
        row456=self.page.locator('[data-catalog-product="456"]')
        row456.locator('.collect-remaining').click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('商品 456')
        job=jobs.rows(self.host.settings)[0]
        self.assertEqual(job['product_id'],'456');self.assertEqual(len(job['modules']),9)
        job['state']='running';jobs.save(self.host.settings,job)
        self.host.publish(job)
        modal=self.page.locator('#collection-attention')
        expect(modal).to_be_visible(timeout=7000);expect(modal).to_contain_text('商品 456')
        modal.get_by_role('button',name='继续',exact=True).click()
        expect(modal).not_to_be_visible()
        self.assertEqual(self.host.command(job)['action'],'continue')
        self.host.publish(job,prompt='c'*32)
        expect(modal).to_be_visible(timeout=7000)
        modal.get_by_role('button',name='停止',exact=True).click()
        self.assertEqual(self.host.command(job)['action'],'stop')
        job.update(state='cancelled',message='已停止',result={'valid':False,'modules':[]});jobs.save(self.host.settings,job)
        button=self.page.locator('[data-catalog-product="123"] .collect-remaining')
        expect(button).to_be_enabled(timeout=7000)
        button.click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('商品 123')
        latest=jobs.rows(self.host.settings)[0]
        self.assertEqual(latest['product_id'],'123');self.assertNotIn('parameters',latest['modules'])
        self.assertEqual(self.host.errors,[])

    def test_source_and_state_filters_reset_and_errors(self):
        """来源及资料筛选独立，重置恢复全部，非法区间明确报错。"""
        self.goto()
        self.page.get_by_label('资料来源',exact=True).select_option('registered')
        self.page.get_by_role('button',name='查询',exact=True).click()
        expect(self.page.locator('.catalog-table tbody tr')).to_have_count(1)
        expect(self.page.locator('.catalog-table tbody tr')).to_have_attribute('data-catalog-product','456')
        self.page.get_by_role('link',name='重置',exact=True).click()
        expect(self.page.locator('.catalog-table tbody tr')).to_have_count(2)
        self.page.get_by_label('资料情况',exact=True).select_option('partial')
        self.page.get_by_role('button',name='查询',exact=True).click()
        expect(self.page.locator('.catalog-table tbody tr')).to_have_attribute('data-catalog-product','123')
        response=self.page.goto('http://127.0.0.1/collections/details?sales_min=10&sales_max=1')
        self.assertEqual(response.status,400)
        expect(self.page.locator('body')).to_contain_text('销量最小值不能大于最大值')
        self.assertEqual(self.host.errors,[])

    def test_material_source_and_latest_detail_entry_are_distinct(self):
        """已有搜索词与直接入口独立显示，搜索入口和未知入口各按本商品证据展示。"""
        direct='d'*32
        self.f.put('tasks',id=direct,name='collection.taobao.detail.open',status='succeeded',started_at=self.f.now)
        self.f.put('search_records',id=30,product_id=1,task_id=direct,record_type='detail_open',captured_at=self.f.now,
            payload={'record':{'task_id':direct,'product_id':'123','input_type':'id'}})
        self.f.put('collection_runs',id=30,product_id=1,task_id=direct,entry_record_id=30,status='complete',started_at=self.f.now)
        self.f.put('products',id=3,platform='taobao',platform_product_id='789',display_title='搜索进入的商品')
        self.f.put('list_observations',id=30,batch_id=11,product_id=3,slot_index=3,title='搜索进入的商品')
        self.f.put('search_records',id=31,product_id=3,task_id=self.f.task,record_type='detail_open',
            source_list_observation_id=30,keyword='粘毛器',captured_at=self.f.now)
        self.f.put('collection_runs',id=31,product_id=3,task_id=self.f.task,entry_record_id=31,status='complete',started_at=self.f.now)
        self.f.put('collection_runs',id=32,product_id=2,task_id=self.f.task,status='failed',started_at=self.f.now)
        self.goto()
        expect(self.page.get_by_role('columnheader',name='资料来源',exact=True)).to_be_visible()
        row=self.page.locator('[data-catalog-product="123"]')
        expect(row.locator('.catalog-origin')).to_have_text('列表与详情')
        expect(row.locator('.catalog-keyword')).to_have_text('粘毛器')
        expect(row.locator('.catalog-detail-entry')).to_have_attribute('data-entry-kind','direct')
        expect(row.locator('.catalog-detail-entry')).to_contain_text('直接采集')
        expect(row.locator('.catalog-detail-entry')).not_to_contain_text('入口搜索词')
        search=self.page.locator('[data-catalog-product="789"] .catalog-detail-entry')
        expect(search).to_have_attribute('data-entry-kind','search')
        expect(search).to_contain_text('搜索列表进入');expect(search).to_contain_text('入口搜索词：粘毛器')
        unknown=self.page.locator('[data-catalog-product="456"] .catalog-detail-entry')
        expect(unknown).to_have_attribute('data-entry-kind','unknown');expect(unknown).to_contain_text('入口未记录')
        self.assertEqual(self.host.errors,[])
