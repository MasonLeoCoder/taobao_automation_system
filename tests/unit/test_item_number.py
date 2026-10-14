"""货号原值保护、回读失败和平台错误不能被报告为成功。"""
import unittest
from unittest.mock import MagicMock, patch
from ecommerce.platforms.qianniu import product_attributes as attributes
from ecommerce.shared.exceptions import NeedsAttention


class ItemNumberTests(unittest.TestCase):
    def page(self, current="", errors=()):
        page = MagicMock()
        group = page.locator.return_value.filter.return_value
        field = group.get_by_role.return_value
        field.input_value.return_value = current
        error = group.locator.return_value
        error.count.return_value = len(errors)
        error.all_text_contents.return_value = list(errors)
        return page, field

    def test_invalid_input_and_different_existing_value_are_not_overwritten(self):
        for value in (None, "", "  ", 123):
            with self.subTest(value=value):
                page, field = self.page()
                with self.assertRaises(ValueError):
                    attributes.fill_item_number(page, value)
                field.fill.assert_not_called()
        page, field = self.page(current="旧货号")
        with patch.object(attributes, "expect"), self.assertRaises(NeedsAttention):
            attributes.fill_item_number(page, "00123")
        field.fill.assert_not_called()

    def test_same_value_is_not_filled_again(self):
        page, field = self.page(current="00123")
        with patch.object(attributes, "expect"):
            attributes.fill_item_number(page, "00123")
        field.fill.assert_not_called()

    def test_truncated_value_or_platform_error_does_not_log_success(self):
        page, _ = self.page()
        with patch.object(attributes, "expect") as assertion, patch.object(attributes, "getLogger") as logger:
            assertion.return_value.to_have_value.side_effect = AssertionError("货号被截断")
            with self.assertRaises(AssertionError):
                attributes.fill_item_number(page, "00123")
            logger.return_value.info.assert_not_called()
        page, _ = self.page(errors=("货号不符合要求",))
        with patch.object(attributes, "expect"), patch.object(attributes, "getLogger") as logger:
            with self.assertRaisesRegex(NeedsAttention, "货号不符合要求"):
                attributes.fill_item_number(page, "00123")
            logger.return_value.info.assert_not_called()
