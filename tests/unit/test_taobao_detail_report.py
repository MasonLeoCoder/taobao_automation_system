"""验证SKU警告与详情调度的协作、阻断边界及可定位的持久化汇总。"""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from ecommerce.modules.collection import detail_service
from ecommerce.modules.collection.detail_skus import sku_continuation_allowed
from ecommerce.modules.collection.detail_storage import save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped

ITEM_ID='1050158363561'


def sku_result(folder,*,bad=None):
    """模拟已完成枚举且只有一项不可选的真实结果口径。"""
    rows=[{'index':1,'name':'正常规格','combination_confirmed':True,'stock_status':'selectable',
           'price_status':'collected','price_value':'3.35','image_status':'downloaded'},
          {'index':2,'name':'赠退货宝【运费险】','combination_confirmed':True,'stock_status':'unavailable_unconfirmed',
           'price_status':'unavailable_unconfirmed','price':None,'price_value':None,
           'unavailability_evidence':['aria-disabled=true'],'image_status':'downloaded'}]
    if bad:
        rows[0].update(bad)
    return {'status':'partial','candidate_count':2,'skus':rows,'continuation_allowed':True,
            'message':'SKU采集部分完成：价格1项，不可选原因未确认1项，图片2张',
            'record_path':str(folder/f'{ITEM_ID}.json'),
            'history_record_path':str(folder/'history/sku-batch/sku_details.json'),
            'image_directory':str(folder/'history/sku-batch/sku图'),'result_section':'sku_details_last_attempt',
            'warnings':[{'code':'sku_unavailable_unconfirmed','sku_name':rows[1]['name'],'message':'规格不可选，原因未确认'}]}


