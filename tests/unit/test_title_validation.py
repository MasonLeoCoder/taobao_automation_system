"""页面长度及业务错误不能被输入回读成功掩盖。"""
import unittest
from unittest.mock import MagicMock, patch
from ecommerce.platforms.qianniu import product_basic as basic
from ecommerce.shared.exceptions import NeedsAttention


class TitleValidationTests(unittest.TestCase):
    def page(self, count, errors=()):
        page = MagicMock()
        group = page.locator.return_value
        group.get_by_role.return_value.input_value.return_value = ""
        counter, error = MagicMock(), MagicMock()
        counter.first.inner_text.return_value = count
        error.count.return_value = len(errors)
        error.all_text_contents.return_value = list(errors)
        group.locator.side_effect = lambda selector: counter if selector.startswith(".content-after") else error
        return page

    def test_too_short_and_platform_error_do_not_log_success(self):
        for count, errors in [("16/30",()),("22/30",("内容不符合要求",))]:
            with self.subTest(count=count), patch.object(basic,"expect"), patch.object(basic,"getLogger") as logger:
                with self.assertRaises(NeedsAttention):
                    basic.fill_guide_title(self.page(count,errors),"这是我的导购标题" if count == "16/30" else "测试商品导购标题内容A2")
                logger.return_value.info.assert_not_called()

    def test_valid_count_and_existing_value_protection(self):
        with patch.object(basic,"expect"):
            basic.fill_guide_title(self.page("20/30"),"测试商品导购标题内容")
            page = self.page("20/30")
            page.locator.return_value.get_by_role.return_value.input_value.return_value = "旧导购标题"
            with self.assertRaises(NeedsAttention):
                basic.fill_guide_title(page,"新导购标题")
            basic.fill_guide_title(page,"新导购标题",replace=True)

    def test_excessive_length_does_not_log_success(self):
        with patch.object(basic,"expect"), patch.object(basic,"getLogger") as logger:
            with self.assertRaisesRegex(ValueError, "32/30"):
                basic.fill_guide_title(self.page("32/30"),"这是我的导购标题" + "2" * 16)
            logger.return_value.info.assert_not_called()
