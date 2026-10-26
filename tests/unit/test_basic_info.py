"""基本信息独立采集、发布及九模块入口的离线验证。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch

from ecommerce.cli.app import main,DETAIL_STEPS
from ecommerce.infrastructure.tasks.runner import TaskResult
from ecommerce.modules.collection.detail_basic_info import collect_basic_info
from ecommerce.modules.collection.detail_skus import save_sku_result
from ecommerce.modules.collection.detail_storage import product_directory
from ecommerce.modules.collection.persistence import validate_publication
from ecommerce.modules.collection.database_records import build_snapshot
from ecommerce.infrastructure.storage.collection_snapshot import open_snapshot
from ecommerce.infrastructure.storage.mysql_migrations import schema_v6,schema_v7
from ecommerce.shared.basic_info import assessment
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.platforms.taobao.detail import resolve_detail_input


class BasicInfoTests(unittest.TestCase):
    def setUp(self):
        """准备临时商品及明确字段样例；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=load_settings(root=Path(temp.name))
        self.page=MagicMock(url='https://item.taobao.com/item.htm?id=123')
        self.page.is_closed.return_value=False
        self.result=MagicMock(page=self.page,target=resolve_detail_input('123'))
        self.guard=MagicMock(task_id='a'*32)
        self.guard.perform.side_effect=lambda label,action,**kw:action()
        self.value={'title':'独立标题','shop_name':'样例店铺','sales_text':'已售 2000+','price_text':'券后 ¥3.9 起',
                    'field_states':{k:'provided' for k in ('title','shop_name','sales_text','price_text')}}

    def collect(self,value=None):
        """调用真实保存流程，隔离浏览器读操作；参数：字段；返回：保存结果。"""
        with patch('ecommerce.modules.collection.detail_basic_info.active_guard',return_value=self.guard),patch(
                'ecommerce.modules.collection.detail_basic_info.read_basic_info',return_value=value if value is not None else self.value):
            return collect_basic_info(self.settings,self.result)

    def test_complete_basic_info_does_not_call_sku_or_download(self):
        with patch('ecommerce.modules.collection.detail_skus.collect_skus') as sku:
            result=self.collect();sku.assert_not_called()
        document=json.loads(Path(result['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(result['status'],'complete');self.assertIn('product_info',document)
        self.assertNotIn('sku_details',document);self.assertFalse((product_directory(self.settings,'123')/'sku图').exists())
        self.assertEqual(result['product_info']['price_text'],'券后 ¥3.9 起')

    def test_partial_basic_attempt_preserves_previous_effective_information(self):
        complete=self.collect()
        before=json.loads(Path(complete['record_path']).read_text(encoding='utf-8'))
        partial=self.collect({**self.value,'title':None,'field_states':{**self.value['field_states'],'title':'ambiguous'}})
        after=json.loads(Path(partial['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(partial['status'],'partial')
        self.assertEqual(after['product_info'],before['product_info'])
        self.assertEqual(after['product_info_last_attempt']['status'],'partial')

    def test_missing_optional_fields_are_explicit_and_do_not_require_sku(self):
        result=self.collect({'title':'标题','field_states':{'title':'provided','sales_text':'not_displayed'}})
        self.assertEqual(result['status'],'complete');self.assertEqual(len(result['warnings']),3)
        self.assertNotIn('sales_count',result['product_info'])

    def test_target_change_before_save_is_rejected(self):
        self.page.url='https://item.taobao.com/item.htm?id=456'
        with self.assertRaises(NeedsAttention):self.collect()

    def test_publication_checks_basic_own_title_source_and_time(self):
        result=self.collect()
        validate_publication('basic_info',result,['product_info'])
        for invalid in ({**result,'product_info':{'title':None}}, {**result,'source_url':None}, {**result,'captured_at':None}):
            with self.assertRaises(ValueError):validate_publication('basic_info',invalid,['product_info'])

    def test_snapshot_has_independent_observation_and_preserves_raw_prices(self):
        result=self.collect()
        reference=build_snapshot(self.settings,'batch',result,result['history_record_path'],module='basic_info')
        header,iterator=open_snapshot(self.settings,reference);rows=list(iterator)
        info=next(row for row in rows if row['table']=='product_info_observations')
        self.assertEqual(header['module'],'basic_info');self.assertEqual(info['values']['price_text'],'券后 ¥3.9 起')
        self.assertFalse(any(row['table']=='sku_observations' for row in rows))

    def test_new_model_keeps_previous_version_frozen(self):
        old=schema_v6.build_metadata();new=schema_v7.build_metadata()
        self.assertNotIn('product_info_observations',old.tables)
        self.assertEqual(len(new.tables),len(old.tables)+1)
        self.assertIn('fk_product_info_batch_owner',{fk.name for fk in new.tables['product_info_observations'].foreign_key_constraints})

    def test_cli_basic_info_is_independent_and_default_has_nine_modules(self):
        self.assertEqual(len(DETAIL_STEPS),9)
        for flags,selected in [(['--basic-info'],{'basic_info'}),(['--skus'],{'skus'}),([] ,set(DETAIL_STEPS))]:
            with patch('ecommerce.cli.app.load_settings'),patch('ecommerce.cli.app.prepare_workspace'),patch(
                    'ecommerce.cli.app.setup_logging'),patch('ecommerce.cli.commands.collection.execute_detail',
                    return_value=TaskResult('a'*32,'succeeded','local',0)) as call:
                self.assertEqual(main(['taobao','detail','--input','123',*flags]),0)
                self.assertEqual({name for name in DETAIL_STEPS if call.call_args.kwargs[name]},selected)


if __name__=='__main__':unittest.main()
