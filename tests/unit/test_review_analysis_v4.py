"""新版正式业务回归：脱敏SQLite、模拟AI及本地Web，不请求任何服务商。"""
import copy
import json
from unittest import TestCase
from unittest.mock import Mock,patch
from tests.unit import test_review_analysis as legacy
from ecommerce.modules.review_analysis import service,v4,v4_runtime
from ecommerce.infrastructure.storage import analysis_store as repository,analysis_review_store
from ecommerce.infrastructure.ai.client import AIError


def extraction(incoming):
    """构造脱敏完整提取；参数：真实发送结构；返回：模拟AI对象。"""
    reviews=[]
    for source in incoming['reviews']:
        quote=source['appends'][0]['text'] if source['appends'] else source['text']
        segment=source['appends'][0]['segment'] if source['appends'] else 'initial'
        point={'observation_id':'o1','information_types':['产品反馈'],'topic':'粘性','fact_text':'测试样本自述',
            'sentiment':'负面' if source['id']==8 else '正面','target':'产品','target_detail':None,'source_type':'使用体验自述',
            'event_scope':'当前购买','comparison':None,'explicit_impact':None,'evidence_ids':['e1'],'uncertainties':[]}
        reviews.append({'id':source['id'],'evidence':[{'evidence_id':'e1','segment':segment,'quote':quote}],
            'usage_stages':[],'observations':[point],'relations':[],'flags':[]})
    return {'schema_version':'reviews-extraction-v1.2','reviews':reviews}


def narrative(incoming):
    """构造可追溯模拟报告，不用于语义能力验收；参数：报告输入；返回：对象。"""
    rows=incoming['extraction']['reviews']
    refs=[{'review_id':r['id'],'observation_id':o['observation_id']} for r in rows for o in r['observations']]
    erefs=[{'review_id':r['id'],'evidence_id':e['evidence_id']} for r in rows for e in r['evidence']]
    return {'schema_version':'competitor-report-v1.2','decision_summary':[],
        'findings':[{'id':'f1','category':'问题与需求','facts':[{'fact_id':'a1','text':'测试样本包含不同表述','observation_refs':refs,'evidence_refs':erefs}],
        'interpretation':{'text':'需要实物核对','basis_observation_refs':copy.deepcopy(refs),'assumptions':['相同测试条件'],'limitations':['未实测']},
        'statistic_ids':[incoming['statistics'][0]['id']],'evidence_strength':'线索性发现','strength_reason':'脱敏模拟','limitations':['不代表市场']}],
        'opportunities':[{'id':'p1','name':'现成款对比','stage':'现成款采购','target_need':'测试用途','product_direction':'比较现成款',
            'supporting_finding_ids':['f1'],'reasoning':'待检验假设','counter_evidence':[{'finding_id':'f1','fact_id':'a1','explanation':'原反证待核对'}],
            'risk_hypotheses':['样本不具代表性'],'conditions':['取得规格'],'missing_information':['报价'],
            'validation_actions':['先询价再拿样对比'],'stop_conditions':['当前样品不符合目标需求则淘汰当前款'],
            'priority':'补充资料后判断','priority_reason':'缺报价','independence_reason':'单一测试方向'}],
        'business_checks':[{'item':'报价','status':'未提供','known_information':None,'material_refs':[],'missing_information':'未提供报价','decision_impact':'尚不能决定采购'}],
        'next_actions':[],'data_issues':[{'description':'模型自由文字把来源误写成999','affected_sections':['findings'],'handling':'此说明不作为程序来源'}]}


