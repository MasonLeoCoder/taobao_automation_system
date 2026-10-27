"""采集字段语义、首屏范围、去重和本地导出的边界。"""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ecommerce.cli.app import parser
from ecommerce.cli.commands import collection
from ecommerce.modules.collection.export import csv_value, save_batch
from ecommerce.platforms.taobao.cards import page_signature, parse_cards, parse_price, product_identity, validate_limit
from ecommerce.shared.config import load_settings


def card(item_id="123456", **values):
    return {"position": 1, "type": "product", "title": "可撕式粘毛器", "price_number_text": "24.75", "currency_symbol": "¥",
            "price_label": "补贴后", "sales_text": "4万+人付款", "shop_name": "样例旗舰店",
            "location_text": "浙江 金华", "dom_item_id": None, "shop_top_ids": [],
            "link": {"scheme": "https:", "host": "item.taobao.com", "path": "/item.htm", "ids": [item_id]},
            "ad_label": None, **values}


def snapshot(cards=None, **values):
    cards = [card()] if cards is None else cards
    return {"origin": "https://s.taobao.com", "path": "/search", "query_keywords": ["粘毛器"],
            "inputs": ["粘毛器"], "paging": {"page": ["1"]}, "page_numbers": ["1"],
            "sort_labels": ["综合"], "sort_parameter": [], "cards": cards, "slots": cards,
            "root_count": 1, "loaded_slot_count": len(cards), "loaded_card_count": len(cards), **values}


