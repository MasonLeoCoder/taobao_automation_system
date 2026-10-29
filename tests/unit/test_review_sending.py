"""评价合并发送、固定快照及预算追加回归；仅临时数据库和模拟AI。"""
import json
from unittest.mock import Mock, patch
import unittest

from ecommerce.modules.review_analysis import planning, rules, service
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.unit import test_review_analysis as fixtures


class ReviewSendingTests(unittest.TestCase):
    def setUp(self):
        """建立隔离资料；参数：无；返回：无。"""
        self.fixture = fixtures.ReviewAnalysisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.settings = self.fixture.settings

    def create(self, **options):
        """不派发进程地创建新计划；参数：可选限制；返回：分析编号。"""
        data = service.preview(self.settings, '123', 5)
        with patch('ecommerce.modules.review_analysis.service.launch'):
            return service.start(self.settings, '123', batch_id=5, model='deepseek-flash',
                input_hash=data['input_hash'], plan_hash=data['plan_hash'], allow_paid=True, **options)['id']

    def add_reviews(self, count, text='粘毛方便，价格实惠。'):
        """补充临时评价；参数：数量和原文；返回：无。"""
        for index in range(count):
            self.fixture.put('review_observations', id=100+index, batch_id=5, position=3+index,
                             content=text, sku_text='测试规格', append_reviews=[])

    def test_100_short_reviews_merge_with_read_only_preview(self):
        """100条短评合并一组，预览完整无昵称、不创建任务或联网。"""
        self.add_reviews(98)
        with patch('ecommerce.modules.review_analysis.service.create_client') as client:
            data = service.preview(self.settings, '123', 5)
            self.assertEqual(data['planned_calls'], 2)
            self.assertEqual(data['chunk_count'], 1)
            self.assertEqual(len(data['plan']['groups'][0]['source_ids']), 100)
            self.assertGreater(data['plan']['groups'][0]['max_output_tokens'], 4096)
            prompt = data['outgoing'][0]['prompt']
            self.assertIn('用了几天粘不住', prompt)
            self.assertNotIn('不可发送的昵称', prompt)
            self.assertNotIn('sk-test-only', prompt)
            self.assertGreater(data['default_tokens'], data['reserved_tokens'])
            self.assertEqual(repository.history(self.settings, '123'), [])
            client.assert_not_called()

    def test_long_reviews_split_without_truncation_and_oversized_single_rejected(self):
        """同时考虑输入与输出，长评完整分组，超长单条启动前拒绝。"""
        self.add_reviews(20, '粘毛效果很好。' * 150)
        data = service.preview(self.settings, '123', 5)
        self.assertGreater(data['chunk_count'], 1)
        ids = [uid for group in data['outgoing'] for uid in group['source_ids']]
        self.assertEqual(ids, [item['id'] for item in data['items']])
        for group in data['outgoing']:
            self.assertLessEqual(len(group['prompt']), planning.MAX_INPUT_CHARACTERS)
            self.assertLessEqual(group['max_output_tokens'], planning.MAX_OUTPUT_TOKENS)

    def test_oversized_single_review_rejected_before_start(self):
        """单条输入或预计输出超限时不截断、不创建任务。"""
        self.add_reviews(1, '长' * 70000)
        with self.assertRaisesRegex(ValueError, '单条评价'):
            service.preview(self.settings, '123', 5)
        self.assertEqual(repository.history(self.settings, '123'), [])

    def test_auto_budget_and_manual_insufficiency(self):
        """自动生成完整预算，手动不足给出差额，费用未确认不能创建。"""
        data = service.preview(self.settings, '123', 5)
        with self.assertRaisesRegex(ValueError, '还差1'):
            self.create(max_tokens=data['reserved_tokens']-1)
        self.assertEqual(repository.history(self.settings, '123'), [])
        task = self.create()
        parameters = repository.read(self.settings, task)['task']['parameters']
        self.assertEqual(parameters['budget_mode'], 'auto')
        self.assertEqual(parameters['max_tokens'], data['default_tokens'])
        self.assertEqual(parameters['max_calls'], 2)

    def test_plan_and_context_change_invalidates_preview(self):
        """商品背景变化使旧预览失效，已创建任务继续使用原背景与提示词。"""
        data = service.preview(self.settings, '123', 5)
        with self.fixture.engine.begin() as conn:
            conn.execute(self.fixture.tables['products'].update().values(display_title='新标题'))
        with self.assertRaisesRegex(ValueError, '发送计划'):
            service.start(self.settings, '123', batch_id=5, model='deepseek-flash', input_hash=data['input_hash'],
                plan_hash=data['plan_hash'], allow_paid=True)
        task = self.create()
        original = repository.read(self.settings, task)['task']['parameters']['plan']
        with patch('ecommerce.modules.review_analysis.service.chunks', side_effect=AssertionError('不能重分组')), \
             patch('ecommerce.modules.review_analysis.service.planning.build', side_effect=AssertionError('不能重算计划')), \
             patch('ecommerce.modules.review_analysis.rules.CLASSIFY', '被修改的规则'):
            client = self.fixture.run_mock(task)
        self.assertEqual(client.complete.call_args_list[0].args[0], original['groups'][0]['prompt'])
        self.assertTrue(client.complete.call_args_list[0].kwargs['json_mode'])
        self.assertEqual(client.complete.call_args_list[0].kwargs['timeout_seconds'], 180)

    def test_other_product_context_and_topics_use_recorded_category(self):
        """背景从同商品已发布资料读取，类目缺失不猜测，其他产品参考可扩展。"""
        from datetime import datetime
        self.fixture.put('collection_batches', id=6, product_id=1, module='parameters', visibility='published',
                         status='complete', captured_at=datetime(2026,10,29,11))
        with self.fixture.engine.begin() as conn:
            conn.execute(self.fixture.tables['parameter_observations'].insert().values(id=1, batch_id=6, position=1, name='商品类目', value='手机充电器'))
        data = service.preview(self.settings, '123', 5)
        self.assertEqual(data['context']['category'], '手机充电器')
        self.assertIn('充电速度', data['plan']['topic_reference']['topics'])
        self.assertNotIn('撕纸体验', data['plan']['topic_reference']['topics'])
        self.assertEqual(data['context']['category_source_batch_id'], 6)

    def test_known_usage_releases_reserve_unknown_usage_keeps_it(self):
        """完整用量按实际结算，缺少用量即使有响应也不释放预留。"""
        task = self.create()
        call = repository.begin_call(self.settings, task, 'classify', 1, 'JSON', [], 1000)
        repository.finish_call(self.settings, task, call, 'invalid', self.fixture.response({}), '模拟格式错误')
        data = repository.read(self.settings, task)
        self.assertEqual(repository.budget_status(data['task']['parameters'], data['calls'])['used_tokens'], 20)
        call = repository.begin_call(self.settings, task, 'classify', 1, 'JSON', [], 1000)
        repository.finish_call(self.settings, task, call, 'unknown', {'usage':{}}, '用量缺失')
        data = repository.read(self.settings, task)
        self.assertEqual(repository.budget_status(data['task']['parameters'], data['calls'])['used_tokens'], 1020)
        self.assertEqual(data['task']['usage']['unknown_calls'], 1)
        self.assertEqual(repository.charged_usage({'reserved_tokens':1000,'unknown':False,'total_tokens':20}, 1), 1000)

    def test_report_allowance_is_held_before_classification(self):
        """逐条调用不能花掉提前为报告保留的额度。"""
        task = self.create()
        data = repository.read(self.settings, task)
        parameters = dict(data['task']['parameters'])
        parameters['max_tokens'] = parameters['plan']['report_reserved_tokens'] + 99
        with self.fixture.engine.begin() as conn:
            conn.execute(self.fixture.tables['analysis_tasks'].update().values(parameters=parameters))
        with self.assertRaisesRegex(ValueError, '未发出新请求'):
            repository.begin_call(self.settings, task, 'classify', 1, 'JSON', [8,9], 100)
        self.assertEqual(repository.read(self.settings, task)['calls'], [])

    def test_explicit_budget_increase_resumes_report_without_resending_reviews(self):
        """额度不足保留明细，确认追加后只请求缺少报告，并记录前后额度。"""
        task = self.create()
        parameters = dict(repository.read(self.settings, task)['task']['parameters'])
        parameters['max_calls'] = 1
        with self.fixture.engine.begin() as conn:
            conn.execute(self.fixture.tables['analysis_tasks'].update().values(parameters=parameters))
        self.fixture.run_mock(task, [self.fixture.response(self.fixture.classified())])
        before = repository.read(self.settings, task)
        self.assertEqual(before['task']['status'], 'partial')
        self.assertTrue(before['report']['content']['coverage']['complete'])
        self.assertFalse(before['report']['content']['coverage']['report_complete'])
        with self.assertRaisesRegex(ValueError, '额度不足'):
            service.resume(self.settings, task, allow_paid=True)
        with patch('ecommerce.modules.review_analysis.service.launch'):
            service.resume(self.settings, task, allow_paid=True, max_calls=2)
        client = self.fixture.run_mock(task, [self.fixture.response({'findings':[],'suggestions':[]})])
        after = repository.read(self.settings, task)
        self.assertEqual(client.complete.call_count, 1)
        self.assertEqual([d['id'] for d in after['details']], [d['id'] for d in before['details']])
        self.assertEqual(after['task']['status'], 'succeeded')
        self.assertEqual(after['task']['parameters']['plan'], parameters['plan'])
        self.assertEqual(after['task']['parameters']['budget_history'][0]['after']['max_calls'], 2)

    def test_report_context_balances_evidence_and_exposes_scope(self):
        """报告输入保留正负例证及总数，不能把筛选引文当频率。"""
        details = []
        for index, sentiment in enumerate(['negative']*8 + ['positive']*8):
            details.append({'result':{'id':index,'kind':'specific','points':[{'topic':'价格','sentiment':sentiment,
                'segment':'initial','quote':'样例反馈'}]}, 'corrections':[]})
        context = rules.report_context(details, rules.snapshot(), source_count=20)
        self.assertEqual(context['coverage']['processed_count'], 16)
        self.assertEqual(context['coverage']['source_count'], 20)
        self.assertEqual(context['statistics']['topics'][0]['count'], 16)
        self.assertEqual({p['sentiment'] for p in context['evidence']}, {'negative','positive'})
        self.assertEqual(len(context['evidence']), 6)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), planning.REPORT_INPUT_CHARACTERS)

    def test_legacy_task_keeps_old_groups_and_budget_policy(self):
        """历史无plan任务仍按10条分组、原输出上限及旧预算策略执行。"""
        self.add_reviews(10)
        data = repository.preview(self.settings, '123', 5)
        legacy = {'schema_version':1,'classify':'旧分类规则 JSON 资料：','report':'旧报告规则 JSON 资料：','groups':rules.GROUPS}
        parameters = {'batch_size':10,'max_output_tokens':4096,'max_calls':10,'max_tokens':1000000,'config_file':'local-only'}
        from ecommerce.infrastructure.storage.task_store import fingerprint
        task, _ = repository.create(self.settings, '123', 5, legacy, parameters, 'deepseek', 'deepseek-flash',
                                    'reviews-v1', fingerprint(data['items']))
        def respond(prompt, **options):
            """仅生成脱敏模拟响应；参数：旧请求；返回：固定结构。"""
            self.assertNotIn('json_mode', options)
            self.assertEqual(options['max_tokens'], 4096)
            if prompt.startswith(legacy['classify']):
                items = json.loads(prompt[len(legacy['classify']):])
                self.assertLessEqual(len(items), 10)
                return self.fixture.response({'reviews':[{'id':item['id'],'kind':'general','points':[]} for item in items]})
            return self.fixture.response({'findings':[],'suggestions':[]})
        with patch('ecommerce.modules.review_analysis.service.create_client') as factory:
            factory.return_value.complete.side_effect = respond
            service.execute(self.settings, task['id'], Mock())
            self.assertEqual(factory.return_value.complete.call_count, 3)
        self.assertEqual(repository.read(self.settings, task['id'])['task']['status'], 'succeeded')