class ReviewV4Tests(TestCase):
    use_v4=True
    def setUp(self):
        """固定旧V4预览分支；参数：无；返回：无。"""
        legacy.ReviewAnalysisTests.setUp(self)
        self.preview_patch=patch.object(service,'preview',service.preview_v4)
        self.preview_patch.start();self.addCleanup(self.preview_patch.stop)
    put=legacy.ReviewAnalysisTests.put
    response=legacy.ReviewAnalysisTests.response

    def start(self):
        """仅创建模拟任务，不派发；参数：无；返回：编号。"""
        data=service.preview(self.settings,'123',5)
        with patch.object(service,'launch'):
            return service.start(self.settings,'123',batch_id=5,model='deepseek-flash',input_hash=data['input_hash'],plan_hash=data['plan_hash'],allow_paid=True)['id']

    def run_mock(self,identifier,mutate=None):
        """按实际发送内容生成模拟响应；参数：任务、可选变异；返回：客户端。"""
        client=Mock()
        def complete(text,**options):
            incoming=json.loads(text.split('以下是输入资料：\n',1)[1]);kind='classify' if 'reviews' in incoming else 'report'
            result=extraction(incoming) if kind=='classify' else narrative(incoming)
            if mutate:result=mutate(kind,result)
            return self.response(result)
        client.complete.side_effect=complete
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,identifier,Mock())
        return client

    def test_default_preview_full_scope_and_unpaid_get(self):
        with patch.object(service,'create_client') as factory:
            data=service.preview(self.settings,'123',5)
            self.assertEqual(data['rule_version'],v4.VERSION)
            self.assertEqual({uid for g in data['plan']['groups'] for uid in g['source_ids']},{8,9})
            self.assertNotIn('不可发送的昵称',json.dumps(data['plan'],ensure_ascii=False))
            self.assertEqual(self.client.get('/analysis/reviews/products/123/new?batch=5&dialog=1').status_code,200)
            factory.assert_not_called()
        self.assertEqual(data['plan']['report_slots'],len(data['plan']['groups'])+1)

    def test_full_flow_read_export_and_separate_review_counts(self):
        task=self.start();client=self.run_mock(task);data=service.read(self.settings,task)
        self.assertEqual(client.complete.call_count,2)
        self.assertEqual(data['task']['status'],'succeeded');self.assertEqual(data['task']['processed_count'],2)
        self.assertEqual(data['review_counts'],{'pending':3,'adopted':0,'rejected':0})
        stats=next(s for s in data['v4_content']['statistics'] if s['kind']=='主题')
        self.assertEqual(stats['count'],2);self.assertEqual(stats['sentiments']['负面']['count'],1)
        with patch.object(service,'create_client') as factory:
            for suffix in ('','/export','/export?format=html','?version=1'):
                response=self.client.get('/analysis/reviews/tasks/'+task+suffix)
                self.assertEqual(response.status_code,200,suffix)
            factory.assert_not_called()

    def test_review_explicit_ack_conflict_history_and_version_binding(self):
        task=self.start();self.run_mock(task);data=service.read(self.settings,task);item=data['v4_content']['review_items'][0]
        values={'report_id':data['report']['id'],'item_key':item['key'],'candidate_hash':item['candidate_hash'],
            'source_hash':data['v4_content']['source_hash'],'expected_revision':0,'decision':'adopted','reviewer':'测试人','reason':'已对照测试依据'}
        endpoint=f'/api/analysis/reviews/tasks/{task}/decisions'
        self.assertEqual(self.client.post(endpoint,json=values,headers=self.headers).status_code,400)
        values['review_context']={'evidence_checked':True}
        self.assertEqual(self.client.post(endpoint,json=values,headers=self.headers).status_code,200)
        self.assertEqual(self.client.post(endpoint,json=values,headers=self.headers).status_code,400)
        values.update(expected_revision=1,decision='pending')
        self.assertEqual(self.client.post(endpoint,json=values,headers=self.headers).status_code,200)
        records=analysis_review_store.decisions(self.settings,task,data['report']['id']);self.assertEqual(len(records),2)
        service.recount(self.settings,task)
        self.assertEqual(self.client.post(endpoint,json={**values,'expected_revision':2},headers=self.headers).status_code,400)
        self.assertEqual(self.client.get(f'/analysis/reviews/tasks/{task}?version=1').status_code,200)

    def test_direction_requires_conditions_and_explicit_stop_scope(self):
        task=self.start();self.run_mock(task);data=service.read(self.settings,task)
        item=next(i for i in data['v4_content']['review_items'] if i['kind']=='选品方向')
        values={'report_id':data['report']['id'],'item_key':item['key'],'candidate_hash':item['candidate_hash'],
            'source_hash':data['v4_content']['source_hash'],'expected_revision':0,'decision':'adopted','reviewer':'测试人','reason':'已核对动作',
            'review_context':{'evidence_checked':True}}
        with self.assertRaises(ValueError):analysis_review_store.save(self.settings,task,**values)
        values['review_context'].update(conditions_checked=True,stop_scope='current_model')
        analysis_review_store.save(self.settings,task,**values)
        self.assertEqual(service.read(self.settings,task)['v4_content']['narrative']['opportunities'][0]['decision'],'pending')

    def test_local_correction_removes_stale_narrative_but_keeps_original(self):
        from ecommerce.modules.review_analysis import v4_view
        task=self.start();self.run_mock(task);old=service.read(self.settings,task);detail=old['details'][0];point=detail['effective']['observations'][0]
        changes={'expected_revision':0,'points':[{'observation_id':point['observation_id'],'topic':'测试新主题','sentiment':'不明确','fact_text':point['fact_text']}],
                 'excluded':False,'reason':'测试修正'}
        with patch.object(service,'create_client') as factory:
            v4_view.correct(self.settings,task,detail['id'],**changes)
            current=service.read(self.settings,task);factory.assert_not_called()
        self.assertIsNone(current['v4_content']['narrative']);self.assertEqual(len(current['reports']),2)
        self.assertEqual(current['details'][0]['result'],old['details'][0]['result'])
        history=self.client.get(f'/analysis/reviews/tasks/{task}?version=1')
        self.assertEqual(history.status_code,200)
        self.assertEqual(old['report']['content']['extraction_snapshot'][0]['observations'][0]['topic'],'粘性')
        with self.assertRaises(ValueError):v4_view.correct(self.settings,task,detail['id'],**changes)

    def test_invalid_one_review_keeps_other_and_generates_partial_report(self):
        task=self.start()
        def mutate(kind,value):
            if kind=='classify':value['reviews'][0]['evidence'][0]['quote']='不存在的测试引文'
            return value
        client=self.run_mock(task,mutate);data=service.read(self.settings,task)
        self.assertEqual(client.complete.call_count,2);self.assertEqual(data['task']['status'],'partial')
        self.assertEqual([r['result']['id'] for r in data['details']],[9]);self.assertIsNotNone(data['v4_content']['narrative'])
        self.assertEqual(data['calls'][0]['status'],'invalid')
        with patch.object(service,'create_client') as factory:
            count=len(data['reports']);service.recover_saved(self.settings,task)
            self.assertEqual(len(repository.read(self.settings,task)['reports']),count);factory.assert_not_called()

    def test_unknown_call_stops_no_retry_and_keeps_budget(self):
        task=self.start();client=Mock();client.complete.side_effect=AIError('测试网络未确认')
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,task,Mock())
        data=service.read(self.settings,task)
        self.assertEqual(client.complete.call_count,1);self.assertEqual(data['calls'][0]['status'],'unknown')
        self.assertGreater(data['budget']['used_tokens'],0)
        with self.assertRaises(ValueError):service.resume(self.settings,task,allow_paid=True)

    def test_plan_length_budget_and_no_silent_sampling(self):
        items=[{'id':i,'text':'测试文字','sku':'测试规格','appends':[]} for i in range(1,101)]
        plan=v4.plan(items,v4.snapshot(),{},30)
        self.assertEqual([uid for g in plan['groups'] for uid in g['source_ids']],list(range(1,101)))
        with self.assertRaises(ValueError):v4.plan([{'id':1,'text':'测试'*10000,'sku':'','appends':[]}],v4.snapshot(),{},30)

    def test_report_local_invalid_part_isolated(self):
        task=self.start()
        def mutate(kind,value):
            if kind=='report':value['findings'][0]['interpretation']['basis_observation_refs'][0]['review_id']='8'
            return value
        self.run_mock(task,mutate);data=service.read(self.settings,task)
        self.assertIsNotNone(data['v4_content']['narrative'])
        self.assertIsNone(data['v4_content']['narrative']['findings'][0]['interpretation'])
        self.assertEqual(data['task']['processed_count'],2)

    def test_frozen_production_prompt_and_processing_match_approved(self):
        from pathlib import Path
        base=Path(__file__).resolve().parents[2]
        for filename,label in [('extract_v4.txt','提取'),('report_v4.txt','报告')]:
            expected=(base/'docs/操作参考/评价提示词V1.2'/f'{label}.md').read_text(encoding='utf-8').split('```text\n',1)[1].rsplit('```',1)[0].strip()
            self.assertEqual(v4.snapshot()['classify' if label=='提取' else 'report'],expected)

    def test_segment_reports_and_final_summary_with_disclosed_basis(self):
        for uid in range(10,32):self.put('review_observations',id=uid,batch_id=5,position=uid,content='脱敏测试评价',sku_text='',append_reviews=[])
        task=self.start();original=v4.prompt
        def limited(rules,kind,incoming):
            if kind=='report' and not incoming.get('segment_findings') and len(incoming['extraction']['reviews'])==24:return 'x'*(v4.MAX_INPUT+1)
            return original(rules,kind,incoming)
        with patch.object(v4,'prompt',side_effect=limited):client=self.run_mock(task)
        data=service.read(self.settings,task);groups=len(data['task']['parameters']['plan']['groups'])
        self.assertEqual(data['task']['status'],'succeeded')
        self.assertEqual(client.complete.call_count,groups*2+1)
        self.assertEqual(data['v4_content']['report_basis']['all_observations'],24)
        self.assertTrue(any(c['kind']=='report' and c['chunk_index']>0 for c in data['calls']))

    def test_report_too_large_stops_without_truncation_or_auto_request(self):
        task=self.start();original=v4.prompt
        with patch.object(v4,'prompt',side_effect=lambda rules,kind,incoming:'x'*(v4.MAX_INPUT+1) if kind=='report' else original(rules,kind,incoming)):
            client=self.run_mock(task)
        data=service.read(self.settings,task)
        self.assertEqual(client.complete.call_count,1)
        self.assertEqual(data['task']['processed_count'],2);self.assertIsNone(data['v4_content']['narrative'])

    def test_review_different_candidate_or_source_never_adopted(self):
        task=self.start();self.run_mock(task);data=service.read(self.settings,task);item=data['v4_content']['review_items'][0]
        common=dict(report_id=data['report']['id'],item_key=item['key'],candidate_hash=item['candidate_hash'],source_hash=data['v4_content']['source_hash'],
            expected_revision=0,decision='rejected',reviewer='测试人',reason='测试不采用')
        for field in ('candidate_hash','source_hash'):
            with self.assertRaises(ValueError):analysis_review_store.save(self.settings,task,**{**common,field:'0'*64})

    def test_missing_report_json_keeps_statistics_and_original_response(self):
        task=self.start();client=Mock()
        client.complete.side_effect=[self.response(extraction({'reviews':service.preview(self.settings,'123',5)['items']})),{'text':'{','finish_reason':'stop','usage':{'total_tokens':1}}]
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,task,Mock())
        data=service.read(self.settings,task)
        self.assertEqual(client.complete.call_count,2);self.assertEqual(len(data['details']),2)
        self.assertIsNone(data['v4_content']['narrative']);self.assertEqual(data['calls'][-1]['response']['text'],'{')
        self.assertEqual(self.client.get(f'/analysis/reviews/tasks/{task}').status_code,200)

    def test_explicit_whole_exclusion_keeps_original_points_and_no_ai(self):
        from ecommerce.modules.review_analysis import v4_view
        task=self.start();self.run_mock(task);data=service.read(self.settings,task);detail=data['details'][0]
        with patch.object(service,'create_client') as factory:
            v4_view.correct(self.settings,task,detail['id'],expected_revision=0,points=[],excluded=True,reason='测试整条排除')
            factory.assert_not_called()
        after=service.read(self.settings,task)
        self.assertEqual(after['v4_content']['coverage']['included_count'],1)
        self.assertEqual(after['details'][0]['result'],detail['result'])

    def test_review_invalid_identity_is_safe_400(self):
        task=self.start();self.run_mock(task)
        bad={'report_id':[],'item_key':None,'candidate_hash':[],'source_hash':'x','expected_revision':0,
             'decision':'rejected','reviewer':'测试人','reason':'测试无效身份'}
        self.assertEqual(self.client.post(f'/api/analysis/reviews/tasks/{task}/decisions',json=bad,headers=self.headers).status_code,400)

    def test_cli_export_is_json_serializable_and_readonly(self):
        from types import SimpleNamespace
        from ecommerce.cli.commands.analysis import execute
        task=self.start();self.run_mock(task)
        path=self.root/'report.json'
        with patch.object(service,'create_client') as factory:
            execute(self.settings,SimpleNamespace(action='export',task_id=task,output=str(path)))
            factory.assert_not_called()
        exported=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(exported['report']['content']['schema_version'],v4.REPORT_VERSION)
