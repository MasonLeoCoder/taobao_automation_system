"""新直接报告的有意义边界回归：容量、分片、费用、缓存、引用和旧任务保护。"""
import copy
import json
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock,patch
from ecommerce.infrastructure.ai.config import load_config,AISettings
from ecommerce.infrastructure.ai.capabilities import Counter,capability
from ecommerce.infrastructure.ai.client import AIError,create_client
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.review_analysis import service,v6,v6_runtime,v6_report
from tests.unit import test_review_analysis as legacy


def report_for(incoming):
    """构造符合引用规则的模拟稿，不代表语义验收；参数：实际user内容；返回：Markdown。"""
    citation=''
    if 'reviews' in incoming:
        row=incoming['reviews'][0];appends=row.get('appends',[])
        if appends:label='追评'+appends[0]['segment'].split(':')[1];quote=appends[0]['text'][:40]
        else:label='初评';quote=row['text'][:40]
        citation=f'【评价{row["id"]}｜{label}】原文：“{quote}”'
    else:
        import re
        match=re.search(r'【评价[^\n]+',incoming['group_findings'][0]['findings'])
        citation=match.group() if match else ''
    return '\n\n'.join('## '+name+'\n这是脱敏模拟内容，真实采购需实测。'+('\n'+citation if i==0 else '') for i,name in enumerate(v6_report.SECTIONS))


class FakeCounter(Counter):
    """可控Token计数，避免测试依赖本机官方数据或远程下载。"""
    def __init__(self,*args):
        """设置可控工具；参数：忽略；返回：实例。"""
        self.tool=True;self.identity={'method':'controlled-test','exact':False}
    def count(self,messages):
        """用字节作为可控单位；参数：消息；返回：数量。"""
        return sum(len(m['content'].encode())+64 for m in messages)+64