class DetailReportTests(unittest.TestCase):
    def test_missing_question_entry_finishes_with_explicit_skip_and_no_module_record(self):
        """入口跳过写入汇总且允许任务结束，不伪造零条问答批次；参数：无；返回：无。"""
        value={'status':'skipped','stop_reason':'entry_not_found','message':'未找到问大家入口，已跳过',
               'entry_search':{'scroll_count':10,'max_scrolls':10,'unchanged_count':0,'stop_reason':'scroll_limit_reached'},
               'warnings':[{'code':'questions_entry_not_found','message':'未找到问大家入口，已跳过'}]}
        with ExitStack() as stack:
            self.setup_service(stack)
            collect=stack.enter_context(patch('ecommerce.modules.collection.detail_questions.collect_questions',return_value=value))
            message=detail_service.open_detail_input(self.settings,ITEM_ID,wait=False,questions=True)
        collect.assert_called_once()
        report=self.report();step=next(s for s in report['steps'] if s['name']=='questions')
        self.assertEqual(step['status'],'skipped');self.assertTrue(step['continuation_allowed'])
        self.assertEqual(step['entry_search']['scroll_count'],10)
        self.assertEqual(report['status'],'complete_with_warnings')
        self.assertIn('含跳过',message)
        self.assertFalse(list(self.folder.rglob('questions.json')))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=replace(load_settings(root=Path(self.temp.name)).for_platform('taobao'),headless=True)
        self.folder=self.settings.workspace/'output/raw/taobao_details'/ITEM_ID
        self.page=MagicMock();self.page.url=resolve_detail_input(ITEM_ID).url
        self.page.is_closed.return_value=False
        self.result=DetailPage(self.page,resolve_detail_input(ITEM_ID),'测试详情')
        self.guard=MagicMock();self.guard.task_id='test-report-task'
        self.guard.perform.side_effect=lambda step,action,**kwargs:action()
        self.output=io.StringIO()

    def setup_service(self,stack):
        browser=stack.enter_context(patch.object(detail_service,'browser_session'))
        browser.return_value.__enter__.return_value=(MagicMock(),self.page)
        stack.enter_context(patch.object(detail_service,'authenticate',return_value=self.page))
        stack.enter_context(patch.object(detail_service,'open_detail',return_value=self.result))
        stack.enter_context(patch.object(detail_service,'inspect_detail_entry',return_value=self.result))
        stack.enter_context(patch.object(detail_service,'save_session'))
        stack.enter_context(patch.object(detail_service,'active_guard',return_value=self.guard))
        stack.enter_context(patch('ecommerce.platforms.taobao.detail.recover_detail_page',return_value=self.result))
        stack.enter_context(redirect_stdout(self.output))

    def report(self):
        return json.loads((self.folder/f'{ITEM_ID}.json').read_text(encoding='utf-8'))['collection_summary']

    def test_warning_only_sku_continues_all_other_requested_modules_and_reports_paths(self):
        calls=[]
        specs=[('detail_video','collect_main_video','video'),('detail_images','collect_main_images','main_images'),
               ('detail_skus','collect_skus','skus'),('detail_parameters','collect_parameters','parameters'),
               ('detail_description','collect_detail_images','detail_images'),('detail_review_summary','collect_review_summary','review_summary'),
               ('detail_reviews','collect_reviews','reviews'),('detail_questions','collect_questions','questions')]
        with ExitStack() as stack:
            self.setup_service(stack)
            for module,function,name in specs:
                value=sku_result(self.folder) if name=='skus' else {'status':'complete','video_status':'downloaded','message':name+'完成','record_path':str(self.folder/f'{ITEM_ID}.json')}
                def action(*args,_name=name,_value=value,**kwargs):
                    calls.append(_name);return _value
                stack.enter_context(patch(f'ecommerce.modules.collection.{module}.{function}',side_effect=action))
            message=detail_service.open_detail_input(self.settings,ITEM_ID,wait=False,**{s[2]:True for s in specs})
        self.assertEqual(calls,[s[2] for s in specs])
        report=self.report()
        self.assertEqual(report['status'],'complete_with_errors')
        sku=next(s for s in report['steps'] if s['name']=='skus')
        self.assertEqual(sku['status'],'failed_skipped')
        self.assertTrue(sku['continuation_allowed'])
        self.assertEqual(sku['image_directory'],str(self.folder/'history/sku-batch/sku图'))
        self.assertIn('存在模块错误',message)
        self.assertIn('赠退货宝',self.output.getvalue())
        self.assertIn('本次图片目录',self.output.getvalue())
        history=json.loads((self.folder/report['history_record_file']).read_text(encoding='utf-8'))
        self.assertEqual(history,report)

    def test_image_failure_cannot_be_relabelled_as_non_blocking_warning(self):
        with ExitStack() as stack:
            self.setup_service(stack)
            stack.enter_context(patch('ecommerce.modules.collection.detail_skus.collect_skus',return_value=sku_result(self.folder,bad={'image_status':'download_failed'})))
            later=stack.enter_context(patch('ecommerce.modules.collection.detail_parameters.collect_parameters',return_value={'status':'complete','message':'参数完成'}))
            detail_service.open_detail_input(self.settings,ITEM_ID,wait=False,skus=True,parameters=True)
        later.assert_called_once()
        report=self.report()
        self.assertEqual(report['status'],'complete_with_errors')
        self.assertEqual(next(s for s in report['steps'] if s['name']=='skus')['status'],'failed_skipped')
        self.assertEqual(next(s for s in report['steps'] if s['name']=='parameters')['status'],'complete')

    def test_unconfirmed_selected_price_and_combination_still_block_continuation(self):
        for bad in ({'price_status':'not_read'},{'price_value':None},{'combination_confirmed':False}):
            with self.subTest(bad=bad):
                self.assertFalse(sku_continuation_allowed(sku_result(self.folder,bad=bad)))

    def test_unknown_disabled_reason_requires_actual_disabled_evidence(self):
        value=sku_result(self.folder)
        value['skus'][1]['unavailability_evidence']=[]
        self.assertFalse(sku_continuation_allowed(value))
        value['skus'][1]['unavailability_evidence']=['disabled属性']
        value['skus'][1]['price_value']='0'
        self.assertFalse(sku_continuation_allowed(value))

    def test_security_or_cancellation_is_persisted_and_never_allows_later_module(self):
        for error,status in ((NeedsAttention('人工输入不可用'),'needs_attention'),(TaskStopped('停止'),'interrupted')):
            with self.subTest(status=status),ExitStack() as stack:
                self.setup_service(stack)
                stack.enter_context(patch('ecommerce.modules.collection.detail_skus.collect_skus',side_effect=error))
                later=stack.enter_context(patch('ecommerce.modules.collection.detail_parameters.collect_parameters'))
                with self.assertRaises(type(error)):
                    detail_service.open_detail_input(self.settings,ITEM_ID,wait=False,skus=True,parameters=True)
                later.assert_not_called()
                self.assertEqual(self.report()['status'],status)
                self.assertEqual(next(s for s in self.report()['steps'] if s['name']=='skus')['status'],status)

    def test_summary_is_final_before_waiting_and_unrequested_steps_are_explicit(self):
        self.settings=replace(self.settings,headless=False)
        with ExitStack() as stack:
            self.setup_service(stack)
            stack.enter_context(patch('ecommerce.modules.collection.detail_skus.collect_skus',return_value=sku_result(self.folder)))
            def hold(_):
                self.assertEqual(self.report()['status'],'complete_with_errors')
            held=stack.enter_context(patch.object(detail_service,'keep_detail_open',side_effect=hold))
            detail_service.open_detail_input(self.settings,ITEM_ID,skus=True)
        held.assert_called_once()
        self.assertTrue(all(s['status']=='not_requested' for s in self.report()['steps'] if s['name']!='skus'))

    def test_interrupted_sku_reports_current_progress_path_and_preserves_old_complete_data(self):
        old={'status':'complete','batch_id':'old-complete'}
        save_detail_section(self.folder,ITEM_ID,'sku_details',old)
        progress=self.folder/'history/current-sku/sku_progress.json'
        def collect(*_args,**_kwargs):
            progress.parent.mkdir(parents=True,exist_ok=True)
            (progress.parent/'sku图').mkdir()
            value={'product_id':ITEM_ID,'task_id':self.guard.task_id,'status':'interrupted',
                   'progress_record_file':'history/current-sku/sku_progress.json'}
            progress.write_text(json.dumps(value),encoding='utf-8')
            save_detail_section(self.folder,ITEM_ID,'sku_details_last_attempt',value)
            raise TaskStopped('停止')
        with ExitStack() as stack:
            self.setup_service(stack)
            stack.enter_context(patch('ecommerce.modules.collection.detail_skus.collect_skus',side_effect=collect))
            with self.assertRaises(TaskStopped):
                detail_service.open_detail_input(self.settings,ITEM_ID,wait=False,skus=True)
        step=next(s for s in self.report()['steps'] if s['name']=='skus')
        self.assertEqual(step['progress_record_path'],str(progress.resolve()))
        self.assertIn('本次进度记录',self.output.getvalue())
        document=json.loads((self.folder/f'{ITEM_ID}.json').read_text(encoding='utf-8'))
        self.assertEqual(document['sku_details'],old)

    def test_interrupted_new_task_does_not_report_previous_task_progress_as_current(self):
        save_detail_section(self.folder,ITEM_ID,'sku_details_last_attempt',{'task_id':'another-task',
                            'progress_record_file':'history/old/sku_progress.json'})
        with ExitStack() as stack:
            self.setup_service(stack)
            stack.enter_context(patch('ecommerce.modules.collection.detail_skus.collect_skus',side_effect=TaskStopped('停止')))
            with self.assertRaises(TaskStopped):
                detail_service.open_detail_input(self.settings,ITEM_ID,wait=False,skus=True)
        step=next(s for s in self.report()['steps'] if s['name']=='skus')
        self.assertNotIn('progress_record_path',step)


if __name__=='__main__':
    unittest.main()
