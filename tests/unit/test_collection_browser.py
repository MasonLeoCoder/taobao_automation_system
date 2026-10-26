"""资料浏览的离线行为验证；临时 SQLite 仅承载查询样例，不模拟正式库迁移。"""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import copy
import hashlib
import tempfile
import unittest

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_browser import browse
from ecommerce.infrastructure.storage.collection_queries import records
from ecommerce.infrastructure.storage.mysql_migrations.schema_v7 import build_metadata
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app


class CollectionBrowserTests(unittest.TestCase):
    def setUp(self):
        """建立独立查询样例和临时网页服务；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base=load_settings(root=Path(temp.name))
        self.app=create_app(base)
        self.settings=replace(base,collection_backend='mysql')
        self.app.config['SETTINGS']=self.settings
        self.client=self.app.test_client()
        # 查询测试使用同版字段类型，省略正式库约束；正式迁移由既有集成测试覆盖。
        metadata=sa.MetaData()
        for table in build_metadata().tables.values():
            columns=[]
            for column in table.c:
                kind=copy.copy(column.type)
                if hasattr(kind,'collation'):
                    kind.collation=None
                columns.append(sa.Column(column.name,kind,nullable=True))
            sa.Table(table.name,metadata,*columns)
        self.engine=sa.create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.store=SimpleNamespace(engine=self.engine,tables=metadata.tables)
        for target in ['ecommerce.infrastructure.storage.collection_browser.collection_store',
                       'ecommerce.infrastructure.storage.collection_queries.collection_store']:
            mock=patch(target,return_value=self.store)
            mock.start()
            self.addCleanup(mock.stop)
        self.task='a'*32
        self.now=datetime(2026,10,25,12)
        self.put('tasks',id=self.task,name='taobao.collect',status='succeeded',started_at=self.now)
        self.put('products',id=1,platform='taobao',platform_product_id='123',display_title='<script>商品</script>')
        self.put('products',id=2,platform='taobao',platform_product_id='456',display_title='仅暂存')
        for identifier,page in [(10,1),(11,2)]:
            self.batch(identifier,'list',page_number=page,search_keyword='粘毛器')
            self.put('list_observations',id=identifier,batch_id=identifier,product_id=1,slot_index=page,title='样例',price_text='10元')
        self.batch(20,'parameters',product_id=1)
        self.put('parameter_observations',id=20,batch_id=20,name='同名参数',value='A',position=1)
        self.put('parameter_observations',id=21,batch_id=20,name='同名参数',value='B',position=2)
        self.put('product_current_sections',product_id=1,section='parameters',module='parameters',batch_id=20)
        self.batch(21,'parameters',product_id=1,status='partial')
        self.batch(22,'main_images',product_id=2,visibility='staging')

    def put(self,table_name,**values):
        """插入本测试样例；参数：表名和字段；返回：无。"""
        with self.engine.begin() as c:
            c.execute(self.store.tables[table_name].insert().values(**values))

    def test_module_skip_requires_exact_task_product_and_latest_confirmed_step(self):
        """跳过查询绑定当前任务商品，较新失败步骤不能借旧跳过放行；参数：无；返回：无。"""
        self.put('collection_runs',id=80,task_id=self.task,product_id=1,started_at=self.now,status='complete_with_warnings')
        record={'status':'skipped','stop_reason':'entry_not_found','message':'未找到问大家入口，已跳过',
                'entry_search':{'scroll_count':3,'max_scrolls':10,'unchanged_count':3,'stop_reason':'stable_position'}}
        self.put('collection_steps',id=80,run_id=80,module='questions',requested=True,status='skipped',
                 continuation_allowed=True,finished_at=self.now,payload={'record':record})
        result=browse(self.settings,'module_skip',task_id=self.task,product_id='123')
        self.assertTrue(result['skipped'])
        self.assertFalse(browse(self.settings,'module_skip',task_id='b'*32,product_id='123')['skipped'])
        self.assertFalse(browse(self.settings,'module_skip',task_id=self.task,product_id='456')['skipped'])
        self.put('collection_runs',id=81,task_id=self.task,product_id=1,started_at=self.now,status='failed')
        self.put('collection_steps',id=81,run_id=81,module='questions',requested=True,status='failed',
                 continuation_allowed=False,finished_at=self.now,payload={'record':{}})
        self.assertFalse(browse(self.settings,'module_skip',task_id=self.task,product_id='123')['skipped'])

    def batch(self,identifier,module,**extra):
        """建立已发布批次；参数：编号、模块和覆盖字段；返回：无。"""
        values=dict(id=identifier,module=module,task_id=self.task,visibility='published',status='complete',
            captured_at=self.now,coverage_status='unknown',collection_mode='limited',requested_limit=3)
        values.update(extra)
        self.put('collection_batches',**values)

    def test_search_group_preserves_duplicate_positions_and_filter(self):
        """一次任务合并页记录，跨页重复保留，来源筛选使用真实搜索词。"""
        result=browse(self.settings,'searches',source='粘毛器')['searches']
        self.assertEqual(len(result),1)
        self.assertEqual((result[0]['pages'],result[0]['count'],result[0]['unique_count']),(2,2,1))
        self.assertEqual(browse(self.settings,'products',source='不存在')['products'],[])
        self.assertEqual(len(browse(self.settings,'products',source='粘毛器')['products']),1)
        self.assertEqual(records(self.settings,10)['records'][0]['platform_product_id'],'123')

    def test_current_partition_history_and_staging_are_separate(self):
        """部分尝试不覆盖有效资料，暂存记录不进入网页。"""
        data=browse(self.settings,'product',product_id='123')
        self.assertEqual(data['current_ids'],[20])
        self.assertIn(21,[b['id'] for b in data['history']])
        self.assertEqual(len(browse(self.settings,'products')['products']),1)
        registered=browse(self.settings,'product',product_id='456')
        self.assertEqual(registered['history'],[])
        self.assertEqual(registered['current_ids'],[])
        page=self.client.get('/collections/products/123?module=parameters').get_data(as_text=True)
        self.assertIn('批次 20',page)
        self.assertIn('id="parameter-value-20" class="parameter-value-text">A</div>',page)
        self.assertIn('id="parameter-value-21" class="parameter-value-text">B</div>',page)
        self.assertNotIn('<script>商品</script>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertEqual(self.client.get('/collections/products/123?batch=22').status_code,404)

    def test_pagination_dates_and_invalid_requests(self):
        """筛选、分页和无效输入给出真实边界，不静默返回全部。"""
        result=browse(self.settings,'searches',limit=1)
        self.assertFalse(result['has_more'])
        self.assertEqual(browse(self.settings,'products',since='2026-10-26')['products'],[])
        self.assertEqual(len(browse(self.settings,'products',until='2026-10-25')['products']),1)
        for path in ['/collections?page=-1','/collections?page=no','/collections?since=bad',
                     '/collections?since=2026-10-26&until=2026-10-25','/collections?state=bad',
                     '/collections/products/123?module=bad','/collections/searches/bad']:
            self.assertEqual(self.client.get(path).status_code,400,path)
        self.assertEqual(self.client.get('/collections/searches/'+self.task+'?batch=20').status_code,404)
        for path in ['/collections','/collections?view=searches','/collections/products/123',
                     '/collections/products/123?module=history','/collections/searches/'+self.task,'/tasks/'+self.task]:
            self.assertEqual(self.client.get(path).status_code,200,path)

    def test_media_only_serves_unchanged_published_output(self):
        """文件访问限于已发布输出素材，拒绝越界、登录资料和变更原件。"""
        data=b'example-image'
        target=self.settings.workspace/'output/raw/test.jpg'
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
        self.batch(30,'main_images',product_id=1)
        for identifier,path in [(1,'output/raw/test.jpg'),(2,'../secret.jpg'),(3,'storage/sessions/secret.jpg')]:
            self.put('file_assets',id=identifier,root_key='workspace',relative_path=path,availability='present',
                size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
            self.put('media_references',id=identifier,batch_id=30,role='main_image',file_asset_id=identifier)
        response=self.client.get('/collections/files/1')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data,data)
        response.close()
        self.assertEqual(self.client.get('/collections/files/2').status_code,404)
        self.assertEqual(self.client.get('/collections/files/3').status_code,404)
        target.write_bytes(b'changed')
        self.assertEqual(self.client.get('/collections/files/1').status_code,409)
        with self.engine.begin() as c:
            t=self.store.tables['collection_batches']
            c.execute(t.update().where(t.c.id==30).values(visibility='staging'))
        self.assertEqual(self.client.get('/collections/files/1').status_code,404)

    def test_unassigned_batches_and_entry_only_product(self):
        """无任务批次分别展示，只有详情入口的商品仍可查看未采集状态。"""
        self.batch(40,'list',task_id=None,search_keyword='旧来源')
        self.batch(41,'list',task_id=None,search_keyword='旧来源')
        result=browse(self.settings,'searches',source='旧来源')['searches']
        self.assertEqual(len(result),2)
        self.assertEqual([r['pages'] for r in result],[1,1])
        self.assertEqual(self.client.get('/collections?view=searches&source=旧来源').status_code,200)
        self.assertEqual(self.client.get('/collections/batches/40').status_code,200)
        self.put('products',id=3,platform='taobao',platform_product_id='789',display_title='仅详情入口')
        self.put('search_records',id=3,product_id=3,record_type='detail_open')
        self.assertEqual(self.client.get('/collections/products/789').status_code,200)

    def test_record_pagination_reports_real_next_page(self):
        """下一页由多取一条判断，不在最后一页显示无效入口。"""
        first=records(self.settings,20,limit=1)
        self.assertTrue(first['has_more'])
        second=records(self.settings,20,limit=1,after=first['next_cursor'])
        self.assertFalse(second['has_more'])
        self.assertEqual(second['records'][0]['value'],'B')

    def test_numbered_pages_cover_products_searches_history_and_media(self):
        """各类资料均固定20条，末页置灰，页码保留筛选和批次。"""
        import re
        for i in range(45):
            self.put('products',id=i+3,platform='taobao',platform_product_id=str(1000+i),display_title='分页样例')
            self.put('search_records',id=100+i,product_id=i+3,record_type='detail_open',captured_at=self.now)
            self.put('list_observations',id=100+i,batch_id=10,product_id=i+3,slot_index=i+3,title='分页样例')
            self.batch(100+i,'list',task_id=f'{i+1:032x}',search_keyword='测试词')
            self.batch(300+i,'parameters',product_id=1)
        pages=[browse(self.settings,'products',page=p,source='粘毛器') for p in (1,2,3)]
        self.assertEqual([len(p['products']) for p in pages],[20,20,6])
        self.assertEqual(pages[-1]['pages'],3)
        search_pages=[browse(self.settings,'searches',page=p,source='测试词') for p in (1,2,3)]
        self.assertEqual([len(p['searches']) for p in search_pages],[20,20,5])
        record_pages=[records(self.settings,10,page=p) for p in (1,2,3)]
        self.assertEqual([len(p['records']) for p in record_pages],[20,20,6])
        self.batch(70,'skus',product_id=1)
        self.put('product_current_sections',product_id=1,section='sku_details',module='skus',batch_id=70)
        for i in range(45):
            self.put('sku_observations',id=200+i,batch_id=70,position=i+1,sku_name='规格'+str(i))
            self.put('media_references',id=200+i,batch_id=70,role='sku_image',position=i+1)
        for path in ['/collections?page=2&source=粘毛器','/collections?view=searches&page=2&category=测试词',
                     '/collections/batches/10?page=2',
                     '/collections/searches/'+self.task+'?batch=10&page=2',
                     '/collections/products/123?module=history&page=2',
                     '/collections/products/123?module=skus&page=2&media_page=2']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200,path)
            body=response.get_data(as_text=True)
            table=re.search(r'<tbody>(.*?)</tbody>',body,re.S).group(1)
            self.assertEqual(len(re.findall(r'<tr(?:\s|>)',table)),20,path)
            self.assertIn('page=3',body)
            self.assertNotIn('after=',body)
            if 'module=skus' in path:
                self.assertEqual(body.count('<figure>'),0)
                self.assertNotIn('media_page=3',body)
                self.assertEqual(body.count('class="sku-image-cell"'),21)
        last=self.client.get('/collections?page=3&source=粘毛器').get_data(as_text=True)
        self.assertNotIn('rel="next"',last)
        self.assertIn('aria-disabled="true">下一页',last)
        self.assertEqual(self.client.get('/collections?page=4&source=粘毛器').status_code,404)

    def test_sorting_uses_actual_capture_times_prices_and_record_fields(self):
        """商品跨页按实际时间排序，价格按数值排序，历史及参数支持正反序。"""
        from ecommerce.modules.collection.queries import history_page
        for i in range(23):
            self.put('products',id=i+3,platform='taobao',platform_product_id=str(1000+i),display_title='样例'+str(i))
            self.batch(100+i,'list',captured_at=datetime(2026,10,i+1,12))
            self.put('list_observations',id=100+i,batch_id=100+i,product_id=i+3,slot_index=1,price_value=100-i)
        asc=[browse(self.settings,'products',page=p,sort_by='captured_at',order='asc') for p in (1,2)]
        desc=[browse(self.settings,'products',page=p,sort_by='captured_at',order='desc') for p in (1,2)]
        self.assertEqual([r['id'] for p in asc for r in p['products']],list(reversed([r['id'] for p in desc for r in p['products']])))
        self.assertEqual(asc[0]['products'][0]['id'],3)
        self.assertEqual(desc[0]['products'][0]['id'],1)
        priced=browse(self.settings,'products',page=1,sort_by='price',order='desc')['products']
        self.assertEqual(priced[0]['id'],3)
        data=records(self.settings,20,page=1,sort_by='value',order='desc')
        self.assertEqual([r['value'] for r in data['records']],['B','A'])
        history=browse(self.settings,'product',product_id='123')['history']
        self.assertEqual(history_page(history,sort_by='captured_at',order='asc')['history_rows'][0]['id'],10)
        for path in ['/collections?sort=bad','/collections/batches/20?sort=bad','/collections/products/123?module=history&order=bad']:
            self.assertEqual(self.client.get(path).status_code,400)

    def test_independent_detail_index_does_not_require_lists_or_content(self):
        """列表独有、详情独有、仅入口和失败运行均保留各自记录，不按标题猜关系。"""
        self.put('products',id=3,platform='taobao',platform_product_id='789',display_title='样例')
        self.put('search_records',id=30,product_id=3,record_type='detail_open',captured_at=self.now)
        self.put('collection_runs',id=30,product_id=3,entry_record_id=30,status='complete',started_at=self.now,task_id=self.task)
        self.batch(30,'parameters',product_id=3,run_id=30)
        self.put('product_current_sections',product_id=3,section='parameters',module='parameters',batch_id=30)
        self.put('products',id=4,platform='taobao',platform_product_id='999',display_title='样例')
        self.put('list_observations',id=14,batch_id=10,product_id=4,slot_index=4,title='样例',price_text='9元')
        self.put('products',id=5,platform='taobao',platform_product_id='888',display_title='仅入口')
        self.put('search_records',id=50,product_id=5,record_type='detail_open',captured_at=self.now)
        self.put('products',id=6,platform='taobao',platform_product_id='777',display_title='运行失败')
        self.put('collection_runs',id=60,product_id=6,status='failed',started_at=self.now,task_id=self.task)
        indexed=browse(self.settings,'details',page=1)
        self.assertEqual({r['platform_product_id'] for r in indexed['products']},{'123','789','888','777'})
        self.assertEqual(browse(self.settings,'sources',product_id='789',page=1)['total'],0)
        for pid in ('123','789','888','777','999'):
            self.assertEqual(self.client.get('/collections/products/'+pid).status_code,200)
        only=browse(self.settings,'product',product_id='888')['detail_summary']
        self.assertFalse(only['has_content'])
        self.assertIn('仅有详情入口',only['label'])
        attempt=browse(self.settings,'product',product_id='777')['detail_summary']
        self.assertEqual(attempt['last_attempt_label'],'失败')
        self.assertEqual(self.client.get('/collections/products/789?observation=10').status_code,404)
        rows=records(self.settings,10,page=1,with_context=True)['records']
        absent=next(r for r in rows if r['product_id']==4)
        self.assertEqual(absent['detail_context']['relation'],'none')
        self.assertEqual(absent['title'],'样例')
        self.assertEqual(absent['price_text'],'9元')

    def test_explicit_source_and_same_task_never_fill_current_gaps_with_old_data(self):
        """准确区分入口、同任务共享与其他时间，关联运行缺失模块不回退旧资料。"""
        self.put('search_records',id=70,product_id=1,record_type='detail_open',captured_at=datetime(2026,10,24))
        self.put('collection_runs',id=70,product_id=1,entry_record_id=70,status='complete',started_at=datetime(2026,10,24),task_id='b'*32)
        self.put('search_records',id=80,product_id=1,record_type='detail_open',source_list_observation_id=10,captured_at=self.now)
        self.put('collection_runs',id=80,product_id=1,entry_record_id=80,status='partial',started_at=self.now,task_id=self.task)
        with self.engine.begin() as c:
            b=self.store.tables['collection_batches']
            c.execute(b.update().where(b.c.id==20).values(run_id=70))
            c.execute(b.update().where(b.c.id==21).values(run_id=80))
        self.batch(23,'main_images',product_id=1,run_id=70)
        self.put('product_current_sections',product_id=1,section='main_images',module='main_images',batch_id=23)
        exact=browse(self.settings,'product',product_id='123',observation_id=10)['source_context']
        shared=browse(self.settings,'product',product_id='123',observation_id=11)['source_context']
        self.assertEqual((exact['relation'],shared['relation']),('entry','same_task'))
        self.assertEqual(exact['context_run']['id'],80)
        page=self.client.get('/collections/products/123?observation=10&module=parameters').get_data(as_text=True)
        self.assertIn('批次 21',page)
        self.assertNotIn('id="parameter-value-20"',page)
        missing=self.client.get('/collections/products/123?observation=10&module=main_images').get_data(as_text=True)
        self.assertIn('本次关联运行没有保存该模块',missing)
        manual=self.client.get('/collections/products/123?observation=10&module=parameters&batch=20').get_data(as_text=True)
        self.assertIn('已手动切换历史批次',manual)
        self.assertIn('id="parameter-value-20" class="parameter-value-text">A</div>',manual)
        self.put('tasks',id='c'*32,name='collect',status='succeeded',started_at=self.now)
        self.batch(90,'list',task_id='c'*32)
        self.put('list_observations',id=90,batch_id=90,product_id=1,slot_index=1)
        other=browse(self.settings,'product',product_id='123',observation_id=90)['source_context']
        self.assertEqual(other['relation'],'other')
        self.assertFalse(other['context_run'])

    def test_navigation_returns_to_filtered_sorted_list_and_rejects_external_urls(self):
        """双向跳转保留列表状态，返回地址只能指向站内。"""
        from html import unescape
        import re
        from urllib.parse import parse_qs,urlsplit
        path='/collections/searches/'+self.task+'?batch=10&sort=price&order=desc&page=1'
        body=self.client.get(path).get_data(as_text=True)
        href=unescape(re.search(r'href="([^"]*/collections/products/123\?[^\"]+)"',body).group(1))
        args=parse_qs(urlsplit(href).query)
        self.assertEqual(args['observation'],['10'])
        self.assertEqual(parse_qs(urlsplit(args['return_to'][0]).query)['sort'],['price'])
        detail=self.client.get(href).get_data(as_text=True)
        self.assertIn('返回上一列表',detail)
        self.assertIn('sort=price&amp;order=desc&amp;page=1',detail)
        for bad in ['https://example.invalid','//example.invalid','/\\example.invalid']:
            page=self.client.get('/collections/products/123',query_string={'return_to':bad}).get_data(as_text=True)
            self.assertNotIn('href="'+bad,page)
        self.assertEqual(self.client.get('/collections/products/123?module=sources').status_code,200)

    def test_media_preview_reports_missing_or_changed_without_deleting_records(self):
        """展示原件缺失和校验变化，原采集记录与文件索引不受影响。"""
        from ecommerce.modules.collection.queries import query
        data=b'original'
        path=self.settings.workspace/'output/raw/check.jpg'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        self.batch(40,'main_images',product_id=1)
        self.put('file_assets',id=40,root_key='workspace',relative_path='output/raw/check.jpg',availability='present',
            size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        self.put('media_references',id=40,batch_id=40,role='main_image',position=1,file_asset_id=40)
        def state():
            return query(self.settings,'records',batch_id=40,page=1)['records'][0]['preview_status']
        self.assertEqual(state(),'present')
        path.write_bytes(b'modified')
        self.assertEqual(state(),'changed')
        path.unlink()
        self.assertEqual(state(),'missing')
        self.assertEqual(len(records(self.settings,40)['records']),1)
        self.assertEqual(records(self.settings,40)['records'][0]['file']['availability'],'present')

    def test_keyword_classification_counts_and_drilldown_isolate_other_words(self):
        """搜索词跨任务汇总，重复商品单独计记录，未记录词与字面同名词不混淆。"""
        self.put('tasks',id='b'*32,name='collect',status='succeeded',started_at=self.now)
        self.batch(30,'list',task_id='b'*32,search_keyword='粘毛器',captured_at=datetime(2026,10,24,12))
        self.put('list_observations',id=30,batch_id=30,product_id=1,slot_index=1)
        self.batch(40,'list',search_keyword='其他词')
        self.put('list_observations',id=40,batch_id=40,product_id=1,slot_index=1)
        self.batch(50,'list',task_id=None,search_keyword=None)
        self.batch(51,'list',task_id=None,search_keyword='')
        self.batch(60,'list',search_keyword='搜索词未记录')
        grouped=browse(self.settings,'keyword_groups',page=1)
        self.assertEqual(grouped['total'],4)
        words={r['keyword']:r for r in grouped['keyword_groups']}
        self.assertEqual((words['粘毛器']['tasks'],words['粘毛器']['pages'],words['粘毛器']['count'],words['粘毛器']['unique_count']),(2,3,3,1))
        self.assertEqual((words[None]['tasks'],words[None]['pages']),(2,2))
        day=browse(self.settings,'keyword_groups',page=1,since='2026-10-25',until='2026-10-25',source='粘毛器')['keyword_groups'][0]
        self.assertEqual((day['tasks'],day['pages'],day['count']),(1,2,2))
        tasks=browse(self.settings,'searches',page=1,category='粘毛器')['searches']
        own=next(r for r in tasks if r['task_id']==self.task)
        self.assertEqual((own['pages'],own['count'],own['keywords']),(2,2,['粘毛器']))
        batches=browse(self.settings,'search',task_id=self.task,category='粘毛器')['pages']
        self.assertEqual([b['id'] for b in batches],[10,11])
        self.assertEqual(self.client.get('/collections/searches/'+self.task+'?category=粘毛器&batch=40').status_code,404)
        page=self.client.get('/collections/searches').get_data(as_text=True)
        self.assertIn('搜索词分类',page)
        self.assertIn('查看商品',page)
        self.assertIn('category_missing=1',page)
        self.assertEqual(self.client.get('/collections/searches?category_missing=1').status_code,200)

    def test_keyword_groups_have_twenty_rows_and_preserve_parent_filters(self):
        """分类页按20词分页，进入分类保留日期，末页无可点击下一页。"""
        import re
        from html import unescape
        from urllib.parse import parse_qs,urlsplit
        for i in range(21): self.batch(100+i,'list',search_keyword='词'+str(i),task_id=None)
        first=browse(self.settings,'keyword_groups',page=1)
        second=browse(self.settings,'keyword_groups',page=2)
        self.assertEqual((len(first['keyword_groups']),len(second['keyword_groups'])),(20,2))
        body=self.client.get('/collections/searches?page=2&since=2026-10-25&sort=keyword&order=asc').get_data(as_text=True)
        self.assertNotIn('rel="next"',body)
        self.assertIn('aria-disabled="true">下一页',body)
        link=unescape(re.search(r'href="([^"]+)"[^>]*>查看商品',body).group(1))
        args=parse_qs(urlsplit(link).query)
        self.assertEqual(args['since'],['2026-10-25'])
        self.assertIn('page=2',args['return_to'][0])
        self.assertEqual(self.client.get(link).status_code,200)

    def test_keyword_products_pick_one_whole_latest_observation_within_dates(self):
        """最新商品行不拼接历史字段，限定日期后选取范围内最新观察。"""
        self.batch(30,'list',search_keyword='粘毛器',captured_at=datetime(2026,10,26,12))
        self.put('list_observations',id=30,batch_id=30,product_id=1,slot_index=1,title='新标题',price_text='2元',price_value=2,
            shop_name='新店',sales_text='新销量',location_text='新地区')
        latest=browse(self.settings,'keyword_products',category='粘毛器',page=1)
        self.assertEqual((latest['scope_products'],latest['scope_records'],latest['total']),(1,3,1))
        row=latest['observations'][0]
        self.assertEqual((row['id'],row['title'],row['price_text'],row['shop_name'],row['occurrences']),(30,'新标题','2元','新店',3))
        older=browse(self.settings,'keyword_products',category='粘毛器',page=1,until='2026-10-25')
        self.assertEqual((older['observations'][0]['id'],older['observations'][0]['price_text']),(11,'10元'))
        self.assertEqual(browse(self.settings,'keyword_products',category='粘毛器',page=1,keyword='样例')['total'],0)
        history=browse(self.settings,'keyword_products',category='粘毛器',page=1,mode='records',keyword='样例')
        self.assertEqual(history['total'],2)
        self.assertEqual(browse(self.settings,'keyword_products',category='粘毛器',page=1,shop='新店')['total'],1)
        self.assertEqual(browse(self.settings,'keyword_products',category='其他词',page=1)['total'],0)

    def test_keyword_product_pagination_keeps_list_only_and_unidentified_records(self):
        """列表独有商品不会因缺详情而消失，身份不明的同名观察不合并。"""
        import re
        for i in range(43):
            self.put('products',id=100+i,platform='taobao',platform_product_id=str(2000+i),display_title='索引')
            self.put('list_observations',id=100+i,batch_id=10,product_id=100+i,slot_index=100+i,title='列表商品'+str(i),price_value=i)
        self.put('list_observations',id=200,batch_id=10,product_id=None,slot_index=200,title='未确认同名')
        self.put('list_observations',id=201,batch_id=10,product_id=None,slot_index=201,title='未确认同名')
        pages=[browse(self.settings,'keyword_products',category='粘毛器',page=p) for p in (1,2,3)]
        self.assertEqual([len(p['observations']) for p in pages],[20,20,6])
        self.assertEqual(len({r['id'] for p in pages for r in p['observations']}),46)
        self.assertEqual(browse(self.settings,'keyword_products',category='粘毛器',page=1,state='none')['total'],45)
        for mode in ('summary','records'):
            body=self.client.get('/collections/searches/products',query_string={'category':'粘毛器','mode':mode,'page':2,'sort':'price','order':'asc'}).get_data(as_text=True)
            table=re.search(r'<tbody>(.*?)</tbody>',body,re.S).group(1)
            self.assertEqual(len(re.findall(r'<tr(?:\s|>)',table)),20)
            self.assertIn('sort=price',body)
            self.assertIn('查看历次采集',body)
        self.assertEqual(self.client.get('/collections/searches/products?category=粘毛器&sort=bad').status_code,400)
        self.assertEqual(self.client.get('/collections/searches/products?category=粘毛器&mode=bad').status_code,400)

    def test_keyword_product_links_preserve_history_dates_and_detail_source(self):
        """分类直接进入商品，详情携带观察来源，商品历史保持词及日期。"""
        import re
        from html import unescape
        from urllib.parse import parse_qs,urlsplit
        home=self.client.get('/collections/searches?since=2026-10-25').get_data(as_text=True)
        href=unescape(re.search(r'href="([^"]+)"[^>]*>查看商品',home).group(1))
        self.assertIn('/collections/searches/products?',href)
        body=self.client.get(href).get_data(as_text=True)
        detail=unescape(re.search(r'href="([^"]+)"[^>]*>查看详情资料',body).group(1))
        args=parse_qs(urlsplit(detail).query)
        self.assertEqual(args['observation'],['11'])
        self.assertIn('/collections/searches/products?',args['return_to'][0])
        history=unescape(re.search(r'href="([^"]+)"[^>]*>该商品采集历史',body).group(1))
        args=parse_qs(urlsplit(history).query)
        self.assertEqual((args['mode'],args['product'],args['since']),(['records'],['123'],['2026-10-25']))
        self.assertEqual(self.client.get(detail).status_code,200)
        self.assertEqual(self.client.get(history).status_code,200)


    def test_list_thumbnail_exact_source_click_and_missing_file(self):
        """最新记录不借用旧图；同批次位置图片可打开；文件缺失时显示明确提示。"""
        root=self.settings.workspace/'output/list-images'
        root.mkdir(parents=True)
        image=root/'sample.png'
        image.write_bytes(b'local-image-fixture')
        self.put('file_assets',id=80,root_key='workspace',relative_path=image.relative_to(self.settings.workspace).as_posix(),
                 size_bytes=image.stat().st_size,sha256=hashlib.sha256(image.read_bytes()).hexdigest(),availability='present')
        self.put('media_references',id=80,batch_id=10,role='list_image',position=1,file_asset_id=80,download_status='downloaded')
        url='/collections/searches/products?category=粘毛器'
        body=self.client.get(url).get_data(as_text=True)
        self.assertNotIn('/collections/files/80',body)
        media=self.store.tables['media_references']
        with self.engine.begin() as c:
            c.execute(media.update().where(media.c.id==80).values(batch_id=11,position=2))
        body=self.client.get(url).get_data(as_text=True)
        self.assertIn('/collections/files/80',body)
        self.assertIn('查看当次列表主图',body)
        response=self.client.get('/collections/files/80')
        self.assertEqual(response.status_code,200)
        response.close()
        image.unlink()
        body=self.client.get(url).get_data(as_text=True)
        self.assertIn('本地图片缺失',body)
        self.assertNotIn('/collections/files/80',body)

    def test_ad_filter_applies_after_latest_and_keeps_unknown_separate(self):
        """汇总按最新观察筛广告，不用旧广告替代；未确认包含空值，未知参数拒绝。"""
        table=self.store.tables['list_observations']
        with self.engine.begin() as c:
            c.execute(table.update().where(table.c.id==10).values(ad_status='identified'))
            c.execute(table.update().where(table.c.id==11).values(ad_status='not_identified'))
        for identifier,status in [(30,'unconfirmed'),(31,None)]:
            self.put('list_observations',id=identifier,batch_id=10,product_id=None,slot_index=identifier,title='未确认',ad_status=status)
        def filtered(status,mode='summary'):
            return browse(self.settings,'keyword_products',category='粘毛器',ad_status=status,mode=mode,page=1)
        self.assertEqual(filtered('identified')['total'],0)
        self.assertEqual([r['id'] for r in filtered('identified','records')['observations']],[10])
        self.assertEqual([r['id'] for r in filtered('not_identified')['observations']],[11])
        self.assertEqual(filtered('unconfirmed')['total'],2)
        self.assertEqual(filtered('')['total'],3)
        self.assertEqual(filtered('')['scope_records'],4)
        self.assertEqual(self.client.get('/collections/searches/products?category=粘毛器&ad_status=invalid').status_code,400)

    def test_ad_filter_persists_across_pagination_sort_and_tabs(self):
        """广告筛选按20条分页，链接和跳页保留条件，重新查询和重置不保留旧页码。"""
        import re
        from html import unescape
        from urllib.parse import parse_qs,urlsplit
        for i in range(23):
            self.put('products',id=100+i,platform='taobao',platform_product_id=str(2000+i))
            self.put('list_observations',id=100+i,batch_id=10,product_id=100+i,slot_index=100+i,title='广告'+str(i),ad_status='identified')
        for mode in ('summary','records'):
            data=browse(self.settings,'keyword_products',category='粘毛器',ad_status='identified',mode=mode,page=2)
            self.assertEqual((data['total'],len(data['observations'])),(23,3))
            response=self.client.get('/collections/searches/products',query_string={'category':'粘毛器','ad_status':'identified','mode':mode,'sort':'title','order':'asc','page':2})
            self.assertEqual(response.status_code,200)
            body=response.get_data(as_text=True)
            self.assertRegex(body,r'<option value="identified" selected>已识别广告</option>')
            self.assertIn('name="ad_status" value="identified"',body)
            tabs=re.search(r'<div class="tabs" aria-label="商品视图">(.*?)</div>',body,re.S).group(1)
            for href in re.findall(r'href="([^"]+)"',tabs):
                args=parse_qs(urlsplit(unescape(href)).query)
                self.assertEqual((args['ad_status'],args['page']),(['identified'],['1']))
            sort_link=unescape(re.search(r'href="([^"]+)"[^>]*>商品标题',body).group(1))
            self.assertEqual(parse_qs(urlsplit(sort_link).query)['ad_status'],['identified'])
            reset=unescape(re.search(r'href="([^"]+)"[^>]*>重置',body).group(1))
            self.assertNotIn('ad_status',parse_qs(urlsplit(reset).query))
            form=re.search(r'<form class="panel filters"(.*?)</form>',body,re.S).group(1)
            self.assertNotIn('name="page"',form)

    def test_sales_filter_sort_latest_and_stable_pagination(self):
        """最新观察先确定再筛销量；空值末尾、边界包含、全量排序后每页20条。"""
        from ecommerce.shared.sales import parse_sales
        table=self.store.tables['list_observations']
        with self.engine.begin() as c:
            c.execute(table.update().where(table.c.id==10).values(**parse_sales('1万+人收货')))
            c.execute(table.update().where(table.c.id==11).values(**parse_sales('500人收货')))
        self.assertEqual(browse(self.settings,'keyword_products',category='粘毛器',sales_min='1000')['total'],0)
        self.assertEqual(browse(self.settings,'keyword_products',category='粘毛器',sales_min='1000',mode='records')['total'],1)
        for i in range(23):
            self.put('products',id=100+i,platform='taobao',platform_product_id=str(2000+i))
            self.put('list_observations',id=100+i,batch_id=10,product_id=100+i,slot_index=100+i,title='商品',**parse_sales(str(i)+'人付款'))
        self.put('list_observations',id=200,batch_id=10,product_id=None,slot_index=200,title='无数量',**parse_sales('行业热销'))
        pages=[browse(self.settings,'keyword_products',category='粘毛器',sort_by='sales',order='desc',page=page) for page in (1,2)]
        self.assertEqual([len(page['observations']) for page in pages],[20,5])
        all_rows=[r for page in pages for r in page['observations']]
        self.assertEqual([r['sales_count'] for r in all_rows],[500]+list(range(22,-1,-1))+[None])
        ascending=browse(self.settings,'keyword_products',category='粘毛器',sort_by='sales',order='asc',page=2)
        self.assertIsNone(ascending['observations'][-1]['sales_count'])
        selected=browse(self.settings,'keyword_products',category='粘毛器',sales_min='10',sales_max='12',sales_metric='paid_people',sales_precision='exact',sales_period='unknown')
        self.assertEqual([r['sales_count'] for r in selected['observations']],[12,11,10])
        for params in ({'sales_min':'-1'},{'sales_min':'2','sales_max':'1'}):
            self.assertEqual(self.client.get('/collections/searches/products',query_string={'category':'粘毛器',**params}).status_code,400)

    def test_sales_controls_keep_filters_and_first_sort_descending(self):
        """筛选及排序链接保留条件，首次点击销量按降序。"""
        import re
        from html import unescape
        from urllib.parse import parse_qs,urlsplit
        params={'category':'粘毛器','sales_min':'0','sales_max':'10000','ad_status':'not_identified'}
        body=self.client.get('/collections/searches/products',query_string=params).get_data(as_text=True)
        self.assertIn('按展示数量筛选',body)
        self.assertIn('销量范围',body)
        for name in ('sales_metric','sales_period','sales_precision'):
            self.assertNotIn('name="'+name+'"',body)
        link=unescape(re.search(r'href="([^\"]+)"[^>]*>销量 / 展示数量',body).group(1))
        args=parse_qs(urlsplit(link).query)
        self.assertEqual((args['sort'],args['order'],args['page']),(['sales'],['desc'],['1']))
        for name in params: self.assertEqual(args[name],[params[name]])
        tabs=re.search(r'<div class="tabs" aria-label="商品视图">(.*?)</div>',body,re.S).group(1)
        for href in re.findall(r'href="([^\"]+)"',tabs):
            query=parse_qs(urlsplit(unescape(href)).query)
            self.assertEqual(query['sales_max'],['10000'])

    def test_sales_range_blank_zero_and_removed_controls(self):
        """留空不限、0是零；旧链接移除隐藏口径条件，避免不可见筛选。"""
        from ecommerce.shared.sales import parse_sales
        self.put('list_observations',id=200,batch_id=10,product_id=None,slot_index=200,title='零',**parse_sales('0人收货'))
        self.put('list_observations',id=201,batch_id=10,product_id=None,slot_index=201,title='未知',**parse_sales(None))
        all_rows=browse(self.settings,'keyword_products',category='粘毛器',sales_min='',sales_max='')
        zero=browse(self.settings,'keyword_products',category='粘毛器',sales_min='0',sales_max='0')
        self.assertEqual(all_rows['total'],3)
        self.assertEqual([r['id'] for r in zero['observations']],[200])
        response=self.client.get('/collections/searches/products',query_string={'category':'粘毛器','sales_min':'0','sales_metric':'paid_people','sales_period':'unknown','sales_precision':'exact'})
        self.assertEqual(response.status_code,302)
        for name in ('sales_metric','sales_period','sales_precision'): self.assertNotIn(name,response.location)
        self.assertIn('sales_min=0',response.location)
        self.assertEqual(self.client.get(response.location).status_code,200)

    def test_search_records_merge_batches_preserve_sources_and_statistics(self):
        """任务批次合并保留重复，统计区别记录与去重身份，来源时间和页码不串位。"""
        self.put('list_observations',id=100,batch_id=10,product_id=None,slot_index=5,title='未知')
        self.batch(30,'list',task_id=self.task,search_keyword='其他词',page_number=1)
        self.put('list_observations',id=101,batch_id=30,product_id=2,slot_index=1,title='其他词商品')
        self.batch(31,'list',visibility='staging',page_number=3,search_keyword='粘毛器')
        self.put('list_observations',id=102,batch_id=31,product_id=2,slot_index=1,title='未发布')
        data=browse(self.settings,'search_records',task_id=self.task,category='粘毛器')
        self.assertEqual(data['summary'],{'record_count':3,'product_count':1,'unknown_count':1,'batch_count':2,'search_page_count':2,'unknown_page_batches':0})
        self.assertEqual({r['id'] for r in data['observations']},{10,11,100})
        for row in data['observations']:
            self.assertEqual(row['page_number'],1 if row['batch_id']==10 else 2)
            self.assertEqual(row['captured_at'],'2026-10-25T12:00:00.000000+08:00')
        own=browse(self.settings,'search_records',task_id=self.task,category='粘毛器',batch_id=10)
        self.assertEqual((own['summary']['record_count'],own['summary']['batch_count']),(2,1))
        self.assertEqual(own['selected_batch']['id'],10)
        empty=browse(self.settings,'search_records',task_id=self.task,since='2026-10-26')
        self.assertEqual((empty['total'],empty['summary']['batch_count']),(0,0))
        body=self.client.get('/collections/searches/'+self.task+'?category=粘毛器').get_data(as_text=True)
        self.assertIn('全部批次',body)
        self.assertIn('2026-10-25 12:00:00',body)
        self.assertIn('原始位置 5',body)
        self.assertIn('批次 #10',body)
        self.assertIn('observation-100',body)
        self.assertIn('1 条身份未确认记录另计',body)
        self.assertEqual(self.client.get('/collections/searches/'+self.task+'?since=2026-10-26').status_code,200)
        self.assertEqual(self.client.get('/collections/searches/'+self.task+'?category=粘毛器&batch=30').status_code,404)

    def test_search_records_global_sort_twenty_pages_and_batch_links(self):
        """跨批次先全量排序，再按20条分页；页签和批次详情保留返回及筛选。"""
        import re
        from html import unescape
        from urllib.parse import parse_qs,urlsplit
        for i in range(43):
            self.put('list_observations',id=100+i,batch_id=10 if i%2 else 11,product_id=1,
                     slot_index=100+i,title='观察'+str(i),price_value=i)
        pages=[browse(self.settings,'search_records',task_id=self.task,sort_by='price',order='desc',page=p) for p in (1,2,3)]
        self.assertEqual([len(p['observations']) for p in pages],[20,20,5])
        rows=[r for p in pages for r in p['observations']]
        self.assertEqual(len({r['id'] for r in rows}),45)
        self.assertEqual([r['price_value'] for r in rows[:20]],[str(i)+'.000000' for i in range(42,22,-1)])
        self.assertEqual(pages[1]['serial_offset'],20)
        original_page=records(self.settings,10,page=2)['records']
        selected_page=browse(self.settings,'search_records',task_id=self.task,batch_id=10,page=2)['observations']
        self.assertEqual([r['id'] for r in selected_page],[r['id'] for r in original_page])
        url='/collections/searches/'+self.task
        body=self.client.get(url,query_string={'category':'粘毛器','since':'2026-10-25','sort':'price','order':'desc','page':2}).get_data(as_text=True)
        self.assertRegex(body,r'<tr id="observation-[0-9]+"><td>21</td>')
        tabs=re.search(r'<div class="tabs" aria-label="采集批次">(.*?)</div>',body,re.S).group(1)
        for href in re.findall(r'href="([^\"]+)"',tabs):
            args=parse_qs(urlsplit(unescape(href)).query)
            self.assertEqual((args['page'],args['sort'],args['since']),(['1'],['price'],['2026-10-25']))
        batch_link=unescape(re.search(r'href="([^\"]+)"[^>]*>批次 #',body).group(1))
        self.assertIn('page=2',parse_qs(urlsplit(batch_link).query)['return_to'][0])
        self.assertEqual(self.client.get(batch_link).status_code,200)

if __name__=='__main__':
    unittest.main()
