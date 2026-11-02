"""来源报告返回的隔离回归；只用临时库和模拟AI，不接触正式数据。"""
import html
import re
from urllib.parse import urlencode, urlsplit, parse_qs
from unittest import TestCase
from unittest.mock import Mock
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.modules.review_analysis import v6, v6_runtime, v6_checks
from ecommerce.modules.question_analysis import runtime as qa_runtime
from ecommerce.modules.composite_analysis import runtime as ca_runtime, service as ca_service
from ecommerce.web.report_navigation import target
from tests.unit import test_composite_analysis as fixtures
from tests.unit import test_question_analysis as qa_fixtures


class NavigationTests(TestCase):
    put = fixtures.CompositeTests.put
    use_v4 = False

    def setUp(self):
        """建立可完整渲染的两份基础报告与一个综合报告。"""
        fixtures.CompositeTests.setUp(self)
        config = load_config(self.root)
        data = self.review_data; plan = v6.plan(self.root, config, config.model, data['items'], data['context'], v6.snapshot())
        with self.engine.begin() as conn:
            conn.execute(self.tables['analysis_tasks'].update().where(self.tables['analysis_tasks'].c.id == self.review_task).values(parameters={
                'plan':plan,'budget_version':3,'max_calls':plan['planned_calls'],'max_tokens':plan['recommended_tokens'],'max_output_tokens':plan['report_output_tokens']}))
        text = '\n\n'.join('## '+name+'\n\n测试资料。'+('\n\n【评价8｜初评】原文：“刚买很好用”' if i==0 else '') for i,name in enumerate(v6_checks.SECTIONS))
        client=Mock();client.complete_messages.return_value={'text':text,'finish_reason':'stop','usage':{'total_tokens':10}}
        v6_runtime.execute(self.settings,self.review_task,Mock(),Mock(return_value=client))
        qa_data=repository.read(self.settings,self.question_task)
        text=qa_fixtures.sample_report([s['snapshot'] for s in qa_data['sources']])
        client.complete_messages.return_value={'text':text,'finish_reason':'stop','usage':{'total_tokens':10}}
        qa_runtime.execute(self.settings,self.question_task,Mock(),Mock(return_value=client))
        self.preview=lambda **kwargs:ca_service.preview(self.settings,'123',**kwargs)
        self.values=lambda value:{k:value[k] for k in ('review_report','question_report','model','input_hash','plan_hash')}
        self.composite_task=fixtures.CompositeTests.start(self)['id']
        client.complete_messages.return_value={'text':fixtures.sample_combined(),'finish_reason':'stop','usage':{'total_tokens':10}}
        ca_runtime.execute(self.settings,self.composite_task,Mock(),Mock(return_value=client))

    def source(self, kind, origin=None, state='a'*32, version=2):
        """取得来源页面；参数：类型、返回地址、状态编号、报告版本；返回：响应。"""
        values={'version':version}
        if origin is not None:values.update(return_to=origin,return_state=state)
        identifier=self.review_task if kind=='reviews' else self.question_task
        return self.client.get('/analysis/'+kind+'/tasks/'+identifier+'?'+urlencode(values))

    def back(self, body):
        """提取主返回地址；参数：页面；返回：安全站内URL。"""
        return html.unescape(re.search(r'href="([^"]+)"[^>]*>← 返回综合',body)[1])

    def test_both_reports_return_to_panel_keep_original_entry(self):
        """综合入口显示来源与主返回，原评价/问大家入口仍保留；直接进入不加来源。"""
        destination='/collections/products/123?module=composite'
        for kind in ('reviews','questions'):
            page=self.source(kind,destination)
            self.assertEqual(page.status_code,200);self.assertIn('来自：综合分析',page.text)
            response=self.client.get(self.back(page.text));self.assertEqual(response.status_code,302)
            parsed=urlsplit(response.location);self.assertEqual(parsed.path,'/collections/products/123')
            self.assertEqual(parse_qs(parsed.query)['_report_back'],['a'*32])
            self.assertIn('返回评价明细' if kind=='reviews' else '返回问大家',page.text)
            direct=self.source(kind)
            self.assertEqual(direct.status_code,200);self.assertNotIn('来自：综合分析',direct.text)

    def test_return_to_exact_composite_version_and_revalidate_deleted(self):
        """综合报告来源返回固定版本，离开后删除的目标安全回商品综合页。"""
        destination='/analysis/composite/tasks/'+self.composite_task+'?version=1'
        page=self.source('reviews',destination)
        self.assertIn('来自：综合报告',page.text)
        back=self.back(page.text);response=self.client.get(back)
        self.assertIn('version=1',response.location)
        with self.engine.begin() as conn:
            conn.execute(self.tables['analysis_reports'].delete().where(self.tables['analysis_reports'].c.analysis_id==self.composite_task))
        response=self.client.get(back)
        self.assertIn('module=composite',response.location);self.assertIn('_report_return_notice=1',response.location)

    def test_strict_same_product_whitelist_and_no_redirect_loop(self):
        """拒绝外站、协议相对、重复参数、控制字符、跨商品与循环返回。"""
        invalid=['http://[','https://outside.example','//outside.example','/%2foutside.example','/collections/products/456?module=composite',
            '/collections/products/123?module=composite&observation=987654',
            '/collections/products/123?module=reviews','/collections/products/123?module=composite&module=composite',
            '/analysis/composite/tasks/'+'f'*32,'/analysis/composite/products/123/return?target=x',
            '/collections/products/123?module=composite%0d%0aLocation:evil',
            '/collections/products/123?module=composite%250d%250aLocation:evil']
        with self.app.test_request_context():
            for value in invalid:
                destination,_,valid=target(self.settings,'123',value)
                self.assertFalse(valid,value);self.assertEqual(destination,'/collections/products/123?module=composite')

    def test_settings_origin_updates_selected_fields_and_rejects_invalid_source(self):
        """独立设置页返回版本和模型，改设置后回链更新；过期报告回综合页。"""
        data=self.preview();values={'review_report':data['review_report'],'question_report':data['question_report'],'model':'deepseek-flash','output_tokens':8192}
        destination='/analysis/composite/products/123/new?'+urlencode(values)
        page=self.source('questions',destination);self.assertIn('来自：综合分析设置',page.text)
        returned=self.client.get(self.back(page.text));self.assertIn('/new?',returned.location)
        changed=self.client.get('/analysis/composite/products/123/new?'+urlencode(dict(values,model='deepseek-v4-pro',dialog='1',origin=destination)))
        self.assertEqual(changed.status_code,200)
        link=html.unescape(re.search(r'data-report-source href="([^"]+)"',changed.text)[1]);query=parse_qs(urlsplit(link).query)
        self.assertIn('model=deepseek-v4-pro',query['return_to'][0])
        with self.engine.begin() as conn:conn.execute(self.tables['analysis_reports'].delete().where(self.tables['analysis_reports'].c.id==data['review_report']))
        returned=self.client.get(self.back(page.text));self.assertIn('_report_return_notice=1',returned.location)

    def test_question_versions_and_local_check_preserve_source(self):
        """版本链接及本地检查均保留来源，免费检查不触发AI。"""
        destination='/collections/products/123?module=composite'
        page=self.source('questions',destination)
        version_link=html.unescape(next(value for value,label in re.findall(r'href="([^"]+)"[^>]*>(版本 2)</a>',page.text)))
        self.assertIn('return_to=',version_link);self.assertIn('return_state=',version_link)
        before=repository.read(self.settings,self.question_task)
        result=self.client.post('/api/analysis/questions/tasks/'+self.question_task+'/check',json={'report_id':before['report']['id']},headers=self.headers)
        self.assertEqual(result.status_code,200);self.assertEqual(result.json['ai_calls'],0)
        page=self.source('questions',destination,version=result.json['version'])
        self.assertIn('返回综合分析',page.text)
        self.assertEqual(len(repository.read(self.settings,self.question_task)['calls']),len(before['calls']))

    def test_downloads_do_not_embed_return_navigation(self):
        """下载保持附件，不把当前返回来源加入离线报告或触发页面跳转。"""
        destination='/collections/products/123?module=composite'
        response=self.client.get('/analysis/questions/tasks/'+self.question_task+'?'+urlencode({'version':2,'download':'html','return_to':destination}))
        self.assertEqual(response.status_code,200);self.assertIn('attachment',response.headers['Content-Disposition'])
        self.assertNotIn('返回综合分析',response.text)
