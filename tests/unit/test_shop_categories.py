"""多分类部分匹配、缺失日志、选择上限与真实失败回归。"""
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from ecommerce.modules.product.workbook import ProductWorkbook
from ecommerce.platforms.qianniu import product_shop_category as platform


class ShopCategoryTests(unittest.TestCase):
    def make_page(self, definitions, existing=()):
        page, group, tree, trigger = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        page.locator.side_effect = lambda selector: group if selector == platform.SHOP_CATEGORY else tree
        group.is_visible.return_value = True
        group.get_by_role.return_value = trigger
        trigger.get_attribute.return_value = "false"
        selected = set(existing)
        items = {}
        for name, settings in definitions.items():
            item, checkbox, label = MagicMock(), MagicMock(), MagicMock()
            item.count.return_value = settings.get("count", 1)
            item.get_by_role.return_value = checkbox
            item.locator.return_value = label
            checkbox.is_checked.side_effect = lambda n=name: n in selected
            checkbox.is_enabled.return_value = settings.get("enabled", True)
            label.click.side_effect = lambda n=name, **kw: selected.add(n)
            items[name] = item
        tree.get_by_role.side_effect = lambda role, name, exact: items[name]
        tree.locator.return_value.count.side_effect = lambda: len(selected)
        return page, items, trigger, selected

    def test_partial_matches_keep_existing_and_log_missing(self):
        page, items, trigger, chosen = self.make_page({"家居清洁":{}, "猫毛清洁":{}, "测试无":{"count":0}}, existing=["家居清洁"])
        with patch.object(platform,"expect"), self.assertLogs(platform.__name__,level="INFO") as logs:
            result = platform.select_shop_categories(page,["家居清洁","测试无","猫毛清洁","猫毛清洁"])
        self.assertEqual(result.selected,["家居清洁","猫毛清洁"])
        self.assertEqual(result.skipped,{"测试无":"未匹配到同名店铺分类"})
        items["家居清洁"].locator.return_value.click.assert_not_called()
        items["猫毛清洁"].locator.return_value.click.assert_called_once()
        self.assertIn("WARNING", "\n".join(logs.output))
        self.assertIn("测试无", "\n".join(logs.output))
        trigger.press.assert_called_once_with("Escape")

    def test_all_unmatched_return_zero_and_continue(self):
        page, items, _, _ = self.make_page({"缺失":{"count":0},"重名":{"count":2},"禁用":{"enabled":False}})
        with patch.object(platform,"expect"), self.assertLogs(platform.__name__,level="WARNING"):
            result = platform.select_shop_categories(page,["缺失","重名","禁用"])
        self.assertEqual(result.selected,[])
        self.assertEqual(len(result.skipped),3)
        for item in items.values(): item.locator.return_value.click.assert_not_called()

    def test_twenty_limit_counts_existing_and_keeps_matches(self):
        existing = [f"原分类{i}" for i in range(19)]
        page, items, _, chosen = self.make_page({"新增一":{},"新增二":{},"原分类0":{}},existing=existing)
        with patch.object(platform,"expect"), self.assertLogs(platform.__name__,level="WARNING"):
            result = platform.select_shop_categories(page,["新增一","新增二","原分类0"])
        self.assertEqual(result.selected,["新增一","原分类0"])
        self.assertIn("20项",result.skipped["新增二"])
        self.assertEqual(len(chosen),20)
        items["新增二"].locator.return_value.click.assert_not_called()

    def test_real_click_failure_is_not_treated_as_missing(self):
        page, items, trigger, _ = self.make_page({"有效":{}})
        items["有效"].locator.return_value.click.side_effect = PlaywrightTimeoutError("click blocked")
        with patch.object(platform,"expect"):
            with self.assertRaises(PlaywrightTimeoutError):
                platform.select_shop_categories(page,["有效"])
        trigger.press.assert_called_once_with("Escape")

    def test_workbook_separators_deduplicate_in_order(self):
        data = ProductWorkbook(Path("test.xlsx"),"Sheet1",{"分类":" 家居清洁;猫毛清洁；除味除臭\n家居清洁，清洁收纳、清洁耗材 "},[])
        self.assertEqual(data.shop_categories(),["家居清洁","猫毛清洁","除味除臭","清洁收纳","清洁耗材"])
        with self.assertRaises(ValueError):
            ProductWorkbook(Path("test.xlsx"),"Sheet1",{"分类":";；,\n"},[]).shop_categories()
