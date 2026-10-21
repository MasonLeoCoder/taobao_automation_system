"""缺货价格口径、销量原文和 Windows SKU 图片文件名。"""
import unittest

from ecommerce.modules.collection.detail_skus import filename_stem
from ecommerce.platforms.taobao.detail_skus import parse_prices, parse_sales


class TaobaoDetailSkuTests(unittest.TestCase):
    def test_sales_lower_bound_is_not_exact_or_monthly_sales(self):
        self.assertEqual(parse_sales('已售 10万+')['sales_lower_bound'], 100000)
        self.assertIsNone(parse_sales('已售 10万+')['sales_value'])
        self.assertEqual(parse_sales('已售 123件')['sales_value'], 123)
        self.assertEqual(parse_sales('已售 1.2万')['sales_relation'], 'rounded_display')
        self.assertIsNone(parse_sales('月销10万')['sales_value'])

    def test_price_labels_and_currency_are_preserved_without_parsing_ranges(self):
        price = {'display':'优惠前 ￥ 3.35', 'label':'优惠前', 'amount_text':'3.35', 'symbol':'￥'}
        self.assertEqual(parse_prices([price])[0]['amount'], '3.35')
        self.assertEqual(parse_prices([price])[0]['label'], '优惠前')
        self.assertEqual(parse_prices([price])[0]['currency'], 'CNY')
        self.assertEqual(parse_prices([{**price, 'amount_text':'3.35-9.90'}]), [])

    def test_filename_preserves_sku_names_and_handles_unsafe_reserved_and_duplicate_names(self):
        used = set()
        self.assertEqual(filename_stem('1卷替换纸+1个手柄', 'first', used), '1卷替换纸+1个手柄')
        self.assertEqual(filename_stem('红/蓝:*?', 'second', used), '红_蓝___')
        self.assertEqual(filename_stem('CON', 'third', used), 'SKU_CON')
        first = filename_stem('ABC', 'fourth', used)
        second = filename_stem('abc', 'fifth', used)
        self.assertNotEqual(first.casefold(), second.casefold())
        self.assertLessEqual(len(filename_stem('😀'*200, 'sixth', used).encode('utf-16-le')), 192)


if __name__ == '__main__':
    unittest.main()
