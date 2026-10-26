"""真实浏览器加载本地列表夹具，验证一次搜索、只读采集和人工恢复。"""
import json
import csv
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection import service
from ecommerce.platforms.taobao.cards import SNAPSHOT, parse_cards
from ecommerce.platforms.taobao.search import canonical_search_url, inspect_search, open_search
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

HOME = '<span class="site-nav-login-info-nick">测试账号</span><form id="J_TSearchForm" action="https://s.taobao.com/search" target="_blank"><input id="q" name="q"><button class="btn-search">搜索</button></form>'


def card_html(item_id="123456", title="可撕式粘毛器", extra="", tracking=False):
    href = f"https://mclick.simba.taobao.com/cc_im?id={item_id}&e=do-not-export" if tracking else f"https://item.taobao.com/item.htm?id={item_id}&token=do-not-export"
    return f"""<div class="search-content-col" style="width:250px;min-height:300px">
    <a id="item_id_{item_id}" class="doubleCardWrapperAdapt--fixture" href="{href}" style="display:block">
    <div class="mainPicAdaptWrapper--fixture"><img class="mainPic--fixture" src="https://img.alicdn.com/fixture-main.webp" width="100" height="100" alt=""></div>
    <div class="title--fixture">{title}</div>
    <div class="innerNormalPriceWrapper--fixture"><span class="unit--fixture">¥</span>
    <div class="innerPriceWrapper--fixture"><span class="priceInt--fixture">24</span><span class="priceFloat--fixture">.75</span></div>
    <span class="priceDesc--fixture">补贴后</span><span class="realSales--fixture">4万+人付款</span>
    <div class="procity--fixture">浙江</div><div class="procity--fixture">金华</div></div></a>
    <a class="shopName--fixture" href="https://store.taobao.com/category.htm?appUid=fixture_shop&topIds={item_id}">
    <span>买过的店</span><span class="shopNameText--fixture">样例旗舰店</span></a>{extra}</div>"""


def results_html(cards, selected="综合"):
    tabs = '<ul class="next-tabs-nav">' + ''.join(
        f'<li class="next-tabs-tab customTabItem--fixture {"active" if label == selected else ""}"><div class="next-tabs-tab-inner">{label}</div></li>'
        for label in ("综合", "销量")) + '</ul>'
    script = """<script>
    window.sortClicks=0;
    document.querySelectorAll('.next-tabs-tab')[1].onclick=()=>{
      window.sortClicks++;
      if(window.ignoreSortClick)return;
      document.querySelectorAll('.next-tabs-tab').forEach(e=>e.classList.remove('active'));
      document.querySelectorAll('.next-tabs-tab')[1].classList.add('active');
      if(window.keepOldResults)return;
      const list=document.querySelector('#content_items_wrapper');list.setAttribute('aria-busy','true');
      setTimeout(()=>{list.innerHTML=window.sortedCards||list.innerHTML;list.removeAttribute('aria-busy');},window.sortDelay||30);
    };
    </script>"""
    return '<input name="q"><div class="next-pagination-display"><em>1</em>/100</div>' + tabs + '<main><div id="content_items_wrapper">' + cards + '</div></main>' + script


def full_page_html():
    cards = [card_html(str(100000 + i)) for i in range(61)]
    cards.insert(26, '<div class="search-content-col"><div id="pcSearchInvestigationCard_root_container">对本次搜索体验满意吗</div></div>')
    cards.insert(27, '<div class="search-content-col"><div class="suggestItemCard--fixture"><div>大家都在搜</div><a href="https://s.taobao.com/search?q=other">粘毛器替换纸</a></div></div>')
    return results_html(''.join(cards), selected="销量")


