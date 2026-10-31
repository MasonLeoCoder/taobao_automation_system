"""本地检查容错、状态隔离、审计及原子更新的脱敏回归；真实AI禁止调用。"""
import copy
from unittest import TestCase
from unittest.mock import Mock,patch
import sqlalchemy as sa
from ecommerce.modules.review_analysis import service,v6_checks,v6_report
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.unit import test_review_analysis_v6 as v6_tests
from tests.fixtures.review_citations_v6 import report_sample


def complete_text(citation):
    """填入七章节的脱敏测试稿；参数：引用文字；返回：正文。"""
    return '\n\n'.join('## '+s+'\n'+(citation if i==0 else '资料不足，需实物验证。') for i,s in enumerate(v6_checks.SECTIONS))


class CitationParsingTests(TestCase):
    def test_common_variants_keep_identity_quote_and_source_unchanged(self):
        """包裹排版/空格/分隔符/换行可兼容，原始文字保持；参数：无；返回：断言。"""
        source={'id':601,'text':'粘性适中，不留胶。','appends':[]};before=copy.deepcopy(source)
        for marker in ('【评价601｜初评】','【601｜初评】','【 评价 601 | 初评 】','**【601｜初评】**','【**601**｜**初评**】','【**601｜初评**】','【`601` | `初评`】'):
            for separator in ('原文：“粘性适中，不留胶。”','\n原文：\n“粘性适中，不留胶。”','**原文：**“粘性适中，不留胶。”'):
                with self.subTest(marker=marker,separator=separator):
                    result=v6_checks.check(complete_text(marker+separator),'stop',[source])
                    self.assertTrue(result['generation']['complete']);self.assertEqual(result['counts']['valid_count'],1)
                    self.assertEqual(result['citations'][0]['quote'],source['text']);self.assertEqual(source,before)

    def test_append_identity_whitespace_and_literal_nested_quotes(self):
        source={'id':601,'text':'前句。\n后句。','appends':[{'segment':'append:2','text':'“不留胶”仍需实测。'}]}
        text=complete_text('【601|追评 2】原文：““不留胶”仍需实测。”\n【601|初评】原文：“前句。\n后句。”')
        result=v6_checks.check(text,'stop',[source]);self.assertEqual(result['counts']['valid_count'],2)
        self.assertEqual(result['citations'][0]['segment'],'append:2')

    def test_quote_changes_are_not_normalized_or_guessed(self):
        source={'id':601,'text':'粘性适中， 不留胶。','appends':[]}
        for marker,quote in (('【601|初评】','粘性适中，不留胶。'),('【601|初评】','粘性适中……不留胶。'),('【999|初评】','粘性适中， 不留胶。'),('【601|追评1】','粘性适中， 不留胶。'),('【评价|初评】','粘性适中， 不留胶。'),('【60**1**|初评】','粘性适中， 不留胶。')):
            with self.subTest(marker=marker,quote=quote):
                result=v6_checks.check(complete_text(marker+'原文：“'+quote+'”'),'stop',[source])
                self.assertTrue(result['generation']['complete']);self.assertEqual(result['counts']['pending_count'],1);self.assertEqual(result['counts']['valid_count'],0)

    def test_literal_ellipsis_in_source_is_not_assumed_to_be_splicing(self):
        source={'id':601,'text':'还行……需要实测。','appends':[]}
        result=v6_checks.check(complete_text('【601|初评】原文：“还行……需要实测。”'),'stop',[source])
        self.assertEqual(result['counts']['valid_count'],1)

    def test_unrecognized_markers_and_missing_quotes_are_visible(self):
        source={'id':601,'text':'好用。','appends':[]}
        text=complete_text('【601】原文：“好用。”\n【601|初评】无引号原文\n【资料不足】')
        result=v6_checks.check(text,'stop',[source]);self.assertEqual(result['counts']['citation_count'],2)
        self.assertEqual(result['counts']['unrecognized_count'],2);self.assertEqual(result['counts']['pending_count'],2)

    def test_generation_failures_and_citation_quality_are_independent(self):
        source={'id':601,'text':'好用。','appends':[]};text=complete_text('【601|初评】原文：“好用。”')
        for value,finish,state in ((text,'length','interrupted'),('## 总体判断\n【601|初评】原文：“好用。”','stop','incomplete_sections'),('','stop','empty'),(None,'stop','empty'),({'unexpected':'object'},'stop','empty')):
            result=v6_checks.check(value,finish,[source]);self.assertFalse(result['generation']['complete']);self.assertEqual(result['generation']['state'],state)
        result=v6_checks.check(complete_text('无引用正文。'),'stop',[source]);self.assertTrue(result['generation']['complete']);self.assertFalse(result['complete'])

    def test_deidentified_real_shape_has_66_65_1_33(self):
        sources,text=report_sample();result=v6_checks.check(text,'stop',sources)
        self.assertTrue(result['generation']['complete']);self.assertEqual(result['counts'],{'citation_count':66,'valid_count':65,'pending_count':1,'unrecognized_count':0,'invalid_count':1,'valid_source_count':33})
        self.assertEqual(result['citations'][0]['original_text'],sources[0]['text']);self.assertIn('省略号',result['citations'][0]['reason'])

    def test_html_variants_link_without_active_external_markup(self):
        html=v6_report.render('**【601 | 初评】** ![x](https://bad.invalid/a) <script>x</script>')
        self.assertIn('href="#review-601"',html);self.assertNotIn('<script>',html);self.assertNotIn('<img',html)


