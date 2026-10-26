"""验证真实模块调度顺序、跳过、入库检查点和中断边界，不启动浏览器。"""
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from ecommerce.modules.collection.detail_service import collect_detail_page
from ecommerce.platforms.taobao.detail import resolve_detail_input
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention,TaskStopped


class RemainingPipelineTests(unittest.TestCase):
    def setUp(self):
        """建立真实汇总文件和浏览器替身；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=load_settings(root=Path(temp.name))
        page=MagicMock(url='https://item.taobao.com/item.htm?id=123')
        self.detail=SimpleNamespace(page=page,target=resolve_detail_input('123'),document_title='本地详情')
        self.guard=MagicMock(task_id='a'*32)
        self.guard.perform.side_effect=lambda label,action,**kw:action()
        self.events=[]
        self.guard.checkpoint.side_effect=lambda label:self.events.append(('checkpoint',label))
        self.called=[];self.interrupt=None;self.partial=False
        def collect(name):
            def action(settings,result,**kw):
                self.assertIs(result,self.detail);self.called.append(name)
                if name==self.interrupt:raise TaskStopped('本地停止')
                return {'status':'partial' if self.partial and name=='skus' else 'complete','video_status':'no_video',
                        'message':'本地模块结果','record_path':str(self.settings.workspace/'fixture.json')}
            return action
        self.module=SimpleNamespace(collect_main_video=collect('video'),collect_skus=collect('skus'),collect_questions=collect('questions'),
                                     sku_continuation_allowed=lambda value:value['status']=='complete')

    def run_pipeline(self):
        """执行三个选中模块，其他六项不请求；参数：无；返回：真实汇总结果。"""
        with patch('ecommerce.modules.collection.detail_service.active_guard',return_value=self.guard),patch(
                'ecommerce.modules.collection.detail_service.inspect_detail_entry',return_value=self.detail),patch(
                'ecommerce.modules.collection.detail_service.save_session'),patch(
                'ecommerce.modules.collection.detail_service.import_module',return_value=self.module):
            return collect_detail_page(self.settings,object(),self.detail,basic_info=False,video=True,main_images=False,
                skus=True,parameters=False,detail_images=False,review_summary=False,reviews=False,questions=True,
                on_module_event=lambda module,stage:self.events.append((module,stage)))

    def test_only_selected_modules_run_and_finished_progress_follows_storage_checkpoint(self):
        result=self.run_pipeline()
        self.assertEqual(self.called,['video','skus','questions'])
        for module in self.called:
            position=self.events.index((module,'finished'))
            self.assertEqual(self.events[position-1],('checkpoint','本模块资料及汇总已确认入库'))
        report=json.loads(next((self.settings.workspace/'output/raw/taobao_details/123/history').glob('*/collection_summary.json')).read_text(encoding='utf-8'))
        self.assertEqual(len(report['steps']),9)
        self.assertEqual({item['name'] for item in report['steps'] if item['requested']},{'video','skus','questions'})
        self.assertEqual(result['status'],'complete')

    def test_partial_module_stops_later_module_and_keeps_earlier_progress(self):
        self.partial=True
        with self.assertRaises(NeedsAttention):self.run_pipeline()
        self.assertEqual(self.called,['video','skus'])
        self.assertIn(('video','finished'),self.events)
        self.assertIn(('skus','finished'),self.events)
        self.assertNotIn(('questions','running'),self.events)

    def test_manual_stop_does_not_replay_module_or_run_later_steps(self):
        self.interrupt='skus'
        with self.assertRaises(TaskStopped):self.run_pipeline()
        self.assertEqual(self.called,['video','skus'])
        self.assertIn(('video','finished'),self.events)
        self.assertNotIn(('skus','finished'),self.events)


if __name__=='__main__':unittest.main()
