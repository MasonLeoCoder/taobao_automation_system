"""图片分析隔离端到端验证：无真实AI、无真实采集或业务写入。"""
import copy
import io
from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock,patch
from PIL import Image
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v21 import build_metadata
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.images import vision
from ecommerce.infrastructure.ai.client import DeepSeekClient,AIError
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.modules.image_analysis import service,store,planner,report,runtime
from tests.unit import test_review_analysis as legacy


def sample():
    """生成带图号的六章模拟稿，不冒充真实模型输出。"""
    return '\n\n'.join('## '+name+'\n\n【可见事实】测试图片。'+('【主图1】【详情1】' if i==0 else '') for i,name in enumerate(report.SECTIONS))


class ImageAnalysisTests(TestCase):
    put=legacy.ReviewAnalysisTests.put

    def setUp(self):
        """临时SQLite与两种PNG，启动及客户端始终模拟。"""
        with patch('tests.unit.test_review_analysis.build_metadata',build_metadata): legacy.ReviewAnalysisTests.setUp(self)
        for batch,module in ((7,'main_images'),(8,'detail_images')):
            self.put('collection_batches',id=batch,product_id=1,module=module,visibility='published',status='complete',captured_at=datetime(2026,11,2,10))
        self.add_image(30,7,'main_image','blue'); self.add_image(31,8,'detail_image','green')

    def add_image(self,media,batch,role,color,size=(64,64)):
        """在临时目录登记实际图片字节及正确摘要。"""
        content=io.BytesIO(); Image.new('RGB',size,color).save(content,'PNG'); data=content.getvalue()
        path=self.root/f'workspace/output/raw/{media}.png'; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
        relative=path.relative_to(self.settings.workspace).as_posix()
        self.put('file_assets',id=media,root_key='workspace',relative_path=relative,size_bytes=len(data),sha256=vision.digest(data),availability='present')
        self.put('media_references',id=media,batch_id=batch,position=1,role=role,file_asset_id=media)
        return path

    def preview(self,**kwargs):
        """只读业务预览。"""
        return service.preview(self.settings,'123',**kwargs)

    def values(self,data):
        """模拟已确认的固定范围与哈希。"""
        return {k:data[k] for k in ('main_batch','detail_batch','model','input_hash','plan_hash','allow_partial')}

    def start(self,force=False):
        """明确费用确认，但不启动真实进程。"""
        value=self.preview()
        with patch.object(service,'launch'): return service.start(self.settings,'123',**self.values(value),allow_paid=True,force=force)['id']

    def test_direct_minimal_snapshot_and_transport(self):
        """两种图片一次提交，无本机路径及base64入库，实际请求图文交错。"""
        data=self.preview(); plan=data['plan']; self.assertEqual(plan['planned_calls'],1); self.assertEqual(plan['image_count'],2)
        encoded=planner.encode(plan['groups'][0]['messages']); self.assertNotIn('relative_path',encoded); self.assertNotIn('base64',encoded)
        task=self.start(); saved=service.read(self.settings,task)
        outgoing=planner.transport(self.settings,saved['task'],plan['groups'][0]['messages'])
        self.assertTrue(outgoing[1]['content'][2]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertEqual(saved['task']['source_count'],2); self.assertEqual(saved['sources'][0]['media_id'],30)
        self.assertIsNone(saved['sources'][0]['review_id']); self.assertIsNone(saved['sources'][0]['question_id'])
        self.assertEqual(planner.body_size(plan['groups'][0]['messages'],'deepseek-flash',plan['report_output_tokens'])[0],
            len(__import__('json').dumps(dict(model='deepseek-flash',messages=outgoing,thinking={'type':'disabled'},stream=False,max_tokens=plan['report_output_tokens']),ensure_ascii=False).encode()))

    def test_consent_stale_and_foreign_batch(self):
        """费用/来源/计划守卫不创建任务，不能跨角色或商品。"""
        data=self.preview(); values=self.values(data)
        with self.assertRaises(ValueError): service.start(self.settings,'123',**values)
        with self.assertRaises(ValueError): service.start(self.settings,'123',**dict(values,plan_hash='changed'),allow_paid=True)
        with self.assertRaises(LookupError): self.preview(main_batch=8)
        self.put('products',id=2,platform='taobao',platform_product_id='456')
        with self.assertRaises(LookupError): service.preview(self.settings,'456',main_batch=7)
        with self.engine.begin() as conn: conn.execute(self.tables['collection_batches'].update().where(self.tables['collection_batches'].c.id==7).values(visibility='staging'))
        with self.assertRaises(LookupError): self.preview(main_batch=7)

    def test_missing_and_partial_explicit(self):
        """缺图不静默舍弃，部分范围明确同意后保留排除原因。"""
        (self.settings.workspace/'output/raw/30.png').unlink()
        data=self.preview(); self.assertIsNone(data['plan']); self.assertTrue(data['blocked'])
        data=self.preview(allow_partial=True); self.assertEqual(data['plan']['image_count'],1)
        self.assertEqual(data['input']['scope']['issues'][0]['label'],'主图1')
        single=self.preview(main_batch=0,allow_partial=True); self.assertEqual(len(single['input']['images']),1)
        (self.settings.workspace/'output/raw/31.png').unlink()
        self.assertIsNone(self.preview(allow_partial=True)['plan'])

    def test_model_vision_is_not_text_capacity(self):
        """Pro有文字容量仍不能用来执行看图。"""
        data=self.preview(model='deepseek-v4-pro'); self.assertIsNone(data['plan']); self.assertIn('不可用于图片分析',data['blocked'])
        with self.assertRaises(ValueError): planner.profile(load_config(self.settings.root),'deepseek-v4-pro')

    def test_changed_original_and_ownership_before_call(self):
        """创建后文件或归属变化在额度预留/联网前停止，零请求。"""
        task=self.start(); path=self.settings.workspace/'output/raw/30.png'; path.write_bytes(b'changed')
        factory=Mock(); self.assertEqual(runtime.execute(self.settings,task,Mock(),factory),'failed'); factory.assert_not_called()
        self.assertEqual(service.read(self.settings,task)['calls'],[])

    def test_database_owner_rechecked(self):
        """原字节未变，但原媒体换批次仍不能绕过发送守卫。"""
        task=self.start()
        with self.engine.begin() as conn: conn.execute(self.tables['media_references'].update().where(self.tables['media_references'].c.id==30).values(batch_id=8))
        factory=Mock(); self.assertEqual(runtime.execute(self.settings,task,Mock(),factory),'failed'); factory.assert_not_called()

    def test_direct_mock_reports_exports_recheck(self):
        """一次模拟API完整保存、三格式导出及零费检查，原报告版本不覆盖。"""
        task=self.start(); client=Mock(); client.complete_images.return_value=dict(text=sample(),finish_reason='stop',usage={'total_tokens':300})
        self.assertEqual(runtime.execute(self.settings,task,Mock(),Mock(return_value=client)),'succeeded')
        saved=service.read(self.settings,task); self.assertEqual(saved['report']['content']['validation']['counts']['valid_count'],2)
        self.assertNotIn('base64',__import__('json').dumps(saved['calls'],default=str))
        page=self.client.get('/analysis/images/tasks/'+task); self.assertEqual(page.status_code,200); self.assertIn('本次固定图片',page.text)
        for fmt in ('md','json','html'):
            value=self.client.get('/analysis/images/tasks/'+task+'?download='+fmt); self.assertEqual(value.status_code,200); self.assertIn('attachment',value.headers['Content-Disposition'])
        result=self.client.post('/api/analysis/images/tasks/'+task+'/check',json={'report_id':saved['report']['id']},headers=self.headers)
        self.assertEqual(result.status_code,200); self.assertEqual(result.json['ai_calls'],0); self.assertEqual(client.complete_images.call_count,1)
        self.assertEqual(len(service.read(self.settings,task)['reports']),2)
        self.assertEqual(self.client.get('/analysis/images/tasks/'+task+'?version=1').status_code,200)
        sha=saved['task']['parameters']['plan']['input']['images'][0]['frames'][0]['sha256']
        self.assertEqual(self.client.get(f'/analysis/images/tasks/{task}/frames/{sha}').status_code,200)
        self.assertEqual(self.client.get(f'/analysis/images/tasks/{task}/frames/{"0"*64}').status_code,400)

    def test_grouped_no_omission_and_summary_without_images(self):
        """模拟低图片数限制触发N+1，覆盖全部切片，汇总不重复发原图。"""
        real=planner.profile
        from dataclasses import replace
        config=replace(load_config(self.settings.root),group_output_tokens=128)
        with patch.object(planner,'profile',side_effect=lambda *a:dict(real(*a),max_images=1)), patch.object(service,'load_config',return_value=config):
            data=self.preview(); self.assertEqual(data['plan']['planned_calls'],3)
            with patch.object(service,'launch'): task=service.start(self.settings,'123',**self.values(data),allow_paid=True)['id']
        client=Mock(); client.complete_images.side_effect=[dict(text='## 本组范围\n【主图1】',finish_reason='stop',usage={'total_tokens':20}),dict(text='## 本组范围\n【详情1】',finish_reason='stop',usage={'total_tokens':20}),dict(text=sample(),finish_reason='stop',usage={'total_tokens':30})]
        self.assertEqual(runtime.execute(self.settings,task,Mock(),Mock(return_value=client)),'succeeded')
        self.assertEqual(client.complete_images.call_count,3)
        self.assertIsInstance(client.complete_images.call_args_list[-1].args[0][1]['content'],str)
        self.assertEqual(service.read(self.settings,task)['task']['usage']['total_tokens'],70)

    def test_active_success_failure_independent_history(self):
        """活动复用，失败新建，成功明确选择，历史不混入评价。"""
        task=self.start(); self.assertEqual(self.start(force=True),task)
        repository.update_task(self.settings,task,status='failed',active_key=None)
        second=self.start(); self.assertNotEqual(second,task)
        repository.update_task(self.settings,second,status='succeeded',active_key=None)
        data=self.preview()
        with patch.object(service,'launch'):
            self.assertTrue(service.start(self.settings,'123',**self.values(data),allow_paid=True)['choice_required'])
        self.assertNotEqual(self.start(force=True),second)
        self.assertEqual(len(store.history(self.settings,'123')['tasks']),3)

    def test_incomplete_and_unknown_no_retry(self):
        """缺章保留可读稿，未知请求保留预算，均只有一次调用。"""
        task=self.start(); client=Mock(); client.complete_images.return_value=dict(text='## 总体判断\n可读但缺章',finish_reason='length',usage={'total_tokens':20})
        self.assertEqual(runtime.execute(self.settings,task,Mock(),Mock(return_value=client)),'partial'); self.assertEqual(client.complete_images.call_count,1)
        task=self.start(); client.complete_images.side_effect=AIError('不向页面展示的内部信息')
        self.assertEqual(runtime.execute(self.settings,task,Mock(),Mock(return_value=client)),'failed')
        saved=service.read(self.settings,task); self.assertEqual(saved['calls'][0]['status'],'unknown'); self.assertGreater(saved['budget']['used_tokens'],0)
        self.assertNotIn('内部信息',saved['task']['message'])

    def test_safe_citations_and_no_ocr_claim(self):
        """未知图号待核对但不阻止可读报告，模型不能执行HTML或外链。"""
        data=self.preview(); sent=report.lookup(data['plan']['groups'][0]['messages']); text=sample()+'【详情999】<script>alert(1)</script> ![bad](https://evil.example/x)'
        value=dict(markdown=text,validation=report.check(text,sent)); result=report.render(value)
        self.assertEqual(value['validation']['counts']['pending_count'],1); self.assertNotIn('<script>',result); self.assertNotIn('<img',result)
        self.assertIn('#image-source-30',result); self.assertTrue(report.generation(text,'stop')['complete'])

    def test_preview_route_csrf_and_image_identity(self):
        """弹窗无业务写入，缩略图固定同商品批次摘要，跨来源拒绝。"""
        page=self.client.get('/analysis/images/products/123/new?dialog=1'); self.assertEqual(page.status_code,200); self.assertIn('data-ia-form',page.text)
        self.assertNotIn('name="allow_paid" required checked',page.text)
        data=self.preview(); image=data['input']['images'][0]; frame=image['frames'][0]
        url=f'/analysis/images/products/123/preview?batch=7&media=30&piece=1&sha={frame["sha256"]}'
        self.assertEqual(self.client.get(url).status_code,200); self.assertEqual(self.client.get(url.replace('batch=7','batch=8')).status_code,400)
        self.assertEqual(self.client.post('/api/analysis/images/products/123',json=self.values(data)).status_code,403)

    def test_long_image_full_coverage_overlap_and_original_unchanged(self):
        """长宽切片均覆盖末端且重叠，原件字节不变。"""
        self.add_image(32,8,'detail_image','orange',(1400,3300))
        data=self.preview(); image=next(s for s in data['input']['images'] if s['media_id']==32); boxes=[v['box'] for v in image['frames']]
        self.assertEqual(max(b[2] for b in boxes),1400); self.assertEqual(max(b[3] for b in boxes),3300)
        self.assertEqual(boxes[0],[0,0,1280,1600]); self.assertEqual(boxes[1][0],1152); self.assertEqual(boxes[2][1],1472)
        original=vision.original(self.settings,image['asset']); self.assertEqual(vision.digest(original),image['asset']['sha256'])
        self.assertTrue(all(vision.cached(self.settings,f).startswith(b'\x89PNG') for f in image['frames']))

    def test_identical_images_deduplicate_preserving_role_aliases(self):
        """相同图片仅发送一次，主图和详情角色均保留。"""
        asset=self.preview()['input']['images'][0]['asset']; data=vision.original(self.settings,asset)
        (self.settings.workspace/'output/raw/31.png').write_bytes(data)
        with self.engine.begin() as conn: conn.execute(self.tables['file_assets'].update().where(self.tables['file_assets'].c.id==31).values(size_bytes=len(data),sha256=vision.digest(data)))
        preview=self.preview(); self.assertEqual(preview['plan']['image_count'],1)
        self.assertEqual(planner.frames(preview['input'])[0]['labels'],['主图1','详情1'])

    def test_malformed_path_animation_and_hash_rejected(self):
        """不越目录、不忽略动画帧，不接受损坏或未核验原件。"""
        asset=self.preview()['input']['images'][0]['asset']
        with self.assertRaises(ValueError): vision.original(self.settings,dict(asset,relative_path='../outside.png'))
        with self.assertRaises(ValueError): vision.original(self.settings,dict(asset,sha256='0'*64))
        frames=[Image.new('RGB',(8,8),v) for v in ('red','blue')]; buf=io.BytesIO(); frames[0].save(buf,'GIF',save_all=True,append_images=frames[1:]); data=buf.getvalue()
        path=self.settings.workspace/'output/raw/30.png'; path.write_bytes(data)
        with self.assertRaisesRegex(ValueError,'动态图片'): vision.prepare(self.settings,dict(asset,size_bytes=len(data),sha256=vision.digest(data)))

    def test_client_image_contract_and_text_remain_separate(self):
        """真实视觉编码仅送固定官方端点，普通文本方法继续拒绝图片结构。"""
        data=self.preview(); task=self.start(); saved=service.read(self.settings,task); values=planner.transport(self.settings,saved['task'],data['plan']['groups'][0]['messages'])
        client=DeepSeekClient(load_config(self.settings.root),'deepseek-flash'); cap=data['plan']['capability']
        with patch.object(client,'_send',return_value={'text':'ok'}) as send:
            client.complete_images(values,max_tokens=8192,capability=cap); self.assertEqual(send.call_count,1)
            with self.assertRaises(AIError): client.complete_messages(values,max_tokens=8192,capability=cap)
            wrong=copy.deepcopy(values); wrong[1]['content'][2]['image_url']['url']='https://evil.example/file.png'
            with self.assertRaises(AIError): client.complete_images(wrong,max_tokens=8192,capability=cap)
            self.assertEqual(send.call_count,1)

    def test_schema_source_exactly_one_kind(self):
        """新模型保留旧来源并扩展原媒体外键，而非在正文里猜关联。"""
        table=build_metadata().tables['analysis_sources']; self.assertIn('media_id',table.c)
        check=next(c for c in table.constraints if c.name=='ck_analysis_source_kind')
        self.assertIn('media_id IS NOT NULL',str(check.sqltext)); self.assertEqual(len(table.c.media_id.foreign_keys),1)

    def test_image_return_target_stays_same_product(self):
        """图片来源返回独立图片页签/报告，跨商品或外站地址不能伪造返回。"""
        from ecommerce.web.report_navigation import target
        task=self.start()
        with self.app.test_request_context():
            url,label,valid=target(self.settings,'123','/collections/products/123?module=composite&analysis=images')
            self.assertTrue(valid);self.assertIn('analysis=images',url);self.assertEqual(label,'图片分析')
            url,label,valid=target(self.settings,'123','/analysis/images/tasks/'+task)
            self.assertTrue(valid);self.assertEqual(label,'图片报告')
            self.assertFalse(target(self.settings,'456','/analysis/images/tasks/'+task)[2])
            self.assertFalse(target(self.settings,'123','https://evil.example/collections/products/123?module=composite')[2])
