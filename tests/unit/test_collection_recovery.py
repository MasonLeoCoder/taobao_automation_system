"""恢复、模块隔离与停止边界；不访问真实平台或业务数据库。"""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import tempfile
import time
import unittest

from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import ModuleBlocked, BrowserClosed, NeedsAttention
from ecommerce.platforms.taobao.verification import TaobaoGuard, Blockage
from ecommerce.modules.collection.recovery import RecoverySession
from ecommerce.modules.collection import dispatch


class ModuleIsolationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.page=MagicMock(url='https://item.taobao.com/item.htm?id=123');self.page.is_closed.return_value=False
        self.context=MagicMock(pages=[self.page])
        self.guard=TaobaoGuard(self.context,Path(temp.name)/'logs',lambda message:'继续')
        self.guard._scan=MagicMock(side_effect=lambda:self.guard._pending[0] if self.guard._pending else None)
        self.guard._capture=MagicMock(return_value='local-evidence')

    def test_business_attention_becomes_module_error_without_manual_wait(self):
        self.guard.on_attention=MagicMock(side_effect=AssertionError('不能等待人工'))
        with self.guard.collection_module('reviews',self.page):
            with self.assertRaises(ModuleBlocked):
                self.guard.request_attention(self.page,'未知评价结构')
        self.guard.on_attention.assert_not_called()
        self.assertEqual(self.guard._pending,[])
        self.assertIsNone(self.guard.failure)

    def test_verification_still_requires_explicit_feedback_inside_module(self):
        challenge=Blockage('verification',self.page,'安全验证')
        self.guard._scan.side_effect=[challenge,None]
        self.guard._reply=MagicMock(return_value='继续')
        with self.guard.collection_module('reviews',self.page):
            self.guard.checkpoint('读取评价')
        self.guard._reply.assert_called_once()
        self.assertEqual(self.guard._reply.call_args.args[1],{'继续'})

    def test_no_progress_timeout_skips_module_and_real_progress_resets_budget(self):
        with self.guard.collection_module('video',self.page,1000):
            self.guard._module_progress_at=time.monotonic()-2
            with self.assertRaises(ModuleBlocked):self.guard.checkpoint()
            self.guard.note_module_progress(counts={'images':2})
            self.guard.checkpoint()
        self.assertEqual(self.guard.last_module_diagnostics['counts']['images'],2)
        self.assertTrue(self.guard.last_module_diagnostics['last_progress_at'])

    def test_manual_pause_time_does_not_consume_module_budget(self):
        with self.guard.collection_module('reviews',self.page,1000):
            self.guard._module_progress_at=time.monotonic()-10
            self.guard.paused_seconds=10
            self.guard.checkpoint()

    def test_closed_business_page_stops_whole_execution(self):
        with self.guard.collection_module('parameters',self.page):
            self.page.is_closed.return_value=True
            with self.assertRaises(BrowserClosed):self.guard.checkpoint()

    def test_failed_sku_request_is_module_error_but_download_access_limit_is_global(self):
        self.guard._relevant=MagicMock(return_value=True)
        request=MagicMock(url='https://item.taobao.com/sku-price',frame=SimpleNamespace(page=self.page))
        request.is_navigation_request.return_value=False
        with self.guard.collection_module('skus',self.page):
            self.guard._failed_signal(request)
            with self.assertRaises(ModuleBlocked):self.guard.checkpoint()
        self.guard._reply=MagicMock(return_value='继续')
        with self.guard.collection_module('main_images',self.page):
            self.guard.request_network_attention(self.page,'HTTP429')
        self.guard._reply.assert_called_once()


