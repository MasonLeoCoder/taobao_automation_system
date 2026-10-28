"""指标说明与最终格式化的展示契约；不改变原始数值或统计规则。"""
import csv
import io
from decimal import Decimal
from pathlib import Path
import unittest

from flask import Flask, render_template_string
from ecommerce.web.routes.analysis import _compact_number, _money, _stat_money
from tests.unit import test_search_analysis as fixtures


class MetricHelpTests(unittest.TestCase):
    def test_display_rounding_trim_and_original_money_are_separate(self):
        """仅最终展示舍入，零/空及小数均保留含义，原始商品价格格式不变。"""
        self.assertEqual(_compact_number(None),'—')
        self.assertEqual(_compact_number(0),'0')
        self.assertEqual(_compact_number(11000),'11,000')
        self.assertEqual(_compact_number(Decimal('1.50')),'1.5')
        self.assertEqual(_compact_number(Decimal('2.345')),'2.35')
        self.assertEqual(_stat_money(Decimal('8.905')),'¥8.91')
        self.assertEqual(_stat_money(Decimal('13.6275')),'¥13.63')
        self.assertEqual(_stat_money(None),'—')
        self.assertEqual(_money(Decimal('8.905')),'¥8.905')

    def test_common_macro_escapes_description_and_reuses_definition(self):
        """公共说明按钮不会提交表单，属性和文字安全转义，定义可在多处复用。"""
        root=Path(__file__).resolve().parents[2]/'src/ecommerce/web/templates'
        app=Flask(__name__,template_folder=str(root))
        with app.app_context():
            html=render_template_string("{% from 'form_controls.html' import help_label %}{{ help_label(value,value) }}",value='"><img src=x onerror=alert(1)>')
            self.assertIn('type="button"',html)
            self.assertIn('data-ui-help-text=',html)
            self.assertNotIn('<img',html)
            self.assertIn('&lt;img',html)
            html=render_template_string("{% from 'metric_help_definitions.html' import middle_metrics as m %}{{m.quantity_median.label}}|{{m.quantity_median.description}}")
            self.assertIn('收货人数中位数',html)
            self.assertIn('中间两个数的平均',html)

    def test_middle_stats_and_csv_keep_unrounded_data(self):
        """同一页面统计价两位、原商品价保留原精度；后端统计和CSV原值未舍入。"""
        f=fixtures.SearchAnalysisTests();f.setUp();self.addCleanup(f.doCleanups)
        for i in range(1,31): f.observation(i,sales_count=5000,price_value=Decimal('8.905'))
        result=f.read()
        self.assertEqual(result['middle']['summary']['price_median'],Decimal('8.905'))
        html=f.client.get('/analysis/searches').get_data(as_text=True)
        self.assertIn('¥8.91',html)
        self.assertIn('¥8.905',html)
        self.assertIn('每个商品的平均收货人数',html)
        self.assertEqual(html.count('help_tooltip.js'),1)
        self.assertNotIn('5,000.0',html.split('id="sa-middle"',1)[1].split('class="sa-sample-line"',1)[0])
        records=list(csv.reader(io.StringIO(f.client.get('/analysis/searches/export.csv?finding=middle').data.decode('utf-8-sig'))))
        self.assertTrue(records[2][4].startswith('8.905'))
