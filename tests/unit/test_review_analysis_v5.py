"""新评价流水线离线回归：归并、排除、真实发送大小及阶段可读性。"""
import copy
import json
from unittest import TestCase
from unittest.mock import Mock,patch
from ecommerce.modules.review_analysis import service,v4,v5,v5_runtime
from ecommerce.infrastructure.storage import analysis_store as repository,analysis_review_store
from ecommerce.infrastructure.ai.client import AIError
from tests.unit import test_review_analysis as legacy
from tests.unit.test_review_analysis_v4 import extraction,narrative


def unpack(incoming):
    """离线还原列式发送视图，检查每项含义；参数：实际输入；返回：校验视图。"""
    rows={}
    for uid,eid,segment,quote in incoming['evidence']:
        row=rows.setdefault(uid,{'id':uid,'evidence':[],'observations':[],'usage_stages':[],'relations':[],'flags':[]})
        row['evidence'].append({'evidence_id':eid,'segment':segment,'quote':quote})
    for values in incoming['observations']:
        uid,oid,types,topic,fact,sentiment,target,source,event,eids,basic=values
        row=rows.setdefault(uid,{'id':uid,'evidence':[],'observations':[],'usage_stages':[],'relations':[],'flags':[]})
        row['observations'].append({'observation_id':oid,'information_types':[incoming['information_type_values'][i] for i in types],
            'topic':topic,'fact_text':fact,'sentiment':incoming['sentiment_values'][sentiment],'target':incoming['target_values'][target],
            'source_type':incoming['source_values'][source],'event_scope':incoming['event_values'][event],'evidence_ids':eids,
            'target_detail':None,'comparison':None,'explicit_impact':None,'uncertainties':[],'eligibility':{'basic_fact':basic}})
    for uid,oid,detail,comparison,impact,uncertainties in incoming['observation_extra']:
        point=next(o for o in rows[uid]['observations'] if o['observation_id']==oid)
        point.update(target_detail=detail,comparison=dict(zip(incoming['comparison_columns'],comparison)) if comparison else None,
            explicit_impact=impact,uncertainties=[dict(zip(incoming['uncertainty_columns'],u)) for u in uncertainties])
    for uid,usage,relations,flags in incoming['review_context']:
        for key,columns,values in [('usage_stages','usage_columns',usage),('relations','relation_columns',relations),('flags','flag_columns',flags)]:
            rows[uid][key]=[dict(zip(incoming[columns],value)) for value in values]
    statistics=[{'id':s[0]} for s in incoming['statistics']]
    return {'extraction':{'reviews':list(rows.values())},'statistics':statistics}


