"""四类交叉分析的隔离业务及模拟接口验收，真实AI与采集为零。"""
import copy
import json
from datetime import datetime
from dataclasses import replace
from itertools import combinations
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import sqlalchemy as sa
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.mysql_migrations.schema_v24 import build_metadata
from ecommerce.infrastructure.ai.config import AISettings
from ecommerce.infrastructure.ai.client import AIError
from ecommerce.modules.cross_analysis import service,store,planner,report,runtime
from ecommerce.modules.cross_analysis.kinds import KINDS,REQUIRED
from ecommerce.modules.image_analysis import independent
from ecommerce.modules.review_analysis import service as reviews
from ecommerce.modules.composite_analysis import planner as shared_planner
from tests.unit import test_composite_analysis as fixtures
from tests.unit import test_image_analysis as image_fixtures
from tests.unit.test_review_analysis_v6 import FakeCounter


def sample(categories):
    """可选章不凑内容，模拟三项核心章节和实际来源标记。"""
    markers = ''.join('【'+KINDS[k]+'报告】' for k in categories)
    return '\n\n'.join('## '+name+'\n\n需拿样验证。'+(markers if i == 0 else '') for i,name in enumerate(REQUIRED))


class CrossTests(TestCase):
    put = fixtures.CompositeTests.put
    add_image = image_fixtures.ImageAnalysisTests.add_image
    use_v4 = False

    def setUp(self):
        """构造四份固定报告，独立SQLite和临时文件，不启动后台。"""
        with patch.object(fixtures,'build_metadata',build_metadata): fixtures.CompositeTests.setUp(self)
        # 基础来源也走模拟执行，保证返回页面具有完整检查及统计结构。
        from ecommerce.modules.review_analysis import v6, v6_runtime, v6_checks
        from ecommerce.modules.question_analysis import runtime as qa_runtime
        from ecommerce.infrastructure.ai.config import load_config
        from tests.unit import test_question_analysis as qa_fixtures
        config=load_config(self.root)
        plan=v6.plan(self.root,config,config.model,self.review_data['items'],self.review_data['context'],v6.snapshot())
        with self.engine.begin() as conn:
            t=self.tables['analysis_tasks'];conn.execute(t.update().where(t.c.id==self.review_task).values(parameters=dict(
                plan=plan,budget_version=3,max_calls=plan['planned_calls'],max_tokens=plan['recommended_tokens'],max_output_tokens=plan['report_output_tokens'])))
        client=Mock();client.complete_messages.return_value=dict(text='\n\n'.join('## '+name+'\n模拟分析。'+('\n【评价8｜初评】原文：“刚买很好用”' if i==0 else '') for i,name in enumerate(v6_checks.SECTIONS)),finish_reason='stop',usage={'total_tokens':10})
        v6_runtime.execute(self.settings,self.review_task,Mock(),Mock(return_value=client))
        data=repository.read(self.settings,self.question_task)
        client.complete_messages.return_value=dict(text=qa_fixtures.sample_report([s['snapshot'] for s in data['sources']]),finish_reason='stop',usage={'total_tokens':10})
        qa_runtime.execute(self.settings,self.question_task,Mock(),Mock(return_value=client))
        for batch,kind,role in ((7,'main_images','main_image'),(8,'detail_images','detail_image')):
            self.put('collection_batches',id=batch,product_id=1,module=kind,visibility='published',status='complete',captured_at=datetime(2026,11,2,10))
            self.add_image(batch+23,batch,role,'blue')
            data = independent.preview(self.settings,'123',kind)
            with patch.object(independent,'launch'):
                task = independent.start(self.settings,'123',kind,**{k:data[k] for k in ('batch_id','model','input_hash','plan_hash','allow_partial')},allow_paid=True)['id']
            from ecommerce.modules.image_analysis import runtime as image_runtime
            from tests.unit.test_independent_image_analysis import sample as image_sample
            client=Mock();client.complete_images.return_value=dict(text=image_sample(kind),finish_reason='stop',usage={'total_tokens':10})
            image_runtime.execute(self.settings,task,Mock(),Mock(return_value=client),module=kind)
        self.data = service.preview(self.settings,'123')

    def values(self,data=None):
        """提取需用户确认的固定参数。"""
        data = data or self.data
        return {k:data[k] for k in ('selection','model','input_hash','plan_hash')}

    def start(self,data=None,**extra):
        """明确费用，后台派发模拟。"""
        with patch.object(service,'launch'): return service.start(self.settings,'123',**self.values(data),allow_paid=True,**extra)

    def execute(self,task,text=None,error=None):
        """仅替换客户端，其他执行及持久化真实走通。"""
        incoming = service.read(self.settings,task)['task']['parameters']['plan']['input']; client = Mock()
        if error: client.complete_messages.side_effect = error
        else: client.complete_messages.return_value = dict(text=text or sample(incoming['categories']),finish_reason='stop',usage={'total_tokens':50})
        with patch.object(reviews,'create_client',return_value=client): state = reviews.execute(self.settings,task,Mock())
        return state,client

    def test_all_eleven_combinations_fixed_minimal_scope(self):
        """全部11种组合只发送所选报告，模型与图片无关，不带原图或路径。"""
        count = 0
        for n in (2,3,4):
            for keys in combinations(KINDS,n):
                selected = {k:self.data['selection'][k] for k in keys}; data = service.preview(self.settings,'123',selected)
                self.assertEqual(data['input']['categories'],list(keys)); self.assertEqual(data['plan']['planned_calls'],1)
                outgoing = json.dumps(data['plan']['groups'][0]['messages'],ensure_ascii=False)
                for forbidden in ('relative_path','base64','私密昵称','不可发送的昵称'): self.assertNotIn(forbidden,outgoing)
                self.assertEqual(set(r['kind'] for r in data['input']['reports']),set(keys))
                task = self.start(data)['id']; state,client = self.execute(task)
                self.assertEqual(state,'succeeded'); self.assertEqual(client.complete_messages.call_count,1); client.complete_images.assert_not_called()
                self.assertEqual(service.read(self.settings,task)['task']['processed_count'],0); count += 1
        self.assertEqual(count,11)

    def test_source_type_consent_old_report_and_stale_plan_rejected(self):
        """单类/跨类型/旧联合/无授权/过期计划均零派发。"""
        with self.assertRaises(ValueError): service.start(self.settings,'123',**self.values())
        with self.assertRaises(ValueError): service.start(self.settings,'123',**dict(self.values(),selection={'reviews':self.data['selection']['reviews']}),allow_paid=True)
        wrong = dict(self.data['selection'],main_images=self.data['selection']['reviews'])
        with self.assertRaises(LookupError): service.preview(self.settings,'123',wrong)
        with self.assertRaises(ValueError): service.start(self.settings,'123',**dict(self.values(),plan_hash='old'),allow_paid=True)
        self.put('products',id=2,platform='taobao',platform_product_id='456')
        with self.assertRaises(LookupError): service.preview(self.settings,'456',self.data['selection'])

    def test_latest_collected_batch_wins_over_old_report_recheck(self):
        """旧批新生成不覆盖新采集默认，最新批无报告仍明确提示。"""
        self.put('collection_batches',id=19,product_id=1,module='main_images',visibility='published',status='complete',captured_at=datetime(2026,11,3,12))
        self.add_image(42,19,'main_image','green')
        dashboard=service.dashboard(self.settings,'123')
        self.assertIn('此前报告',dashboard['availability']['main_images']['note'])
        data=independent.preview(self.settings,'123','main_images',batch_id=19)
        with patch.object(independent,'launch'):
            task=independent.start(self.settings,'123','main_images',**{k:data[k] for k in ('batch_id','model','input_hash','plan_hash','allow_partial')},allow_paid=True)['id']
        repository.save_report(self.settings,task,dict(markdown='## 总体判断\n新批报告【主图1】',generation={'complete':True},validation={'counts':{}},detail_revisions={}))
        repository.update_task(self.settings,task,status='succeeded',active_key=None)
        old=next(r for r in self.data['input']['reports'] if r['kind']=='main_images')
        repository.save_report(self.settings,old['analysis_id'],dict(markdown='## 总体判断\n旧批后来重查',generation={'complete':True},validation={'counts':{}},detail_revisions={}))
        current=service.preview(self.settings,'123')
        selected=next(r for r in current['input']['reports'] if r['kind']=='main_images')
        self.assertEqual(selected['batch_ids'],[19])

    def test_partial_readable_limits_and_media_role_guard(self):
        """可读缺章保留限制，媒体角色被改则来源校验拒绝。"""
        saved=repository.read(self.settings,self.question_task)
        value=copy.deepcopy(saved['report']['content']);value['generation']['complete']=False
        repository.save_report(self.settings,self.question_task,value)
        repository.update_task(self.settings,self.question_task,status='partial',active_key=None)
        data=service.preview(self.settings,'123');self.assertTrue(any('问大家' in s and '完整性' in s for s in data['input']['warnings']))
        with self.engine.begin() as conn:
            media=self.tables['media_references'];conn.execute(media.update().where(media.c.id==30).values(role='detail_image'))
        with self.assertRaises(ValueError): service.preview(self.settings,'123')

    def test_freeze_sources_then_new_versions_and_batch_order(self):
        """创建后快照不随来源版本变化；旧批后重查不抢最近采集默认。"""
        task = self.start()['id']; before = copy.deepcopy(service.read(self.settings,task)['task']['parameters']['plan']['input'])
        old_id = self.data['selection']['reviews']; new_content = dict(self.review_content,markdown='## 总体判断\n改动')
        repository.save_report(self.settings,self.review_task,new_content)
        self.assertEqual(service.read(self.settings,task)['task']['parameters']['plan']['input'],before)
        self.assertEqual(service.preview(self.settings,'123',self.data['selection'])['input_hash'],self.data['input_hash'])
        with repository.connection(self.settings) as (conn,tables):
            options = store.choices(conn,tables,1,'reviews'); self.assertEqual(options[0]['version'],3); self.assertTrue(any(v['id']==old_id for v in options))
        # 更新候选优先级只依赖原采集时间，而不是报告后来生成时间。
        with self.engine.begin() as conn:
            t=self.tables['collection_batches'];conn.execute(t.update().where(t.c.id==5).values(captured_at=datetime(2026,10,1)))
        self.assertTrue(service.dashboard(self.settings,'123')['ready'])

    def test_missing_reports_do_not_autorun_or_block_other_pairs(self):
        """无图片报告仍能评价问答，少于两类可选界面保留，禁止创建。"""
        with self.engine.begin() as conn:
            r,t = self.tables['analysis_reports'],self.tables['analysis_tasks']
            conn.execute(r.delete().where(r.c.analysis_id.in_(sa.select(t.c.id).where(t.c.analysis_type.in_(['main_images','detail_images'])))))
        data = service.preview(self.settings,'123');self.assertEqual(data['input']['categories'],['reviews','questions'])
        data = service.preview(self.settings,'123',{}); self.assertIsNone(data['plan'])
        url='/analysis/composite/products/123/new?dialog=1&sources=1'
        response=self.client.get(url);self.assertEqual(response.status_code,200);self.assertIn('请选择至少两类',response.text)

    def test_active_success_failure_choices(self):
        """活动互斥，成功可查看，失败新任务；明确重建也不能绕过活动锁。"""
        first = self.start()['id'];self.assertEqual(self.start(force=True)['id'],first)
        self.execute(first);self.assertTrue(self.start()['choice_required'])
        next_id=self.start(force=True)['id'];self.assertNotEqual(first,next_id)
        repository.update_task(self.settings,next_id,status='failed',active_key=None)
        # 成功结果仍可优先查看；明确重建得到全新任务。
        self.assertNotEqual(self.start(force=True)['id'],next_id)

    def test_unknown_and_incomplete_never_auto_retry(self):
        """未知费用预留，返回不完整保留正文，均仅一次请求。"""
        first=self.start()['id'];state,client=self.execute(first,error=AIError('私密错误'))
        self.assertEqual(state,'failed');self.assertEqual(client.complete_messages.call_count,1)
        self.assertGreater(service.read(self.settings,first)['budget']['used_tokens'],0)
        second=self.start()['id'];state,client=self.execute(second,text='## 总体判断\n可读但缺核心章节')
        self.assertEqual(state,'partial');self.assertEqual(client.complete_messages.call_count,1)
        self.assertIn('可读',service.read(self.settings,second)['report']['content']['markdown'])

    def test_checks_distinguish_report_image_and_original(self):
        """报告归属、已有图号、连续文字三种检查区分；未选和伪原文不通过。"""
        incoming=self.data['input']; text='【评价报告】【主图报告】【主图1】【评价8｜初评】原文：“刚买很好用”'
        checked=report.check(text,incoming);self.assertEqual(checked['counts']['valid_count'],4)
        for wrong in ('【视频报告】','【主图999】','【评价8｜初评】原文：“刚买…好用”'):
            self.assertEqual(report.check(wrong,incoming)['counts']['valid_count'],0)
        value=dict(markdown=text+'<script>alert(1)</script>',validation=checked)
        self.assertNotIn('<script>',report.render(value));self.assertIn('<details',report.render(value))
        self.assertTrue(report.generation(sample(KINDS),'stop')['complete'])

    def test_web_exports_recheck_and_image_return_context(self):
        """固定来源网页三格式、零费检查及图片返回交叉报告。"""
        task=self.start()['id'];self.execute(task);saved=service.read(self.settings,task)
        url='/analysis/composite/tasks/'+task
        page=self.client.get(url);self.assertEqual(page.status_code,200);self.assertIn('评价 × 问大家 × 主图 × 详情图',page.text)
        for fmt in ('md','html','json'):
            result=self.client.get(url+'?download='+fmt);self.assertEqual(result.status_code,200);self.assertIn('attachment',result.headers['Content-Disposition'])
        checked=self.client.post('/api/analysis/composite/tasks/'+task+'/check',json={'report_id':saved['report']['id']},headers=self.headers)
        self.assertEqual(checked.status_code,200);self.assertEqual(checked.json['ai_calls'],0)
        source=next(s for s in self.data['input']['reports'] if s['kind']=='main_images')
        page=self.client.get('/analysis/images/main_images/tasks/'+source['analysis_id']+'?'+urlencode(dict(version=1,return_to=url)))
        self.assertEqual(page.status_code,200);self.assertIn('返回交叉报告',page.text)

    def test_preview_routes_guard_and_legacy_hidden(self):
        """新弹窗、严格JSON与同源校验，旧类型不冒充新报告。"""
        for suffix in ('','?dialog=1'):
            response=self.client.get('/analysis/composite/products/123/new'+suffix);self.assertEqual(response.status_code,200)
        url='/api/analysis/composite/products/123';values=self.values()
        self.assertEqual(self.client.post(url,json=values).status_code,403)
        self.assertEqual(self.client.post(url,json=values,headers=self.headers).status_code,400)
        self.assertEqual(self.client.post(url,json=dict(values,allow_paid=True,other=True),headers=self.headers).status_code,400)
        self.assertEqual(self.client.post(url,json=dict(values,allow_paid=True),headers=dict(self.headers,Origin='https://other.test')).status_code,403)

    def test_grouping_preserves_chapters_and_exact_requests(self):
        """超容才N＋1，全文无丢字；固定请求数与组稿持久化。"""
        content=dict(self.review_content,markdown=self.review_content['markdown']+'\n\n## 长章节\n'+'长段资料。'*10000)
        repository.save_report(self.settings,self.review_task,content)
        config=replace(AISettings(api_key='sk-test-only'),report_output_tokens=512,group_output_tokens=512,
            capabilities={'deepseek-flash':{'context_tokens':50000,'max_output_tokens':8192}})
        with patch.object(service,'load_config',return_value=config),patch.object(shared_planner,'Counter',FakeCounter):
            data=service.preview(self.settings,'123');task=self.start(data)['id']
        self.assertEqual(data['plan']['mode'],'grouped')
        parts=[p for g in data['plan']['groups'] for p in json.loads(g['messages'][-1]['content'])['parts']]
        self.assertEqual(''.join(p['text'] for p in parts if p['kind']=='reviews' and p['type']=='report_fragment'),content['markdown'].replace('【8｜初评】','【评价8｜初评】'))
        client=Mock();client.complete_messages.side_effect=[dict(text='本组发现【评价报告】',finish_reason='stop',usage={'total_tokens':10}) for _ in data['plan']['groups']]+[dict(text=sample(KINDS),finish_reason='stop',usage={'total_tokens':10})]
        with patch.object(planner,'Counter',FakeCounter):self.assertEqual(runtime.execute(self.settings,task,Mock(),Mock(return_value=client)),'succeeded')
        self.assertEqual(client.complete_messages.call_count,data['plan']['planned_calls'])
        self.assertEqual(len(service.read(self.settings,task)['report']['content']['phases']),len(data['plan']['groups']))