class TaobaoCollectionTests(unittest.TestCase):
    def test_limit_validated_before_launch(self):
        for value in (0, -1, True, "20", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_limit(value)
        self.assertIsNone(validate_limit(None))
        self.assertEqual(validate_limit(1000), 1000)
        with tempfile.TemporaryDirectory() as folder, patch.object(collection, "run_task") as runner:
            with self.assertRaises(ValueError):
                collection.execute_collect(load_settings(root=Path(folder)), "粘毛器", limit=0)
            runner.assert_not_called()

    def test_default_collect_routes_without_a_limit(self):
        args = parser().parse_args(["taobao", "collect", "--keyword", "粘毛器"])
        self.assertIsNone(args.limit)
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(root=Path(folder))
            with patch.object(collection, "run_task", side_effect=lambda settings, name, action: action()), \
                    patch("ecommerce.modules.collection.service.collect_keyword", return_value="done") as collect:
                self.assertEqual(collection.execute_collect(settings, "粘毛器"), "done")
                self.assertIsNone(collect.call_args.kwargs["limit"])

    def test_cli_collect_routes_independent_profile_and_shared_lock(self):
        args = parser().parse_args(["taobao", "collect", "--keyword", "粘毛器", "--limit", "10", "--no-wait"])
        self.assertEqual((args.action, args.limit, args.no_wait), ("collect", 10, True))
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(root=Path(folder))
            with patch.object(collection, "run_task", side_effect=lambda settings, name, action: action()) as runner, \
                    patch("ecommerce.modules.collection.service.collect_keyword", return_value="done") as collect:
                self.assertEqual(collection.execute_collect(settings, " 粘毛器 ", limit=10, wait=False), "done")
                self.assertEqual(runner.call_args.args[0].profile_path, settings.for_platform("taobao").profile_path)
                self.assertEqual(runner.call_args.args[1], "collection.taobao.collect")
                self.assertEqual(collect.call_args.args[0].profile_path, settings.for_platform("taobao").profile_path)
                self.assertEqual(collect.call_args.kwargs["limit"], 10)

    def test_price_and_sales_keep_display_meaning(self):
        row = parse_cards(snapshot(), "粘毛器", 20)["items"][0]
        self.assertEqual(row["price_text"], "¥24.75 补贴后")
        self.assertEqual(row["price_value"], "24.75")
        self.assertEqual(row["sales_text"], "4万+人付款")
        self.assertEqual(row["ad_status"], "not_identified")
        self.assertNotIn("monthly_sales", row)
        received = parse_cards(snapshot([card(sales_text="1万+人收货")]), "粘毛器", 20)["items"][0]
        self.assertEqual(received["sales_text"], "1万+人收货")
        for text in ("5-10", "面议", "-1", "1,2", "1.234", "NaN"):
            self.assertIsNone(parse_price(text))
        self.assertEqual(parse_price("1,234.50"), "1234.50")
        row = parse_cards(snapshot([card(price_number_text="5-10")]), "粘毛器", 20)["items"][0]
        self.assertEqual(row["price_text"], "¥5-10 补贴后")
        self.assertIn("price_value", row["missing_fields"])

    def test_missing_optional_fields_do_not_become_zero(self):
        row = parse_cards(snapshot([card(shop_name=None, sales_text=None, link=None, dom_item_id="123456")]), "粘毛器", 20)["items"][0]
        self.assertIsNone(row["sales_text"])
        self.assertEqual(row["link_status"], "constructed_from_dom_id")
        self.assertEqual(row["field_states"]["sales_text"], "not_provided")
        self.assertIn("shop_name", row["missing_fields"])

    def test_user_confirmed_image_marker_marks_ad_recommendation(self):
        from ecommerce.platforms.taobao.ad_markers import AD_RECOMMENDATION_IMAGE_URL
        row = parse_cards(snapshot([card(ad_recommendation=True)]), "粘毛器", 20)["items"][0]
        self.assertTrue(row["ad_recommendation"])
        self.assertEqual(row["ad_label"], "广告推荐位")
        self.assertEqual(row["ad_status"], "identified")
        self.assertEqual(row["ad_marker_url"], AD_RECOMMENDATION_IMAGE_URL)
        payload = {"batch_id": "ad-example", "items": [row]}
        with tempfile.TemporaryDirectory() as folder:
            _, csv_path = save_batch(folder, payload)
            with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                output = list(csv.DictReader(stream))
            self.assertEqual(output[0]["广告推荐位"], "是")
            self.assertEqual(output[0]["广告识别依据"], "命中用户确认的广告标识图片")

    def test_absent_image_marker_is_not_classified_as_natural_ranking(self):
        row = parse_cards(snapshot(), "粘毛器", 20)["items"][0]
        self.assertFalse(row["ad_recommendation"])
        self.assertEqual(row["ad_status"], "not_identified")

    def test_trusted_direct_links_are_canonical_and_ambiguous_ids_rejected(self):
        item_id, url, note = product_identity(card())
        self.assertEqual(url, "https://item.taobao.com/item.htm?id=123456")
        altered = card()
        altered["link"]["host"] = "item.taobao.com.evil.example"
        self.assertIsNone(product_identity(altered)[0])
        altered["link"]["host"] = "item.taobao.com"
        altered["link"]["ids"] = ["123456", "654321"]
        self.assertIsNone(product_identity(altered)[0])

    def test_tracking_link_needs_corroborating_public_item_id(self):
        value = card()
        value["link"]["host"] = "mclick.simba.taobao.com"
        value["link"]["path"] = "/cc_im"
        self.assertIsNone(product_identity(value)[0])
        value["shop_top_ids"] = ["123456"]
        self.assertEqual(product_identity(value)[0], "123456")
        value["shop_top_ids"] = ["999999"]
        value["dom_item_id"] = "123456"
        self.assertEqual(product_identity(value)[0], "123456")

    def test_same_product_in_different_positions_remains_separate(self):
        cards = [card(), card(position=2, ad_recommendation=True), card("654321", position=3), card(position=4), card(position=5)]
        result = parse_cards(snapshot(cards), "粘毛器", 4)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual([row["position"] for row in result["items"]], [1, 2, 3, 4])
        self.assertEqual(result["counts"]["same_id_extra_positions"], 3)
        self.assertEqual(result["counts"]["exported_ad_count"], 1)

    def test_missing_required_card_fields_are_reported_and_limit_is_bounded(self):
        result = parse_cards(snapshot([card(title=None), card("234567", position=2), card("345678", position=3)]), "粘毛器", 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["blocking_issues"][0]["position"], 1)
        self.assertEqual(result["blocking_issues"][0]["type"], "invalid_product")

    def test_survey_does_not_consume_product_limit_and_counts_balance(self):
        values = [card(), {"position": 2, "type": "search_experience_survey"}, card("234567", position=3)]
        result = parse_cards(snapshot(values), "粘毛器", 2)
        self.assertEqual([row["slot_index"] for row in result["items"]], [1, 3])
        self.assertEqual([row["product_index"] for row in result["items"]], [1, 2])
        self.assertEqual(result["counts"]["survey_count"], 1)
        self.assertTrue(result["counts"]["counts_balanced"])
        self.assertFalse(result["blocking_issues"])

    def test_no_limit_keeps_all_products_and_nonproduct_counts(self):
        values = [card(str(100000 + i), position=i + 1) for i in range(61)]
        values += [{"position": 62, "type": "search_experience_survey"},
                   {"position": 63, "type": "search_suggestions"}]
        result = parse_cards(snapshot(values), "粘毛器")
        self.assertEqual(len(result["items"]), 61)
        self.assertIsNone(result["limit"])
        self.assertFalse(result["limit_reached"])
        self.assertEqual(result["counts"]["survey_count"], 1)
        self.assertEqual(result["counts"]["search_suggestions_count"], 1)
        self.assertEqual(result["counts"]["product_slot_count"], 61)
        self.assertTrue(result["counts"]["counts_balanced"])
        limited = parse_cards(snapshot(values), "粘毛器", 52)
        self.assertEqual(len(limited["items"]), 52)
        self.assertTrue(limited["limit_reached"])

    def test_unknown_in_export_range_blocks_but_outside_range_is_recorded(self):
        values = [card(), {"position": 2, "type": "unknown", "reason": "未知卡片"}, card("234567", position=3)]
        result = parse_cards(snapshot(values), "粘毛器", 2)
        self.assertEqual(result["counts"]["unknown_count"], 1)
        self.assertEqual(len(result["blocking_issues"]), 1)
        result = parse_cards(snapshot(values), "粘毛器", 1)
        self.assertEqual(len(result["issues"]), 1)
        self.assertFalse(result["blocking_issues"])
        result = parse_cards(snapshot(values), "粘毛器")
        self.assertEqual(len(result["blocking_issues"]), 1)

    def test_dom_and_link_identity_conflict_is_not_exported(self):
        value = card(dom_item_id="999999")
        result = parse_cards(snapshot([value]), "粘毛器", 20)
        self.assertFalse(result["items"])
        self.assertIn("不一致", result["blocking_issues"][0]["reason"])

    def test_page_keyword_offset_and_sort_ambiguity_are_rejected(self):
        alterations = [{"query_keywords": ["其他词"]}, {"inputs": ["其他词"]},
                       {"origin": "https://s.taobao.com.evil.example"}, {"paging": {"page": ["2"]}},
                       {"page_numbers": ["2"]}, {"paging": {"s": ["48"]}}, {"paging": {"page": ["1", "2"]}},
                       {"sort_labels": ["综合", "销量"]}, {"sort_parameter": ["sale", "price"]}]
        for values in alterations:
            with self.subTest(values=values), self.assertRaises(ValueError):
                page_signature(snapshot(**values), "粘毛器")

    def test_csv_formula_text_and_long_identifiers_preserve_safe_values(self):
        for value in ("=1+1", "  =1+1", "+cmd", "-1", "@SUM(A1)", "\tunsafe"):
            self.assertTrue(csv_value(value).startswith("'"))
        self.assertEqual(csv_value("1234567890123456789", identifier=True), "'1234567890123456789")
        self.assertEqual(csv_value("24.75"), "24.75")

    def test_export_roundtrip_keeps_unicode_quotes_and_original_json(self):
        row = parse_cards(snapshot(), "粘毛器", 20)["items"][0]
        row["title"] = '=1+1,"粘毛器"\n标题'
        payload = {"batch_id": "test-batch", "captured_at": "2026-10-21T04:00:00+00:00", "keyword": "粘毛器",
                   "page_number": 1, "sort_label": "综合", "items": [row]}
        with tempfile.TemporaryDirectory() as folder:
            json_path, csv_path = save_batch(folder, payload)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["items"][0]["title"], row["title"])
            self.assertTrue(csv_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                output = list(csv.DictReader(stream))
            self.assertEqual(len(output), 1)
            self.assertEqual(output[0]["商品标题"], "'" + row["title"])
            self.assertEqual(output[0]["价格数值"], "24.75")
            self.assertEqual(output[0]["销量提示原文"], "4万+人付款")
            self.assertEqual(output[0]["广告推荐位"], "未检测到指定标记")
            with self.assertRaises(FileExistsError):
                save_batch(folder, payload)


if __name__ == "__main__":
    unittest.main()
