"""账本金额、月份与输入边界验证，不访问正式库。"""
from unittest import TestCase
from unittest.mock import patch
from decimal import Decimal
from ecommerce.modules.ledger import service
from ecommerce.infrastructure.storage.mysql import _normalize_type


class LedgerRulesTests(TestCase):
    def test_amount_precision_and_invalid_numbers(self):
        self.assertEqual(service.amount('10.01'), Decimal('10.01'))
        self.assertEqual(service.amount('0', allow_zero=True), Decimal('0'))
        for value in ('0', '-1', '1.001', 'NaN', 'Infinity', '1e2', '10000000000', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.amount(value)

    def test_month_boundaries_and_invalid_values(self):
        self.assertEqual(str(service.month('2026-12')[1]), '2027-01-01')
        for value in ('2026-13', '2026-1', '9999-12', '0001-01'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.month(value)

    def test_historical_date_is_preserved_and_not_created_time(self):
        form = dict(id='a'*32, occurred_at='2026-10-28T12:34:56', direction='expense',
                    category_id='1', amount='38.25', note='补记', revision='0')
        with patch('ecommerce.modules.ledger.service.repository.save_entry') as save:
            service.save_entry(None, form)
            self.assertEqual(save.call_args.args[3]['occurred_at'].isoformat(), '2026-10-28T12:34:56')
            for changes in (dict(direction='other'), dict(occurred_at='2026-11-03T12:00:00+08:00'), dict(note='字'*501)):
                with self.assertRaises(ValueError):
                    service.save_entry(None, {**form, **changes})

    def test_mysql_numeric_decimal_alias(self):
        self.assertEqual(_normalize_type('NUMERIC(12, 2)'), _normalize_type('DECIMAL(12, 2)'))

    def test_shares_use_separate_denominators_and_amount_order(self):
        """同名分类分方向计算，明细筛选之外的整月合计作为分母。"""
        groups = [dict(name='补单', direction='expense', amount=Decimal('2')),
                  dict(name='代发采购', direction='expense', amount=Decimal('10')),
                  dict(name='提现', direction='income', amount=Decimal('1'))]
        shares = service.category_shares(groups, dict(expense=Decimal('12'), income=Decimal('1')))
        self.assertEqual([row['name'] for row in shares['expense']], ['代发采购', '补单'])
        self.assertEqual([row['percent'] for row in shares['expense']], [Decimal('83.33'), Decimal('16.67')])
        self.assertEqual(shares['income'][0]['percent'], Decimal('100'))
        self.assertEqual(service.category_shares([], dict(expense=Decimal(0), income=Decimal(0))), dict(expense=[], income=[]))
