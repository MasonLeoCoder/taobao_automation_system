"""本地页面验证评价概览；全部评价入口设置陷阱以防越过当前阶段。"""
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from html import escape
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.detail_review_summary import collect_review_summary
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID = '1050158363561'


def tag(label, count):
    value = '' if count is None else '<span class="imprItemCount--fixture">' + escape(count) + '</span>'
    return '<span class="imprItem--fixture">' + escape(label) + value + '</span>'


DEFAULT_TAGS = tag('价格低', '315') + tag('使用体验劲道', '290') + tag('质量很好', '224')


def module(tags=DEFAULT_TAGS, count='10万+', rate='近3个月好评率高达99.5%'):
    rate_html = '' if rate is None else '<span class="rateTextTitle--fixture" title="' + escape(rate, quote=True) + '">' + escape(rate) + '</span>'
    return ('<div class="tabDetailItem--fixture"><div class="Comments--fixture">'
            '<div class="titleWrapper--fixture"><div class="tabDetailItemTitle--fixture">用户评价<span>·</span>' + escape(count) + '</div>' + rate_html + '</div>'
            '<div class="condition--fixture"><div class="expressionWrap--fixture">' + tags + '</div></div>'
            '<div class="comments--fixture">PRIVATE_REVIEW_AUTHOR<div>PRIVATE_REVIEW_CONTENT</div><img src="private-review-image.png"></div>'
            '<div class="footer--fixture"><div class="ShowButton--fixture" onclick="window.allClicks++">查看全部评价</div></div></div>'
            '<div class="askAnswerWrap--fixture">问大家' + tag('问答不属于评价', '999') + '</div></div>')


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用本地评价概览浏览器检查')
class TaobaoDetailReviewSummaryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(load_settings(root=Path(self.temp.name)).for_platform('taobao'), headless=True, timeout_ms=650,
                                taobao_timing={key: (0, 0) for key in DEFAULT_TAOBAO_TIMING})
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.body = module()
        def route(route):
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body='<!doctype html><meta charset="utf-8">'
                '<span class="site-nav-login-info-nick">测试账号</span><script>window.allClicks=0</script>' + self.body)
        self.context.route('**/*', route)
        self.page = self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id=' + ITEM_ID)
        self.result = DetailPage(self.page, resolve_detail_input(ITEM_ID), '评价概览测试商品')

    def reload(self, body):
        self.body = body
        self.page.reload()

    def guard(self, attention=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def collect(self, attention=None):
        with self.guard(attention):
            return collect_review_summary(self.settings, self.result, stable_ms=80)

    def document(self):
        return json.loads((product_directory(self.settings, ITEM_ID) / (ITEM_ID + '.json')).read_text(encoding='utf-8'))

    def test_count_period_and_ordered_tags_match_overview(self):
        result = self.collect()
        self.assertEqual(result['review_count']['raw'], '10万+')
        self.assertIsNone(result['review_count']['value'])
        self.assertEqual(result['review_count']['lower_bound'], 100000)
        self.assertEqual(result['positive_rate']['percent'], 99.5)
        self.assertEqual(result['positive_rate']['period_raw'], '近3个月')
        self.assertEqual([(t['label'], t['count']['value']) for t in result['tags']], [('价格低', 315), ('使用体验劲道', 290), ('质量很好', 224)])
        self.assertEqual(result['tag_count'], 3)
        self.assertTrue(result['all_reviews_entry_present'])

    def test_no_individual_comments_or_images_in_history_or_json_and_no_all_click(self):
        result = self.collect()
        folder = product_directory(self.settings, ITEM_ID)
        for path in folder.rglob('*'):
            if path.suffix in {'.json', '.html'}:
                contents = path.read_text(encoding='utf-8')
                self.assertNotIn('PRIVATE_REVIEW', contents)
                self.assertNotIn('private-review-image', contents)
                self.assertNotIn('问答不属于评价', contents)
        self.assertEqual(self.page.evaluate('allClicks'), 0)
        self.assertFalse(result['all_reviews_opened'])
        self.assertFalse(result['individual_reviews_collected'])
        html_path = folder / result['html_file']
        self.assertEqual(hashlib.sha256(html_path.read_bytes()).hexdigest(), result['html_sha256'])
        self.assertEqual(len(html_path.read_bytes()), result['html_size_bytes'])

    def test_dynamic_labels_with_digits_duplicate_names_and_no_fixed_cap(self):
        self.reload(module(''.join(tag('第' + str(i) + '次回购2', str(i)) for i in range(12)) + tag('第1次回购2', '3')))
        result = self.collect()
        self.assertEqual(result['tag_count'], 13)
        self.assertEqual([t['index'] for t in result['tags']], list(range(1, 14)))
        self.assertEqual(result['tags'][0]['label'], '第0次回购2')
        self.assertEqual(result['tags'][-1]['label'], '第1次回购2')

    def test_missing_rate_or_visible_tags_is_not_invented_as_zero(self):
        self.reload(module('', count='0', rate=None))
        result = self.collect()
        self.assertEqual(result['review_count']['value'], 0)
        self.assertEqual(result['positive_rate']['status'], 'not_displayed')
        self.assertEqual(result['tags_status'], 'not_displayed')

    def test_hidden_modules_and_tags_and_external_recommendations_excluded(self):
        self.reload('<div style="display:none">' + module() + '</div>' + module(DEFAULT_TAGS +
            '<span style="display:none">' + tag('隐藏标签', '1') + '</span>') + tag('外部推荐', '1000'))
        result = self.collect()
        self.assertEqual(result['tag_count'], 3)

    def test_unknown_count_format_retains_raw_without_guessing(self):
        self.reload(module(tag('一般', '较多'), count='超过很多条'))
        result = self.collect()
        self.assertEqual(result['review_count']['status'], 'unparsed')
        self.assertEqual(result['review_count']['raw'], '超过很多条')
        self.assertIsNone(result['tags'][0]['count']['value'])

    def test_broken_tag_count_does_not_replace_previous_complete_summary(self):
        first = self.collect()
        self.reload(module(tag('只有标签没有数量', None)))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message: '停止')
        document = self.document()
        self.assertEqual(document['review_summary']['batch_id'], first['batch_id'])
        self.assertEqual(document['review_summary_last_attempt']['status'], 'partial')
        self.assertEqual(self.page.evaluate('allClicks'), 0)

    def test_loading_waits_for_manual_confirmation_before_reading(self):
        self.page.locator('[class^="Comments--"]').evaluate('e=>e.setAttribute("aria-busy","true")')
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('[class^="Comments--"]').evaluate('e=>e.removeAttribute("aria-busy")')
            return '继续'
        result = self.collect(attention)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['status'], 'complete')

    def test_unloaded_empty_container_is_not_assumed_zero_reviews(self):
        self.reload('<div class="Comments--fixture"></div>')
        with self.assertRaises(TaskStopped):
            self.collect(lambda message: '停止')
        self.assertNotIn('review_summary', self.document())
        count = self.document()['review_summary_last_attempt']['review_count']
        self.assertIsNone(count['raw'])
        self.assertIsNone(count['value'])
        self.assertEqual(count['status'], 'not_loaded')

    def test_lazy_overview_navigation_once_does_not_click_all_reviews(self):
        self.reload('<button class="tabTitle--fixture" id="nav">用户评价</button><main id="target"></main><script>'
            'window.navClicks=0;document.querySelector("#nav").onclick=()=>{navClicks++;document.querySelector("#target").innerHTML='
            + json.dumps(module(), ensure_ascii=False) + '};</script>')
        result = self.collect()
        self.assertEqual(result['tag_count'], 3)
        self.assertEqual(self.page.evaluate('navClicks'), 1)
        self.assertEqual(self.page.evaluate('allClicks'), 0)

    def test_unknown_summary_layout_waits_for_manual_repair(self):
        self.reload(module().replace('<div class="expressionWrap--fixture">', '<div class="expressionWrap--fixture"><div id="unknown">未知总结</div>'))
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('#unknown').evaluate('e=>e.remove()')
            return '继续'
        result = self.collect(attention)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(prompts), 1)

    def test_wrong_product_waits_until_manually_restored(self):
        self.page.evaluate("history.replaceState(null,'','?id=1091534543310')")
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.evaluate("history.replaceState(null,'','?id=" + ITEM_ID + "')")
            return '继续'
        result = self.collect(attention)
        self.assertEqual(result['product_id'], ITEM_ID)
        self.assertEqual(len(prompts), 1)

    def test_repeat_keeps_history_and_all_other_sections(self):
        folder = product_directory(self.settings, ITEM_ID)
        others = {key: {'saved': key} for key in ('video', 'main_images', 'product_info', 'sku_details', 'parameters', 'detail_images')}
        for key, value in others.items():
            save_detail_section(folder, ITEM_ID, key, value)
        first, second = self.collect(), self.collect()
        self.assertNotEqual(first['batch_id'], second['batch_id'])
        self.assertTrue(Path(first['history_record_path']).is_file())
        for key, value in others.items():
            self.assertEqual(self.document()[key], value)

    def test_publish_failure_preserves_previous_json(self):
        first = self.collect()
        destination = Path(first['record_path'])
        original = destination.read_bytes()
        with patch('ecommerce.modules.collection.detail_review_summary.save_detail_section', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(destination.read_bytes(), original)
        self.assertFalse(list(product_directory(self.settings, ITEM_ID).rglob('*.tmp')))

    def test_changed_summary_at_save_is_recaptured_after_confirmation(self):
        prompts = []
        with self.guard(lambda message: prompts.append(message) or '继续') as guard:
            perform = guard.perform
            changed = False
            def intercept(step, action, **kwargs):
                nonlocal changed
                if step == '保存评价概览与概览 HTML 片段' and not changed:
                    changed = True
                    self.page.locator('[class^="imprItemCount--"]').first.evaluate("e=>e.textContent='316'")
                return perform(step, action, **kwargs)
            with patch.object(guard, 'perform', side_effect=intercept):
                result = collect_review_summary(self.settings, self.result, stable_ms=80)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['tags'][0]['count']['value'], 316)

    def test_service_routes_only_overview_with_previous_steps_disabled(self):
        from ecommerce.modules.collection.detail_service import open_detail_input
        from ecommerce.platforms.taobao import session
        @contextmanager
        def browser(settings):
            yield self.context, self.page
        with patch.object(session, 'base_browser_session', browser):
            message = open_detail_input(self.settings, ITEM_ID, wait=False, review_summary=True)
        self.assertIn('评价概览采集完成', message)
        self.assertEqual(self.document()['review_summary']['tag_count'], 3)
        for key in ('video', 'main_images', 'sku_details', 'parameters', 'detail_images'):
            self.assertNotIn(key, self.document())
        self.assertEqual(self.page.evaluate('allClicks'), 0)


if __name__ == '__main__':
    unittest.main()