class AnalysisCommentMigrationTests(unittest.TestCase):
    def test_comment_revision_preserves_all_columns_and_relationships(self):
        """0012只更新指定备注，旧模型独立，表列与约束保持一致。"""
        from ecommerce.infrastructure.storage.mysql_migrations import schema_v11, schema_v12
        old, new = schema_v11.build_metadata(), schema_v12.build_metadata()
        self.assertEqual(set(old.tables), set(new.tables))
        changed = set()
        for name in old.tables:
            left, right = old.tables[name], new.tables[name]
            self.assertEqual([(c.name,str(c.type),c.nullable,c.primary_key) for c in left.c],
                             [(c.name,str(c.type),c.nullable,c.primary_key) for c in right.c])
            self.assertEqual({(f.name,tuple(e.target_fullname for e in f.elements)) for f in left.foreign_key_constraints},
                             {(f.name,tuple(e.target_fullname for e in f.elements)) for f in right.foreign_key_constraints})
            for column in left.c:
                if column.comment != right.c[column.name].comment:
                    changed.add((name,column.name))
        self.assertEqual(changed, set(schema_v12.COMMENT_UPDATES))
        self.assertEqual(schema_v11.REVISION, '0011_analysis')
        self.assertEqual(schema_v12.REVISION, '0012_analysis_sending')
