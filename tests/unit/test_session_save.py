"""登录状态原子保存及文件冲突回归；仅使用临时目录和合成登录资料。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import errno
import json
import os
import tempfile
import threading
import unittest

from ecommerce.infrastructure.browser import session
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import AutomationError


class SessionSaveTests(unittest.TestCase):
    def setUp(self):
        """准备合成状态与旧正式/故障文件；参数：无；返回：无。"""
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=load_settings(root=Path(folder.name)).for_platform('taobao')
        self.path=self.settings.session_path
        self.path.parent.mkdir(parents=True)
        self.original=b'{"cookies":[],"origins":[],"synthetic":"old"}'
        self.path.write_bytes(self.original)
        self.legacy=self.path.with_suffix('.tmp');self.legacy.write_bytes(b'synthetic incident evidence')
        self.state={'cookies':[{'name':'synthetic','value':'SYNTHETIC_SECRET_NEVER_LOG','domain':'.local.test','path':'/'}],
            'origins':[{'origin':'https://local.test','localStorage':[{'name':'合成状态','value':'仅用于隔离测试'}]}]}
        self.context=Mock();self.context.storage_state.return_value=self.state

    def error(self,code):
        """构造Windows合成系统错误；参数：错误码；返回：文件异常。"""
        error=OSError(errno.EACCES,'synthetic conflict')
        error.winerror=code
        return error

    def assert_original(self):
        """核对正式旧状态与原故障现场未改变；参数：无；返回：无。"""
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(self.legacy.read_bytes(),b'synthetic incident evidence')

    def test_save_once_uses_same_directory_unique_temp_and_preserves_old_incident(self):
        """成功保存兼容JSON，临时文件不复用旧故障路径且在关闭写句柄后替换。"""
        original=Path.replace;seen=[]
        def replace_file(source,destination):
            self.assertEqual(source.parent,self.path.parent)
            self.assertNotEqual(source,self.legacy)
            self.assertEqual(self.path.read_bytes(),self.original if not seen else json.dumps(self.state,ensure_ascii=False).encode())
            self.assertEqual(json.loads(source.read_text(encoding='utf-8')),self.state)
            seen.append(source)
            return original(source,destination)
        with patch.object(Path,'replace',replace_file),patch.object(session.time,'sleep') as sleep:
            session.save_session(self.context,self.settings)
            session.save_session(self.context,self.settings)
        self.assertEqual(len(set(seen)),2)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),self.state)
        self.assertFalse(any(self.path.parent.glob('.taobao.json-*.tmp')))
        self.assertEqual(self.legacy.read_bytes(),b'synthetic incident evidence')
        sleep.assert_not_called()

    def test_access_and_sharing_conflicts_retry_only_replacement_with_one_snapshot(self):
        """前两次访问/共享/锁冲突后第三次成功，只读取一次浏览器快照。"""
        original=Path.replace
        for code in (5,32,33):
            with self.subTest(code=code):
                attempts=[]
                def replace_file(source,destination):
                    attempts.append(source)
                    if len(attempts)<3:raise self.error(code)
                    return original(source,destination)
                self.context.reset_mock()
                with patch.object(Path,'replace',replace_file),patch.object(session.time,'sleep') as sleep,self.assertLogs(session.__name__,level='WARNING') as logs:
                    session.save_session(self.context,self.settings)
                self.context.storage_state.assert_called_once_with()
                self.assertEqual(len(attempts),3);self.assertEqual(len(set(attempts)),1)
                self.assertEqual([call.args[0] for call in sleep.call_args_list],[.2,.5])
                self.assertTrue(all(record.levelname=='WARNING' for record in logs.records))
                self.assertNotIn('SYNTHETIC_SECRET_NEVER_LOG','\n'.join(logs.output))

    def test_persistent_access_failure_preserves_original_and_fails_with_red_error(self):
        """持续拒绝访问总计三次即失败，清理本次临时文件，保留旧文件和原故障现场。"""
        with patch.object(Path,'replace',side_effect=self.error(5)) as replace_file,patch.object(session.time,'sleep') as sleep,self.assertLogs(session.__name__,level='WARNING') as logs:
            with self.assertRaises(AutomationError) as caught:
                session.save_session(self.context,self.settings)
        self.assert_original();self.context.storage_state.assert_called_once_with()
        self.assertEqual(replace_file.call_count,3);self.assertEqual(sleep.call_count,2)
        self.assertIn('替换尝试=3/3',str(caught.exception));self.assertIn('WinError 5',str(caught.exception))
        self.assertEqual([record.levelname for record in logs.records],['WARNING','WARNING','ERROR'])
        self.assertNotIn('SYNTHETIC_SECRET_NEVER_LOG','\n'.join(logs.output))
        self.assertFalse(any(self.path.parent.glob('.taobao.json-*.tmp')))

    def test_non_conflict_replace_error_does_not_retry_or_replace_old_file(self):
        """路径缺失等非访问冲突立即失败，不重复保存浏览器状态。"""
        error=OSError(errno.ENOENT,'synthetic missing path');error.winerror=3
        with patch.object(Path,'replace',side_effect=error) as replace_file,patch.object(session.time,'sleep') as sleep,self.assertLogs(session.__name__,level='ERROR'):
            with self.assertRaises(AutomationError):session.save_session(self.context,self.settings)
        replace_file.assert_called_once();sleep.assert_not_called();self.assert_original()

    def test_flush_failure_preserves_original_without_attempting_replace(self):
        """临时文件同步失败时不能替换原状态，且不重试浏览器。"""
        with patch.object(session.os,'fsync',side_effect=OSError(errno.ENOSPC,'synthetic full disk')),patch.object(Path,'replace') as replace_file,self.assertLogs(session.__name__,level='ERROR'):
            with self.assertRaises(AutomationError):session.save_session(self.context,self.settings)
        self.assert_original();replace_file.assert_not_called()
        self.assertFalse(any(self.path.parent.glob('.taobao.json-*.tmp')))

    def test_temp_name_collision_does_not_delete_a_file_not_created_by_this_call(self):
        """即使编号碰撞也不能删除其他调用的临时文件。"""
        existing=self.path.with_name('.taobao.json-'+'a'*32+'.tmp');existing.write_bytes(b'synthetic unrelated temp')
        with patch.object(session,'uuid4',return_value=SimpleNamespace(hex='a'*32)),self.assertLogs(session.__name__,level='ERROR'):
            with self.assertRaises(AutomationError):session.save_session(self.context,self.settings)
        self.assert_original();self.assertEqual(existing.read_bytes(),b'synthetic unrelated temp')

    def test_cleanup_failure_does_not_mask_the_original_replace_error(self):
        """清理失败单独警告，不覆盖原替换失败原因。"""
        original=Path.unlink
        def unlink_file(path,*args,**kwargs):
            if path.name.startswith('.taobao.json-'):raise self.error(32)
            return original(path,*args,**kwargs)
        with patch.object(Path,'replace',side_effect=self.error(5)),patch.object(Path,'unlink',unlink_file),patch.object(session.time,'sleep'),self.assertLogs(session.__name__,level='WARNING') as logs:
            with self.assertRaises(AutomationError) as caught:session.save_session(self.context,self.settings)
        self.assertIn('WinError 5',str(caught.exception));self.assertIn('未能清理',logs.output[-1]);self.assert_original()

    def test_invalid_snapshot_or_browser_read_failure_never_changes_files(self):
        """无效或读取失败的浏览器快照不覆盖旧状态，不暴露内容。"""
        for value in ({'cookies':'bad','origins':[]},{'cookies':[],'origins':[],'bad':float('nan')}):
            self.context.storage_state.return_value=value
            with self.assertLogs(session.__name__,level='ERROR'),self.assertRaises(AutomationError):
                session.save_session(self.context,self.settings)
            self.assert_original()
        self.context.storage_state.side_effect=RuntimeError('synthetic browser closed')
        with self.assertRaises(RuntimeError):session.save_session(self.context,self.settings)
        self.assert_original()

    def test_taobao_and_qianniu_session_paths_remain_independent(self):
        """同一保存方法仍分别写入淘宝、千牛路径，支持首次无旧状态的保存。"""
        base=load_settings(root=self.settings.root)
        for settings in (base,base.for_platform('taobao')):
            session.save_session(self.context,settings)
            self.assertEqual(json.loads(settings.session_path.read_text(encoding='utf-8')),self.state)
        self.assertNotEqual(base.session_path,base.for_platform('taobao').session_path)

    def test_concurrent_saves_use_different_temps_and_leave_one_complete_snapshot(self):
        """并发写入的临时文件互不覆盖，最终正式文件仍是某次完整快照。"""
        barrier=threading.Barrier(2);lock=threading.Lock();seen=set()
        original=Path.replace
        def replace_file(source,destination):
            with lock:
                first=source not in seen;seen.add(source)
            if first:barrier.wait(timeout=5)
            return original(source,destination)
        states=[{**self.state,'synthetic_version':number} for number in (1,2)]
        contexts=[Mock(storage_state=Mock(return_value=state)) for state in states]
        with patch.object(Path,'replace',replace_file),ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda context:session.save_session(context,self.settings),contexts))
        self.assertEqual(len(seen),2)
        self.assertIn(json.loads(self.path.read_text(encoding='utf-8')),states)
        self.assertFalse(any(self.path.parent.glob('.taobao.json-*.tmp')))
        self.assertEqual(self.legacy.read_bytes(),b'synthetic incident evidence')

    @unittest.skipUnless(os.name=='nt','Windows真实文件共享锁验证')
    def test_windows_real_file_lock_can_be_released_before_second_replace(self):
        """用系统文件句柄真实阻止删除共享，释放占用后重试成功，不修改真实登录文件。"""
        import ctypes as c
        from ctypes import wintypes as w
        kernel=c.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
        kernel.CreateFileW.restype=w.HANDLE;kernel.CloseHandle.argtypes=[w.HANDLE]
        handle=kernel.CreateFileW(str(self.path),0x80000000,3,None,3,0x80,None)
        self.assertNotEqual(handle,c.c_void_p(-1).value)
        def release(delay):
            nonlocal handle
            self.assert_original();self.assertEqual(delay,.2)
            kernel.CloseHandle(handle);handle=None
        try:
            with patch.object(session.time,'sleep',side_effect=release) as sleep,self.assertLogs(session.__name__,level='WARNING') as logs:
                session.save_session(self.context,self.settings)
            sleep.assert_called_once_with(.2)
            self.assertIn('WinError',logs.output[0])
            self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),self.state)
            self.context.storage_state.assert_called_once_with()
        finally:
            if handle is not None:kernel.CloseHandle(handle)