class CitationWorkflowTests(TestCase):
    use_v4=True
    setUp=v6_tests.ReviewV6Tests.setUp
    put=v6_tests.ReviewV6Tests.put
    start=v6_tests.ReviewV6Tests.start
    run_mock=v6_tests.ReviewV6Tests.run_mock
    response=v6_tests.ReviewV6Tests.response

    def seed(self,legacy=False):
        """建立脱敏66处返回，旧版场景仅用于临时库；参数：是否模拟旧检查；返回：分析编号。"""
        sources,text=report_sample()
        with self.engine.begin() as conn:
            r=self.tables['review_observations'];conn.execute(r.delete().where(r.c.batch_id==5))
            conn.execute(r.insert(),[{'id':s['id'],'batch_id':5,'position':i+1,'content':s['text'],'sku_text':s['sku'],'append_reviews':[]} for i,s in enumerate(sources)])
        identifier=self.start();client=Mock();client.complete_messages.return_value={'text':text,'finish_reason':'stop','usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
        with patch.object(service,'create_client',return_value=client):service.execute(self.settings,identifier,Mock())
        self.assertEqual(client.complete_messages.call_count,1)
        if legacy:
            data=repository.read(self.settings,identifier);old=copy.deepcopy(data['report']['content'])
            for key in ('generation','inspection'):old.pop(key)
            old['validation']={'state':'needs_review','complete':False,'citations':[],'issues':['旧格式未识别。']};old['coverage']['referenced_count']=0
            with self.engine.begin() as conn:
                conn.execute(self.tables['analysis_reports'].update().where(self.tables['analysis_reports'].c.id==data['report']['id']).values(content=old))
                conn.execute(self.tables['analysis_calls'].update().where(self.tables['analysis_calls'].c.analysis_id==identifier).values(status='invalid',error='旧规则未识别。'))
            repository.update_task(self.settings,identifier,status='partial',message='旧规则报告未完成。')
        return identifier

    def test_citation_warning_does_not_trigger_paid_continuation_or_failed_cache(self):
        identifier=self.seed();data=service.read(self.settings,identifier)
        self.assertEqual(data['task']['status'],'succeeded');self.assertEqual(data['resume_budget']['required_calls'],0)
        self.assertEqual(data['v6_content']['validation']['counts']['pending_count'],1)
        client=Mock();client.complete_messages.side_effect=AssertionError('不得额外调用AI')
        with patch.object(service,'create_client',return_value=client),patch.object(service,'launch') as launch:
            with self.assertRaisesRegex(ValueError,'已完成'):service.resume(self.settings,identifier,allow_paid=True)
            repository.update_task(self.settings,identifier,status='partial')
            preview=service.preview(self.settings,'123',5)
            reused=service.start(self.settings,'123',batch_id=5,model=preview['model'],input_hash=preview['input_hash'],allow_paid=True)
            self.assertEqual(reused['id'],identifier);self.assertFalse(reused['can_continue']);launch.assert_not_called();client.complete_messages.assert_not_called()

    def test_legacy_get_preserves_history_and_local_recheck_updates_only_selected_task(self):
        identifier=self.seed(legacy=True);before=repository.read(self.settings,identifier)
        with patch.object(service,'create_client',side_effect=AssertionError('本地检查不得联网')):
            page=self.client.get('/analysis/reviews/tasks/'+identifier);self.assertEqual(page.status_code,200)
            self.assertIn('旧规则检查结果',page.get_data(as_text=True));self.assertNotIn('查看剩余请求与费用',page.get_data(as_text=True))
            self.assertEqual(repository.read(self.settings,identifier),before)
            result=self.client.post('/api/analysis/reviews/tasks/'+identifier+'/recount',json={'report_id':before['report']['id']},headers=self.headers)
            self.assertEqual(result.status_code,200);self.assertEqual(result.json['ai_calls'],0)
        after=repository.read(self.settings,identifier);content=after['report']['content']
        self.assertEqual(after['task']['status'],'succeeded');self.assertEqual(content['validation']['counts']['valid_count'],65)
        self.assertEqual(after['reports'][0],before['reports'][0]);self.assertEqual(after['calls'],before['calls']);self.assertEqual(after['sources'],before['sources'])
        self.assertEqual(content['markdown'],before['report']['content']['markdown']);self.assertEqual(content['inspection']['based_on_report_id'],before['report']['id'])
        self.assertEqual(content['inspection']['origin'],'local_recheck');self.assertTrue(content['inspection']['checked_at'].endswith('+08:00'))
        self.assertEqual(after['task']['usage'],before['task']['usage'])

    def test_history_export_and_stale_page_do_not_replace_new_checks(self):
        identifier=self.seed();old=repository.read(self.settings,identifier)['report'];service.recount(self.settings,identifier,report_id=old['id'])
        before=repository.read(self.settings,identifier)
        with patch.object(service,'create_client',side_effect=AssertionError('历史和导出不得联网')):
            page=self.client.get('/analysis/reviews/tasks/'+identifier+'?version=1');self.assertEqual(page.status_code,200)
            self.assertNotIn('data-ra-recount=',page.get_data(as_text=True))
            self.assertEqual(self.client.get('/analysis/reviews/tasks/'+identifier+'/export?version=1&format=html').status_code,200)
            stale=self.client.post('/api/analysis/reviews/tasks/'+identifier+'/recount',json={'report_id':old['id']},headers=self.headers)
            self.assertEqual(stale.status_code,400)
        self.assertEqual(repository.read(self.settings,identifier),before)

    def test_inputs_changed_during_inspection_rejects_stale_write(self):
        identifier=self.seed();data=repository.read(self.settings,identifier);signature=repository.check_inputs(data)
        with self.engine.begin() as conn:
            c=self.tables['analysis_calls'];conn.execute(c.update().where(c.c.analysis_id==identifier).values(status='invalid'))
        before=repository.read(self.settings,identifier)
        with self.assertRaisesRegex(ValueError,'已变化'):
            repository.save_local_report(self.settings,identifier,data['report']['content'],data['report']['id'],signature,{'status':'succeeded','message':'测试','owner':None,'active_key':None})
        self.assertEqual(repository.read(self.settings,identifier),before)

    def test_local_report_and_task_state_rollback_together(self):
        identifier=self.seed(legacy=True);before=repository.read(self.settings,identifier)
        def fail_update(conn,cursor,statement,parameters,context,executemany):
            """模拟报告已插入后的状态更新故障；参数：SQL事件；返回：抛错。"""
            if statement.startswith('UPDATE analysis_tasks'):raise RuntimeError('模拟状态写入失败')
        sa.event.listen(self.engine,'before_cursor_execute',fail_update)
        try:
            with self.assertRaisesRegex(RuntimeError,'模拟'):service.recount(self.settings,identifier)
        finally:sa.event.remove(self.engine,'before_cursor_execute',fail_update)
        self.assertEqual(repository.read(self.settings,identifier),before)

    def test_running_or_live_owner_cannot_recheck(self):
        identifier=self.seed();repository.update_task(self.settings,identifier,owner={'pid':123,'started_at':'2026-10-31T00:00:00+00:00'})
        with patch.object(service,'owner_alive',return_value=True),self.assertRaisesRegex(ValueError,'进程'):service.recount(self.settings,identifier)
        repository.update_task(self.settings,identifier,status='running',owner=None)
        with self.assertRaisesRegex(ValueError,'运行期间'):service.recount(self.settings,identifier)
