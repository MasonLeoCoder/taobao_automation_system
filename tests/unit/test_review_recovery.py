"""逐条校验、本地恢复和补处理回归；脱敏样例及模拟AI，不发送真实请求。"""
import copy
import json
import unittest
from unittest.mock import Mock, patch
from ecommerce.modules.review_analysis import rules, service, validation
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.unit import test_review_analysis as fixtures


def source(uid, text):
    """构造脱敏来源；参数：编号、初评；返回：来源字典。"""
    return {'id':uid,'text':text,'sku':'测试规格','appends':[]}


def review(uid, text, topic='品质笼统认可', kind='specific', sentiment='positive'):
    """构造模拟返回；参数：编号、引文、主题、类型及倾向；返回：未校验分类。"""
    return {'id':uid,'kind':kind,'points':[{'topic':topic,'sentiment':sentiment,'segment':'initial','quote':text}]}


class IndividualValidationTests(unittest.TestCase):
    def assess(self, values, sources):
        """调用本地评估；参数：模拟分类与来源；返回：结构化结果。"""
        return validation.assess(json.dumps({'reviews':values},ensure_ascii=False),sources)

    def test_safe_normalization_keeps_raw_input_and_records_changes(self):
        """明确类型和唯一空白差异可规范化，原始返回对象保持不变。"""
        values = [review(1,'一直在回购','回购经历','general'),review(2,'质量很好',kind='general'),
                  review(3,'还会回购','回购意愿','general'),review(4,'小巧，手柄好握','使用便利与顺手')]
        raw = copy.deepcopy(values)
        result = self.assess(values,[source(1,'一直在回购'),source(2,'质量很好'),source(3,'还会回购'),source(4,'小巧， 手柄好握')])
        self.assertEqual(values,raw)
        self.assertEqual(len(result['results']),4)
        self.assertEqual(len(result['adjustments']),4)
        self.assertEqual(result['results'][3]['points'][0]['quote'],'小巧， 手柄好握')
        self.assertEqual(result['issues'],[])

    def test_punctuation_splicing_and_ambiguous_whitespace_are_not_rewritten(self):
        """拼句、标点变化和多处空白匹配不自动修复，也不吞掉同组有效结果。"""
        values=[review(1,'结构合理，质感不错'),review(2,'好用，方便','使用便利与顺手'),
                review(3,'小巧，好握','使用便利与顺手'),{'id':4,'kind':'general','points':[]}]
        result=self.assess(values,[source(1,'结构合理。清洁省力。质感不错'),source(2,'好用。方便'),
            source(3,'小巧， 好握。小巧，  好握'),source(4,'满意')])
        self.assertEqual([item['id'] for item in result['results']],[4])
        self.assertEqual({issue['review_id'] for issue in result['issues']},{1,2,3})
        self.assertEqual(result['adjustments'],[])

    def test_missing_duplicate_and_extra_ids_do_not_discard_other_valid_reviews(self):
        """重复编号隔离、缺失和额外编号全列出，唯一且合法的结果仍保存。"""
        values=[review(1,'质量好'),review(1,'质量好'),review(3,'质量好'),review(99,'质量好')]
        result=self.assess(values,[source(1,'质量好'),source(2,'满意'),source(3,'质量好')])
        self.assertEqual([item['id'] for item in result['results']],[3])
        self.assertEqual({issue['code'] for issue in result['issues']},{'duplicate_review','missing_review','unexpected_id'})

    def test_semantic_checks_flag_size_inference_and_omitted_negation(self):
        """原文存在仍可能不支持主题或倾向，明确尺寸推断及遗漏否定需复核。"""
        values=[review(1,'长度合适','安装、更换与适配'),review(2,'很好用','使用便利与顺手'),
                review(3,'接口兼容，长度合适','安装、更换与适配')]
        result=self.assess(values,[source(1,'长度合适'),source(2,'不是很好用'),source(3,'接口兼容，长度合适')])
        self.assertEqual([item['id'] for item in result['results']],[3])
        self.assertEqual({issue['review_id'] for issue in result['issues']},{1,2})

    def test_empty_and_arbitrary_general_types_are_not_promoted(self):
        """不把非空白empty或任意带主题general强行放宽通过。"""
        result=self.assess([review(1,'满意',kind='empty'),review(2,'发货很快','物流与发货','general')],
                           [source(1,'满意'),source(2,'发货很快')])
        self.assertEqual(result['results'],[])
        self.assertEqual(len(result['issues']),2)

    def test_truncated_or_unparseable_response_is_explicit(self):
        """截断和损坏JSON留作整次问题，不伪造已处理条数。"""
        for text,finish in [('{}','length'),('not-json','stop')]:
            result=validation.assess(text,[source(1,'满意')],finish_reason=finish)
            self.assertEqual(result['results'],[])
            self.assertIsNone(result['issues'][0]['review_id'])
        parameters={'validation_history':[{'call_id':'test','source_ids':[1],
            'issues':[{'review_id':None,'message':'原始返回损坏'}]}]}
        self.assertTrue(validation.pending_issues(parameters,set()))
        self.assertEqual(validation.pending_issues(parameters,{1}),[])


