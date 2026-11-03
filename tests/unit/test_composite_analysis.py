"""综合选品隔离存储及模拟API验证，不调用真实AI或采集。"""
import copy
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock, patch
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v20 import build_metadata
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.review_analysis import service as reviews
from ecommerce.modules.review_analysis import v6
from ecommerce.modules.question_analysis import service as questions
from ecommerce.modules.composite_analysis import service, store, planner, runtime, report
from tests.unit import test_question_analysis as qa_tests


def sample_combined():
    """构造九章且评价/问题编号互不混淆的模拟报告。"""
    return '\n\n'.join('## '+name+'\n\n待拿样验证。'+('\n\n【评价8｜初评】原文：“刚买很好用”\n\n【问题10｜回答20】原文：“我使用两周没有掉胶”' if index == 0 else '') for index, name in enumerate(report.SECTIONS))


class CompositeTests(TestCase):
    put = qa_tests.QuestionTests.put
    use_v4 = False

    def setUp(self):
        """临时SQLite及两份固定基础报告；参数：无；返回：测试环境。"""
        with patch('tests.unit.test_question_analysis.build_metadata', build_metadata):
            qa_tests.QuestionTests.setUp(self)
        self.review_data = reviews.preview(self.settings, '123', 5)
        row, _ = repository.create(self.settings, '123', 5, v6.snapshot(), {'plan': {'context': self.review_data['context'], 'report_output_tokens': 8192}},
            'deepseek', 'deepseek-flash', 'reviews-v6', self.review_data['input_hash'])
        self.review_task = row['id']
        self.review_content = {'markdown': '## 总体判断\n\n需要拿样。\n\n【8｜初评】原文：“刚买很好用”\n\n【8｜初评】原文：“很好用”',
            'generation': {'complete': True}, 'validation': {'counts': {'citation_count': 2, 'pending_count': 0}}, 'detail_revisions': {}}
        repository.save_report(self.settings, self.review_task, self.review_content)
        repository.update_task(self.settings, self.review_task, status='succeeded', active_key=None)
        qa = questions.preview(self.settings, '123', 6)
        with patch.object(questions, 'launch'):
            self.question_task = questions.start(self.settings, '123', batch_id=6, model=qa['model'],
                input_hash=qa['input_hash'], plan_hash=qa['plan_hash'], allow_paid=True)['id']
        self.question_content = {'markdown': qa_tests.sample_report(qa['items']), 'generation': {'complete': True},
            'validation': {'counts': {'citation_count': 1, 'pending_count': 1}}, 'basic_statistics': {'answer_count': 1}, 'detail_revisions': {}}
        repository.save_report(self.settings, self.question_task, self.question_content)
        repository.update_task(self.settings, self.question_task, status='partial', active_key=None)

    def preview(self, **kwargs):
        """只读模拟数据预览；参数：覆盖；返回：固定计划。"""
        return service.preview(self.settings, '123', **kwargs)

    def values(self, data):
        """构造用户确认的数据；参数：预览；返回：创建参数。"""
        return {k: data[k] for k in ('review_report','question_report','model','input_hash','plan_hash')}

    def start(self, **kwargs):
        """不启动真实后台的创建；参数：覆盖；返回：结果。"""
        data = self.preview()
        with patch.object(service, 'launch'):
            return service.start(self.settings, '123', **self.values(data), allow_paid=True, **kwargs)

    def test_preview_minimal_deduplicated_and_frozen_versions(self):
        """仅报告引用原文、排除昵称与未引用评价，来源版本固定不随重查漂移。"""
        data = self.preview(); self.assertEqual(data['plan']['planned_calls'], 1)
        incoming = data['input']; self.assertEqual(len(incoming['reports']), 2)
        self.assertEqual(len(incoming['reports'][0]['evidence']), 1)
        outgoing = planner.encode(data['plan']['groups'][0]['messages'])
        self.assertNotIn('私密昵称', outgoing); self.assertNotIn('不可发送的昵称', outgoing)
        self.assertNotIn('很满意', outgoing); self.assertIn('【评价8｜初评】', outgoing)
        self.assertEqual(incoming['reports'][1]['pending_citations'], 1)
        task = self.start()['id']; saved = service.read(self.settings, task)
        self.assertEqual(len(saved['sources']), 2)
        old = copy.deepcopy(saved['task']['parameters']['plan']['input'])
        repository.save_report(self.settings, self.review_task, dict(self.review_content, markdown='## 总体判断\n改动'))
        self.assertEqual(service.read(self.settings, task)['task']['parameters']['plan']['input'], old)
        explicit = self.preview(review_report=data['review_report'], question_report=data['question_report'])
        self.assertEqual(explicit['input_hash'], data['input_hash'])

    def test_source_identity_consent_and_stale_plan(self):
        """跨类型/跨商品、无费用许可及过期计划不创建或发出请求。"""
        data = self.preview(); values = self.values(data)
        with self.assertRaises(ValueError): service.start(self.settings, '123', **values)
        with self.assertRaises(LookupError): self.preview(review_report=data['question_report'])
        self.put('products', id=2, platform='taobao', platform_product_id='456')
        with self.assertRaises(LookupError): service.preview(self.settings, '456', data['review_report'], data['question_report'])
        with self.assertRaises(ValueError): service.start(self.settings, '123', **dict(values, plan_hash='old'), allow_paid=True)
        with self.engine.begin() as conn:
            conn.execute(self.tables['collection_batches'].update().where(self.tables['collection_batches'].c.id == 5).values(visibility='staging'))
        with self.assertRaises(ValueError): self.preview()

    def test_active_success_and_failed_creation_choices(self):
        """活动任务不重复；成功明确选择；失败默认新建，强制也不能绕过活动互斥。"""
        task = self.start()['id']; self.assertEqual(self.start(force=True)['id'], task)
        repository.update_task(self.settings, task, status='failed', active_key=None)
        second = self.start()['id']; self.assertNotEqual(task, second)
        repository.save_report(self.settings, second, {'detail_revisions': {}, 'markdown': sample_combined()})
        repository.update_task(self.settings, second, status='succeeded', active_key=None)
        self.assertTrue(self.start()['choice_required'])
        third = self.start(force=True)['id']; self.assertNotEqual(second, third)

    def test_execute_report_exports_and_zero_call_recheck(self):
        """模拟一次请求保存报告，导出及本地检查零调用，旧版本保留。"""
        task = self.start()['id']; client = Mock()
        client.complete_messages.return_value = {'text': sample_combined(), 'finish_reason': 'stop',
            'usage': {'prompt_tokens': 100, 'completion_tokens': 200, 'total_tokens': 300}}
        with patch('ecommerce.modules.review_analysis.service.create_client', return_value=client):
            self.assertEqual(reviews.execute(self.settings, task, Mock()), 'succeeded')
        self.assertEqual(client.complete_messages.call_count, 1)
        saved = service.read(self.settings, task); content = saved['report']['content']
        self.assertEqual(content['validation']['counts']['valid_count'], 2)
        self.assertEqual(saved['task']['processed_count'], 0)
        self.assertEqual(saved['task']['usage']['total_tokens'], 300)
        # 旧综合执行与存储保留，但新Web只读取cross-v1，不提供旧报告兼容入口。
        self.assertEqual(self.client.get('/analysis/composite/tasks/'+task).status_code,400)
        checked = runtime.recheck(self.settings,saved,saved['report']['id'])
        self.assertEqual(checked['ai_calls'],0)
        self.assertEqual(len(service.read(self.settings,task)['reports']),2)
        with self.assertRaises(ValueError): runtime.recheck(self.settings,service.read(self.settings,task),saved['report']['id'])
        self.assertEqual(client.complete_messages.call_count, 1)
        with self.assertRaises(LookupError): service.read(self.settings, self.question_task)

    def test_failure_unknown_usage_and_partial_readable_are_preserved(self):
        """未知请求只有一次，额度保留；缺章可读内容仍保存且不补写。"""
        from ecommerce.infrastructure.ai.client import AIError
        task = self.start()['id']; client = Mock(); client.complete_messages.side_effect = AIError('敏感上游信息')
        self.assertEqual(runtime.execute(self.settings, task, Mock(), Mock(return_value=client)), 'failed')
        saved = service.read(self.settings, task); self.assertEqual(saved['calls'][0]['status'], 'unknown')
        self.assertGreater(saved['budget']['used_tokens'], 0); self.assertEqual(client.complete_messages.call_count, 1)
        self.assertNotIn('敏感上游信息', saved['task']['message'])
        next_task = self.start()['id']; client = Mock()
        client.complete_messages.return_value = {'text': '## 总体判断\n已有可读内容。', 'finish_reason': 'stop', 'usage': {'total_tokens': 20}}
        self.assertEqual(runtime.execute(self.settings, next_task, Mock(), Mock(return_value=client)), 'partial')
        self.assertIn('已有可读内容', service.read(self.settings, next_task)['report']['content']['markdown'])
        self.assertEqual(client.complete_messages.call_count, 1)

    def test_citation_categories_actual_sent_scope_and_safe_render(self):
        """拒绝裸编号、错误归属、未发送段与拼接；安全渲染不执行模型HTML。"""
        incoming = self.preview()['input']
        for text in ('【8｜初评】原文：“刚买很好用”','【问题11｜回答20】原文：“我使用两周没有掉胶”',
                     '【评价9｜初评】原文：“很满意”','【评价8｜追评1】原文：“用了几天粘不住”',
                     '【评价8｜初评】原文：“刚买…好用”'):
            self.assertEqual(report.check(text, 'stop', incoming)['counts']['valid_count'], 0)
        value = {'markdown': sample_combined()+'\n<script>alert(1)</script>', 'validation': report.check(sample_combined(), 'stop', incoming)}
        rendered = report.render(value); self.assertNotIn('<script>', rendered); self.assertIn('<details', rendered)
        self.assertEqual(len(report.reading(value)['sections']), 9)

    def test_preview_route_guard_and_missing_sources(self):
        """弹窗整页可读，未确认及外站POST拒绝；缺来源不自动基础分析。"""
        for suffix in ('', '?dialog=1'):
            response = self.client.get('/analysis/composite/products/123/new'+suffix)
            self.assertEqual(response.status_code, 200); self.assertIn('data-ca-form', response.text)
        values = self.values(self.preview())
        self.assertEqual(self.client.post('/api/analysis/composite/products/123', json=values).status_code, 403)
        self.assertEqual(self.client.post('/api/analysis/composite/products/123', json=values, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post('/api/analysis/composite/products/123', json=dict(values,allow_paid=True), headers=dict(self.headers,Origin='https://outside.example')).status_code, 403)
        with self.engine.begin() as conn:
            conn.execute(self.tables['analysis_reports'].delete().where(self.tables['analysis_reports'].c.analysis_id == self.question_task))
        with patch.object(service, 'launch') as launch:
            with self.assertRaises(ValueError): self.preview()
            launch.assert_not_called()

    def test_group_capacity_fragments_and_request_count(self):
        """容量不足才分组，原文片段不丢字，组数加一且汇总超限预检拒绝。"""
        from ecommerce.infrastructure.ai.config import AISettings
        from tests.unit.test_review_analysis_v6 import FakeCounter
        incoming = self.preview()['input']; incoming['reports'][0]['markdown'] = '长报告原文。'*14000
        config = replace(AISettings(api_key='sk-test-only'), report_output_tokens=512, group_output_tokens=512,
            capabilities={'deepseek-flash': {'context_tokens': 30000, 'max_output_tokens': 8192}})
        with patch.object(planner, 'Counter', FakeCounter):
            plan = planner.plan(self.root, config, config.model, incoming, {'report':'主报告','group':'整理'})
        self.assertEqual(plan['mode'], 'grouped'); self.assertEqual(plan['planned_calls'], len(plan['groups'])+1)
        import json
        units = [unit for group in plan['groups'] for unit in json.loads(group['messages'][-1]['content'])['parts']]
        self.assertEqual(''.join(v['text'] for v in units if v['type']=='report_fragment' and v['kind']=='reviews'), incoming['reports'][0]['markdown'])
        too_small = replace(config, capabilities={'deepseek-flash': {'context_tokens': 4000, 'max_output_tokens': 8192}})
        with patch.object(planner, 'Counter', FakeCounter):
            with self.assertRaises(ValueError): planner.plan(self.root, too_small, config.model, incoming, {'report':'主报告','group':'整理'})

    def test_group_runtime_executes_exact_plan_and_preserves_phases(self):
        """超过容量时严格N组加一次总稿，组稿与请求、最终依据均可回查。"""
        from ecommerce.infrastructure.ai.config import AISettings
        from tests.unit.test_review_analysis_v6 import FakeCounter
        repository.save_report(self.settings, self.review_task, dict(self.review_content, markdown=self.review_content['markdown']+'\n'+'长段资料。'*4000))
        config = replace(AISettings(api_key='sk-test-only'), report_output_tokens=512, group_output_tokens=512,
            capabilities={'deepseek-flash': {'context_tokens':30000,'max_output_tokens':8192}})
        with patch.object(service, 'load_config', return_value=config), patch.object(planner, 'Counter', FakeCounter):
            data = self.preview()
            with patch.object(service, 'launch'):
                task = service.start(self.settings, '123', **self.values(data), allow_paid=True)['id']
        self.assertEqual(data['plan']['mode'], 'grouped');client=Mock()
        client.complete_messages.side_effect=[{'text':'本组发现：需要拿样验证。','finish_reason':'stop','usage':{'total_tokens':10}} for _ in data['plan']['groups']]+[{'text':sample_combined(),'finish_reason':'stop','usage':{'total_tokens':30}}]
        with patch.object(runtime, 'Counter', FakeCounter):
            self.assertEqual(runtime.execute(self.settings, task, Mock(), Mock(return_value=client)), 'succeeded')
        saved=service.read(self.settings,task)
        self.assertEqual(client.complete_messages.call_count,data['plan']['planned_calls'])
        self.assertEqual(len(saved['report']['content']['phases']),len(data['plan']['groups']))
        self.assertIn('group_findings',saved['calls'][-1]['request']['messages'][-1]['content'])
