"""采集快照与范围规则的离线检查，不连接数据库。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ecommerce.infrastructure.storage.collection_snapshot import json_array,digest_file
from ecommerce.modules.collection.database_records import build_snapshot,scope_contract,decimal_value
from ecommerce.modules.collection.history import snapshot_history,preview_history,history_lock
from ecommerce.modules.collection.persistence import validate_publication
from ecommerce.shared.config import load_settings
from tests.collection_fixture import create_sources


class CollectionStorageTests(unittest.TestCase):
    def test_history_backup_excludes_both_platform_resources(self):
        """淘宝资源忙时拒绝历史备份，失败后已取得的其他资源锁正常释放。"""
        from ecommerce.infrastructure.tasks.lock import shop_lock
        from ecommerce.shared.exceptions import TaskBusy
        with shop_lock(self.settings.for_platform('taobao').profile_path):
            with self.assertRaises(TaskBusy):
                with history_lock(self.settings):
                    self.fail('采集资源忙时不能进入备份')
        with history_lock(self.settings):
            pass

    def setUp(self):
        """建立脱敏临时数据；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings=load_settings(root=Path(temp.name))
        self.fixture=create_sources(self.settings,'a'*32,'123456789')

    def test_streaming_json_handles_chunks_unicode_and_nested_answers(self):
        """超过块大小的中文、多行和嵌套值保持完整。"""
        path=self.settings.root/'stream.json'
        values=[{'body':'中文\n"转义"'*20000,'answers':[{'text':'值'}]},{'body':'末尾'}]
        path.write_text(json.dumps({'metadata':'样例','rows':values},ensure_ascii=False),encoding='utf-8')
        self.assertEqual(list(json_array(path,'rows')),values)
        path.write_text('{"rows":[{"unfinished":',encoding='utf-8')
        with self.assertRaises(ValueError):
            list(json_array(path,'rows'))
        for invalid in ['{"rows":[1,]}','{"rows":[],}']:
            path.write_text(invalid,encoding='utf-8')
            with self.assertRaises(ValueError):
                list(json_array(path,'rows'))

    def test_preview_preserves_duplicates_partial_scope_and_question_hierarchy(self):
        """重复位置不合并，部分SKU、限定评价和分层回答数量保持原意。"""
        manifest=snapshot_history(self.settings)
        report=preview_history(self.settings,manifest)
        counts=report['counts']
        self.assertEqual((counts['products'],counts['collection_batches'],counts['collection_runs']),(2,9,1))
        self.assertEqual((counts['list_observations'],counts['sku_observations'],counts['parameter_observations']),(2,1,2))
        self.assertEqual((counts['review_observations'],counts['question_observations'],counts['answer_observations']),(1,2,2))
        self.assertEqual(counts['product_current_sections'],7)
        self.assertEqual(report['coverage']['reviews:limit_reached'],1)
        self.assertEqual(report['coverage']['questions:visible_end_confirmed'],1)
        self.assertEqual(report['issues'][0]['code'],'entry_without_detail_data')
        original=self.fixture['paths']['parameters'].read_bytes()
        self.assertEqual(original,(manifest.parent/'files'/self.fixture['paths']['parameters'].relative_to(self.settings.workspace)).read_bytes())

    def test_identity_is_independent_of_limits_and_missing_is_not_unlimited(self):
        """不同上限及未提供条件分属不同范围，不由 complete 推定全量。"""
        record={'scope':'page_available_reviews','status':'complete','mode':'limited','requested_limit':100}
        key,_,coverage=scope_contract('reviews',record)
        self.assertEqual(coverage,'unknown')
        self.assertNotEqual(key,scope_contract('reviews',dict(record,requested_limit=None))[0])
        self.assertNotEqual(scope_contract('reviews',{'scope':record['scope']})[0],scope_contract('reviews',{'scope':record['scope'],'mode':None})[0])
        with self.assertRaises(ValueError):
            decimal_value('1.0000001')
        self.assertIsNone(decimal_value(None))
        self.assertEqual(decimal_value('0'),'0')

    def test_export_working_database_mismatch_and_modified_media_fail(self):
        """明细、工作库或原文件不一致时停止，不以部分解析冒充成功。"""
        record=self.fixture['records']['reviews']
        export=self.fixture['root']/record['files'][0]['history_file']
        data=json.loads(export.read_text(encoding='utf-8'))
        data['reviews'][0]['content']='被修改'
        export.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        record['files'][0].update(sha256=digest_file(export),size_bytes=export.stat().st_size)
        with self.assertRaises(ValueError):
            build_snapshot(self.settings,'batch',record,self.fixture['paths']['reviews'],module='reviews')
        with self.assertRaises(ValueError):
            validate_publication('skus',self.fixture['records']['skus'],['sku_details'])


if __name__=='__main__':
    unittest.main()
