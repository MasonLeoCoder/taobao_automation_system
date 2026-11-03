"""独立主图/详情分析的隔离业务、来源与Web回归，无真实AI。"""
from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock,patch
from dataclasses import replace
from types import SimpleNamespace
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v22 import build_metadata
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.modules.image_analysis import independent,service as legacy,planner,report,runtime
from ecommerce.modules.image_analysis.kinds import kind
from ecommerce.modules.review_analysis import service as reviews
from tests.unit import test_image_analysis as core


def sample(module,extra=''):
    """仅模拟所选模块的完整章节和定位，不冒充真实模型。"""
    marker='【主图1】' if module=='main_images' else '【详情1】'
    return '\n\n'.join('## '+title+'\n\n【可见事实】模拟图片文字。'+(marker if i==0 else '') for i,title in enumerate(kind(module)['sections']))+extra


class IndependentImageTests(TestCase):
    put=core.ImageAnalysisTests.put
    add_image=core.ImageAnalysisTests.add_image

    def setUp(self):
        """复用图片字节和临时SQLite，本轮无正式数据写入。"""
        with patch.object(core,'build_metadata',build_metadata): core.ImageAnalysisTests.setUp(self)
        for target in ('ecommerce.infrastructure.storage.collection_browser.collection_store','ecommerce.infrastructure.storage.collection_queries.collection_store'):
            current=patch(target,return_value=SimpleNamespace(engine=self.engine,tables=self.tables));current.start();self.addCleanup(current.stop)

    def preview(self,module='main_images',**kwargs):
        """生成单模块实际计划，不联网。"""
        return independent.preview(self.settings,'123',module,**kwargs)

    def values(self,data):
        """提取固定批次、设置及用户选择。"""
        return {key:data[key] for key in ('batch_id','model','input_hash','plan_hash','allow_partial')}

    def start(self,module='main_images',force=False,**kwargs):
        """明确费用，但派发仅模拟。"""
        data=self.preview(module,**kwargs)
        with patch.object(independent,'launch'):
            return independent.start(self.settings,'123',module,**self.values(data),allow_paid=True,force=force)['id']

    def run_mock(self,module,task=None,text=None):
        """走通真实共用执行分派，只有客户端模拟。"""
        task=task or self.start(module); client=Mock();client.complete_images.return_value=dict(text=text or sample(module),finish_reason='stop',usage={'total_tokens':50})
        with patch.object(reviews,'create_client',return_value=client): state=reviews.execute(self.settings,task,Mock())
        return task,client,state

    def test_single_module_scope_and_frozen_prompts(self):
        """另一模块不进入计划、缺失提示或来源；提示和章节独立。"""
        for module,batch,label,forbidden in (('main_images',7,'主图','详情1'),('detail_images',8,'详情','主图1')):
            data=self.preview(module); self.assertEqual(data['batch_id'],batch); self.assertFalse(data['input']['scope']['issues'])
            self.assertEqual(data['input']['module'],module); self.assertEqual(len(data['input']['images']),1)
            self.assertTrue(data['input']['images'][0]['label'].startswith(label))
            self.assertEqual(data['rules']['version'],kind(module)['version']); self.assertEqual(data['rules']['sections'],list(kind(module)['sections']))
            frames=planner.frames(data['input']); self.assertNotIn(forbidden,frames[0]['labels'])
            self.assertNotIn('category',data['input']['product']); self.assertNotIn('relative_path',planner.encode(data['plan']['groups'][0]['messages']))
            self.assertEqual(data['plan']['planned_calls'],1)

    def test_missing_other_module_never_requires_partial_consent(self):
        """只分析主图时，详情原件缺失或无批次不应阻止。"""
        (self.settings.workspace/'output/raw/31.png').unlink()
        data=self.preview(); self.assertIsNotNone(data['plan']); self.assertFalse(data['input']['scope']['issues'])
        self.assertIsNone(self.preview('detail_images')['plan'])

    def test_consent_stale_cross_module_and_unknown_arguments(self):
        """无费用许可、陈旧哈希、跨模块批次以及多余字段均拒绝。"""
        data=self.preview(); values=self.values(data)
        with self.assertRaises(ValueError): independent.start(self.settings,'123','main_images',**values)
        with self.assertRaises(ValueError): independent.start(self.settings,'123','main_images',**dict(values,plan_hash='changed'),allow_paid=True)
        with self.assertRaises(ValueError): independent.start(self.settings,'123','main_images',**dict(values,batch_id=None),allow_paid=True)
        with self.assertRaises(LookupError): self.preview(batch_id=8)
        with self.assertRaises(ValueError): self.preview(batch_id=0)
        url='/api/analysis/images/main_images/products/123'
        self.assertEqual(self.client.post(url,json=values).status_code,403)
        for extra in ({'detail_batch':8},{'module':'detail_images'},{'rules':{}}):
            self.assertEqual(self.client.post(url,json=dict(values,allow_paid=True,**extra),headers=self.headers).status_code,400)

    def test_active_success_failed_tasks_separate(self):
        """不同模块各有活动任务；同模块复用，失败新建，成功明确选择。"""
        first=self.start(); self.assertEqual(self.start(force=True),first)
        second=self.start('detail_images');self.assertNotEqual(first,second)
        repository.update_task(self.settings,first,status='failed',active_key=None)
        fresh=self.start();self.assertNotEqual(first,fresh)
        self.run_mock('main_images',fresh)
        data=self.preview()
        with patch.object(independent,'launch') as launched:
            result=independent.start(self.settings,'123','main_images',**self.values(data),allow_paid=True)
            self.assertTrue(result['choice_required']);self.assertEqual(result['id'],fresh);launched.assert_not_called()
        self.assertNotEqual(self.start(force=True),fresh)

    def test_execute_independent_sections_storage_and_export(self):
        """两类分别执行、保存、导出和免费检查；图号不冒充OCR通过。"""
        for module in ('main_images','detail_images'):
            task,client,state=self.run_mock(module);self.assertEqual(state,'succeeded');client.complete_images.assert_called_once()
            data=independent.read(self.settings,task,module);value=data['report']['content']
            self.assertEqual(data['task']['analysis_type'],module);self.assertEqual(value['schema_version'],kind(module)['report_version'])
            self.assertTrue(value['generation']['complete']);self.assertEqual(value['validation']['counts']['valid_count'],1)
            self.assertEqual(value['module'],module);self.assertEqual(data['task']['processed_count'],0)
            base=f'/analysis/images/{module}/tasks/{task}'
            self.assertEqual(self.client.get(base).status_code,200)
            self.assertIn(kind(module)['label']+'分析报告',self.client.get(base).text)
            for fmt in ('md','html','json'):
                response=self.client.get(base+'?download='+fmt);self.assertEqual(response.status_code,200);self.assertIn('attachment',response.headers['Content-Disposition'])
            result=self.client.post(f'/api/analysis/images/{module}/tasks/{task}/check',json={'report_id':data['report']['id']},headers=self.headers)
            self.assertEqual(result.status_code,200);self.assertEqual(result.json['ai_calls'],0)
            self.assertEqual(len(independent.read(self.settings,task,module)['reports']),2)
            self.assertEqual(self.client.get(base+'?version=1').status_code,200)
            other='detail_images' if module=='main_images' else 'main_images'
            self.assertEqual(self.client.get(f'/analysis/images/{other}/tasks/{task}').status_code,400)
            self.assertEqual(self.client.get('/analysis/images/tasks/'+task).status_code,400)

    def test_different_section_contract_preserves_incomplete(self):
        """主图正文不能作为详情七章完整稿，缺章可读内容仍保存，不自动补写。"""
        task,client,state=self.run_mock('detail_images',text=sample('main_images'))
        self.assertEqual(state,'partial');self.assertEqual(client.complete_images.call_count,1)
        data=independent.read(self.settings,task,'detail_images')
        self.assertIn('阅读结构与介绍逻辑',data['report']['content']['generation']['missing_sections'])
        self.assertTrue(data['report']['content']['markdown'])

    def test_history_current_batch_no_legacy_or_other_type(self):
        """历史不混入旧联合，成功报告严格对应当前批次。"""
        data=legacy.preview(self.settings,'123')
        with patch.object(legacy,'launch'): old=legacy.start(self.settings,'123',**core.ImageAnalysisTests.values(self,data),allow_paid=True)['id']
        first,_,_=self.run_mock('main_images'); detail=self.start('detail_images')
        self.put('collection_batches',id=9,product_id=1,module='main_images',visibility='published',status='complete',captured_at=datetime(2026,11,3,10))
        self.add_image(32,9,'main_image','red')
        second=self.start(batch_id=9)
        all_rows=independent.history(self.settings,'123','main_images');self.assertEqual({t['id'] for t in all_rows['tasks']},{first,second})
        current=independent.history(self.settings,'123','main_images',7);self.assertEqual(current['latest_result']['id'],first)
        other=independent.history(self.settings,'123','main_images',9);self.assertIsNone(other['latest_result']);self.assertEqual(other['tasks'][0]['id'],second)
        self.assertNotIn(old,{t['id'] for t in current['tasks']});self.assertNotIn(detail,{t['id'] for t in current['tasks']})
        with self.assertRaises(ValueError): independent.read(self.settings,old)

    def test_module_pages_and_retired_joint_entry(self):
        """入口在各自模块，联合生成入口撤下；更换批次不借用成功报告。"""
        task,_,_=self.run_mock('main_images')
        for module,batch in (('main_images',7),('detail_images',8)):
            page=self.client.get(f'/collections/products/123?module={module}&batch={batch}')
            self.assertEqual(page.status_code,200);self.assertIn('data-ia-open',page.text)
            self.assertIn(f'/analysis/images/{module}/products/123/new?batch={batch}',page.text)
            self.assertIn('查看分析记录',page.text)
            self.assertEqual('查看本批报告' in page.text,module=='main_images')
            self.assertEqual(self.client.get(f'/analysis/images/{module}/products/123?batch={batch}').status_code,200)
        self.assertEqual(self.client.get('/collections/products/123?module=composite&analysis=images').status_code,302)
        self.assertEqual(self.client.get('/analysis/images/products/123/new').status_code,302)
        self.assertEqual(self.client.post('/api/analysis/images/products/123',json={},headers=self.headers).status_code,400)
        combined=self.client.get('/collections/products/123?module=composite')
        self.assertNotIn('data-ia-open',combined.text);self.assertNotIn('主图与详情</a>',combined.text)

    def test_previews_batch_input_and_actual_frame(self):
        """预览只有单一批次控件，可实际读取图片，另一模块不能混入。"""
        for module,batch in (('main_images',7),('detail_images',8)):
            for suffix in ('','?dialog=1'):
                response=self.client.get(f'/analysis/images/{module}/products/123/new'+suffix)
                self.assertEqual(response.status_code,200);self.assertIn('name="batch_id"',response.text)
                self.assertNotIn('name="main_batch"',response.text);self.assertNotIn('name="detail_batch"',response.text)
            bad=self.client.get(f'/analysis/images/{module}/products/123/new?detail_batch=8');self.assertEqual(bad.status_code,400)
            self.assertEqual(self.client.get(f'/analysis/images/{module}/products/123/new?batch=0').status_code,400)
            data=self.preview(module)
            with patch.object(independent,'launch') as launched:
                created=self.client.post(f'/api/analysis/images/{module}/products/123',json=dict(self.values(data),allow_paid=True),headers=self.headers)
                self.assertEqual(created.status_code,200);self.assertTrue(created.json['created']);launched.assert_called_once()
                self.assertIn(f'/analysis/images/{module}/tasks/',created.json['url'])

    def test_all_pages_and_role_filter_not_just_visible_twenty(self):
        """整批超过20张全部纳入，其他用途媒体不参与。"""
        for value in range(32,55): self.add_image(value,7,'main_image','blue')
        self.add_image(60,7,'screenshot','red')
        data=self.preview();self.assertEqual(len(data['input']['images']),24)
        self.assertEqual(data['plan']['source_ids'],[30]+list(range(32,55)))
        self.assertEqual(data['plan']['image_count'],1)

    def test_local_source_changed_stops_before_any_call(self):
        """创建后原件变化，单模块在联网前失败，不借另一模块补齐。"""
        task=self.start();(self.settings.workspace/'output/raw/30.png').write_bytes(b'changed')
        client=Mock()
        self.assertEqual(runtime.execute(self.settings,task,Mock(),client,module='main_images'),'failed');client.assert_not_called()
        self.assertEqual(independent.read(self.settings,task,'main_images')['calls'],[])
        self.assertIsNone(self.preview()['plan']);self.assertIsNotNone(self.preview('detail_images')['plan'])

    def test_partial_collection_and_missing_images_explicit(self):
        """采集部分完成与文件缺失均需明确同意，全部不可用仍不能开始。"""
        with self.engine.begin() as conn: conn.execute(self.tables['collection_batches'].update().where(self.tables['collection_batches'].c.id==7).values(status='partial'))
        self.assertIsNone(self.preview()['plan']);self.assertIsNotNone(self.preview(allow_partial=True)['plan'])
        (self.settings.workspace/'output/raw/30.png').unlink();self.assertIsNone(self.preview(allow_partial=True)['plan'])

    def test_grouped_requests_keep_single_module_and_summary(self):
        """分组N＋1仍只发送所选模块，汇总不重新传图。"""
        self.add_image(32,7,'main_image','red');real=planner.profile;config=replace(load_config(self.settings.root),group_output_tokens=128)
        with patch.object(planner,'profile',side_effect=lambda *args:dict(real(*args),max_images=1)),patch.object(independent,'load_config',return_value=config):
            data=self.preview();self.assertEqual(data['plan']['planned_calls'],3)
            with patch.object(independent,'launch'): task=independent.start(self.settings,'123','main_images',**self.values(data),allow_paid=True)['id']
        client=Mock();client.complete_images.side_effect=[dict(text='## 本组\n【主图1】',finish_reason='stop',usage={'total_tokens':10}),dict(text='## 本组\n【主图2】',finish_reason='stop',usage={'total_tokens':10}),dict(text=sample('main_images'),finish_reason='stop',usage={'total_tokens':20})]
        self.assertEqual(runtime.execute(self.settings,task,Mock(),Mock(return_value=client),module='main_images'),'succeeded')
        self.assertEqual(client.complete_images.call_count,3);self.assertIsInstance(client.complete_images.call_args.args[0][1]['content'],str)

    def test_frozen_prompts_contain_approved_rules(self):
        """批准的六章/七章、宣称边界及选品动作均在实际冻结提示中。"""
        for module in ('main_images','detail_images'):
            rules=planner.snapshot(module)
            for title in kind(module)['sections']:self.assertIn('## '+title,rules['report'])
            for word in ('现成款','包装','供应商','不猜测','SKU','图号'):self.assertIn(word,rules['report'])
        self.assertIn('关键宣称',planner.snapshot('detail_images')['report'])
        self.assertIn('主图1仅表示',planner.snapshot('main_images')['report'])
