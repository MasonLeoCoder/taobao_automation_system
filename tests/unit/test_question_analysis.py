"""问答分析隔离事务与模拟API回归，禁止真实AI请求。"""
import copy
import json
from unittest.mock import Mock,patch
from unittest import TestCase
from ecommerce.infrastructure.storage.mysql_migrations.schema_v19 import build_metadata
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.question_analysis import service,checks,planner,runtime
from ecommerce.web.routes.question_analysis import render_report
from tests.unit import test_review_analysis as legacy


def sample_report(items):
    """构造仅用于接口测试的七章报告；参数：样例问题；返回：Markdown。"""
    q=items[0];a=q['answers'][0] if q['answers'] else None
    cite=f'【问题{q["id"]}｜回答{a["id"]}】原文：“{a["text"]}”' if a else f'【问题{q["id"]}】原文：“{q["text"]}”'
    return '\n\n'.join('## '+name+'\n\n脱敏测试内容。'+('\n\n'+cite if index==0 else '') for index,name in enumerate(checks.SECTIONS))


class QuestionTests(TestCase):
    put=legacy.ReviewAnalysisTests.put
    def setUp(self):
        """复用临时库并使用新来源结构；参数：无；返回：无。"""
        with patch('tests.unit.test_review_analysis.build_metadata',build_metadata):legacy.ReviewAnalysisTests.setUp(self)
        self.put('collection_batches',id=6,product_id=1,module='questions',visibility='published',status='complete')
        self.put('question_observations',id=10,batch_id=6,position=1,question='会不会掉胶？',answers_complete=False,answer_stop_reason='not_expanded')
        self.put('question_observations',id=11,batch_id=6,position=2,question='养猫家庭适用吗？',answers_complete=True,answer_stop_reason='exact_answer_count')
        self.put('answer_observations',id=20,batch_id=6,question_id=10,position=1,content='我使用两周没有掉胶',author='私密昵称',sku_text='规格甲',payload={'identity_labels':['已购'],'media':['secret']})

    def create_qa(self):
        """只创建模拟任务；参数：无；返回：编号。"""
        data=service.preview(self.settings,'123',6)
        with patch.object(service,'launch'):
            return service.start(self.settings,'123',batch_id=6,model=data['model'],input_hash=data['input_hash'],plan_hash=data['plan_hash'],allow_paid=True)['id']

    def test_direct_scope_messages_storage_report_and_recheck(self):
        """整批及无回答问题保留，模拟一次报告，查看不调用接口，原文默认折叠。"""
        before=service.preview(self.settings,'123',6);self.assertEqual(before['answer_count'],1)
        self.assertEqual(before['plan']['planned_calls'],1)
        outgoing=before['plan']['groups'][0]['prompt'];self.assertNotIn('私密昵称',outgoing);self.assertNotIn('secret',outgoing)
        identifier=self.create_qa();client=Mock();client.complete_messages.return_value={'text':sample_report(before['items']),'finish_reason':'stop','usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}}
        with patch('ecommerce.modules.review_analysis.service.create_client',return_value=client):
            from ecommerce.modules.review_analysis.service import execute
            self.assertEqual(execute(self.settings,identifier,Mock()),'succeeded')
        self.assertEqual(client.complete_messages.call_count,1)
        data=service.read(self.settings,identifier);self.assertEqual(data['task']['processed_count'],0)
        self.assertEqual(data['sources'][0]['question_id'],10);self.assertIsNone(data['sources'][0]['review_id'])
        self.assertEqual(data['report']['content']['validation']['counts']['valid_count'],1)
        page=self.client.get('/analysis/questions/tasks/'+identifier);self.assertEqual(page.status_code,200);self.assertIn('ui-report-evidence',page.text)
        for format in ('md','html','json'):self.assertEqual(self.client.get('/analysis/questions/tasks/'+identifier+'?download='+format).status_code,200)
        response=self.client.post('/api/analysis/questions/tasks/'+identifier+'/check',json={'report_id':data['report']['id']},headers=self.headers)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['ai_calls'],0)
        self.assertEqual(len(repository.history(self.settings,'123',6,analysis_type='questions')),1)
        self.assertEqual(repository.history(self.settings,'123',6),[])
        self.assertEqual(self.client.get('/analysis/questions/products/123?batch=6').status_code,200)

    def test_question_answer_binding_and_quote_check(self):
        """错题回答不能通过引用检查；问题顾虑可直接引用，不当作缺陷。"""
        items=service.preview(self.settings,'123',6)['items']
        wrong=checks.check('【问题11｜回答20】原文：“我使用两周没有掉胶”','stop',items)
        self.assertEqual(wrong['counts']['valid_count'],0)
        correct=checks.check('【问题10】原文：“会不会掉胶？”','stop',items)
        self.assertEqual(correct['counts']['valid_count'],1)

    def test_consent_wrong_batch_stale_plan_and_duplicate(self):
        """费用守卫、跨商品批次、过期计划及重复启动的选择行为。"""
        data=service.preview(self.settings,'123',6)
        values=dict(batch_id=6,model=data['model'],input_hash=data['input_hash'],plan_hash=data['plan_hash'])
        with self.assertRaises(ValueError):service.start(self.settings,'123',**values)
        with self.assertRaises(LookupError):service.preview(self.settings,'123',5)
        with self.assertRaises(ValueError):service.start(self.settings,'123',**dict(values,plan_hash='changed'),allow_paid=True)
        identifier=self.create_qa();repository.update_task(self.settings,identifier,status='failed',active_key=None)
        with patch.object(service,'launch') as launch:
            result=service.start(self.settings,'123',**values,allow_paid=True)
            self.assertTrue(result['choice_required']);launch.assert_not_called()
            fresh=service.start(self.settings,'123',**values,allow_paid=True,force=True)
            self.assertNotEqual(fresh['id'],identifier);launch.assert_called_once()
        self.assertEqual(self.client.post('/api/analysis/questions/products/123',json=values).status_code,403)

    def test_no_answers_and_safe_render(self):
        """没有回答仍可生成需求报告；不让模型HTML执行。"""
        items=[{'id':11,'text':'养猫家庭适用吗？','answers':[]}]
        text=sample_report(items)+'\n\n<script>alert(1)</script>'
        value=checks.check(text,'stop',items);self.assertTrue(value['generation']['complete'])
        rendered=render_report({'markdown':text,'validation':value});self.assertNotIn('<script>',rendered)
        self.assertIn('<details',rendered)

    def test_preview_templates_and_history(self):
        """整页、弹窗、切换模型和历史均可本地读取；参数：无；返回：断言。"""
        for suffix in ('','&dialog=1','&dialog=1&model=deepseek-v4-pro&output_tokens=4096'):
            page=self.client.get('/analysis/questions/products/123/new?batch=6'+suffix)
            self.assertEqual(page.status_code,200);self.assertIn('data-qa-form',page.text)
        self.assertEqual(self.client.get('/analysis/questions/products/123?batch=6').status_code,200)
        history=self.client.get('/analysis/questions/products/123?batch=6').text
        self.assertIn('data-qa-open',history)
        self.assertIn('本批尚未分析',history)
        form=self.client.get('/analysis/questions/products/123/new?batch=6&dialog=1').text
        self.assertIn('ui-confirm-footer',form)
        self.assertIn('data-qa-cancel',form)
        self.assertNotIn('data-qa-advanced open',form)

    def test_long_answer_fragments_preserve_full_text(self):
        """单题分片保持问题语境及回答编号，不丢字或改写。"""
        from tests.unit.test_review_analysis_v6 import FakeCounter
        row={'id':1,'text':'适合吗？','answers':[{'id':2,'text':'长回答。'*8000}]}
        profile={'context_tokens':12000}
        parts=planner.fragments(row,{'group':'整理'},'标题',FakeCounter(),profile,512)
        self.assertGreater(len(parts),1)
        self.assertEqual(''.join(p['answers'][0]['text'] for p in parts),row['answers'][0]['text'])
        self.assertTrue(all(p['id']==1 and p['answers'][0]['id']==2 for p in parts))

    def test_grouped_plan_and_final_summary(self):
        """确实超限才N组加一次汇总；参数：无；返回：固定来源和计费断言。"""
        from dataclasses import replace
        from ecommerce.infrastructure.ai.config import AISettings
        from tests.unit.test_review_analysis_v6 import FakeCounter
        config=replace(AISettings(),report_output_tokens=512,group_output_tokens=512,
            capabilities={'deepseek-flash':{'context_tokens':30000,'max_output_tokens':8192}})
        items=[{'id':i,'text':'问题'+str(i),'answers':[{'id':i+100,'text':'使用反馈很长。'*220}],'answers_complete':True} for i in range(1,9)]
        with patch.object(planner,'Counter',FakeCounter):
            plan=planner.plan(self.root,config,config.model,items,{'title':'样例'},{'report':'生成','group':'整理'})
        self.assertEqual(plan['mode'],'grouped');self.assertEqual(plan['planned_calls'],len(plan['groups'])+1)
        self.assertEqual({i for g in plan['groups'] for i in g['source_ids']},set(range(1,9)))
        calls=[{'chunk_index':g['index'],'request':{'source_ids':g['source_ids']},'response':{'text':'本组证据'}} for g in plan['groups']]
        values=planner.summary_messages({'rules':{'report':'生成','group':'整理'},'parameters':{'plan':plan}},calls)
        self.assertIn('group_findings',json.loads(values[-1]['content']))

    def test_network_failure_is_not_automatically_retried(self):
        """网络未知保留占用且不自动重发；参数：无；返回：一次调用和失败记录。"""
        from ecommerce.infrastructure.ai.client import AIError
        identifier=self.create_qa();client=Mock();client.complete_messages.side_effect=AIError('测试连接失败')
        runtime.execute(self.settings,identifier,Mock(),Mock(return_value=client))
        data=service.read(self.settings,identifier);self.assertEqual(client.complete_messages.call_count,1)
        self.assertEqual(data['calls'][0]['status'],'unknown');self.assertEqual(data['task']['status'],'failed')
        self.assertEqual(repository.budget_status(data['task']['parameters'],data['calls'])['used_calls'],1)

    def test_reading_groups_quotes_and_preserves_report(self):
        """只读目录、明确标签及引用分组保留原稿，含义不明确不自动分类。"""
        from ecommerce.modules.question_analysis.view import reading,emphasize
        items=service.preview(self.settings,'123',6)['items']
        text=sample_report(items)+'\n\n【问题10】原文：“会不会掉胶？”\n\n【问题10｜回答20】原文：“我使用两周没有掉胶”'
        content={'markdown':text,'validation':checks.check(text,'stop',items)};original=copy.deepcopy(content)
        result=reading(content)
        self.assertEqual(len(result['sections']),7);self.assertTrue(result['sections'][0]['lead'])
        rendered=''.join(s['html'] for s in result['sections'])
        self.assertIn('2处引用',rendered);self.assertIn('qa-citation-3',rendered);self.assertIn('#answer-20',rendered)
        self.assertEqual(content,original)
        self.assertIn('ui-report-key-label',emphasize('<p>先补资料：检查规格。</p>'))
        self.assertEqual(emphasize('<p>用户问会不会掉胶。</p>'),'<p>用户问会不会掉胶。</p>')
        self.assertEqual(emphasize('<p><strong>先补资料：检查规格。</strong></p>'),'<p><strong>先补资料：检查规格。</strong></p>')
        listed=emphasize('<p>待核实事项：规格未确认；长度未提供。</p>')
        self.assertIn('<ul>',listed);self.assertIn('规格未确认；',listed)
        quoted={'markdown':text+'\n\n> ## 总体判断\n> 引用内标题不应进入报告目录。','validation':checks.check(text,'stop',items)}
        self.assertEqual(len(reading(quoted)['sections']),7)
