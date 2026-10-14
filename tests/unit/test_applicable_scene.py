"""适用场景空值、未匹配回退与真实操作错误回归。"""
import unittest
from unittest.mock import MagicMock, patch
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from ecommerce.platforms.qianniu import product_attributes as attributes
from ecommerce.shared.exceptions import NeedsAttention


class ApplicableSceneTests(unittest.TestCase):
    def page(self, names, search_text="", readonly=False):
        page, group, popup, options, field = [MagicMock() for _ in range(5)]
        page.locator.side_effect = lambda selector: group if selector==attributes.ATTRIBUTE_GROUP else popup
        group.filter.return_value = group
        group.get_by_role.return_value = field
        field.get_attribute.side_effect = lambda name: "false" if name == "aria-expanded" else "" if readonly and name == "readonly" else None
        group.locator.return_value.count.return_value = 0
        popup.get_by_role.return_value.count.return_value = 1
        popup.get_by_role.return_value.input_value.return_value = search_text
        popup.locator.return_value = options
        options.count.return_value = len(names)
        options.locator.return_value.all_text_contents.return_value = names
        chosen = []
        targets = [MagicMock() for _ in names]
        for index,target in enumerate(targets):
            target.click.side_effect = lambda i=index, **kwargs: chosen.append(names[i])
        options.nth.side_effect = lambda index: targets[index]
        return page, field, options, chosen, popup

    def test_exact_match_uses_correct_field_option_not_free_text(self):
        cases = [(attributes.select_applicable_scene,"适用场景",["地板","沙发","衣物"],"沙发"),
                 (attributes.select_handle_shape,"握柄形态",["弧形柄","折叠柄","直柄"],"折叠柄")]
        for select,field_name,names,value in cases:
            with self.subTest(field=field_name):
                page,field,_,chosen,_ = self.page(names)
                with patch.object(attributes,"expect"):
                    result = select(page,f" {value} ")
                self.assertEqual(chosen,[value])
                self.assertIsNone(result.fallback_reason)
                page.get_by_title.assert_called_once_with(field_name,exact=True)
                field.fill.assert_not_called()

    def test_missing_unmatched_and_invalid_type_log_dynamic_first_option(self):
        for value in (None,"","  ","未知",123):
            with self.subTest(value=value):
                page,_,_,chosen,_ = self.page(["本页第一项","沙发"])
                with patch.object(attributes,"expect"), self.assertLogs(attributes.__name__,level="WARNING") as logs:
                    result = attributes.select_applicable_scene(page,value)
                self.assertEqual(chosen,["本页第一项"])
                self.assertEqual(result.selected,"本页第一项")
                self.assertEqual(result.requested,value)
                self.assertTrue(result.fallback_reason)
                self.assertIn("原因=", logs.output[0])
                self.assertIn("本页第一项", logs.output[0])

    def test_clear_search_before_defaulting_to_first(self):
        page,_,_,_,popup = self.page(["原始第一项"],search_text="旧筛选")
        with patch.object(attributes,"expect"), self.assertLogs(attributes.__name__,level="WARNING"):
            attributes.select_applicable_scene(page,None)
        popup.get_by_role.return_value.fill.assert_called_once_with("")

    def test_no_options_or_click_failure_does_not_report_success(self):
        page,_,_,_,_ = self.page([])
        with patch.object(attributes,"expect"):
            with self.assertRaises(NeedsAttention):
                attributes.select_applicable_scene(page,None)
        page,_,options,_,_ = self.page(["沙发"])
        options.nth.return_value = MagicMock()
        options.nth.side_effect = None
        options.nth.return_value.click.side_effect = PlaywrightTimeoutError("click blocked")
        with patch.object(attributes,"expect"), patch.object(attributes,"getLogger") as logger:
            with self.assertRaises(PlaywrightTimeoutError):
                attributes.select_applicable_scene(page,"沙发")
            logger.return_value.info.assert_not_called()

    def test_readonly_fields_exact_match_reads_selected_label(self):
        cases = [(attributes.select_pet_type,"适用宠物类型",["长毛猫","短毛犬","通用"],"通用"),
                 (attributes.select_adhesive_material,"粘面材质",["PU材质","双面胶","可水洗胶","强力胶","无痕可移胶","涤纶丝","热熔胶","环保胶","硅胶"],"硅胶"),
                 (attributes.select_adhesion_strength,"粘性强度",["中粘","弱粘","强粘"],"强粘")]
        for select,field_name,names,value in cases:
            with self.subTest(field=field_name):
                page,field,_,chosen,_ = self.page(names,readonly=True)
                with patch.object(attributes,"expect") as assertion:
                    result = select(page,value)
                self.assertEqual(chosen,[value])
                self.assertIsNone(result.fallback_reason)
                page.get_by_title.assert_called_once_with(field_name,exact=True)
                # 只读搜索框为空，检查显示标签而非input值。
                self.assertFalse(any(call[0].endswith("to_have_value") for call in assertion.mock_calls))
                self.assertTrue(any(call[0].endswith("to_have_text") for call in assertion.mock_calls))
                field.fill.assert_not_called()

    def test_pet_type_missing_or_unmatched_logs_first_actual_option(self):
        for value in (None,"","未知宠物"):
            with self.subTest(value=value):
                page,_,_,chosen,_ = self.page(["长毛猫","短毛犬","通用"],readonly=True)
                with patch.object(attributes,"expect"), self.assertLogs(attributes.__name__,level="WARNING") as logs:
                    result = attributes.select_pet_type(page,value)
                self.assertEqual(chosen,["长毛猫"])
                self.assertEqual(result.selected,"长毛猫")
                self.assertIn("适用宠物类型",logs.output[0])
                self.assertIn("原因=",logs.output[0])

    def test_handle_shape_fallback_logs_reason_and_actual_first(self):
        for value in (None,"","未知握柄"):
            with self.subTest(value=value):
                page,_,_,chosen,_ = self.page(["弧形柄","折叠柄","直柄"])
                with patch.object(attributes,"expect"), self.assertLogs(attributes.__name__,level="WARNING") as logs:
                    result = attributes.select_handle_shape(page,value)
                self.assertEqual(chosen,["弧形柄"])
                self.assertEqual(result.selected,"弧形柄")
                self.assertIn("握柄形态",logs.output[0])
                self.assertIn("原因=",logs.output[0])

    def test_adhesive_material_fallback_logs_actual_first(self):
        for value in (None,"","未列出材质"):
            with self.subTest(value=value):
                page,_,_,chosen,_ = self.page(["PU材质","硅胶"],readonly=True)
                with patch.object(attributes,"expect"), self.assertLogs(attributes.__name__,level="WARNING") as logs:
                    result = attributes.select_adhesive_material(page,value)
                self.assertEqual(chosen,["PU材质"])
                self.assertEqual(result.selected,"PU材质")
                self.assertIn("粘面材质",logs.output[0])
                self.assertIn("原因=",logs.output[0])

    def test_adhesion_strength_defaults_to_list_first_not_recommended_value(self):
        for value in (None,"","不存在的粘度"):
            with self.subTest(value=value):
                page,_,_,chosen,_ = self.page(["中粘","弱粘","强粘"],readonly=True)
                with patch.object(attributes,"expect"), self.assertLogs(attributes.__name__,level="WARNING") as logs:
                    result = attributes.select_adhesion_strength(page,value)
                self.assertEqual(chosen,["中粘"])
                self.assertEqual(result.selected,"中粘")
                self.assertIn("粘性强度",logs.output[0])
                self.assertIn("原因=",logs.output[0])
