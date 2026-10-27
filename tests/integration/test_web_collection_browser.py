"""隔离浏览器验证网页采集交互；请求转入临时 Flask，禁止访问真实平台。"""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright,expect
from ecommerce.infrastructure.tasks import web_jobs as jobs
from ecommerce.infrastructure.tasks.ownership import atomic_json
from tests.unit import test_collection_browser as fixtures


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用隔离浏览器验证')
class WebCollectionBrowserTests(unittest.TestCase):
    def recovery_job(self):
        """准备带错误及未完成模块的网页任务，数据库边界使用明确替身。"""
        from ecommerce.modules.collection import web_collection as service
        options={'mode':'detail','input':'123','modules':['video','questions'],'all_reviews':False,'all_questions':True}
        job=service.start_configured(self.settings,options)
        row=jobs.read(self.settings,job['id']);row.update(state='cancelled',recovery_ready=True);self.save(row)
        failure={'product_id':'123','module':'video','state':'failed_skipped','task_id':job['id'],'message':'合成视频下载失败',
            'progress':{'last_progress_at':'2026-10-26T12:30:00+08:00','counts':{'received_bytes':1024}}}
        result={'batch_id':None,'valid':False,'satisfied':False,'has_errors':True,'failures':[failure],
            'modules':[{'module':m,'valid':False,'batch_id':None} for m in options['modules']],
            'counts':{'complete':0,'no_entry':0,'failed':1,'not_run':1,'reused':0}}
        mock=patch.object(service,'saved_result',return_value=result);mock.start();self.addCleanup(mock.stop)
        return job,options

    def test_recovery_preview_requires_confirm_and_preserves_original_scope(self):
        """查看预览不派发，明确确认后提交版本和原数量范围。"""
        from ecommerce.modules.collection import dispatch,web_collection as service
        job,options=self.recovery_job()
        view={'revision':7,'options':options,'finished':False,'pages':[],
            'items':[{'product_id':'123','module':'questions','action':'clean_restart','state':'running',
                      'cleanup':{'batch_count':1,'file_count':2}}],
            'next':{'page':None,'product_id':'123','module':'questions'}}
        with patch.object(dispatch,'preview',return_value=view),patch.object(dispatch,'resume',side_effect=lambda s,i,r,**kw:service.start_configured(s,options)) as resume:
            self.page.goto('http://127.0.0.1/modules/collection')
            expect(self.page.locator('.collection-module-error')).to_contain_text('合成视频下载失败')
            expect(self.page.locator('.collection-module-error')).to_contain_text('已接收字节 1024')
            self.page.get_by_role('button',name='查看恢复计划',exact=True).click()
            expect(self.page.locator('#collection-recovery-dialog')).to_be_visible()
            expect(self.page.locator('#collection-recovery-plan')).to_contain_text('清理未完成结果，新建批次重采')
            expect(self.page.locator('#collection-recovery-plan')).to_contain_text('2 个批次内文件')
            resume.assert_not_called()
            self.assertEqual(len(jobs.rows(self.settings)),1)
            self.page.get_by_role('button',name='按此计划继续',exact=True).click()
            expect(self.page.locator('#collection-recovery-dialog')).not_to_be_visible()
            self.assertEqual(resume.call_args.args,(self.settings,job['id'],7))
            self.assertRegex(resume.call_args.kwargs['submission_key'],'^[a-f0-9]{32}$')
            newest=jobs.rows(self.settings)[0]
            self.assertEqual(newest['options']['all_questions'],True)
            self.assertEqual(newest['options']['modules'],['video','questions'])
        self.assertEqual(self.errors,[])

    def test_error_retry_queues_without_starting_browser_and_queue_resume_is_explicit(self):
        """错误可定位并排队，队列按钮有明确操作，点击补采不抢占浏览器。"""
        from ecommerce.modules.collection import dispatch
        job,options=self.recovery_job();rows=[]
        def retry(settings,identifier,product,module):
            rows.append({'id':'c'*32,'state':'paused','product_id':product,'module':module})
            return {'id':'c'*32,'message':'补采请求已保存，等待手动继续队列'}
        def control(settings,action):
            rows[0]['state']='waiting' if action=='resume' else 'paused'
            return rows
        with patch.object(dispatch,'queue',side_effect=lambda settings:list(rows)),patch.object(dispatch,'retry',side_effect=retry) as submit,patch.object(dispatch,'control_queue',side_effect=control) as command:
            self.page.goto('http://127.0.0.1/modules/collection')
            self.page.get_by_role('button',name='重新采集此模块',exact=True).click()
            expect(self.page.locator('#collection-queue-list')).to_contain_text('已暂停')
            submit.assert_called_once_with(self.settings,job['id'],'123','video')
            command.assert_not_called()
            self.assertEqual(len(jobs.rows(self.settings)),1)
            self.page.get_by_role('button',name='继续队列',exact=True).click()
            expect(self.page.locator('#collection-queue-list')).to_contain_text('等待当前采集结束')
            command.assert_called_once_with(self.settings,'resume')
        self.assertEqual(self.errors,[])

    def setUp(self):
        """临时数据和浏览器；参数：无；返回：无，所有采集派发均模拟。"""
        self.fixture=fixtures.CollectionBrowserTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.settings=replace(self.fixture.settings,task_backend='mysql')
        from tests.job_store_fixture import JobDatabaseFixture
        self.job_db=JobDatabaseFixture(self,self.settings)
        self.fixture.app.config['SETTINGS']=self.settings
        for target,kwargs in [
            ('ecommerce.infrastructure.tasks.web_jobs.launch',{'return_value':SimpleNamespace(pid=9999)}),
            ('ecommerce.infrastructure.tasks.web_jobs.alive',{'return_value':True}),
            ('ecommerce.infrastructure.tasks.ownership.active_local_tasks',{'side_effect':self.active}),
            ('ecommerce.modules.collection.web_collection.active_local_tasks',{'side_effect':self.active}),
            ('ecommerce.modules.collection.dispatch.active_local_tasks',{'return_value':[]})]:
            mock=patch(target,**kwargs);mock.start();self.addCleanup(mock.stop)
        self.playwright=sync_playwright().start();self.addCleanup(self.playwright.stop)
        self.browser=self.playwright.chromium.launch(headless=True);self.addCleanup(self.browser.close)
        self.context=self.browser.new_context(viewport={'width':1440,'height':1000})
        self.context.route('**/*',self.route)
        self.page=self.context.new_page()
        self.errors=[];self.page.on('pageerror',lambda error:self.errors.append(str(error)))

    def active(self,settings):
        """模拟已由统一执行器启动的本地任务；参数：配置；返回：当前活动记录。"""
        return [{'id':row['id'],'name':'collection.taobao.detail.open','storage_pending':False}
                for row in jobs.rows(self.settings) if row['state']=='running']

    def route(self,route):
        """所有网络请求仅交给临时服务；参数：浏览器路由；返回：本地响应。"""
        request=route.request;url=urlsplit(request.url)
        if url.hostname!='127.0.0.1':route.abort();return
        response=self.fixture.client.open(url.path+('?' + url.query if url.query else ''),method=request.method,
                    headers={key:value for key,value in request.headers.items() if key.lower() not in {'host','content-length'}},
                    data=request.post_data_buffer,base_url='http://127.0.0.1')
        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
        response.close()

    def launch(self):
        """点击视频模块并模拟执行器就绪；参数：无；返回：派发。"""
        self.page.goto('http://127.0.0.1/collections/products/123')
        video=self.page.locator('[data-collection-module="video"]')
        expect(video.get_by_role('button',name='采集数据',exact=True)).to_be_visible()
        expect(self.page.locator('[data-collection-module="parameters"]').get_by_role('link',name='查看资料 →')).to_be_visible()
        video.get_by_role('button',name='采集数据',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('启动中')
        row=jobs.rows(self.settings)[0];row['state']='running';self.save(row)
        return row

    def test_keyword_form_dispatches_parameters_once_and_restores_progress(self):
        """配置关键词任务、重复点击去重，并在刷新后恢复进度；参数：无；返回：无。"""
        self.page.goto('http://127.0.0.1/modules/collection')
        self.page.get_by_label('搜索关键词',exact=True).fill('粘毛器')
        self.page.get_by_label('采集页数',exact=True).fill('3')
        self.page.get_by_label('不限数量',exact=True).uncheck()
        self.page.get_by_label('每页最多商品数',exact=True).fill('3')
        self.page.locator('#collection-with-details').check()
        expect(self.page.locator('#collection-request-summary')).to_contain_text('每页最多 3 个商品')
        expect(self.page.locator('#collection-detail-order')).to_be_visible()
        submit=self.page.get_by_role('button',name='开始采集',exact=True)
        expect(submit).to_be_enabled()
        submit.evaluate('(button) => {button.click();button.click();}')
        expect(self.page.locator('#collection-task-bar')).to_contain_text('关键词“粘毛器”')
        self.assertEqual(len(jobs.rows(self.settings)),1)
        row=jobs.rows(self.settings)[0]
        self.assertEqual(row['options'],{'mode':'keyword','keyword':'粘毛器','pages':3,'limit':3,'with_details':True})
        row.update(state='running',progress={'current_page':2,'completed_page_count':1,'collected_count':3,'detail_completed_count':2})
        self.save(row)
        self.page.reload()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('已保存 1/3 页',timeout=7000)
        expect(self.page.locator('#collection-job-list')).to_contain_text('详情已处理 2 个商品')
        expect(self.page.locator('#collection-submit')).to_be_enabled()
        row.update(state='succeeded',message='已保存结果；'+('详细说明；'*40),result={'batch_id':20,'valid':True})
        self.save(row)
        expect(self.page.locator('#collection-submit')).to_be_enabled(timeout=7000)
        result=self.page.locator('#collection-job-list').get_by_role('link',name='查看采集结果',exact=True)
        expect(result).to_have_attribute('href','/collections/searches/'+row['id'])
        expect(self.page.locator('#collection-job-list details')).not_to_have_attribute('open','')
        self.assertIn('每页最多 3 个商品',self.page.locator('#collection-job-list').inner_text())
        self.assertEqual(self.errors,[])

    def test_detail_form_selects_modules_independent_ranges_and_new_product(self):
        """新商品无需历史记录，模块和数量范围按配置派发；参数：无；返回：无。"""
        self.page.goto('http://127.0.0.1/modules/collection')
        self.page.get_by_role('tab',name='商品详情采集',exact=True).click()
        self.page.get_by_label('商品 ID／链接',exact=True).fill('456')
        expect(self.page.locator('#collection-keyword')).to_be_disabled()
        self.page.get_by_role('button',name='取消全选',exact=True).click()
        self.page.get_by_role('button',name='开始采集',exact=True).click()
        expect(self.page.locator('#collection-submit-status')).to_contain_text('至少选择一个')
        self.assertEqual(jobs.rows(self.settings),[])
        expect(self.page.get_by_label('评价数量范围',exact=True)).to_be_disabled()
        self.page.get_by_label('评价明细',exact=True).check()
        self.page.get_by_label('问大家',exact=True).check()
        self.page.get_by_label('评价数量范围',exact=True).select_option('all')
        self.page.get_by_role('button',name='开始采集',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('商品 456')
        row=jobs.rows(self.settings)[0]
        self.assertEqual(row['modules'],['reviews','questions'])
        self.assertTrue(row['options']['all_reviews']);self.assertFalse(row['options']['all_questions'])
        self.assertEqual(self.errors,[])

    def test_keyword_manual_feedback_uses_current_task_and_global_controls(self):
        """关键词登录提示在其他页面仍可恢复并明确继续；参数：无；返回：无。"""
        self.page.goto('http://127.0.0.1/modules/collection')
        self.page.get_by_label('搜索关键词',exact=True).fill('测试')
        self.page.get_by_role('button',name='开始采集',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('关键词“测试”')
        row=jobs.rows(self.settings)[0];row['state']='running';self.save(row);self.publish(row)
        self.page.goto('http://127.0.0.1/tasks')
        modal=self.page.locator('#collection-attention')
        expect(modal).to_be_visible(timeout=7000)
        expect(modal).to_contain_text('关键词“测试”')
        modal.get_by_role('button',name='继续',exact=True).click()
        expect(modal).not_to_be_visible()
        command=self.command(row)
        self.assertEqual((command['action'],command['prompt_id']),('continue','b'*32))
        self.assertEqual(self.errors,[])

    def test_recollect_link_prefills_product_and_module_and_mobile_layout_fits(self):
        """重新采集带入当前商品和模块，窄屏不出现整页溢出；参数：无；返回：无。"""
        self.page.goto('http://127.0.0.1/collections/products/123?module=video')
        self.page.get_by_role('link',name='重新采集／配置采集范围 →',exact=True).click()
        expect(self.page.get_by_label('商品 ID／链接',exact=True)).to_have_value('123')
        expect(self.page.locator('[name="modules"]:checked')).to_have_count(1)
        expect(self.page.get_by_label('视频',exact=True)).to_be_checked()
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.assertEqual(self.errors,[])

    def test_configuration_history_failure_prevents_submission(self):
        """状态不可核实时禁止派发并显示明确错误；参数：无；返回：无。"""
        self.page.route('**/api/collection-jobs?*',lambda route:route.fulfill(status=503,json={'error':'数据库暂不可用'}))
        self.page.goto('http://127.0.0.1/modules/collection')
        expect(self.page.locator('#collection-task-bar')).to_contain_text('数据库暂不可用')
        expect(self.page.locator('#collection-submit')).to_be_disabled()
        self.assertEqual(jobs.rows(self.settings),[])
        self.assertEqual(self.errors,[])

    def test_skipped_questions_close_task_without_attention_or_successful_data_claim(self):
        """页面区分入口跳过和采集成功，刷新后正常收尾可重新发起；参数：无；返回：无。"""
        from ecommerce.modules.collection import web_collection as service
        self.page.goto('http://127.0.0.1/modules/collection?mode=detail&input=123&modules=questions')
        self.page.get_by_role('button',name='开始采集',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('商品 123')
        row=jobs.rows(self.settings)[0]
        record={'status':'skipped','stop_reason':'entry_not_found','message':'未找到问大家入口，已跳过',
            'entry_search':{'scroll_count':3,'max_scrolls':10,'unchanged_count':3,'stop_reason':'stable_position'}}
        self.fixture.put('collection_runs',id=90,task_id=row['id'],product_id=1,started_at=self.fixture.now,status='complete_with_warnings')
        self.fixture.put('collection_steps',id=90,run_id=90,module='questions',requested=True,status='skipped',
                         continuation_allowed=True,finished_at=self.fixture.now,payload={'record':record})
        row.update(state='succeeded',message='详情采集结束（含跳过项目）',result=service.saved_result(self.settings,row))
        self.save(row)
        expect(self.page.locator('#collection-job-list')).to_contain_text('已结束（含跳过项目）',timeout=7000)
        expect(self.page.locator('#collection-submit')).to_be_enabled()
        expect(self.page.locator('#collection-attention')).not_to_be_visible()
        self.page.goto('http://127.0.0.1/collections/products/123')
        card=self.page.locator('[data-collection-module="questions"]')
        expect(card).to_contain_text('已跳过',timeout=7000)
        expect(card).to_contain_text('未找到问大家入口')
        expect(card.get_by_role('button',name='重新采集',exact=True)).to_be_enabled()
        self.assertEqual(self.errors,[])

    def publish(self,row,prompt='b'*32,allowed=None):
        """模拟已经提交数据库的当前人工提示。"""
        self.job_db.prompt(row,{'task_id':row['id'],'prompt_id':prompt,'allowed':allowed or ['继续'],
            'message':'请手动扫码登录 <script>不能执行</script>','step':'等待人工登录淘宝'})

    def save(self,row):
        """准备任务结论及其对应的查询样例，不用结果缓存冒充数据库资料。"""
        import sqlalchemy as sa
        result=row.get('result') or {}
        values=result.get('modules',[dict(result,module=row.get('module'))])
        for value in values:
            if not value.get('batch_id'):
                continue
            module=value.get('module') or row['module']
            table=self.fixture.store.tables['collection_batches']
            with self.fixture.engine.begin() as c:
                batch=c.execute(sa.select(table).where(table.c.id==value['batch_id'])).mappings().first()
            if batch:
                with self.fixture.engine.begin() as c:
                    c.execute(table.update().where(table.c.id==value['batch_id']).values(task_id=row['id'],module=module,
                        status='no_video' if value.get('valid') and module=='video' else 'complete' if value.get('valid') else 'partial'))
            else:
                self.fixture.batch(value['batch_id'],module,product_id=1,status='complete' if value.get('valid') else 'partial')
                with self.fixture.engine.begin() as c:
                    c.execute(table.update().where(table.c.id==value['batch_id']).values(task_id=row['id']))
            if value.get('valid'):
                pointer=self.fixture.store.tables['product_current_sections']
                section={'basic_info':'product_info','skus':'sku_details'}.get(module,module)
                with self.fixture.engine.begin() as c:
                    c.execute(pointer.delete().where(pointer.c.product_id==1,pointer.c.section==section))
                    c.execute(pointer.insert().values(product_id=1,section=section,module=module,batch_id=value['batch_id']))
        self.job_db.save(row)

    def command(self,row):
        """读取数据库待接收指令并清空提示。"""
        from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
        result=CollectionJobStore(self.settings).receive_control(row['id'],row['claim_token'])
        self.job_db.prompt(row,None)
        return result

    def test_batch_counts_and_status_are_consistent_across_pages(self):
        """概况、历史、模块和批次页统一显示真实数量，不把空记录显示为0。"""
        for path in ['/collections/products/123','/collections/products/123?module=history',
                     '/collections/products/123?module=parameters','/collections/batches/20']:
            self.page.goto('http://127.0.0.1'+path)
            expect(self.page.locator('main')).to_contain_text('2项参数')
            expect(self.page.locator('main')).not_to_contain_text('未确认覆盖范围')
        self.page.goto('http://127.0.0.1/collections/batches/21')
        expect(self.page.locator('main')).to_contain_text('部分完成')
        expect(self.page.locator('main')).not_to_contain_text('0项参数')
        self.assertEqual(self.errors,[])

    def test_batch_counts_and_status_are_consistent_across_pages(self):
        """概况、历史、模块和批次页统一显示真实数量，不把空记录显示为0。"""
        for path in ['/collections/products/123','/collections/products/123?module=history',
                     '/collections/products/123?module=parameters','/collections/batches/20']:
            self.page.goto('http://127.0.0.1'+path)
            expect(self.page.locator('main')).to_contain_text('2项参数')
            expect(self.page.locator('main')).not_to_contain_text('未确认覆盖范围')
        self.page.goto('http://127.0.0.1/collections/batches/21')
        expect(self.page.locator('main')).to_contain_text('部分完成')
        expect(self.page.locator('main')).not_to_contain_text('0项参数')
        self.assertEqual(self.errors,[])

    def test_continue_completion_and_new_result_link(self):
        row=self.launch();self.publish(row)
        modal=self.page.locator('#collection-attention');expect(modal).to_be_visible(timeout=7000)
        expect(modal).to_contain_text('等待人工登录淘宝')
        modal.get_by_role('button',name='继续',exact=True).click()
        expect(modal).not_to_be_visible()
        command=self.command(row)
        self.assertEqual((command['action'],command['prompt_id']),('continue','b'*32))
        row.update(state='succeeded',message='已确认无视频',result={'batch_id':20,'valid':True,'label':'查看采集结果'})
        self.save(row)
        video=self.page.locator('[data-collection-module="video"]')
        expect(video.get_by_role('link',name='查看采集结果 →')).to_be_visible(timeout=7000)
        expect(video.locator('.module-collect')).to_have_count(0)
        self.assertIn('batch=20',video.get_by_role('link',name='查看采集结果 →').get_attribute('href'))
        self.assertEqual(self.errors,[])

    def test_refresh_and_other_page_restore_native_dialog_and_stop(self):
        row=self.launch();self.publish(row,allowed=['确认','取消'])
        self.page.goto('http://127.0.0.1/tasks')
        modal=self.page.locator('#collection-attention');expect(modal).to_be_visible(timeout=7000)
        expect(modal.get_by_role('button',name='确认',exact=True)).to_be_visible()
        expect(modal.get_by_role('button',name='取消',exact=True)).to_be_visible()
        expect(modal.get_by_role('button',name='继续',exact=True)).to_have_count(0)
        self.page.reload();expect(modal).to_be_visible(timeout=7000)
        modal.get_by_role('button',name='停止',exact=True).click()
        self.assertEqual(self.command(row)['action'],'stop')
        self.assertEqual(self.errors,[])

    def test_partial_keeps_attempt_and_retry(self):
        row=self.launch()
        row.update(state='needs_attention',message='仅保存部分资料',result={'batch_id':21,'valid':False})
        self.save(row)
        video=self.page.locator('[data-collection-module="video"]')
        expect(video.get_by_role('link',name='查看本次采集尝试')).to_be_visible(timeout=7000)
        expect(video.get_by_role('button',name='重新采集')).to_be_enabled()
        expect(video).to_contain_text('仅保存部分资料')
        self.assertEqual(self.errors,[])

    def test_pending_halt_displays_real_reason_in_collection_and_logs_without_continue(self):
        """未入库中断在采集栏和日志页可见，停止前保持资源占用且不展示继续。"""
        from ecommerce.infrastructure.tasks.events import EventJournal
        row=self.launch()
        journal=EventJournal(self.settings,row['id'])
        journal.append({'op':'event','task_id':row['id'],'database':self.settings.mysql.database,
            'event_type':'session_halted','state':'needs_attention','step':'复核评价',
            'details':{'reason':'采集停止原因字段校验失败'}})
        self.job_db.prompt(row,{'halted':True,'message':'采集停止原因字段校验失败','allowed':[]})
        original=journal.path.read_bytes()
        self.page.reload()
        bar=self.page.locator('#collection-task-bar')
        expect(bar).to_contain_text('采集已中断',timeout=7000)
        expect(bar).to_contain_text('采集停止原因字段校验失败')
        expect(bar.get_by_role('button',name='停止采集')).to_be_visible()
        expect(self.page.locator('#collection-attention')).not_to_be_visible()
        expect(self.page.get_by_role('button',name='继续',exact=True)).to_have_count(0)
        self.page.goto('http://127.0.0.1/logs?task_id='+row['id'])
        notice=self.page.locator('[data-local-task-alert]')
        expect(notice).to_contain_text('采集已中断');expect(notice).to_contain_text('采集停止原因字段校验失败')
        self.assertEqual(journal.path.read_bytes(),original);self.assertFalse(journal.ack.exists())
        self.assertEqual(self.errors,[])

    def test_halted_bulk_task_keeps_completed_module_label_and_marks_only_current_module(self):
        """多模块任务中断不改写已完成模块的显示，当前模块仍显示中断原因。"""
        from ecommerce.infrastructure.tasks.events import EventJournal
        from ecommerce.modules.collection.web_collection import start_configured
        job=start_configured(self.settings,{'mode':'detail','input':'123','modules':['video','questions']})
        row=jobs.read(self.settings,job['id'])
        row.update(state='running',progress={'current_module':'questions','started_modules':['video','questions'],
            'results':[{'module':'video','valid':True,'batch_id':20,'label':'查看资料'}]})
        self.save(row)
        journal=EventJournal(self.settings,row['id'])
        journal.append({'op':'event','task_id':row['id'],'database':self.settings.mysql.database,
            'event_type':'session_halted','state':'needs_attention','details':{'reason':'保存问答失败'}})
        self.job_db.prompt(row,{'halted':True,'message':'保存问答失败','allowed':[]})
        self.page.goto('http://127.0.0.1/collections/products/123')
        video=self.page.locator('[data-collection-module="video"] .module-job-status')
        questions=self.page.locator('[data-collection-module="questions"] .module-job-status')
        expect(video).to_contain_text('采集完成',timeout=7000)
        expect(video).not_to_contain_text('中断')
        expect(questions).to_contain_text('采集已中断');expect(questions).to_contain_text('保存问答失败')
        self.assertEqual(self.errors,[])

    def test_basic_info_button_dispatches_separately_and_refresh_displays_own_data(self):
        """网页基本信息入口不采SKU，完成后从独立表读取。"""
        self.page.goto('http://127.0.0.1/collections/products/123')
        basic=self.page.locator('[data-collection-module="basic_info"]')
        basic.get_by_role('button',name='采集基本信息',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('基本信息')
        row=jobs.rows(self.settings)[0];self.assertEqual(row['module'],'basic_info')
        self.fixture.batch(80,'basic_info',product_id=1)
        self.fixture.put('product_info_observations',id=80,batch_id=80,product_id=1,module='basic_info',
                         title='独立保存的详情标题',shop_name='详情店铺',sales_text='已售 20+',price_text='¥9 起',captured_at=self.fixture.now,field_states={})
        self.fixture.put('product_current_sections',product_id=1,section='product_info',module='basic_info',batch_id=80)
        row.update(state='succeeded',message='基本信息已保存',result={'batch_id':80,'valid':True,'label':'查看资料'})
        self.save(row)
        expect(basic.get_by_role('link',name='查看资料 →')).to_be_visible(timeout=7000)
        self.page.reload()
        expect(basic).to_contain_text('独立保存的详情标题')
        expect(basic).to_contain_text('详情店铺')
        expect(basic.get_by_role('link',name='查看资料 →')).to_have_count(1)
        expect(self.page.locator('[data-collection-module="skus"]').get_by_role('button',name='采集 SKU',exact=True)).to_be_visible()
        self.assertEqual(self.errors,[])

    def test_collect_remaining_skips_global_valid_recovers_progress_and_retry_skips_saved_module(self):
        """旧来源页面仍按商品全局跳过，部分完成后重新点击只补剩余模块。"""
        self.page.goto('http://127.0.0.1/collections/products/123?observation=10')
        button=self.page.get_by_role('button',name='采集所有（剩余 8 项）',exact=True)
        button.click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('采集所有')
        row=jobs.rows(self.settings)[0]
        self.assertEqual(row['mode'],'remaining');self.assertNotIn('parameters',row['modules'])
        row.update(state='running',progress={'current_module':'basic_info','started_modules':['basic_info'],'results':[]})
        self.fixture.batch(80,'basic_info',product_id=1)
        self.fixture.put('product_info_observations',id=80,batch_id=80,product_id=1,module='basic_info',title='补采标题',captured_at=self.fixture.now,field_states={})
        self.fixture.put('product_current_sections',product_id=1,section='product_info',module='basic_info',batch_id=80)
        saved={'module':'basic_info','batch_id':80,'valid':True,'label':'查看资料'}
        row['progress'].update(current_module='skus',started_modules=['basic_info','skus'],results=[saved]);self.save(row)
        expect(self.page.locator('#collection-task-bar')).to_contain_text('已完成 1/8 项',timeout=7000)
        expect(self.page.locator('.collect-remaining')).to_be_enabled()
        self.page.reload()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('当前：SKU',timeout=7000)
        self.publish(row)
        modal=self.page.locator('#collection-attention');expect(modal).to_be_visible(timeout=7000)
        expect(modal).to_contain_text('采集所有')
        modal.get_by_role('button',name='停止',exact=True).click()
        self.assertEqual(self.command(row)['action'],'stop')
        row.update(state='cancelled',message='已停止，基本信息保留',result={'valid':False,'modules':[saved]});self.save(row)
        expect(self.page.get_by_role('button',name='采集所有（剩余 7 项）',exact=True)).to_be_enabled(timeout=7000)
        self.page.get_by_role('button',name='采集所有（剩余 7 项）',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('跳过 2 项',timeout=7000)
        latest=jobs.rows(self.settings)[0]
        self.assertNotIn('basic_info',latest['modules']);self.assertNotIn('parameters',latest['modules'])
        self.assertEqual(self.errors,[])

    def test_reclassified_sku_is_viewable_skipped_and_not_overwritten_by_old_job(self):
        """旧部分任务不覆盖新完成批次，缺货显示和补采计划使用同一有效结果。"""
        from ecommerce.modules.collection.web_collection import start
        job=start(self.settings,'123','skus');row=jobs.read(self.settings,job['id'])
        row.update(state='needs_attention',message='旧任务未确认有效资料',result={'batch_id':30,'valid':False})
        self.save(row)
        self.fixture.batch(30,'skus',product_id=1,status='partial')
        self.fixture.batch(31,'skus',product_id=1,derivation_kind='sku_stock_rule',derived_from_batch_id=30)
        self.fixture.put('product_current_sections',product_id=1,section='sku_details',module='skus',batch_id=31)
        self.fixture.put('sku_observations',id=31,batch_id=31,position=1,sku_name='替换装',
                         stock_status='out_of_stock',price_status='out_of_stock',price_text=None,price_value=None)
        self.page.goto('http://127.0.0.1/collections/products/123')
        sku=self.page.locator('[data-collection-module="skus"]')
        expect(sku.get_by_role('link',name='查看资料 →',exact=True)).to_be_visible()
        self.page.wait_for_function("document.querySelector('[data-collection-module=skus]').dataset.latestBatch==='31'")
        self.page.wait_for_timeout(2200)
        expect(sku.locator('.module-job-status')).to_be_empty()
        response=self.fixture.client.get('/api/collections/products/123/jobs').get_json()
        self.assertIn('skus',response['plan']['skipped_modules'])
        self.assertNotIn('skus',response['plan']['modules'])
        sku.get_by_role('link',name='查看资料 →',exact=True).click()
        expect(self.page.locator('.batch-summary')).to_contain_text('从 SKU 批次 30 重新归类')
        expect(self.page.locator('table')).to_contain_text('缺货，未取得价格')
        expect(self.page.locator('table')).to_contain_text('库存状态：明确缺货')
        self.assertEqual(self.errors,[])

    def test_sku_images_stay_in_rows_when_sorting_paging_and_opening_original(self):
        """图片按规格外键跟随列表，统一20条分页并能打开本地原件。"""
        import hashlib
        from tests.integration.test_taobao_detail_skus_browser import png
        self.fixture.batch(70,'skus',product_id=1)
        self.fixture.put('product_current_sections',product_id=1,section='sku_details',module='skus',batch_id=70)
        root=self.settings.workspace/'output/sku-list-browser';root.mkdir(parents=True)
        for i in range(1,22):
            path=root/f'{i}.png';path.write_bytes(png(i%12+1))
            self.fixture.put('sku_observations',id=700+i,batch_id=70,position=i,sku_name=f'规格{i}',price_value=i,
                price_text=f'¥{i}',image_status='downloaded',stock_status='selectable',price_status='collected')
            self.fixture.put('file_assets',id=800+i,root_key='workspace',relative_path=path.relative_to(self.settings.workspace).as_posix(),
                availability='present',size_bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            self.fixture.put('media_references',id=900+i,batch_id=70,role='sku_image',position=100-i,
                sku_observation_id=700+i,file_asset_id=800+i,download_status='downloaded')
        self.page.goto('http://127.0.0.1/collections/products/123?module=skus&sort=price&order=desc')
        rows=self.page.locator('tbody tr');expect(rows).to_have_count(20)
        expect(rows.first.locator('.sku-thumbnail')).to_have_attribute('src','/collections/files/821')
        expect(self.page.locator('.media-grid,.media-sort')).to_have_count(0)
        expect(self.page.locator('.numbered-pagination')).to_have_count(1)
        self.page.get_by_role('link',name='价格 / 来源信息').click()
        expect(rows.first.locator('.sku-thumbnail')).to_have_attribute('src','/collections/files/801')
        self.page.wait_for_function("document.querySelector('.sku-thumbnail').naturalWidth > 0")
        size=rows.first.locator('.sku-thumbnail').evaluate("e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,fit:getComputedStyle(e).objectFit})")
        self.assertEqual(size,{'width':96,'height':96,'fit':'contain'})
        rows.first.get_by_role('link',name='查看规格图：规格1',exact=True).click()
        preview=self.page.locator('#image-preview');expect(preview).to_be_visible()
        with self.page.expect_popup() as opened:
            preview.get_by_role('link',name='打开原图').click()
        original=opened.value;original.wait_for_load_state()
        self.assertTrue(original.url.endswith('/collections/files/801'));original.close()
        preview.get_by_role('button',name='关闭图片预览').click()
        self.page.get_by_role('link',name='下一页',exact=True).click()
        expect(rows).to_have_count(1)
        expect(rows.first.locator('.sku-thumbnail')).to_have_attribute('src','/collections/files/821')
        expect(self.page.get_by_text('第 2 / 2 页 · 共 21 条 · 每页 20 条')).to_be_visible()
        self.assertEqual(self.errors,[])

    def test_all_nine_complete_disables_bulk_button_without_dispatch(self):
        """九个有效模块时按钮置灰，不生成后台派发。"""
        from ecommerce.modules.collection.web_collection import MODULES
        for index,module in enumerate(MODULES):
            identifier=100+index
            self.fixture.batch(identifier,module,product_id=1)
            self.fixture.put('product_current_sections',product_id=1,section='product_info' if module=='basic_info' else 'sku_details' if module=='skus' else module,
                             module=module,batch_id=identifier)
        self.page.goto('http://127.0.0.1/collections/products/123')
        expect(self.page.get_by_role('button',name='已全部采集',exact=True)).to_be_disabled()
        self.assertEqual(jobs.rows(self.settings),[])
        self.assertEqual(self.errors,[])

    def test_running_task_accepts_another_request_into_database_queue(self):
        """当前任务继续，网页新申请入库排队，不抢占已有浏览器。"""
        row=self.launch()
        self.page.goto('http://127.0.0.1/modules/collection')
        self.page.get_by_label('搜索关键词',exact=True).fill('隔离排队样例')
        self.page.get_by_role('button',name='加入采集队列',exact=True).click()
        expect(self.page.locator('#collection-queue-list')).to_contain_text('隔离排队样例',timeout=7000)
        queued=jobs.rows(self.settings)
        self.assertEqual(len(queued),2)
        self.assertEqual(next(item for item in queued if item['id']!=row['id'])['state'],'queued')
        self.assertEqual(jobs.read(self.settings,row['id'])['state'],'running')
        self.assertEqual(self.errors,[])

    def test_database_outage_keeps_explicit_emergency_stop_for_known_execution(self):
        """断库显示最后确认时间，新提交禁用，已知执行仍有紧急停止。"""
        from ecommerce.infrastructure.storage.mysql import MySQLStorageError
        row=self.launch()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('采集中',timeout=7000)
        with patch('ecommerce.modules.collection.web_collection.active',side_effect=MySQLStorageError('合成断库')),patch(
                'ecommerce.infrastructure.tasks.web_jobs.read',side_effect=MySQLStorageError('合成断库')),patch(
                'ecommerce.infrastructure.tasks.collection_control.emergency_stop',return_value='合成紧急停止已保存') as stop:
            bar=self.page.locator('#collection-task-bar')
            expect(bar).to_contain_text('最后确认时间',timeout=7000)
            bar.get_by_role('button',name='紧急停止已知采集',exact=True).click()
            expect(bar).to_contain_text('合成紧急停止已保存')
            self.assertEqual(stop.call_args.args[1],row['id'])
        self.assertEqual(self.errors,[])

    def paused_job(self,product='123'):
        """准备未启动的暂停申请；参数：商品编号；返回：申请。"""
        from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
        from ecommerce.infrastructure.tasks.ownership import execution_key
        from ecommerce.modules.collection import web_collection
        CollectionJobStore(self.settings).pause(execution_key(self.settings.for_platform('taobao')),True,'合成用户暂停')
        return web_collection.start_configured(self.settings,{'mode':'detail','input':product,'modules':['video']})

    def test_paused_card_starts_only_selected_request_and_double_click_does_not_duplicate(self):
        first=self.paused_job();second=self.paused_job('456')
        with patch.object(jobs,'launch',return_value=SimpleNamespace(pid=9999)) as launch:
            self.page.goto('http://127.0.0.1/modules/collection')
            card=self.page.locator(f'[data-job-id="{first["id"]}"]')
            button=card.get_by_role('button',name='开始本次采集',exact=True)
            expect(button).to_be_enabled()
            expect(self.page.locator('#collection-submit')).to_have_text('保存采集申请')
            button.evaluate('node=>{node.click();node.click();}')
            expect(card).to_contain_text('启动中',timeout=7000)
            expect(card.get_by_role('button',name='开始本次采集',exact=True)).to_have_count(0)
            self.assertEqual(jobs.read(self.settings,second['id'])['dispatch_state'],'paused')
            self.assertEqual(len(jobs.rows(self.settings)),2)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[1],first['id'])
        self.assertEqual(self.errors,[])

    def test_paused_task_bar_exposes_start_without_resuming_queue(self):
        job=self.paused_job()
        from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
        from ecommerce.infrastructure.tasks.ownership import execution_key
        with patch.object(jobs,'launch',return_value=SimpleNamespace(pid=9999)) as launch:
            self.page.goto('http://127.0.0.1/modules/collection')
            bar=self.page.locator('#collection-task-bar')
            expect(bar.get_by_role('button',name='开始本次采集',exact=True)).to_be_visible()
            bar.get_by_role('button',name='开始本次采集',exact=True).click()
            expect(bar).to_contain_text('启动中',timeout=7000)
            self.assertTrue(CollectionJobStore(self.settings).queue_state(execution_key(self.settings.for_platform('taobao')))['paused'])
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[1],job['id'])
        self.assertEqual(self.errors,[])

    def test_start_busy_error_is_visible_and_saved_request_remains_available(self):
        job=self.paused_job()
        from ecommerce.modules.collection import dispatch
        from ecommerce.shared.exceptions import TaskBusy
        with patch.object(dispatch,'start_job',side_effect=TaskBusy('浏览器繁忙，本次申请保留等待')):
            self.page.goto('http://127.0.0.1/modules/collection')
            card=self.page.locator(f'[data-job-id="{job["id"]}"]')
            card.get_by_role('button',name='开始本次采集',exact=True).click()
            expect(card.get_by_role('alert')).to_contain_text('保留等待')
            expect(card.get_by_role('button',name='开始本次采集',exact=True)).to_be_enabled()
            self.assertEqual(jobs.read(self.settings,job['id'])['dispatch_state'],'paused')
        self.assertEqual(self.errors,[])

    def history_requests(self,count):
        """准备超过一页的合成暂停申请；参数：数量；返回：按数据库排序的申请。"""
        from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
        from ecommerce.infrastructure.tasks.ownership import execution_key
        from ecommerce.modules.collection import web_collection
        store=CollectionJobStore(self.settings)
        key=execution_key(self.settings.for_platform('taobao'))
        store.pause(key,True,'合成分页暂停')
        for number in range(count):
            web_collection.start_configured(self.settings,{'mode':'detail','input':str(500000+number),'modules':['video']})
        return store.rows(key,limit=count)

    def test_history_pages_show_all_requests_and_preserve_filter_on_reload_and_refresh(self):
        """45条申请可完整翻页，自动刷新及整页重载保持页码、筛选和原详情输入。"""
        rows=self.history_requests(45)
        with patch.object(jobs,'launch') as launch:
            self.page.goto('http://127.0.0.1/modules/collection?mode=detail&input=123&jobs_page=2&jobs_status=waiting')
            cards=self.page.locator('#collection-job-list .collection-job')
            expect(cards).to_have_count(20)
            expect(self.page.locator('#collection-history-page')).to_have_text('第 2 / 3 页')
            self.assertEqual(cards.evaluate_all('nodes=>nodes.map(node=>node.dataset.jobId)'),[row['id'] for row in rows[20:40]])
            self.page.locator('#collection-history-next').click()
            expect(cards).to_have_count(5)
            expect(self.page.locator('#collection-history-page')).to_have_text('第 3 / 3 页')
            expect(self.page.locator('#collection-history-next')).to_be_disabled()
            self.assertEqual(cards.evaluate_all('nodes=>nodes.map(node=>node.dataset.jobId)'),[row['id'] for row in rows[40:]])
            self.page.locator('#collection-history-prev').click()
            expect(cards).to_have_count(20)
            self.page.reload()
            expect(self.page.locator('#collection-history-filter')).to_have_value('waiting')
            expect(self.page.locator('#collection-history-page')).to_have_text('第 2 / 3 页')
            expect(self.page.locator('#collection-input')).to_have_value('123')
            self.paused_job('999999')
            expect(self.page.locator('#collection-history-status')).to_contain_text('共 46 条',timeout=7000)
            expect(self.page.locator('#collection-history-page')).to_have_text('第 2 / 3 页')
            self.assertIn('jobs_status=waiting',self.page.url)
            launch.assert_not_called()
        self.assertEqual(self.errors,[])

    def test_history_filter_keeps_off_page_active_task_and_stopped_recovery_actions(self):
        """历史页筛选不隐藏独立运行状态；已停止任务仍可打开恢复入口。"""
        rows=self.history_requests(25)
        running=rows[-1];running.update(state='running',recovery_ready=True);self.save(running)
        self.page.goto('http://127.0.0.1/modules/collection?jobs_page=2&jobs_status=waiting')
        bar=self.page.locator('#collection-task-bar')
        expect(bar).to_contain_text(f'商品 {running["product_id"]}')
        expect(bar).to_contain_text('采集中')
        expect(self.page.locator('#collection-history-page')).to_have_text('第 2 / 2 页')
        expect(self.page.locator('#collection-job-list [data-start-job]').first).to_be_disabled()
        self.page.locator('#collection-history-filter').select_option('stopped')
        expect(self.page.locator('#collection-history-status')).to_contain_text('该状态下暂无')
        expect(self.page.locator('#collection-job-list .collection-job')).to_have_count(0)
        expect(bar).to_contain_text('采集中')
        running['state']='cancelled';self.save(running)
        card=self.page.locator(f'[data-job-id="{running["id"]}"]')
        expect(card).to_contain_text('已停止',timeout=7000)
        expect(card.get_by_role('button',name='查看恢复计划',exact=True)).to_be_visible()
        expect(card.locator('[data-start-job]')).to_have_count(0)
        self.assertEqual(self.errors,[])

    def test_history_last_page_clamps_when_filtered_records_leave_and_mobile_does_not_overflow(self):
        """最后一页因状态变更消失时回到有效页；移动布局不横向溢出。"""
        rows=self.history_requests(21)
        self.page.goto('http://127.0.0.1/modules/collection?jobs_page=2&jobs_status=waiting')
        expect(self.page.locator('#collection-history-page')).to_have_text('第 2 / 2 页')
        self.save({**rows[-1],'state':'cancelled'})
        expect(self.page.locator('#collection-history-page')).to_have_text('第 1 / 1 页',timeout=7000)
        self.assertIn('jobs_page=1',self.page.url)
        self.page.set_viewport_size({'width':390,'height':844})
        controls=self.page.locator('.collection-history-controls')
        controls.scroll_into_view_if_needed()
        expect(controls.get_by_label('任务状态')).to_be_visible()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
        self.assertEqual(self.errors,[])

    def test_history_fast_filter_changes_do_not_render_stale_response(self):
        """等待请求延迟时切换到已停止，不被旧响应覆盖筛选和任务列表。"""
        rows=self.history_requests(2)
        self.save({**rows[-1],'state':'cancelled'})
        self.page.goto('http://127.0.0.1/modules/collection')
        expect(self.page.locator('#collection-job-list .collection-job')).to_have_count(2)
        self.page.evaluate("""() => {
          const original=window.fetch;
          window.fetch=async (...args) => {
            const response=await original(...args);
            if (String(args[0]).includes('/api/collection-jobs?page=1&status=waiting')) await new Promise(resolve=>setTimeout(resolve,600));
            return response;
          };
        }""")
        with self.page.expect_request('**/api/collection-jobs?page=1&status=waiting'):
            self.page.locator('#collection-history-filter').select_option('waiting')
        self.page.locator('#collection-history-filter').select_option('stopped')
        expect(self.page.locator('#collection-history-status')).to_contain_text('共 1 条',timeout=7000)
        expect(self.page.locator('#collection-history-filter')).to_have_value('stopped')
        cards=self.page.locator('#collection-job-list .collection-job')
        expect(cards).to_have_count(1)
        expect(cards.first).to_contain_text('已停止')
        self.assertIn('jobs_status=stopped',self.page.url)
        self.assertEqual(self.errors,[])

    def test_history_completed_filter_keeps_unconfirmed_result_warning(self):
        """执行结束但资料未核实仍保留警告，不因完成筛选显示成正在运行。"""
        job=self.paused_job()
        self.save({**jobs.read(self.settings,job['id']),'state':'succeeded'})
        self.page.goto('http://127.0.0.1/modules/collection?jobs_status=completed')
        card=self.page.locator(f'[data-job-id="{job["id"]}"]')
        expect(card).to_contain_text('已结束（结果待核对）')
        expect(card).to_contain_text('执行已结束，尚未确认完整结果')
        expect(card.locator('[data-start-job]')).to_have_count(0)
        expect(self.page.locator('#collection-task-bar')).not_to_be_visible()
        self.assertEqual(self.errors,[])

    def test_task_description_stays_open_and_scrolled_across_updates_until_manually_closed(self):
        """自动刷新更新真实说明文本时保留展开、内部阅读位置及键盘焦点，手动收起同样保留。"""
        job=self.paused_job()
        row=jobs.read(self.settings,job['id'])
        note=('合成任务说明：保留阅读状态与原错误信息。\n'*80)
        row.update(state='failed',message=note+'初始说明');self.save(row)
        self.page.goto('http://127.0.0.1/modules/collection?jobs_status=failed')
        details=self.page.locator(f'[data-job-id="{job["id"]}"] details')
        summary=details.locator('summary')
        expect(summary).to_have_text('查看任务说明')
        summary.click()
        expect(details).to_have_js_property('open',True)
        details.locator('p').evaluate('node=>node.scrollTop=120')
        for update in ('第一次刷新','第二次刷新'):
            row['message']=note+update;self.save(row)
            expect(details.locator('p')).to_contain_text(update,timeout=7000)
            expect(details).to_have_js_property('open',True)
            self.assertEqual(details.locator('p').evaluate('node=>node.scrollTop'),120)
            expect(summary).to_be_focused()
        summary.click()
        row['message']=note+'手动收起后刷新';self.save(row)
        expect(details.locator('p')).to_contain_text('手动收起后刷新',timeout=7000)
        expect(details).to_have_js_property('open',False)
        self.assertEqual(self.errors,[])

    def test_task_description_state_is_keyed_by_request_when_paging_and_filtering(self):
        """不同任务的说明互不串用，翻页返回及筛选后保留各自展开或收起。"""
        rows=self.history_requests(21)
        note='合成分页任务说明。'*30
        for row in (rows[0],rows[-1]):
            self.save({**row,'state':'failed','message':note})
        self.page.goto('http://127.0.0.1/modules/collection')
        first=self.page.locator(f'[data-job-id="{rows[0]["id"]}"] details')
        expect(first.locator('summary')).to_be_attached()
        first.locator('summary').click()
        expect(first).to_have_js_property('open',True)
        self.page.locator('#collection-history-next').click()
        other=self.page.locator(f'[data-job-id="{rows[-1]["id"]}"] details')
        expect(other).to_have_js_property('open',False)
        other.locator('summary').click()
        expect(other).to_have_js_property('open',True)
        self.page.locator('#collection-history-prev').click()
        expect(first).to_have_js_property('open',True)
        first.locator('summary').click()
        self.page.locator('#collection-history-filter').select_option('waiting')
        expect(self.page.locator('#collection-history-status')).to_contain_text('共 19 条')
        self.page.locator('#collection-history-filter').select_option('failed')
        expect(first).to_have_js_property('open',False)
        expect(other).to_have_js_property('open',True)
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
