"""分页参数、真实页码证据和末页判断的离线检查。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ecommerce.cli.app import parser
from ecommerce.cli.commands import collection
from ecommerce.platforms.taobao.cards import page_signature,parse_cards
from ecommerce.platforms.taobao.pagination import validate_pages,inspect_pagination
from ecommerce.shared.config import load_settings
from tests.unit.test_taobao_collection import snapshot


class PaginationTests(unittest.TestCase):
    """核对分页边界，不启动浏览器或数据库。"""
    def test_page_count_and_per_page_limit_validate_before_task(self):
        """默认一页，显式三页，非法页数不得产生任务。"""
        self.assertEqual(parser().parse_args(['taobao','collect','--keyword','粘毛器']).page,1)
        args=parser().parse_args(['taobao','collect','--keyword','粘毛器','--page','3','--limit','3'])
        self.assertEqual((args.page,args.limit),(3,3))
        with tempfile.TemporaryDirectory() as folder,patch.object(collection,'run_task') as runner:
            for value in [0,-1,True,1.5,'3']:
                with self.subTest(value=value),self.assertRaises(ValueError):
                    collection.execute_collect(load_settings(root=Path(folder)),'粘毛器',pages=value)
            runner.assert_not_called()
        self.assertEqual(validate_pages(3),3)

    def test_second_page_needs_consistent_explicit_evidence(self):
        """第二页可采，但不能忽略URL、DOM和偏移量矛盾。"""
        raw=snapshot(paging={'page':['2']},page_numbers=['2'])
        self.assertEqual(parse_cards(raw,'粘毛器',3,page_number=2)['signature']['page_number'],2)
        with self.assertRaises(ValueError):
            page_signature(raw,'粘毛器')
        for paging,numbers in [({'page':['2']},['1']),({'page':['1']},['2']),({'s':['48']},[]),({'page':['2'],'s':['0']},['2']),({'page':['2','2']},['2'])]:
            with self.subTest(paging=paging,numbers=numbers),self.assertRaises(ValueError):
                page_signature(snapshot(paging=paging,page_numbers=numbers),'粘毛器',expected_page=2)

    def test_disabled_or_missing_button_does_not_prove_end(self):
        """必须同时核对总页数，不能把按钮异常当作末页。"""
        base={'group_count':1,'variant':'normal','next_count':1,'next_disabled':False,'total_pages':['100']}
        self.assertFalse(inspect_pagination({'pagination':base},2)['last_page'])
        for updates in [{'next_disabled':True},{'next_count':0},{'total_pages':['2','100']},{'group_count':2}]:
            with self.subTest(updates=updates),self.assertRaises(ValueError):
                inspect_pagination({'pagination':dict(base,**updates)},2)
        result=inspect_pagination({'pagination':dict(base,total_pages=['2'],next_disabled=True)},2)
        self.assertTrue(result['last_page'])
        self.assertEqual(result['total_pages'],2)