class ReviewV6Tests(TestCase):
    use_v4=True
    setUp=legacy.ReviewAnalysisTests.setUp
    put=legacy.ReviewAnalysisTests.put
    response=legacy.ReviewAnalysisTests.response

    def test_projection_omits_blank_appends_preserves_original_number_and_text(self):
        """空追评不发送，后续序号与原文不变，来源不修改；参数：无；返回：断言。"""
        source={'id':7,'text':' 初评 ','sku':'','appends':[{'segment':'append:1','text':'  '},{'segment':'append:2','text':' 真实追评 '}]}
        original=copy.deepcopy(source);projected=v6.project(source)
        self.assertEqual(projected,{'id':7,'text':' 初评 ','appends':[{'segment':'append:2','text':' 真实追评 '}]})
        self.assertEqual(source,original)

    def start(self,**kwargs):
        """只建隔离任务，不派发；参数：可覆盖确认值；返回：编号。"""
        data=service.preview(self.settings,'123',5)
        values=dict(batch_id=5,model=data['model'],input_hash=data['input_hash'],plan_hash=data['plan_hash'],allow_paid=True)
        values.update(kwargs)
        with patch.object(service,'launch'):return service.start(self.settings,'123',**values)['id']

    def run_mock(self,identifier,fail_at=None,finish='stop'):
        """按实际消息单次返回，禁止旧逐条接口；参数：任务、故障、结束；返回：客户端。"""
        client=Mock();client.complete.side_effect=AssertionError('新流程不得调用旧逐条接口')
        def complete(messages,**options):
            if fail_at and client.complete_messages.call_count==fail_at:raise AIError('脱敏网络连接失败')
            result=self.response(report_for(json.loads(messages[-1]['content'])));result['text']=report_for(json.loads(messages[-1]['content']));result['finish_reason']=finish
            return result
        client.complete_messages.side_effect=complete
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,identifier,Mock())
        return client

    def grouped_task(self):
        """构造必须分组的模拟容量；参数：无；返回：任务编号。"""
        with self.engine.begin() as conn:
            table=self.tables['review_observations'];conn.execute(table.update().values(content='长评甲。'*1500,append_reviews=[]))
        original=load_config(self.root);config=replace(original,report_output_tokens=1024,group_output_tokens=512,capabilities={'deepseek-flash':{'context_tokens':36000}})
        with patch('ecommerce.modules.review_analysis.service.load_config',return_value=config),patch('ecommerce.modules.review_analysis.v6.Counter',FakeCounter):
            return self.start()

    def test_full_source_one_request_no_trace_metadata_or_empty_fields(self):
        with patch.object(service,'create_client') as factory:
            data=service.preview(self.settings,'123',5);factory.assert_not_called()
        plan=data['plan'];self.assertEqual(plan['mode'],'direct');self.assertEqual(plan['planned_calls'],1)
        self.assertEqual(plan['report_output_tokens'],8192);self.assertEqual(len(plan['groups'][0]['messages']),2)
        incoming=json.loads(plan['groups'][0]['messages'][-1]['content'])
        self.assertEqual(set(incoming),{'product','reviews'});self.assertEqual([r['id'] for r in incoming['reviews']],[8,9])
        self.assertEqual(incoming['reviews'][0]['appends'][0]['text'],'用了几天粘不住');self.assertNotIn('appends',incoming['reviews'][1])
        self.assertNotIn('不可发送的昵称',v6.encode(incoming));self.assertNotIn('title_source_batch_id',v6.encode(incoming))
        identifier=self.start();client=self.run_mock(identifier);data=service.read(self.settings,identifier)
        self.assertEqual(client.complete_messages.call_count,1);self.assertEqual(data['task']['status'],'succeeded')
        self.assertEqual(data['task']['processed_count'],0);self.assertFalse(data['details'])
        self.assertEqual(data['v6_content']['validation']['state'],'complete');self.assertEqual(data['v6_content']['coverage']['source_count'],2)
        self.assertEqual(data['calls'][0]['request']['messages'],plan['groups'][0]['messages'])
        self.assertFalse(data['calls'][0]['request']['json_mode'])

    def test_model_and_output_replan_invalidates_old_confirmation(self):
        first=service.preview(self.settings,'123',5);other=service.preview(self.settings,'123',5,model='deepseek-v4-pro',output_tokens=1024)
        self.assertNotEqual(first['plan_hash'],other['plan_hash']);self.assertNotEqual(first['plan']['estimated_cost'],other['plan']['estimated_cost'])
        with self.assertRaisesRegex(ValueError,'计划'):
            self.start(model='deepseek-v4-pro')
        with patch.object(service,'create_client') as factory:
            result=self.client.get('/api/analysis/reviews/products/123/plan?batch=5&model=deepseek-v4-pro&output_tokens=1024')
            self.assertEqual(result.status_code,200);self.assertEqual(result.json['plan_hash'],other['plan_hash']);factory.assert_not_called()

    def test_success_reuse_ignores_budget_capacity_and_estimator_but_not_prompt(self):
        identifier=self.start();self.run_mock(identifier)
        config=replace(load_config(self.root),capabilities={'deepseek-flash':{'context_tokens':900000,'input_usd_per_million':0.2}})
        with patch.object(service,'load_config',return_value=config):
            data=service.preview(self.settings,'123',5)
            with patch.object(service,'launch') as launch:
                result=service.start(self.settings,'123',batch_id=5,model=data['model'],input_hash=data['input_hash'],plan_hash=data['plan_hash'],allow_paid=True)
                self.assertEqual(result['id'],identifier);self.assertTrue(result['choice_required']);launch.assert_not_called()
        old=repository.read(self.settings,identifier)
        second=self.start(force=True);self.assertNotEqual(second,identifier);self.assertEqual(repository.read(self.settings,identifier),old)

    def test_budget_and_unknown_error_stop_without_retry(self):
        data=service.preview(self.settings,'123',5)
        with self.assertRaisesRegex(ValueError,'不足'):self.start(max_tokens=data['reserved_tokens']-1)
        identifier=self.start();client=self.run_mock(identifier,fail_at=1);data=service.read(self.settings,identifier)
        self.assertEqual(client.complete_messages.call_count,1);self.assertEqual(data['task']['status'],'failed');self.assertIn('网络',data['task']['message'])
        self.assertEqual(data['calls'][0]['status'],'unknown');self.assertEqual(data['budget']['used_tokens'],data['calls'][0]['usage']['reserved_tokens'])

    def test_truncated_and_bad_reference_remain_readable(self):
        identifier=self.start();self.run_mock(identifier,finish='length');data=service.read(self.settings,identifier)
        self.assertEqual(data['task']['status'],'partial');self.assertTrue(data['v6_content']['markdown']);self.assertEqual(data['v6_content']['generation']['state'],'interrupted')
        result=v6_report.check('## 总体判断\n【评价8｜初评】原文：“原文不存在”','stop',[{'id':8,'text':'原文','appends':[]}])
        self.assertEqual(result['state'],'needs_review');self.assertFalse(result['citations'][0]['valid'])

    def test_incomplete_report_is_not_cached_as_success(self):
        identifier=self.start();client=Mock()
        client.complete_messages.return_value={'text':'## 总体判断\n只有简短结论。','finish_reason':'stop','usage':{'total_tokens':10}}
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,identifier,Mock())
        data=service.read(self.settings,identifier)
        self.assertEqual(data['task']['status'],'partial');self.assertTrue(data['v6_content']['markdown'])
        self.assertEqual(data['resume_budget']['required_calls'],1);self.assertEqual(client.complete_messages.call_count,1)

    def test_grouped_plan_n_plus_one_and_resume_keeps_completed(self):
        identifier=self.grouped_task();plan=repository.read(self.settings,identifier)['task']['parameters']['plan']
        self.assertEqual(plan['mode'],'grouped');self.assertEqual(plan['planned_calls'],len(plan['groups'])+1)
        client=self.run_mock(identifier,fail_at=2);data=service.read(self.settings,identifier)
        self.assertEqual(client.complete_messages.call_count,2);self.assertEqual(len(data['v6_content']['phases']),1)
        remaining=v6_runtime.continuation(data);self.assertEqual(remaining['required_calls'],len(plan['groups']))
        self.assertNotIn(plan['groups'][0],remaining['groups'])
        budget=data['resume_budget']
        with patch.object(service,'launch'):
            service.resume(self.settings,identifier,allow_paid=True,acknowledge_unknown=True,
                max_tokens=budget['suggested_tokens'],max_calls=budget['suggested_calls'],plan_hash=budget['plan_hash'])
        resumed=self.run_mock(identifier);current=service.read(self.settings,identifier)
        self.assertEqual(resumed.complete_messages.call_count,2)
        self.assertEqual(current['task']['status'],'succeeded');self.assertEqual(len(current['v6_content']['phases']),2)

    def test_unplannable_summary_and_more_than_1000_reviews_checked_before_api(self):
        config=replace(load_config(self.root),report_output_tokens=1024,group_output_tokens=2048,capabilities={'deepseek-flash':{'context_tokens':7500}})
        items=[{'id':n+1,'text':'长评。'*900,'sku':'规格','appends':[]} for n in range(8)]
        with patch('ecommerce.modules.review_analysis.v6.Counter',FakeCounter),self.assertRaisesRegex(ValueError,'汇总'):
            v6.plan(self.root,config,'deepseek-flash',items,{'title':'商品'},v6.snapshot())
        with self.engine.begin() as conn:
            conn.execute(self.tables['review_observations'].insert(),[{'id':n+100,'batch_id':5,'position':n+3,'content':'短评','sku_text':'规格','append_reviews':[]} for n in range(1000)])
        with patch.object(service,'create_client') as factory:
            data=service.preview(self.settings,'123',5);self.assertEqual(len(data['items']),1002);self.assertEqual(data['planned_calls'],1);factory.assert_not_called()

    def test_single_long_review_fragments_cover_original_with_same_identity(self):
        item={'id':8,'text':'长评正文。'*3000,'sku':'测试规格','appends':[{'segment':'append:1','text':'追评。'*2000}]}
        counter=FakeCounter();profile={'context_tokens':10000};rules={'group':'归纳','report':'报告'}
        parts=v6.fragments(item,rules,'商品',counter,profile,512)
        self.assertGreater(len(parts),1);self.assertEqual({p['id'] for p in parts},{8})
        for segment,text in [('initial',item['text']),('append:1',item['appends'][0]['text'])]:
            covered=set()
            for part in parts:
                fragment=part['fragment']
                if fragment['segment']!=segment:continue
                covered.update(range(fragment['start'],fragment['end']))
                actual=part['text'] if segment=='initial' else part['appends'][0]['text']
                self.assertEqual(actual,text[fragment['start']:fragment['end']])
            self.assertEqual(len(covered),len(text))

    def test_no_text_requires_no_ai_and_no_fake_classification(self):
        with self.engine.begin() as conn:conn.execute(self.tables['review_observations'].update().values(content='',append_reviews=[]))
        identifier=self.start()
        with patch.object(service,'create_client') as factory:service.execute(self.settings,identifier,Mock());factory.assert_not_called()
        data=service.read(self.settings,identifier);self.assertEqual(data['v6_content']['origin'],'local_empty');self.assertEqual(len(data['calls']),0)

    def test_page_export_and_local_checks_never_create_ai(self):
        identifier=self.start();self.run_mock(identifier)
        with patch.object(service,'create_client') as factory:
            for suffix in ('','/export?format=html','/export?format=md','/export'):
                result=self.client.get('/analysis/reviews/tasks/'+identifier+suffix);self.assertEqual(result.status_code,200)
            original=repository.read(self.settings,identifier)['report']['content']['markdown']
            self.client.post('/api/analysis/reviews/tasks/'+identifier+'/recount',headers=self.headers,json={})
            checked=repository.read(self.settings,identifier)['report']['content']
            self.assertEqual(checked['markdown'],original)
            self.assertTrue(any('原文未改写' in note for note in checked['issues']))
            factory.assert_not_called()
        result=self.client.get('/analysis/reviews/tasks/'+identifier+'?version=1');self.assertIn('历史版本',result.get_data(as_text=True))
        self.assertEqual(repository.read(self.settings,identifier)['task']['processed_count'],0)

    def test_html_output_cannot_load_scripts_images_or_external_links(self):
        rendered=v6_report.render('<script>alert(1)</script> ![x](https://bad.invalid/a) [x](javascript:alert(1)) 【评价8｜初评】')
        self.assertNotIn('<script>',rendered);self.assertNotIn('<img',rendered);self.assertNotIn('href="javascript:',rendered)
        self.assertIn('href="#review-8"',rendered)

    def test_unknown_model_capacity_rejected_before_payment(self):
        with self.assertRaisesRegex(ValueError,'容量'):
            capability(replace(AISettings(),models=('unknown',),model='unknown'),'unknown')

    def test_message_client_no_60000_character_gate_and_no_json_mode(self):
        with patch('ecommerce.infrastructure.ai.client.build_opener') as factory:
            response=factory.return_value.open.return_value.__enter__.return_value
            response.read.return_value=b'{"choices":[{"message":{"content":"report"},"finish_reason":"stop"}],"usage":{"total_tokens":10}}'
            client=create_client(replace(AISettings(),api_key='sk-test-only'))
            client.complete_messages([{'role':'system','content':'规则'},{'role':'user','content':'a'*60001}],max_tokens=8192,capability=capability(AISettings(),'deepseek-flash'))
            payload=json.loads(factory.return_value.open.call_args.args[0].data)
            self.assertEqual(len(payload['messages']),2);self.assertNotIn('response_format',payload);self.assertFalse(payload['stream'])
