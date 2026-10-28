"""销量解析与迁移契约的离线边界检查。"""
import unittest
from ecommerce.shared.sales import parse_sales, parse_taobao_list_sales, sales_filters, MAX_COUNT, FIELDS
from ecommerce.infrastructure.storage.mysql_migrations import schema_v5, schema_v6
from ecommerce.infrastructure.storage.mysql_checks import check_expression_tree
import sqlalchemy as sa


class SalesTests(unittest.TestCase):
    def test_numbers_units_metrics_and_periods(self):
        """保留数量性质、口径、周期，不用浮点数制造精确销量。"""
        for raw,count,precision,metric,period in (
            ('1000+人收货',1000,'lower_bound','received_people','unknown'),
            ('1.2万+人付款',12000,'lower_bound','paid_people','unknown'),
            ('已售500件',500,'exact','sold_units','unknown'),
            ('月售500件',500,'exact','sold_units','monthly'),
            ('约1万件已售',10000,'approximate','sold_units','unknown'),
            ('近30天500件已售',500,'exact','sold_units','last_30_days'),
            ('累计1,000人付款',1000,'exact','paid_people','cumulative'),
            ('１．２万＋人付款',12000,'lower_bound','paid_people','unknown'),
            ('1.23万件已售',12300,'approximate','sold_units','unknown'),
            ('0人收货',0,'exact','received_people','unknown'),
            (str(MAX_COUNT),MAX_COUNT,'exact','unknown','unknown')):
            with self.subTest(raw=raw):
                parsed=parse_sales(raw)
                self.assertEqual(tuple(parsed[k] for k in FIELDS),(count,precision,metric,period,'parsed'))

    def test_unknown_not_provided_and_conflicting_formats(self):
        """无数量不当0，多个数字及不支持格式不猜测。"""
        for raw,status in [(None,'not_provided'),('  ','not_provided'),('本月行业热销','no_quantity'),
            ('近30天热销','no_quantity'),('累计本月热销','unrecognized'),
            ('500人付款 100人收货','unrecognized'),('近7天100件已售','unrecognized'),
            ('1.2人收货','unrecognized'),('约1000+人收货','unrecognized'),
            ('0.00001万件已售','unrecognized'),(str(MAX_COUNT+1),'unrecognized'),
            ('1.0000000000000000000000000000001万件已售','unrecognized'),
            ('1,00人付款','unrecognized'),('-1人付款','unrecognized')]:
            with self.subTest(raw=raw):
                parsed=parse_sales(raw)
                self.assertEqual((parsed['sales_count'],parsed['sales_precision'],parsed['sales_parse_status']),
                                 (None,'unknown',status))
        self.assertEqual(parse_sales('本月行业热销')['sales_period'],'monthly')
        with self.assertRaises(ValueError): parse_sales(100)

    def test_filters_validate_bounds_and_codes(self):
        """范围包含0，负数、小数、溢出、反向及非法枚举拒绝。"""
        self.assertEqual(sales_filters({'sales_min':'0','sales_max':'2000'})['sales_min'],0)
        for options in ({'sales_min':'-1'},{'sales_max':'1.5'},{'sales_min':'3','sales_max':'2'},
                        {'sales_max':str(MAX_COUNT+1)},{'sales_metric':'paid'}, {'sales_min':True}):
            with self.subTest(options=options),self.assertRaises(ValueError): sales_filters(options)

    def test_frozen_schema_adds_five_columns_and_valid_checks(self):
        """不修改旧模型；新列备注默认值及约束可被结构核对器理解。"""
        old=schema_v5.build_metadata().tables['list_observations']
        new=schema_v6.build_metadata().tables['list_observations']
        self.assertEqual(set(new.c.keys())-set(old.c.keys()),set(FIELDS))
        self.assertEqual(set(new.c.keys())-set(schema_v5.build_metadata().tables['list_observations'].c.keys()),set(FIELDS))
        for name in FIELDS: self.assertTrue(new.c[name].comment)
        for check in new.constraints:
            if isinstance(check,sa.CheckConstraint): check_expression_tree(str(check.sqltext))

    def test_snapshot_rejects_inconsistent_fields_and_unsupported_rule_version(self):
        """JSON数值不得与原文冲突，也不能混用半套字段或未知规则版本。"""
        from pathlib import Path
        from types import SimpleNamespace
        from ecommerce.modules.collection.database_records import batch_rows
        item={'title':'样例','product_index':1,'position':1,'sales_text':'1000+人收货'}
        for changes in ({'sales_count':1000},{**parse_sales(item['sales_text']),'sales_count':2000}, {'sales_parser_version':3}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                list(batch_rows(SimpleNamespace(), 'list', {'items':[{**item,**changes}]},Path('.')))

    def test_taobao_received_period_uses_confirmed_source_rule_and_preserves_explicit_period(self):
        """淘宝列表收货人数补近30天，普通文本与付款/件数不推断，明确周期不覆盖。"""
        for raw in ('1000+人收货','0人收货','１万＋人已收货'):
            with self.subTest(raw=raw):
                old=parse_sales(raw)
                new=parse_taobao_list_sales(raw)
                self.assertEqual(old['sales_period'],'unknown')
                self.assertEqual(new,{**old,'sales_period':'last_30_days'})
                self.assertEqual(parse_taobao_list_sales(raw,version=1),old)
        for raw in ('本月100人收货','累计100人收货','近30天100人收货','500人付款','500件已售',
                    '本月行业热销',None,'约1000+人收货'):
            self.assertEqual(parse_taobao_list_sales(raw),parse_sales(raw),raw)
        for version in (3,True,'2'):
            with self.assertRaises(ValueError): parse_taobao_list_sales('100人收货',version=version)

    def test_v1_and_v2_raw_exports_are_validated_by_their_own_rule(self):
        """旧JSON按V1核验且保留原文，新JSON按V2核验，混用规则拒绝。"""
        from pathlib import Path
        from types import SimpleNamespace
        from ecommerce.modules.collection.database_records import batch_rows
        item={'title':'样例','product_index':1,'position':1,'sales_text':'1000+人收货'}
        for version,period in ((1,'unknown'),(2,'last_30_days')):
            values={**item,**parse_taobao_list_sales(item['sales_text'],version=version),'sales_parser_version':version}
            record=list(batch_rows(SimpleNamespace(),'list',{'items':[values]},Path('.')))[0]
            self.assertEqual(record['values']['sales_period'],period)
            self.assertEqual(record['values']['payload'],values)
        invalid={**item,**parse_sales(item['sales_text']),'sales_parser_version':2}
        with self.assertRaises(ValueError): list(batch_rows(SimpleNamespace(),'list',{'items':[invalid]},Path('.')))
        import csv,json,tempfile
        from ecommerce.modules.collection.export import save_batch
        with tempfile.TemporaryDirectory() as folder:
            values={**item,**parse_taobao_list_sales(item['sales_text']),'sales_parser_version':2}
            json_path,csv_path=save_batch(folder,{'batch_id':'period-v2','items':[values]})
            self.assertEqual(json.loads(json_path.read_text(encoding='utf-8'))['items'][0]['sales_period'],'last_30_days')
            with csv_path.open(encoding='utf-8-sig',newline='') as stream:
                exported=list(csv.DictReader(stream))
            self.assertEqual(exported[0]['销量周期'],'近30天')