@unittest.skipUnless(os.environ.get("TAOBAO_BROWSER_TESTS") == "1", "显式启用本地浏览器测试")
class TaobaoCollectionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        # 主图下载也只能使用本地夹具，不能绕过浏览器路由访问真实 CDN。
        from tests.unit.test_list_images import Response
        from tests.integration.test_taobao_detail_images_browser import png
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(png(1))
        download_patch = patch('ecommerce.modules.collection.list_images.build_opener', return_value=opener)
        download_patch.start()
        self.addCleanup(download_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(load_settings(root=Path(self.temp.name)).for_platform("taobao"), headless=True,
                                taobao_timing={key: (0, 0) for key in DEFAULT_TAOBAO_TIMING})
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.requests = []
        self.body = results_html(card_html() + card_html("234567", tracking=True))

        def route(request):
            self.requests.append(request.request.url)
            url = urlsplit(request.request.url)
            if url.hostname == "s.taobao.com":
                keyword = parse_qs(url.query)["q"][0]
                body = self.body + '<script>document.querySelector("input[name=q]").value=' + json.dumps(keyword) + '</script>'
            else:
                body = HOME
            request.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        self.context.route("**/*", route)
        self.home = self.context.new_page()
        self.home.goto("https://www.taobao.com/")

    def guard(self, attention=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def read_payload(self):
        paths = list((self.settings.workspace / "output/raw/taobao_products").glob("*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def test_collect_service_searches_once_and_never_opens_details_or_scrolls(self):
        from ecommerce.platforms.taobao import session
        @contextmanager
        def browser(settings):
            yield self.context, self.home
        with patch.object(session, "base_browser_session", browser):
            message = service.collect_keyword(self.settings, "粘毛器", limit=2, wait=False)
        payload = self.read_payload()
        self.assertEqual(payload["collected_count"], 2)
        self.assertEqual(payload["items"][1]["product_id"], "234567")
        self.assertEqual(payload["items"][0]["shop_name"], "样例旗舰店")
        self.assertEqual(payload["items"][0]["sales_text"], "4万+人付款")
        self.assertEqual(payload["sort_label"], "销量")
        self.assertEqual(payload["sort_status"], "confirmed")
        page = next(p for p in self.context.pages if urlsplit(p.url).hostname == "s.taobao.com")
        self.assertEqual(page.evaluate("sortClicks"), 1)
        self.assertEqual(len([url for url in self.requests if urlsplit(url).hostname == "s.taobao.com"]), 1)
        self.assertTrue(all(urlsplit(url).hostname in {"s.taobao.com", "www.taobao.com", "img.alicdn.com"} for url in self.requests))
        self.assertFalse(payload["scroll_performed"])
        self.assertNotIn("do-not-export", json.dumps(payload))
        self.assertIn("2 条商品展示记录", message)

    def test_all_tracking_cards_can_confirm_search_readiness(self):
        self.body = results_html(card_html(tracking=True))
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            self.assertEqual(result.status, "results")
            self.assertEqual(result.visible_items, 0)
            service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(self.read_payload()["collected_count"], 1)

    def test_default_collect_exports_full_page_over_fifty_products_and_skips_suggestions(self):
        self.body = full_page_html()
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            service.collect_from_search(self.settings, self.context, result)
        payload = self.read_payload()
        self.assertEqual(payload["collected_count"], 61)
        self.assertIsNone(payload["requested_limit"])
        self.assertEqual(payload["collection_status"], "all_loaded_cards_collected")
        self.assertEqual(payload["items"][-1]["slot_index"], 63)
        self.assertEqual(payload["items"][-1]["product_index"], 61)
        self.assertEqual(payload["counts"]["survey_count"], 1)
        self.assertEqual(payload["counts"]["search_suggestions_count"], 1)
        self.assertEqual(payload["counts"]["unknown_count"], 0)
        self.assertTrue(payload["counts"]["counts_balanced"])
        self.assertEqual(len(payload["skipped"]), 2)
        self.assertFalse(payload["pagination_performed"])
        self.assertEqual(payload["detail_pages_opened"], 0)
        self.assertEqual(len([url for url in self.requests if urlsplit(url).hostname == "s.taobao.com"]), 1)
        csv_path = next((self.settings.workspace / "output/raw/taobao_products").glob("*.csv"))
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 61)

    def test_explicit_limit_above_fifty_truncates_only_valid_products(self):
        self.body = full_page_html()
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            service.collect_from_search(self.settings, self.context, result, limit=52)
        payload = self.read_payload()
        self.assertEqual(payload["collected_count"], 52)
        self.assertEqual(payload["requested_limit"], 52)
        self.assertEqual(payload["collection_status"], "limit_reached")
        self.assertEqual(payload["items"][-1]["slot_index"], 54)

    def test_suggestions_require_matching_structure_and_heading_and_no_product(self):
        mismatches = [
            '<div class="search-content-col"><div>大家都在搜</div></div>',
            '<div class="search-content-col"><div class="suggestItemCard--fixture">未确认内容</div></div>',
            card_html(extra='<div class="suggestItemCard--fixture"><div>大家都在搜</div></div>'),
        ]
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            for markup in mismatches:
                with self.subTest(markup=markup):
                    result.page.locator('#content_items_wrapper').evaluate('(e,html)=>e.innerHTML=html', markup)
                    parsed = parse_cards(result.page.evaluate(SNAPSHOT), "粘毛器")
                    self.assertEqual(parsed["counts"]["search_suggestions_count"], 0)
                    self.assertEqual(len(parsed["blocking_issues"]), 1)

    def test_ad_marker_is_scoped_per_card_and_hidden_copy_counts_as_presence(self):
        from ecommerce.platforms.taobao.ad_markers import AD_RECOMMENDATION_IMAGE_URL
        marker = f'<img class="mainP4pPic--changedHash" src="{AD_RECOMMENDATION_IMAGE_URL}">'
        hidden = f'<div style="display:none">{marker}</div>'
        unrelated = '<img class="mainP4pPic--changedHash" src="https://example.invalid/other.png">'
        self.body = results_html(card_html(extra=marker + hidden) + card_html("234567", extra=unrelated)
                                 + card_html("345678", extra=hidden))
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            parsed = parse_cards(result.page.evaluate(SNAPSHOT), "粘毛器", 20)
        self.assertEqual([row["ad_recommendation"] for row in parsed["items"]], [True, False, True])
        self.assertEqual(len(parsed["items"]), 3)

    def test_unknown_card_structure_pauses_without_creating_zero_success(self):
        self.body = '<input name="q"><a href="https://item.taobao.com/item.htm?id=123456">旧版列表证据</a>'
        with self.guard(lambda message: "停止"):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            with self.assertRaises(TaskStopped):
                service.collect_from_search(self.settings, self.context, result)
        self.assertFalse(list((self.settings.workspace / "output/raw/taobao_products").glob("*.json")))

    def test_challenge_requires_feedback_and_does_not_resubmit_search(self):
        messages = []
        def attention(message):
            messages.append(message)
            page = next(page for page in self.context.pages if urlsplit(page.url).hostname == "s.taobao.com")
            page.locator(".J_MIDDLEWARE_FRAME_WIDGET").evaluate("e=>e.remove()")
            return "继续"
        with self.guard(attention):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            result.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div class=J_MIDDLEWARE_FRAME_WIDGET>请拖动下方滑块完成验证</div>')")
            service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(len([url for url in self.requests if urlsplit(url).hostname == "s.taobao.com"]), 1)

    def test_page_two_requires_restore_before_reading_or_saving(self):
        messages = []
        def attention(message):
            messages.append(message)
            result.page.evaluate("history.replaceState(null,'','?q='+encodeURIComponent('粘毛器')+'&page=1');document.querySelector('.next-pagination-display em').innerText='1'")
            return "继续"
        with self.guard(attention):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            result.page.evaluate("history.replaceState(null,'','?q='+encodeURIComponent('粘毛器')+'&page=2');document.querySelector('.next-pagination-display em').innerText='2'")
            service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.read_payload()["page_number"], 1)

    def test_hidden_templates_and_parent_opacity_do_not_become_products(self):
        self.body = results_html('<div style="opacity:0">' + card_html() + '</div>' + card_html("234567"))
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            raw = result.page.evaluate(SNAPSHOT)
            parsed = parse_cards(raw, "粘毛器", 20)
        self.assertEqual([row["product_id"] for row in parsed["items"]], ["234567"])

    def test_explicit_empty_result_exports_empty_dataset(self):
        self.body = '<input name="q"><p>抱歉，没有找到相关宝贝</p>'
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            service.collect_from_search(self.settings, self.context, result)
        payload = self.read_payload()
        self.assertEqual(payload["collection_status"], "empty")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["sort_status"], "not_applicable_empty_results")

    def test_page_change_during_screenshot_pauses_then_saves_restored_conditions(self):
        messages = []
        changed = False
        def attention(message):
            messages.append(message)
            result.page.evaluate("history.replaceState(null,'','?q='+encodeURIComponent('粘毛器')+'&page=1');document.querySelector('.next-pagination-display em').innerText='1'")
            return "继续"
        with self.guard(attention):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            original = result.page.screenshot
            def screenshot(*args, **kwargs):
                nonlocal changed
                value = original(*args, **kwargs)
                if not changed:
                    changed = True
                    result.page.evaluate("history.replaceState(null,'','?q='+encodeURIComponent('粘毛器')+'&page=2');document.querySelector('.next-pagination-display em').innerText='2'")
                return value
            with patch.object(result.page, "screenshot", side_effect=screenshot):
                service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.read_payload()["page_number"], 1)

    def test_sales_sort_waits_for_reordered_cards_before_export(self):
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            replacement = card_html("234567", title="销量第一的新结果") + card_html("123456")
            result.page.evaluate("value=>{window.sortedCards=value;window.sortDelay=300}", replacement)
            service.collect_from_search(self.settings, self.context, result, limit=2)
        payload = self.read_payload()
        self.assertEqual([row["product_id"] for row in payload["items"]], ["234567", "123456"])
        self.assertEqual(payload["items"][0]["title"], "销量第一的新结果")
        self.assertEqual(payload["sort_label"], "销量")
        self.assertEqual(result.page.evaluate("sortClicks"), 1)

    def test_already_selected_sales_is_not_clicked_again(self):
        self.body = results_html(card_html(), selected="销量")
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(result.page.evaluate("sortClicks"), 0)
        self.assertEqual(self.read_payload()["sort_label"], "销量")

    def test_selection_without_list_refresh_requires_manual_confirmation(self):
        messages = []
        with self.guard(lambda message: messages.append(message) or "继续"):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            result.page.evaluate("window.keepOldResults=true")
            service.collect_from_search(replace(self.settings, timeout_ms=200), self.context, result, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertIn("商品列表更新", messages[0])
        self.assertEqual(result.page.evaluate("sortClicks"), 1)
        self.assertEqual(self.read_payload()["collected_count"], 1)

    def test_ineffective_sort_click_stops_without_export_or_second_click(self):
        messages = []
        with self.guard(lambda message: messages.append(message) or "停止"):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            result.page.evaluate("window.ignoreSortClick=true")
            with self.assertRaises(TaskStopped):
                service.collect_from_search(replace(self.settings, timeout_ms=200), self.context, result)
        self.assertEqual(result.page.evaluate("sortClicks"), 1)
        self.assertEqual(len(messages), 1)
        self.assertFalse(list((self.settings.workspace / "output/raw/taobao_products").glob("*.json")))

    def test_verification_after_sort_click_resumes_without_reclick_or_research(self):
        messages = []
        def attention(message):
            messages.append(message)
            result.page.locator(".J_MIDDLEWARE_FRAME_WIDGET").evaluate("e=>e.remove()")
            return "继续"
        with self.guard(attention):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            result.page.evaluate("""() => document.querySelectorAll('.next-tabs-tab')[1].addEventListener('click',()=>{
              document.body.insertAdjacentHTML('beforeend','<div class=J_MIDDLEWARE_FRAME_WIDGET>请拖动下方滑块完成验证</div>');
            })""")
            service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(result.page.evaluate("sortClicks"), 1)
        self.assertEqual(len([url for url in self.requests if urlsplit(url).hostname == "s.taobao.com"]), 1)

    def test_changing_back_to_comprehensive_before_read_requires_restore(self):
        from ecommerce.platforms.taobao.sorting import sort_by_sales
        messages = []
        def attention(message):
            messages.append(message)
            result.page.evaluate("""() => {
              document.querySelectorAll('.next-tabs-tab')[0].classList.remove('active');
              document.querySelectorAll('.next-tabs-tab')[1].classList.add('active');
            }""")
            return "继续"
        with self.guard(attention):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            def change_after_sort(*args):
                sorted_result = sort_by_sales(*args)
                sorted_result.page.evaluate("""() => {
                  document.querySelectorAll('.next-tabs-tab')[1].classList.remove('active');
                  document.querySelectorAll('.next-tabs-tab')[0].classList.add('active');
                }""")
                return sorted_result
            with patch("ecommerce.platforms.taobao.sorting.sort_by_sales", side_effect=change_after_sort):
                service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.read_payload()["sort_label"], "销量")

    def test_confirmed_user_dom_samples_keep_fields_positions_and_survey_counts(self):
        fixture_dir = Path(__file__).resolve().parents[1] / "fixtures/taobao"
        ad = json.loads((fixture_dir / "product_ad.json").read_text(encoding="utf-8"))
        video = json.loads((fixture_dir / "product_video.json").read_text(encoding="utf-8"))
        survey = {"tag": "div", "attrs": {"class": "search-content-col"}, "children": [
            {"tag": "div", "attrs": {"id": "pcSearchInvestigationCard_root_container"}, "children": ["对本次搜索体验满意吗"]}]}
        self.body = results_html(card_html(), selected="销量")
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            # 逐节点构建已存在的 DOM，保留用户样本的嵌套 a，不交给 HTML 解析器重排。
            result.page.evaluate("""trees=>{
              const build=v=>{
                if(typeof v==='string')return document.createTextNode(v);
                const e=document.createElement(v.tag);for(const [k,value] of Object.entries(v.attrs))e.setAttribute(k,value);
                for(const child of v.children)e.appendChild(build(child));return e;
              };
              const root=document.querySelector('#content_items_wrapper');root.replaceChildren(...trees.map(build));
            }""", [ad, video, survey, video])
            service.collect_from_search(self.settings, self.context, result)
        payload = self.read_payload()
        self.assertEqual(payload["schema_version"], 6)
        self.assertEqual(payload['requested_page_count'], 1)
        self.assertFalse(payload['pagination_performed'])
        first, second, repeated = payload["items"]
        self.assertEqual(first["product_id"], "1007759256325")
        self.assertEqual(first["shop_name"], "爱格智汇家家居旗舰店")
        self.assertTrue(first["ad_recommendation"])
        self.assertEqual(first["price_text"], "¥13.9 优惠后")
        self.assertEqual(first["sales_text"], "本月行业热销")
        self.assertEqual(first["attribute_tags"], ["粘毛滚", "塑料", "中国大陆"])
        self.assertEqual(first["rank_position"], 7)
        self.assertEqual(first["sku_reference"], "6001949700618")
        self.assertIsNone(first["shop_url"])
        self.assertEqual(first["field_states"]["shop_url"], "not_provided")
        self.assertEqual(first["field_states"]["video_url"], "not_applicable")
        self.assertEqual(second["shop_name"], "守望薰衣草v的小店57")
        self.assertEqual(second["price_text"], "¥3.35")
        self.assertIsNone(second["price_label"])
        self.assertEqual(second["sales_text"], "1万+人收货")
        self.assertEqual(second["service_tags"], ["直接下单享优惠", "退货宝", "包邮"])
        self.assertTrue(second["video_present"])
        self.assertTrue(second["video_icon_url"].endswith("64-64.png"))
        self.assertIsNone(second["video_url"])
        self.assertEqual(second["field_states"]["video_url"], "not_provided")
        self.assertTrue(second["main_image_url"].endswith(".jpg_.webp"))
        self.assertEqual(second["sku_reference"], "6249057859361")
        self.assertIn("appUid=fixture_shop", second["shop_url"])
        self.assertFalse(second["ad_recommendation"])
        self.assertEqual(repeated["product_id"], second["product_id"])
        self.assertEqual([row["slot_index"] for row in payload["items"]], [1, 2, 4])
        self.assertEqual([row["product_index"] for row in payload["items"]], [1, 2, 3])
        self.assertEqual(payload["counts"]["survey_count"], 1)
        self.assertEqual(payload["counts"]["same_id_extra_positions"], 1)
        self.assertTrue(payload["counts"]["counts_balanced"])
        csv_path = next((self.settings.workspace / "output/raw/taobao_products").glob("*.csv"))
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            output = list(csv.DictReader(stream))
        self.assertEqual(output[0]["广告推荐位"], "是")
        self.assertEqual(output[1]["视频标识"], "有")
        self.assertIn("视频链接：页面未提供", output[1]["字段状态说明"])
        self.assertEqual(output[2]["原始格子位置"], "4")

    def test_unknown_slot_inside_requested_range_stops_before_export(self):
        self.body = results_html(card_html() + '<div class="search-content-col">未确认卡片</div>' + card_html("234567"))
        with self.guard(lambda message: "停止"):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            with self.assertRaises(TaskStopped):
                service.collect_from_search(self.settings, self.context, result, limit=2)
        self.assertFalse(list((self.settings.workspace / "output/raw/taobao_products").glob("*.json")))

    def test_card_content_change_during_screenshot_is_recaptured_after_feedback(self):
        messages = []
        changed = False
        with self.guard(lambda message: messages.append(message) or "继续"):
            result = open_search(self.context, self.home, "粘毛器", 1000)
            original = result.page.screenshot
            def screenshot(*args, **kwargs):
                nonlocal changed
                value = original(*args, **kwargs)
                if not changed:
                    changed = True
                    result.page.locator('[class*="title--"]').first.evaluate("e=>e.innerText='变化后的商品标题'")
                return value
            with patch.object(result.page, "screenshot", side_effect=screenshot):
                service.collect_from_search(self.settings, self.context, result, limit=1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.read_payload()["items"][0]["title"], "变化后的商品标题")

    def test_lazy_main_image_is_loaded_by_guarded_same_page_scroll(self):
        self.body = results_html(card_html() + card_html("234567") + card_html("345678"), selected="销量")
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            result.page.evaluate("""() => {
              const slot=document.querySelector('#content_items_wrapper').children[2];
              slot.style.marginTop=Math.max(0,innerHeight*1.6-slot.getBoundingClientRect().top)+'px';
              slot.querySelector('.mainPicAdaptWrapper--fixture').remove();
              document.body.insertAdjacentHTML('beforeend','<div style="height:1000px"></div>');
              addEventListener('scroll',()=>{
                if(slot.getBoundingClientRect().top<innerHeight*.4&&!slot.querySelector('.mainPicAdaptWrapper--fixture')){
                  const parent=document.createElement('div');parent.className='mainPicAdaptWrapper--fixture';
                  const img=document.createElement('img');img.className='mainPic--fixture';img.width=100;img.height=100;
                  img.src='https://img.alicdn.com/lazy-visible.webp';parent.append(img);slot.prepend(parent);
                }
              });
            }""")
            service.collect_from_search(self.settings, self.context, result, limit=3)
        payload = self.read_payload()
        self.assertTrue(payload["scroll_performed"])
        self.assertGreaterEqual(payload["image_loading"]["scroll_count"], 2)
        self.assertEqual(payload["image_loading"]["missing_after"], 0)
        self.assertEqual(payload["items"][2]["main_image_url"], "https://img.alicdn.com/lazy-visible.webp")

    def test_video_hover_hidden_static_main_image_still_uses_confirmed_structure(self):
        self.body = results_html(card_html(), selected="销量")
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 1000)
            result.page.evaluate("""() => {
              document.querySelector('.mainPicAdaptWrapper--fixture').style.cssText='visibility:hidden;opacity:0';
              document.querySelector('.search-content-col').insertAdjacentHTML('afterbegin',
                '<div class="imageSwitch--fixture"><img class="mainImg--fixture" src="https://img.alicdn.com/hidden-copy.jpg" width=100 height=100></div>');
            }""")
            service.collect_from_search(self.settings, self.context, result, limit=1)
        payload = self.read_payload()
        self.assertEqual(payload["items"][0]["main_image_url"], "https://img.alicdn.com/fixture-main.webp")
        self.assertEqual(payload["image_loading"]["scroll_count"], 0)


if __name__ == "__main__":
    unittest.main()