class SavedRecoveryTests(unittest.TestCase):
    def setUp(self):
        """建立隔离数据库；参数：无；返回：无。"""
        self.fixture=fixtures.ReviewAnalysisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.settings=self.fixture.settings

    def failed(self):
        """保存旧版整组失败原响应；参数：无；返回：任务、调用、原始响应。"""
        task=self.fixture.start()
        call=repository.begin_call(self.settings,task,'classify',1,'原始请求',[8,9],1000)
        bad=self.fixture.classified()
        bad['reviews'][0]['points'][0]['quote']='拼接的不存在引文'
        response=self.fixture.response(bad)
        repository.finish_call(self.settings,task,call,'invalid',response,'旧版整组拒收')
        repository.update_task(self.settings,task,status='failed',active_key=None,owner=None)
        service.build_report(self.settings,task,issues=['旧版失败'])
        return task,call,response

    def test_local_recovery_is_idempotent_and_preserves_original_call(self):
        """已付费返回本地恢复有效条目且可重复点击，不建调用、不改原响应和错误。"""
        task,call,response=self.failed()
        before=repository.read(self.settings,task)
        with patch('ecommerce.modules.review_analysis.service.create_client') as client, patch('ecommerce.modules.review_analysis.service.launch') as launch:
            first=service.recover_saved(self.settings,task)
            second=service.recover_saved(self.settings,task)
            client.assert_not_called(); launch.assert_not_called()
        after=repository.read(self.settings,task)
        self.assertEqual(first['processed_count'],1)
        self.assertEqual(second['added_count'],0)
        self.assertEqual(after['calls'],before['calls'])
        self.assertEqual(after['calls'][0]['response'],response)
        self.assertEqual(len(after['task']['parameters']['validation_history']),1)
        self.assertEqual(len(after['reports']),2)
        self.assertEqual(after['task']['status'],'partial')
        self.assertEqual(service.read(self.settings,task)['validation_issues'][0]['review_id'],8)

    def test_paid_resume_requires_local_recovery_and_only_sends_remaining_ids(self):
        """先利用原返回，补处理只含问题评价，原计划与已保存明细不变。"""
        task,_,_=self.failed()
        with patch('ecommerce.modules.review_analysis.service.launch') as launch:
            with self.assertRaisesRegex(ValueError,'先执行'):
                service.resume(self.settings,task,allow_paid=True)
            launch.assert_not_called()
        service.recover_saved(self.settings,task)
        data=service.read(self.settings,task)
        plan=copy.deepcopy(data['task']['parameters']['plan'])
        self.assertEqual(data['resume_budget']['supplement']['source_ids'],[8])
        self.assertEqual(data['resume_budget']['required_calls'],2)
        with patch('ecommerce.modules.review_analysis.service.launch'):
            service.resume(self.settings,task,allow_paid=True,plan_hash=data['resume_budget']['plan_hash'])
        value=self.fixture.classified();value['reviews']=value['reviews'][:1]
        client=self.fixture.run_mock(task,[self.fixture.response(value),self.fixture.response({'findings':[],'suggestions':[]})])
        after=repository.read(self.settings,task)
        self.assertEqual(client.complete.call_count,2)
        self.assertEqual(after['calls'][1]['request']['source_ids'],[8])
        self.assertEqual(after['task']['parameters']['plan'],plan)
        self.assertEqual(after['task']['status'],'succeeded')
        self.assertEqual(len(after['details']),2)
        self.assertEqual(after['calls'][0]['status'],'invalid')
        self.assertEqual(len(after['task']['parameters']['supplement_history']),1)

    def test_stale_plan_and_running_owner_are_blocked(self):
        """不能用过期补处理预览或与活进程同时恢复。"""
        task,_,_=self.failed()
        with patch('ecommerce.modules.review_analysis.service.owner_alive',return_value=True):
            repository.update_task(self.settings,task,owner={'pid':1,'started_at':'2026-10-30T00:00:00+08:00'})
            with self.assertRaisesRegex(ValueError,'仍存活'):
                service.recover_saved(self.settings,task)
        repository.update_task(self.settings,task,owner=None)
        service.recover_saved(self.settings,task)
        with self.assertRaisesRegex(ValueError,'计划已变化'):
            service.resume(self.settings,task,allow_paid=True,plan_hash='stale')
        with patch('ecommerce.modules.review_analysis.service.launch') as launch:
            result=self.fixture.client.post('/api/analysis/reviews/tasks/'+task+'/resume',
                json={'allow_paid':True,'plan_hash':None},headers=self.fixture.headers)
            self.assertEqual(result.status_code,400)
            launch.assert_not_called()

    def test_web_recovery_needs_token_but_not_fee_confirmation(self):
        """本地恢复独立入口需同源令牌，不接受费用或未知参数，不调用AI。"""
        task,_,_=self.failed()
        url='/api/analysis/reviews/tasks/'+task+'/recover'
        self.assertEqual(self.fixture.client.post(url,json={}).status_code,403)
        self.assertEqual(self.fixture.client.post(url,json={'allow_paid':True},headers=self.fixture.headers).status_code,400)
        with patch('ecommerce.modules.review_analysis.service.create_client') as client:
            response=self.fixture.client.post(url,json={},headers=self.fixture.headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['ai_calls'],0)
            client.assert_not_called()

    def test_recovery_after_statistics_failure_does_not_duplicate_details(self):
        """明细已提交但统计失败时可再次本地恢复，不重复插入或调用AI。"""
        task,_,_=self.failed()
        with patch('ecommerce.modules.review_analysis.service.build_report',side_effect=ValueError('模拟统计失败')):
            with self.assertRaisesRegex(ValueError,'模拟统计失败'):
                service.recover_saved(self.settings,task)
        self.assertEqual(len(repository.read(self.settings,task)['details']),1)
        with patch('ecommerce.modules.review_analysis.service.create_client') as client:
            result=service.recover_saved(self.settings,task)
            self.assertEqual(result['added_count'],0)
            client.assert_not_called()
        data=repository.read(self.settings,task)
        self.assertEqual(data['task']['status'],'partial')
        self.assertEqual(len(data['task']['parameters']['validation_history']),1)
        self.assertEqual(len(data['reports']),2)
