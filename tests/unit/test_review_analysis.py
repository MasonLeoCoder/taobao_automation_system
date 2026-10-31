"""评价分析事务与业务回归；隔离SQLite与模拟AI，不发送真实资料。"""
import copy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.mysql_migrations.schema_v11 import build_metadata, NEW_TABLES
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.review_analysis import service, rules
from ecommerce.web.app import create_app


class ReviewAnalysisTests(unittest.TestCase):
    def setUp(self):
        """建立临时数据库和脱敏评价；参数：无；返回：无。"""
        # 本组冻结旧版任务和响应的兼容行为，新版有独立用例。
        if not getattr(self,'use_v4',False):
            preview=patch.object(service,'preview',side_effect=service.preview_legacy)
            preview.start();self.addCleanup(preview.stop)
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = load_settings(root=self.root)
        self.app = create_app(base)
        self.settings = replace(base, task_backend='mysql', collection_backend='mysql')
        self.app.config.update(SETTINGS=self.settings, TESTING=True)
        self.client = self.app.test_client()
        self.headers = {'X-Collection-Token': self.app.config['COLLECTION_TOKEN']}
        metadata = sa.MetaData()
        original = build_metadata()
        if getattr(self,'use_v4',False):
            from ecommerce.infrastructure.storage.mysql_migrations.schema_v14 import build_metadata as current_metadata
            original=current_metadata()
        for t in original.tables.values():
            columns = []
            for col in t.c:
                kind = copy.copy(col.type)
                if hasattr(kind, 'collation'): kind.collation = None
                columns.append(sa.Column(col.name, kind, primary_key=col.primary_key, nullable=col.nullable if t.name in NEW_TABLES else True))
            target = sa.Table(t.name, metadata, *columns)
            if t.name in NEW_TABLES:
                for c in t.constraints:
                    if isinstance(c, sa.UniqueConstraint): target.append_constraint(sa.UniqueConstraint(*c.columns.keys()))
        self.engine = sa.create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=sa.pool.StaticPool)
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.tables = metadata.tables
        mock = patch('ecommerce.infrastructure.storage.analysis_store.collection_store', return_value=SimpleNamespace(engine=self.engine, tables=self.tables))
        mock.start(); self.addCleanup(mock.stop)
        (self.root / 'config').mkdir()
        (self.root / 'config/ai.toml').write_text('[ai]\napi_key="sk-test-only"', encoding='utf-8')
        self.put('products', id=1, platform='taobao', platform_product_id='123', display_title='测试商品')
        self.put('collection_batches', id=5, product_id=1, module='reviews', visibility='published', status='complete',
                 captured_at=datetime(2026,10,29,10), coverage_status='limit_reached', requested_limit=100, stop_reason='limit_reached')
        self.put('review_observations', id=8, batch_id=5, position=1, content='刚买很好用', sku_text='测试规格', author='不可发送的昵称',
                 append_reviews=[{'content':'用了几天粘不住'}])
        self.put('review_observations', id=9, batch_id=5, position=2, content='很满意', sku_text='测试规格', append_reviews=[])

    def put(self, name, **values):
        """保存隔离样例；参数：表名及值；返回：无。"""
        with self.engine.begin() as conn: conn.execute(self.tables[name].insert().values(**values))

    def start(self, force=False):
        """不派发真实进程地创建任务；参数：强制另建；返回：编号。"""
        data = service.preview(self.settings, '123')
        with patch('ecommerce.modules.review_analysis.service.launch'):
            return service.start(self.settings, '123', batch_id=5, model='deepseek-flash', input_hash=data['input_hash'],
                max_calls=10, max_tokens=200000, allow_paid=True, force=force)['id']

    def response(self, text):
        """构造完整模拟响应；参数：文字；返回：客户端结构。"""
        return {'text':json.dumps(text,ensure_ascii=False), 'finish_reason':'stop', 'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20}}

    def classified(self):
        """构造含追评负面依据的结果；参数：无；返回：分类JSON。"""
        return {'reviews':[{'id':8,'kind':'specific','points':[{'topic':'粘性','sentiment':'negative','segment':'append:1','quote':'用了几天粘不住'}]},
                           {'id':9,'kind':'general','points':[]}]}

    def run_mock(self, task, failure=None):
        """模拟两个AI阶段；参数：任务、可选错误；返回：Mock客户端。"""
        client = Mock()
        client.complete.side_effect = failure or [self.response(self.classified()), self.response({'findings':[{
            'title':'追评中有粘性问题','text':'本样本出现使用几天后粘不住的反馈，需核实。',
            'evidence':[{'id':8,'segment':'append:1','quote':'用了几天粘不住'}]}], 'suggestions':[]})]
        with patch('ecommerce.modules.review_analysis.service.create_client', return_value=client):
            service.execute(self.settings,task,Mock())
        return client

    def test_snapshot_reuse_and_source_change(self):
        """同输入防重复、快照无昵称、来源变化不能使用旧预览。"""
        task = self.start()
        self.assertEqual(self.start(),task)
        self.assertEqual(self.start(force=True),task)  # 运行中强制选项也复用
        data=repository.read(self.settings,task)
        self.assertNotIn('不可发送的昵称',json.dumps(data['sources'],ensure_ascii=False))
        self.assertEqual(data['sources'][0]['snapshot']['appends'][0]['segment'],'append:1')
        preview=service.preview(self.settings,'123')
        with self.engine.begin() as conn: conn.execute(self.tables['review_observations'].update().where(self.tables['review_observations'].c.id==8).values(content='已变化'))
        with self.assertRaises(ValueError):
            repository.create(self.settings,'123',5,rules.snapshot(),{},'deepseek','deepseek-flash',rules.VERSION,preview['input_hash'])

    def test_execution_saved_result_read_recount_and_correction(self):
        """完整分析保存两次调用，查看和人工修订不再调用AI，并保留旧版本。"""
        task=self.start(); client=self.run_mock(task)
        self.assertEqual(client.complete.call_count,2)
        data=service.read(self.settings,task)
        self.assertEqual(data['task']['status'],'succeeded')
        self.assertEqual(data['task']['processed_count'],2)
        self.assertEqual(data['task']['usage']['total_tokens'],40)
        detail=next(d for d in data['details'] if d['result']['id']==8)
        with patch('ecommerce.modules.review_analysis.service.create_client') as factory:
            for path in (f'/analysis/reviews/tasks/{task}',f'/api/analysis/reviews/tasks/{task}',f'/analysis/reviews/tasks/{task}/export',f'/analysis/reviews/tasks/{task}/export?format=html', '/analysis/reviews/products/123'):
                self.assertEqual(self.client.get(path).status_code,200,path)
            point={**detail['result']['points'][0], 'sentiment':'unclear'}
            service.correct(self.settings,task,detail['id'],expected_revision=0,kind='specific',points=[point],excluded=False,reason='需要进一步核实')
            changed=service.read(self.settings,task)
            self.assertEqual(len(changed['reports']),2)
            self.assertEqual(changed['report']['content']['findings'],[])
            self.assertEqual(changed['report']['content']['statistics']['topics'][0]['counts']['unclear'],1)
            with self.assertRaises(ValueError): service.correct(self.settings,task,detail['id'],expected_revision=0,kind='specific',points=[point],excluded=False,reason='旧页面')
            service.recount(self.settings,task)
            factory.assert_not_called()
        self.assertEqual(changed['details'][0]['result'],data['details'][0]['result'])

    def test_invalid_quote_stops_and_preserves_response(self):
        """AI编造引用时只隔离问题条目，合格评价与原始响应均保留，无自动重试。"""
        task=self.start(); value=self.classified(); value['reviews'][0]['points'][0]['quote']='编造的不存在内容'
        client=self.run_mock(task,[self.response(value)])
        data=repository.read(self.settings,task)
        self.assertEqual(client.complete.call_count,1)
        self.assertEqual(data['task']['status'],'partial')
        self.assertEqual([detail['result']['id'] for detail in data['details']],[9])
        self.assertEqual(data['calls'][0]['status'],'invalid')
        self.assertIsNotNone(data['calls'][0]['response'])
        self.assertEqual(self.client.get('/api/analysis/reviews/products/123/summary?batch=5').json['result_id'],task)

    def test_call_budget_and_no_implicit_paid_start(self):
        """预算耗尽前不发新请求；未授权、跨站和错误来源不能创建。"""
        data=service.preview(self.settings,'123')
        values=dict(batch_id=5,model='deepseek-flash',input_hash=data['input_hash'],max_calls=10,max_tokens=200000,allow_paid=True,force=False)
        self.assertEqual(self.client.get('/analysis/reviews/products/123/new',follow_redirects=True).status_code,200)
        self.assertEqual(self.client.post('/api/analysis/reviews/products/123',json=values).status_code,403)
        self.assertEqual(self.client.post('/api/analysis/reviews/products/123',json={},headers=self.headers).status_code,400)
        for changes in ({'allow_paid':False},{'max_calls':0},{'input_hash':'wrong'},{'batch_id':99},{'model':'unknown'},{'model':[]}):
            self.assertEqual(self.client.post('/api/analysis/reviews/products/123',json={**values,**changes},headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/analysis/reviews/products/123',json=values,headers={**self.headers,'Origin':'https://other.example'}).status_code,403)
        task=self.start()
        with self.engine.begin() as conn:
            t=self.tables['analysis_tasks']; conn.execute(t.update().where(t.c.id==task).values(parameters={**repository.read(self.settings,task)['task']['parameters'],'max_calls':1}))
        client=self.run_mock(task,[self.response(self.classified())])
        self.assertEqual(client.complete.call_count,1)
        self.assertEqual(repository.read(self.settings,task)['task']['status'],'partial')

    def test_resume_unknown_requires_explicit_risk_acknowledgement(self):
        """未知请求恢复需要风险确认，跳过已有明细，不能复用完成任务为新付费执行。"""
        task=self.start()
        repository.begin_call(self.settings,task,'classify',1,'test',[8,9],100)
        repository.update_task(self.settings,task,status='interrupted',active_key=None,owner=None)
        with self.assertRaises(ValueError): service.resume(self.settings,task,allow_paid=True)
        with patch('ecommerce.modules.review_analysis.service.launch') as launch:
            service.resume(self.settings,task,allow_paid=True,acknowledge_unknown=True)
            launch.assert_called_once()

    def test_statistics_mixed_and_strict_evidence(self):
        """同义主题按一条计数，初评正面追评负面合并mixed，引用不可跨评价。"""
        item={'id':1,'text':'粘得干净','appends':[{'segment':'append:1','text':'后来粘不住'}]}
        points=[{'topic':'粘性','sentiment':'positive','segment':'initial','quote':'粘得干净'},
                {'topic':'滚筒粘性','sentiment':'negative','segment':'append:1','quote':'后来粘不住'}]
        result=rules.classification(json.dumps({'reviews':[{'id':1,'kind':'specific','points':points}]}),[item])[0]
        stats=rules.statistics([{'result':result,'corrections':[]}],rules.snapshot())
        self.assertEqual(stats['topics'][0]['count'],1)
        self.assertEqual(stats['topics'][0]['counts']['mixed'],1)
        context=rules.report_context([{'result':result,'corrections':[]}],rules.snapshot())
        self.assertEqual(context['evidence'][0]['sentiment'],'negative')
        with self.assertRaises(ValueError): rules.validate_report(json.dumps({'findings':[{'title':'x','text':'x','evidence':[{'id':3,'segment':'initial','quote':'粘得干净'}]}],'suggestions':[]}),{'evidence':[{'id':1,**points[0]}]})

    def test_saved_receipt_replayed_without_repeating_classification(self):
        """响应已落盘但数据库未确认时先补写，只调用缺少的报告阶段。"""
        from ecommerce.infrastructure.tasks.ownership import atomic_json
        task=self.start()
        call=repository.begin_call(self.settings,task,'classify',1,'test',[8,9],1000)
        atomic_json(self.settings.workspace/'output/analysis_receipts'/task/(call+'.json'), self.response(self.classified()))
        report=self.response({'findings':[],'suggestions':[]})
        client=self.run_mock(task,[report])
        self.assertEqual(client.complete.call_count,1)
        result=repository.read(self.settings,task)
        self.assertEqual(result['task']['status'],'succeeded')
        self.assertEqual(len(result['details']),2)
        # 再执行一轮仅复用已校验报告，不重复请求。
        with patch('ecommerce.modules.review_analysis.service.create_client') as factory:
            service.execute(self.settings,task,Mock())
            factory.return_value.complete.assert_not_called()

    def test_newer_explicit_no_reviews_does_not_fall_back(self):
        """最新确认无评价时不拿旧批次冒充最新，历史批次仍可显式选择。"""
        self.put('collection_batches', id=6, product_id=1, module='reviews', visibility='published', status='no_reviews',
                 captured_at=datetime(2026,10,29,11), coverage_status='no_reviews')
        with self.assertRaises(ValueError):service.preview(self.settings,'123')
        self.assertEqual(service.preview(self.settings,'123',5)['batch']['id'],5)

    def test_worker_uses_task_events_without_browser_resource(self):
        """工作进程独立分析锁，复用通用任务日志且不获取浏览器资料锁。"""
        from ecommerce.modules.review_analysis.worker import run
        task=self.start()
        event=Mock(); event.owner={}; event.task_id='f'*32
        client=Mock()
        client.complete.side_effect=[self.response(self.classified()),self.response({'findings':[],'suggestions':[]})]
        with patch('ecommerce.modules.review_analysis.worker.TaskEvents',return_value=event), \
             patch('ecommerce.infrastructure.storage.task_store.task_store',return_value=Mock()), \
             patch('ecommerce.modules.review_analysis.service.create_client',return_value=client):
            self.assertEqual(run(self.settings,task),0)
        event.start.assert_called_once()
        event.finish.assert_called_once()
        self.assertFalse(self.settings.profile_path.exists())
        data=repository.read(self.settings,task)
        self.assertEqual(data['task']['execution_task_id'],'f'*32)
        self.assertEqual(data['task']['status'],'succeeded')

    def test_launcher_passes_only_config_reference(self):
        """子进程参数无密钥；不复用浏览器工作进程。"""
        task=self.start()
        process=Mock();process.pid=12345
        with patch('ecommerce.modules.review_analysis.service.subprocess.Popen',return_value=process) as spawn, \
             patch('ecommerce.modules.review_analysis.service.process_started_at',return_value='2026-10-29T00:00:00+00:00'):
            service.launch(self.settings,task)
        args=spawn.call_args.args[0]
        self.assertIn('ecommerce.modules.review_analysis.worker',args)
        self.assertNotIn('sk-test-only',repr(spawn.call_args))

    def test_history_filters_batches_and_preserves_success_after_new_failure(self):
        """不同批次独立显示，同批新任务失败不能遮住旧成功报告，GET不调用AI。"""
        first=self.start();self.run_mock(first)
        failed=self.start(force=True)
        repository.update_task(self.settings,failed,status='failed',active_key=None,message='合成失败')
        self.put('collection_batches',id=6,product_id=1,module='reviews',visibility='published',status='complete',captured_at=datetime(2026,10,29,11))
        self.put('review_observations',id=10,batch_id=6,position=1,content='另一批',sku_text='另一个规格',append_reviews=[])
        preview=service.preview(self.settings,'123',6)
        with patch('ecommerce.modules.review_analysis.service.launch'):
            second=service.start(self.settings,'123',batch_id=6,model='deepseek-flash',input_hash=preview['input_hash'],max_calls=10,max_tokens=200000,allow_paid=True)['id']
        repository.update_task(self.settings,second,status='failed',active_key=None)
        with patch('ecommerce.modules.review_analysis.service.create_client') as paid:
            current=service.history_page(self.settings,'123',5)
            self.assertEqual({t['id'] for t in current['tasks']},{first,failed})
            self.assertEqual(current['latest_result']['id'],first)
            self.assertEqual(service.history_page(self.settings,'123')['selected_batch'],6)
            self.assertEqual(len(service.history_page(self.settings,'123','all')['tasks']),3)
            five=self.client.get('/api/analysis/reviews/products/123/summary?batch=5').json
            six=self.client.get('/api/analysis/reviews/products/123/summary?batch=6').json
            self.assertEqual(five['result_id'],first)
            self.assertIsNone(six['result_id'])
            text=self.client.get('/analysis/reviews/products/123?batch=5').get_data(as_text=True)
            self.assertIn('最近完成的报告',text)
            self.assertIn('查看报告',text)
            self.assertIn('最新任务失败',text)
            self.assertNotIn(second,text)
            all_text=self.client.get('/analysis/reviews/products/123?batch=all').get_data(as_text=True)
            self.assertIn(second,all_text)
            paid.assert_not_called()
        with self.assertRaises(LookupError):service.history_page(self.settings,'123',99)

    def test_confirmation_fixed_batch_and_full_paginated_list(self):
        """确认页固定明确批次，其他页码排序和最新批次不改变整批输入。"""
        for i in range(10,33):
            self.put('review_observations',id=i,batch_id=5,position=i,content='分页样例',sku_text='规格',append_reviews=[])
        self.put('collection_batches',id=6,product_id=1,module='reviews',visibility='published',status='complete',captured_at=datetime(2026,10,29,11))
        self.put('review_observations',id=40,batch_id=6,position=1,content='新批次',sku_text='规格',append_reviews=[])
        with patch('ecommerce.modules.review_analysis.service.create_client') as paid:
            text=self.client.get('/analysis/reviews/products/123/new?batch=5&page=2&sort=date&reviews_batch=6').get_data(as_text=True)
            self.assertIn('全部 25 条评价，包含所有分页',text)
            self.assertIn('data-batch="5"',text)
            self.assertNotIn('name="batch"',text)
            self.assertEqual(len(service.preview(self.settings,'123',5)['items']),25)
            paid.assert_not_called()
        store=SimpleNamespace(engine=self.engine,tables=self.tables)
        with patch('ecommerce.infrastructure.storage.collection_browser.collection_store',return_value=store), \
             patch('ecommerce.infrastructure.storage.collection_queries.collection_store',return_value=store):
            page=self.client.get('/collections/products/123?module=reviews&reviews_batch=5&page=2')
            self.assertEqual(page.status_code,200)
            text=page.get_data(as_text=True)
            self.assertIn('batch=5',text)
            self.assertIn('当前列表全部 25 条评价',text)
            self.assertEqual(text.count('data-review-analysis="start"'),1)

    def test_summary_displays_active_task_from_selected_batch(self):
        """有存活进程时展示本批进度，状态查询不创建新任务。"""
        task=self.start()
        repository.update_task(self.settings,task,owner={'pid':1,'started_at':'2026-10-29T00:00:00+00:00'})
        with patch('ecommerce.modules.review_analysis.service.owner_alive',return_value=True):
            result=self.client.get('/api/analysis/reviews/products/123/summary?batch=5').json
        self.assertEqual(result['active_id'],task)
        self.assertIsNone(result['result_id'])

    def test_latest_success_remains_available_beyond_history_window(self):
        """即使成功报告不在最近100次列表中，也保持可直达的成功结果入口。"""
        from datetime import timedelta
        from uuid import uuid4
        task=self.start();self.run_mock(task)
        data=repository.read(self.settings,task)
        for index in range(101):
            new_id=uuid4().hex
            row={**data['task'],'id':new_id,'status':'failed','processed_count':0,'created_at':data['task']['created_at']+timedelta(minutes=index+1)}
            self.put('analysis_tasks',**row)
            self.put('analysis_sources',**{**data['sources'][0],'id':uuid4().hex,'analysis_id':new_id})
        value=service.history_page(self.settings,'123',5)
        self.assertEqual(len(value['tasks']),100)
        self.assertNotIn(task,{row['id'] for row in value['tasks']})
        self.assertEqual(value['latest_result']['id'],task)

    def test_dialog_preparation_is_read_only_and_matches_standalone_form(self):
        """弹窗和独立确认页复用固定来源表单，准备过程不创建任务或调用AI。"""
        with patch('ecommerce.modules.review_analysis.service.create_client') as paid:
            result=self.client.get('/analysis/reviews/products/123/new?batch=5&dialog=1')
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.headers['X-Review-Analysis-Fragment'],'1')
            text=result.get_data(as_text=True)
            self.assertIn('data-batch="5"',text)
            self.assertIn('全部 2 条评价',text)
            self.assertIn('name="allow_paid"',text)
            self.assertNotIn('name="batch"',text)
            self.assertNotIn('<script',text)
            self.assertNotIn('sk-test-only',text)
            self.assertNotIn('<html',text)
            fallback=self.client.get('/analysis/reviews/products/123/new?batch=5').get_data(as_text=True)
            self.assertIn('ui-confirm-form',fallback)
            self.assertIn('data-batch="5"',fallback)
            self.assertEqual(repository.history(self.settings,'123'),[])
            paid.assert_not_called()

    def test_dialog_invalid_source_returns_safe_closable_fragment(self):
        """无效来源在弹窗内提供关闭和说明，不嵌入整页或异常堆栈。"""
        response=self.client.get('/analysis/reviews/products/123/new?batch=999&dialog=1')
        self.assertEqual(response.status_code,404)
        self.assertEqual(response.headers['X-Review-Analysis-Fragment'],'1')
        text=response.get_data(as_text=True)
        self.assertIn('暂时无法准备分析',text)
        self.assertIn('data-ra-close',text)
        self.assertNotIn('data-ra-start',text)
