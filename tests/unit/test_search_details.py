"""列表到详情的来源、重复位置、异常边界与实时快照离线验证。"""
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ecommerce.cli.app import parser
from ecommerce.infrastructure.storage.collection_snapshot import open_snapshot
from ecommerce.modules.collection import search_details
from ecommerce.modules.collection.database_records import build_snapshot
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import TaskStopped
from tests.collection_fixture import create_sources


class SearchDetailTests(unittest.TestCase):
    def setUp(self):
        """创建隔离原件；参数：无；返回：无，不使用正式库或真实网页。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings=load_settings(root=Path(temporary.name))
        self.task='a'*32
        self.guard=SimpleNamespace(task_id=self.task)
        self.result=SimpleNamespace(keyword='样例')
        self.record={'task_id':self.task,'batch_id':self.task+'-list','page_number':1,'keyword':'样例',
            'search_method':'keyword','sort_label':'销量','search_options':None,
            'items':[{'product_id':identifier,'product_index':i,'product_url':'https://item.taobao.com/item.htm?id='+identifier}
                     for i,identifier in enumerate(['123456','234567','123456'],1)]}
        path=self.settings.workspace/'list.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.record),encoding='utf-8')
        self.collected={'json_path':str(path),'source_batch_key':self.record['batch_id'],'page_number':1,'collected_count':3}

    def patches(self,stack):
        """隔离页面边界；参数：补丁上下文；返回：真实业务调度的依赖替身。"""
        stack.enter_context(patch.object(search_details,'active_guard',return_value=self.guard))
        stack.enter_context(patch.object(search_details,'emit_event'))
        stack.enter_context(patch.object(search_details,'confirm_search_page'))
        opened=stack.enter_context(patch.object(search_details,'open_list_detail'))
        returned=stack.enter_context(patch.object(search_details,'return_to_search'))
        collected=stack.enter_context(patch.object(search_details,'collect_detail_page'))
        collected.side_effect=lambda settings,context,detail,**kw: {'status':'complete','entry_key':kw['source']['list_source']['product_id']+'-entry',
            'source_run_key':kw['source']['list_source']['product_id']+'-run'}
        return opened,returned,collected

    def test_cli_details_are_opt_in_and_page_limit_keep_existing_meaning(self):
        """普通列表不会默认访问详情；页数、每页数量与新开关分别解析。"""
        base=['taobao','collect','--keyword','样例']
        self.assertFalse(parser().parse_args(base).with_details)
        args=parser().parse_args(base+['--with-details','--page','3','--limit','3'])
        self.assertTrue(args.with_details)
        self.assertEqual((args.page,args.limit),(3,3))

    def test_duplicate_observations_keep_positions_but_successful_details_reuse_in_task(self):
        """三处展示中重复商品只采一次详情，并明确记录对应运行。"""
        attempts,seen=[],{}
        with ExitStack() as stack:
            opened,returned,collected=self.patches(stack)
            search_details.collect_page_details(self.settings,object(),self.result,self.collected,seen,attempts)
        self.assertEqual((opened.call_count,returned.call_count,collected.call_count),(2,2,2))
        self.assertEqual([a['status'] for a in attempts],['complete','complete','reused_in_task'])
        self.assertEqual(attempts[2]['source_run_key'],attempts[0]['source_run_key'])
        self.assertEqual(attempts[2]['list_source']['product_index'],3)
        self.assertEqual(collected.call_args_list[0].kwargs['source']['keyword'],'样例')

    def test_stop_retains_partial_attempt_and_does_not_visit_next_product(self):
        """停止不会被当作成功缓存，也不会继续下一商品。"""
        attempts,seen=[],{}
        with ExitStack() as stack:
            opened,returned,collected=self.patches(stack)
            collected.side_effect=TaskStopped('模拟人工停止')
            with self.assertRaises(TaskStopped):
                search_details.collect_page_details(self.settings,object(),self.result,self.collected,seen,attempts)
        self.assertEqual(opened.call_count,1)
        self.assertFalse(returned.called)
        self.assertEqual(attempts[0]['status'],'interrupted')
        self.assertEqual(seen,{})

    def test_wrong_batch_file_is_rejected_before_opening_any_detail(self):
        """入口只接受本页已确认原件，不能把其他批次串入当前任务。"""
        with ExitStack() as stack:
            opened,_,_=self.patches(stack)
            with self.assertRaises(ValueError):
                search_details.collect_page_details(self.settings,object(),self.result,dict(self.collected,source_batch_key='wrong'),{},[])
            self.assertFalse(opened.called)

    def test_search_origin_does_not_change_detail_coverage_scope(self):
        """搜索来源用于追溯，不产生另一个参数完整性范围。"""
        record={'batch_id':'params','task_id':self.task,'product_id':'123456','status':'complete','items':[]}
        path=self.settings.workspace/'parameters.json'
        path.write_text(json.dumps(record),encoding='utf-8')
        plain,_=open_snapshot(self.settings,build_snapshot(self.settings,'batch',record,path,module='parameters'))
        source={'keyword':'样例','search_method':'keyword','page_number':2,'search_options':None,
                'list_source':{'source_batch_key':'list','product_index':1,'product_id':'123456'}}
        linked,_=open_snapshot(self.settings,build_snapshot(self.settings,'batch',record,path,module='parameters',source=source))
        self.assertEqual(plain['values']['scope_key'],linked['values']['scope_key'])
        self.assertEqual(linked['values']['result_payload']['record']['keyword'],'样例')
        self.assertNotIn('keyword',json.loads(path.read_text(encoding='utf-8')))



if __name__=='__main__':
    unittest.main()