class ReviewV5Tests(TestCase):
    use_v4=True
    def setUp(self):
        """固定原V5任务；参数：无；返回：无。"""
        legacy.ReviewAnalysisTests.setUp(self)
        preview=patch.object(service,'preview',service.preview_v5);preview.start();self.addCleanup(preview.stop)
    put=legacy.ReviewAnalysisTests.put
    response=legacy.ReviewAnalysisTests.response

    def start(self):
        """创建脱敏任务，不派发；参数：无；返回：编号。"""
        data=service.preview(self.settings,'123',5)
        with patch.object(service,'launch'):
            return service.start(self.settings,'123',batch_id=5,model='deepseek-flash',input_hash=data['input_hash'],plan_hash=data['plan_hash'],allow_paid=True)['id']

    def run_mock(self,identifier,mutate=None,finish='stop'):
        """按实际发送视图模拟接口；参数：任务、变异、结束原因；返回：客户端。"""
        client=Mock()
        def complete(text,**options):
            incoming=json.loads(text.split('以下是输入资料：\n',1)[1]);kind='classify' if 'reviews' in incoming else 'report'
            value=extraction(incoming) if kind=='classify' else narrative(unpack(incoming))
            if mutate:value=mutate(kind,value)
            result=self.response(value);result['finish_reason']=finish
            return result
        client.complete.side_effect=complete
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,identifier,Mock())
        return client

    def phase_task(self):
        """用两小组和精确字符边界触发阶段回退；参数：无；返回：任务和测试边界。"""
        identifier=self.start();data=repository.read(self.settings,identifier);plan=data['task']['parameters']['plan']
        groups=[]
        for index,source in enumerate(data['sources'],1):
            group=v5.plan([source['snapshot']],data['task']['rules'],plan['context'],plan['timeout_seconds'])['groups'][0]
            group['index']=index;groups.append(group)
        parameters=copy.deepcopy(data['task']['parameters']);parameters['plan']['groups']=groups;parameters['plan']['report_slots']=3;parameters['max_calls']=5
        with self.engine.begin() as conn:
            table=self.tables['analysis_tasks'];conn.execute(table.update().where(table.c.id==identifier).values(parameters=parameters))
        # 用一次完整脱敏提取计算边界，不写入这份中间提取。
        assessed=v5.assess(json.dumps(extraction({'reviews':[s['snapshot'] for s in data['sources']]}),ensure_ascii=False),[s['snapshot'] for s in data['sources']])
        data['details']=[{'id':str(i),'source_id':s['id'],'result':r,'corrections':[],'revision':0} for i,(s,r) in enumerate(zip(data['sources'],assessed['results']))]
        limit=len(v5.prompt(data['task']['rules'],'report',v5.context(data)))-1
        return identifier,limit

    def test_default_preview_new_fingerprint_and_no_fee(self):
        current=service.preview(self.settings,'123',5);old=service.preview_v4(self.settings,'123',5)
        self.assertEqual(current['rule_version'],v5.VERSION);self.assertNotEqual(current['plan_hash'],old['plan_hash'])
        with self.assertRaisesRegex(ValueError,'费用'):service.start(self.settings,'123',batch_id=5,model=current['model'],input_hash=current['input_hash'])

    def test_safe_fragments_renumber_without_changing_quotes(self):
        source={'id':8,'text':'包装很好，粘毛方便','appends':[]};value=extraction({'reviews':[source]});second=copy.deepcopy(value['reviews'][0])
        value['reviews'][0]['evidence'][0]['quote']='包装很好';second['evidence'][0]['quote']='粘毛方便';value['reviews'].append(second)
        original=copy.deepcopy(value);result=v5.assess(json.dumps(value,ensure_ascii=False),[source])
        self.assertEqual(value,original);self.assertEqual(len(result['results'][0]['observations']),2)
        self.assertEqual([e['quote'] for e in result['results'][0]['evidence']],['包装很好','粘毛方便'])
        self.assertEqual(result['results'][0]['observations'][1]['evidence_ids'],['e2'])
        self.assertEqual(result['adjustments'][0]['reference_maps'][1]['observation_ids'],{'o1':'o2'})
        self.assertTrue(result['outcomes'][0]['merged_fragments'])

    def test_unsafe_duplicate_excluded_without_retry_other_row_reported(self):
        identifier=self.start()
        def mutate(kind,value):
            if kind=='classify':
                bad=copy.deepcopy(value['reviews'][0]);bad['evidence'][0]['quote']='不存在于原文';value['reviews'].append(bad)
            return value
        client=self.run_mock(identifier,mutate);data=service.read(self.settings,identifier)
        self.assertEqual(client.complete.call_count,2);self.assertEqual(data['task']['status'],'succeeded')
        self.assertEqual(data['v4_content']['coverage']['state_counts']['excluded'],1)
        self.assertEqual(data['v4_content']['coverage']['unprocessed_count'],0)
        self.assertEqual(v5_runtime.continuation(data)['source_ids'],[])
        self.assertIn('排除1条',data['task']['message'])

    def test_blank_processed_without_report_or_fake_direction(self):
        with self.engine.begin() as conn:conn.execute(self.tables['review_observations'].update().values(content='',append_reviews=[]))
        identifier=self.start()
        def mutate(kind,value):
            for row in value['reviews']:row.update(evidence=[],observations=[])
            return value
        client=self.run_mock(identifier,mutate);data=service.read(self.settings,identifier)
        self.assertEqual(client.complete.call_count,1);self.assertEqual(data['task']['status'],'succeeded')
        self.assertEqual(data['v4_content']['coverage']['state_counts']['no_information'],2)
        self.assertFalse(data['v4_content']['review_items']);self.assertIsNone(data['v4_content']['narrative'])

    def test_truncation_not_exclusion_and_no_followup(self):
        identifier=self.start();client=self.run_mock(identifier,finish='length');data=service.read(self.settings,identifier)
        self.assertEqual(client.complete.call_count,1);self.assertEqual(data['task']['status'],'failed')
        self.assertEqual(data['v4_content']['coverage']['state_counts']['unfinished'],2)
        self.assertEqual(data['v4_content']['coverage']['state_counts']['excluded'],0)

    def test_bad_root_not_exclusion(self):
        identifier=self.start();client=self.run_mock(identifier,lambda kind,value:[]);data=service.read(self.settings,identifier)
        self.assertEqual(client.complete.call_count,1);self.assertEqual(data['v4_content']['coverage']['state_counts']['unfinished'],2)

    def test_actual_wire_roundtrip_preserves_meaning(self):
        identifier=self.start();self.run_mock(identifier);data=repository.read(self.settings,identifier);incoming=v5.context(data)
        restored=unpack(v5.wire(incoming))
        for expected,actual in zip(incoming['extraction']['reviews'],restored['extraction']['reviews']):
            self.assertEqual(expected['evidence'],actual['evidence'])
            for before,after in zip(expected['observations'],actual['observations']):
                for key in v5.contract.OBSERVATION['properties']:self.assertEqual(before[key],after[key])
        call=data['calls'][-1];self.assertIn('review-report-wire-v1',call['request']['prompt'])
        self.assertEqual(call['request']['report_input'],incoming)
        self.assertEqual(len(data['task']['parameters']['validation_history'][0]['outcomes']),2)

    def test_segment_scope_does_not_mark_other_processed_reviews_pending(self):
        identifier=self.start();self.run_mock(identifier);data=repository.read(self.settings,identifier)
        incoming=v5.context(data,v5.rows(data)[:1])
        self.assertEqual(incoming['scope']['pending_count'],0);self.assertEqual(incoming['segment_scope']['processed_elsewhere_count'],1)
        self.assertFalse(incoming['pending_issues'])

    def test_phase_fallback_readable_export_and_namespace_ack(self):
        identifier,limit=self.phase_task()
        with patch.object(v5,'MAX_INPUT',limit):client=self.run_mock(identifier)
        data=service.read(self.settings,identifier);content=data['v4_content']
        self.assertEqual(client.complete.call_count,4);self.assertEqual(data['task']['status'],'partial')
        self.assertIsNone(content['narrative']);self.assertEqual(len(content['phase_reports']),2)
        self.assertEqual(len({i['key'] for i in content['review_items']}),6)
        item=data['review_by_key']['segment:1:opportunity:p1']
        values=dict(report_id=data['report']['id'],item_key=item['key'],candidate_hash=item['candidate_hash'],source_hash=content['source_hash'],expected_revision=0,decision='adopted',reviewer='测试',reason='范围检查',review_context={'evidence_checked':True})
        with self.assertRaisesRegex(ValueError,'淘汰范围'):analysis_review_store.save(self.settings,identifier,**values)
        values['review_context'].update(conditions_checked=True,stop_scope='current_model')
        analysis_review_store.save(self.settings,identifier,**values)
        page=self.client.get('/analysis/reviews/tasks/'+identifier).get_data(as_text=True)
        self.assertIn('segment-1-finding-f1',page);self.assertIn('segment-2-finding-f1',page)
        exported=self.client.get('/analysis/reviews/tasks/'+identifier+'/export?format=html').get_data(as_text=True)
        self.assertIn('已保存的分段报告',exported);self.assertIn('本段发现与依据',exported);self.assertNotIn('<form',exported)

    def test_failed_start_requires_choice_and_explicit_restart_preserves_old(self):
        identifier=self.start();repository.update_task(self.settings,identifier,status='failed',active_key=None,owner=None,message='模拟失败')
        before=repository.read(self.settings,identifier);preview=service.preview(self.settings,'123',5)
        values=dict(batch_id=5,model=preview['model'],input_hash=preview['input_hash'],plan_hash=preview['plan_hash'],allow_paid=True)
        with patch.object(service,'launch') as launch:
            response=self.client.post('/api/analysis/reviews/products/123',json=values,headers=self.headers)
            self.assertEqual(response.status_code,200);choice=response.get_json()
            self.assertTrue(choice['choice_required']);self.assertTrue(choice['can_continue']);self.assertEqual(choice['id'],identifier)
            launch.assert_not_called()
            response=self.client.post('/api/analysis/reviews/products/123',json={**values,'force':True},headers=self.headers)
            self.assertEqual(response.status_code,202);new=response.get_json()
            self.assertNotEqual(new['id'],identifier);launch.assert_called_once()
        self.assertEqual(repository.read(self.settings,identifier),before)

    def test_active_task_reused_even_force_but_success_partial_need_choice(self):
        identifier=self.start();preview=service.preview(self.settings,'123',5)
        values=dict(batch_id=5,model=preview['model'],input_hash=preview['input_hash'],plan_hash=preview['plan_hash'],allow_paid=True)
        with patch.object(service,'launch') as launch:
            active=service.start(self.settings,'123',**values,force=True)
            self.assertFalse(active['choice_required']);self.assertEqual(active['id'],identifier)
            for status in ('succeeded','partial','interrupted'):
                repository.update_task(self.settings,identifier,status=status,active_key=None,owner=None)
                choice=service.start(self.settings,'123',**values)
                self.assertTrue(choice['choice_required']);self.assertEqual(choice['can_continue'],status!='succeeded')
            launch.assert_not_called()

    def test_upstream_safe_error_saved_and_displayed_without_retry(self):
        identifier=self.start();client=Mock();client.complete.side_effect=AIError('DeepSeek返回HTTP 402：余额不足，请检查账户额度。')
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,identifier,Mock())
        data=service.read(self.settings,identifier)
        self.assertEqual(client.complete.call_count,1);self.assertIn('HTTP 402',data['calls'][0]['error'])
        self.assertIn('余额不足',data['task']['message']);self.assertEqual(data['calls'][0]['status'],'unknown')
        self.assertEqual(data['task']['status'],'failed');self.assertEqual(data['v4_content']['coverage']['state_counts']['excluded'],0)