class RecoveryRulesTests(unittest.TestCase):
    def test_platform_selection_is_idempotent_and_roundtrip_preserves_qianniu_identity(self):
        """CLI结束派发与Web共用同一淘宝资源，重复选择不能误认资料冲突。"""
        platform=self.settings.for_platform('taobao')
        self.assertIs(platform.for_platform('taobao'),platform)
        self.assertEqual(platform.for_platform('qianniu').profile_path,self.settings.profile_path)
        self.assertEqual(platform.for_platform('qianniu').session_path,self.settings.session_path)
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=load_settings(root=Path(temp.name))
        self.session=MagicMock(task_id='b'*32)
        self.persistence=MagicMock()
        self.persistence.read.return_value=None
        self.patch=patch('ecommerce.modules.collection.recovery.RecoveryStore',return_value=self.persistence)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.options={'mode':'detail','input':'123','modules':['reviews'],'all_reviews':True,'all_questions':False}

    def test_scope_is_frozen_and_completed_no_entry_and_failed_skip_are_distinct(self):
        tracker=RecoverySession(self.settings,self.session,self.options,None,[])
        self.options['modules'].append('questions')
        self.assertEqual(tracker.options['modules'],['reviews'])
        tracker.finish_module('123','reviews',{'status':'complete'})
        tracker.finish_module('123','questions',{'status':'skipped','stop_reason':'entry_not_found'})
        tracker.finish_module('123','skus',{'status':'failed_skipped'},failed=True)
        self.assertTrue(all(tracker.should_skip('123',m) for m in ['reviews','questions','skus']))
        self.assertEqual(tracker.counts(),{'complete':1,'no_entry':1,'failed':1,'not_run':0,'reused':3})

    def test_unfinished_module_is_cleaned_before_new_running_state(self):
        tracker=RecoverySession(self.settings,self.session,self.options,None,['a'*32])
        self.persistence.batches.return_value=[{'source_batch_key':'a'*32+'-old','task_id':'a'*32}]
        with patch('ecommerce.modules.collection.recovery.current_events',return_value=None),patch.object(tracker,'check_stop'):
            tracker.prepare('123','reviews')
        self.persistence.cleanup.assert_called_once()
        self.assertEqual(tracker.module('123','reviews')['state'],'running')
        self.assertEqual(self.session.checkpoint.call_count,1)

    def test_cleanup_failure_does_not_start_new_batch(self):
        tracker=RecoverySession(self.settings,self.session,self.options,None,['a'*32])
        self.persistence.batches.return_value=[{'source_batch_key':'old'}]
        self.persistence.cleanup.side_effect=PermissionError('文件占用')
        with patch.object(tracker,'check_stop'),self.assertRaises(PermissionError):tracker.prepare('123','reviews')
        self.assertEqual(tracker.module('123','reviews')['state'],'cleaning')

    def test_legacy_missing_plan_and_changed_scope_are_rejected(self):
        with self.assertRaises(ValueError):RecoverySession(self.settings,self.session,self.options,'a'*32,[])
        self.persistence.read.return_value={'format_version':1,'options':{},'active_task_id':'a'*32}
        with self.assertRaises(ValueError):RecoverySession(self.settings,self.session,self.options,'a'*32,[])

    def test_snapshot_cannot_change_after_a_new_module_progresses(self):
        tracker=RecoverySession(self.settings,self.session,self.options,None,[])
        old=tracker.snapshot()
        tracker.finish_module('123','reviews',{'status':'complete'})
        self.assertEqual(old['checkpoint']['products'],{})


class QueueBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=load_settings(root=Path(temp.name))
        self.persistence=MagicMock()
        self.patch=patch('ecommerce.infrastructure.storage.collection_jobs.CollectionJobStore',return_value=self.persistence)
        self.persistence.rows.return_value=[]
        self.persistence.claim.return_value=None
        self.patch.start();self.addCleanup(self.patch.stop)

    def test_stop_is_durable_even_when_database_is_unavailable(self):
        from ecommerce.infrastructure.storage.mysql import MySQLStorageError
        self.persistence.pause.side_effect=MySQLStorageError('断库')
        dispatch.pause_queue(self.settings)
        self.assertTrue(dispatch.pause_marker(self.settings).is_file())
        with patch('ecommerce.modules.collection.web_collection._start') as launch:
            with self.assertRaises(MySQLStorageError):dispatch.dispatch_next(self.settings)
        launch.assert_not_called()

    def test_running_collection_prevents_retry_dispatch(self):
        with patch.object(dispatch,'active_local_tasks',return_value=[{'id':'a'*32}]),patch('ecommerce.modules.collection.web_collection._start') as launch:
            self.assertIsNone(dispatch.dispatch_next(self.settings))
        launch.assert_not_called()
        self.persistence.queue_transition.assert_not_called()

    def test_web_restart_pauses_waiting_queue_without_starting_a_browser(self):
        self.persistence.rows.return_value=[{'id':'a'*32,'dispatch_state':'waiting'}]
        with patch('ecommerce.modules.collection.web_collection._start') as launch:
            dispatch.on_web_start(self.settings)
        launch.assert_not_called()
        self.persistence.pause.assert_called()
        self.assertFalse(dispatch.pause_marker(self.settings).exists())

    def test_cancelled_execution_pauses_queue_and_does_not_dispatch_next(self):
        with patch.object(dispatch,'dispatch_next') as launch:
            dispatch.after_execution(self.settings,'a'*32,'cancelled')
        launch.assert_not_called()
        self.persistence.pause.assert_called()
        self.assertFalse(dispatch.pause_marker(self.settings).exists())


if __name__=='__main__':unittest.main()
