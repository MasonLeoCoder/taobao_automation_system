"""禁用即缺货的证据、完成边界、持久化与补采跳过；无真实浏览器或业务库写入。"""
from copy import deepcopy
import unittest

from ecommerce.shared.sku_stock import classify_disabled_sku,disabled_stock_evidence
from ecommerce.modules.collection.detail_skus import _notices,sku_continuation_allowed
from ecommerce.modules.collection.persistence import validate_publication
from ecommerce.modules.collection.web_collection import remaining_plan


class SkuStockTests(unittest.TestCase):
    def record(self):
        """生成有明确禁用标记的脱敏规格；参数：无；返回：完整候选记录。"""
        return {'status':'partial','candidate_count':1,'processed_count':1,'skus':[
            {'index':1,'name':'替换装','properties':[{'group':'规格','name':'替换装','value_id':'1'}],
             'combination_confirmed':True,'stock_status':'unavailable_unconfirmed',
             'price_status':'unavailable_unconfirmed','price':None,'price_value':None,
             'price_reason':'旧口径未确认','unavailability_evidence':['data-disabled=true'],
             'image_status':'downloaded'}]}

    def finish(self,record):
        """调用实际模块判断；参数：候选记录；返回：归类后的同一记录。"""
        for row in record['skus']:classify_disabled_sku(row)
        record['warnings'],record['blocking_issues']=_notices(record)
        record['status']='partial' if record['warnings'] or record['blocking_issues'] else 'complete'
        return record

    def test_known_disabled_evidence_only(self):
        for marker in ['disabled属性','disabled','aria-disabled=true','data-disabled=true','class=isDisabled--abc','class=soldOut--abc']:
            with self.subTest(marker=marker):self.assertTrue(disabled_stock_evidence([marker]))
        for evidence in [None,'data-disabled=true',[],['click timeout'],['data-disabled=false'],['class=notDisabled--abc']]:
            with self.subTest(evidence=evidence):self.assertIsNone(disabled_stock_evidence(evidence))

    def test_disabled_stock_is_complete_publishable_and_skipped(self):
        record=self.finish(self.record());row=record['skus'][0]
        self.assertEqual(record['status'],'complete');self.assertEqual(record['warnings'],[])
        self.assertEqual(row['stock_status'],'out_of_stock');self.assertEqual(row['price_status'],'out_of_stock')
        self.assertIsNone(row['price']);self.assertIsNone(row['price_value'])
        self.assertEqual(row['unavailability_evidence'],['data-disabled=true'])
        self.assertTrue(sku_continuation_allowed(record));validate_publication('skus',record,['sku_details'])
        plan=remaining_plan({'history':[{'id':12,'module':'skus','status':record['status']}],'current_ids':[12]})
        self.assertIn('skus',plan['skipped_modules']);self.assertNotIn('skus',plan['modules'])

    def test_disabled_without_evidence_stays_partial(self):
        record=self.record();record['skus'][0]['unavailability_evidence']=[]
        self.assertEqual(self.finish(record)['status'],'partial')
        with self.assertRaises(ValueError):validate_publication('skus',record,['sku_details'])

    def test_real_collection_failures_are_not_relaxed(self):
        for patch in [{'image_status':'download_failed'}, {'combination_confirmed':False},
                      {'price_status':'unconfirmed'}, {'price_value':'9.9'}]:
            record=self.record();record['skus'][0].update(patch)
            with self.subTest(patch=patch):
                self.assertEqual(self.finish(record)['status'],'partial')
                with self.assertRaises(ValueError):validate_publication('skus',record,['sku_details'])
        record=self.record();record['candidate_count']=2
        self.assertEqual(self.finish(record)['status'],'partial')

    def test_no_image_and_missing_platform_sku_id_do_not_block(self):
        record=self.record();record['skus'][0].update(image_status='no_image',sku_id=None)
        self.assertEqual(self.finish(record)['status'],'complete')
        validate_publication('skus',record,['sku_details'])

    def test_contradictory_price_is_never_silently_removed(self):
        row=self.record()['skus'][0];row['price_value']='9.9';before=deepcopy(row)
        self.assertFalse(classify_disabled_sku(row));self.assertEqual(row,before)


if __name__=='__main__':unittest.main()
