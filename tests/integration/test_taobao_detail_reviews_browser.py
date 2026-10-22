"""评价侧栏本地模拟：数量上限、滚动回收、过滤、人工恢复与发布回滚。"""
import csv
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

from ecommerce.modules.collection.detail_reviews import TEMPLATE_REVIEW, collect_reviews
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID = '1050158363561'


def review(index, content=None, *, identity=True, author=None, extra='', media='', append=''):
    identity_attr = f' data-review-id="{index}"' if identity else ''
    return (f'<div class="Comment--fixture"{identity_attr} style="min-height:110px">'
        '<div class="header--fixture"><div class="userInfo--fixture"><div class="userName--fixture">'
        + escape(author or '用户' + str(index)) + '</div><div class="meta--fixture">2026年10月1日<span></span>已购：规格A</div></div></div>'
        '<div class="contentWrapper--fixture"><div class="content--fixture" style="white-space:pre-wrap">' + escape(content if content is not None else '实际评价' + str(index))
        + '</div>' + media + '</div>' + append + extra + '<div class="splitLine2--fixture"></div></div>')


def drawer(content, *, count='10万+', selected='全部', tail='没有更多评价'):
    return ('<div class="Drawer--fixture" style="position:fixed;right:0;top:0;width:680px;height:650px;background:white">'
        '<div class="Comments--fixture"><div class="titleWrapper--fixture"><div class="tabDetailItemTitle--fixture">用户评价·' + count + '</div></div>'
        '<div class="condition--fixture"><span id="filter" class="imprItem--fixture isSelected--fixture">' + selected + '</span>'
        '<span class="sortByText--fixture">默认排序</span></div>'
        '<div id="review-list" class="comments--fixture" style="overflow-y:scroll;height:480px">' + content
        + ('<div id="end">' + tail + '</div>' if tail else '') + '</div></div></div>')


def inline_append(content, *, time='32天后追评：', media='', extra=''):
    return ('<div class="append--fixture"><div class="splitLine--fixture"></div>'
        '<div class="content--fixture"><span class="appendInternal--fixture">' + escape(time) + '</span><span>'
        + escape(content) + '</span></div>' + media + extra + '</div>')


def folded_notice(count='1978', extra=''):
    return '<div class="foldInfoWrap--fixture">已折叠' + count + '条对你帮助不大的评价' + extra + '</div>'


def seller_reply(*, css_class='reply--fixture', extra=''):
    return ('<div class="' + css_class + '"><div class="splitLine--fixture"></div>'
        '<div class="HideableText--fixture"><div class="text--fixture">商家回复：PRIVATE_SELLER_REPLY</div>'
        '<span class="showMore--fixture" onclick="window.sellerClicks=(window.sellerClicks||0)+1">更多</span>'
        '</div>' + extra + '</div>')


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用本地逐条评价检查')
class TaobaoDetailReviewsBrowserTests(unittest.TestCase):
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
        self.settings = replace(load_settings(root=Path(self.temp.name)).for_platform('taobao'), headless=True, timeout_ms=450,
                                taobao_timing={key: (0, 0) for key in DEFAULT_TAOBAO_TIMING})
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.body = drawer(''.join(review(i) for i in range(5)))
        self.requests = []
        def route(route):
            self.requests.append(route.request.url)
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body='<!doctype html><meta charset="utf-8">'
                '<span class="site-nav-login-info-nick">测试账号</span><div style="height:1800px">商品主体</div>' + self.body)
        self.context.route('**/*', route)
        self.page = self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id=' + ITEM_ID)
        self.result = DetailPage(self.page, resolve_detail_input(ITEM_ID), '评价测试商品')

    def reload(self, body):
        self.body = body
        self.page.reload()

    def guard(self, attention=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def collect(self, attention=None, all_reviews=False):
        with self.guard(attention):
            return collect_reviews(self.settings, self.result, all_reviews=all_reviews, stable_ms=40)

    def folder(self):
        return product_directory(self.settings, ITEM_ID)

    def document(self):
        return json.loads((self.folder() / (ITEM_ID + '.json')).read_text(encoding='utf-8'))

    def items(self, result):
        return json.loads((self.folder() / result['files'][0]['history_file']).read_text(encoding='utf-8'))['reviews']

    def test_default_caps_valid_reviews_at_100_in_page_order(self):
        self.reload(drawer(''.join(review(i) for i in range(125))))
        result = self.collect()
        self.assertEqual(result['collected_count'], 100)
        self.assertEqual(result['stop_reason'], 'limit_reached')
        self.assertFalse(result['end_confirmed'])
        self.assertEqual([v['review_id'] for v in self.items(result)], [str(i) for i in range(100)])

    def test_all_removes_cap_and_confirms_only_page_list_end(self):
        self.reload(drawer(''.join(review(i) for i in range(125))))
        result = self.collect(all_reviews=True)
        self.assertEqual(result['collected_count'], 125)
        self.assertIsNone(result['requested_limit'])
        self.assertEqual(result['stop_reason'], 'end_of_list')
        self.assertTrue(result['end_confirmed'])
        self.assertEqual(result['scope'], 'page_available_reviews')

    def test_short_list_collects_all_available_reviews(self):
        self.reload(drawer(''.join(review(i) for i in range(42))))
        result = self.collect()
        self.assertEqual(result['collected_count'], 42)
        self.assertEqual(result['stop_reason'], 'end_of_list')

    def test_exactly_100_is_conservatively_limit_reached(self):
        self.reload(drawer(''.join(review(i) for i in range(100))))
        self.assertEqual(self.collect()['stop_reason'], 'limit_reached')

    def test_no_reviews_requires_explicit_empty_and_logs_it(self):
        self.reload(drawer('<div>暂无评价</div>', count='0', tail=''))
        with self.assertLogs('ecommerce.modules.collection.detail_reviews', level='INFO') as output:
            result = self.collect()
        self.assertEqual(result['status'], 'no_reviews')
        self.assertEqual(result['message'], '无评价')
        self.assertIn('无评价', '\n'.join(output.output))
        self.assertEqual(result['collected_count'], 0)
        self.assertEqual(self.items(result), [])

    def test_blank_loading_is_not_no_reviews(self):
        self.reload(drawer('', tail=''))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message: '停止')
        doc = self.document()
        self.assertNotIn('reviews', doc)
        self.assertEqual(doc['reviews_last_attempt']['status'], 'partial')
        self.assertEqual(doc['reviews_last_attempt']['review_availability'], 'unconfirmed')

    def test_zero_summary_without_open_button_records_no_reviews(self):
        self.reload('<div class="Comments--main"><div class="titleWrapper--main"><div class="tabDetailItemTitle--main">用户评价·0</div></div>'
            '<div class="condition--main"><div class="expressionWrap--main"></div></div></div>')
        result = self.collect()
        self.assertEqual(result['status'], 'no_reviews')
        self.assertEqual(self.items(result), [])

    def test_wrong_product_requires_restore_before_opening(self):
        self.page.evaluate("history.replaceState(null,'','?id=1091534543310')")
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.evaluate("history.replaceState(null,'','?id=" + ITEM_ID + "')")
            return '继续'
        result = self.collect(attention)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['collected_count'], 5)

    def test_template_reviews_do_not_consume_default_quota(self):
        self.reload(drawer(''.join(review(i, TEMPLATE_REVIEW) for i in range(110)) + ''.join(review(i) for i in range(110, 230))))
        result = self.collect()
        self.assertEqual((result['collected_count'], result['skipped_template_count'], result['scanned_count']), (100, 110, 210))

    def test_all_filtered_is_distinct_from_no_reviews(self):
        self.reload(drawer(''.join(review(i, TEMPLATE_REVIEW) for i in range(12))))
        result = self.collect()
        self.assertEqual(result['collected_count'], 0)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['all_filtered'])
        self.assertEqual(result['review_availability'], 'present')

    def test_template_with_media_or_real_append_is_retained_and_seller_reply_excluded(self):
        media = '<div class="album--fixture"><img src="https://img.alicdn.com/review.jpg"><video src="https://cloud.video.taobao.com/review.mp4"></video></div>'
        append = '<div class="append--fixture"><span class="appendTime--fixture">15天后追评</span><div class="content--fixture">使用后补充评价</div></div>'
        reply = '<div class="sellerReply--fixture">商家回复：PRIVATE_SELLER_REPLY</div>'
        self.reload(drawer(review(1, TEMPLATE_REVIEW, media=media, extra=reply) + review(2, TEMPLATE_REVIEW, append=append)))
        result = self.collect()
        items = self.items(result)
        self.assertEqual(result['collected_count'], 2)
        self.assertEqual(items[0]['image_urls'], ['https://img.alicdn.com/review.jpg'])
        self.assertEqual(items[0]['video_urls'], ['https://cloud.video.taobao.com/review.mp4'])
        self.assertEqual(items[1]['append_reviews'][0]['content'], '使用后补充评价')
        for p in self.folder().rglob('*.json'):
            self.assertNotIn('PRIVATE_SELLER_REPLY', p.read_text(encoding='utf-8'))
        self.assertFalse(list(self.folder().rglob('*.mp4')))

    def test_text_containing_template_is_not_filtered(self):
        self.reload(drawer(review(1, TEMPLATE_REVIEW + '，但我觉得一般')))
        self.assertEqual(self.collect()['collected_count'], 1)

    def test_nested_seller_replies_preserve_initial_append_and_buyer_media_only(self):
        buyer_media = '<div class="album--fixture"><img src="https://img.alicdn.com/buyer.jpg"></div>'
        seller_media = '<div class="album--fixture"><img src="https://img.alicdn.com/private-seller.jpg"><video src="https://cloud.video.taobao.com/private-seller.mp4"></video></div>'
        append_media = '<div class="album--fixture"><video src="https://cloud.video.taobao.com/buyer-append.mp4"></video></div>'
        self.reload(drawer(review(1, '买家初评', media=buyer_media + seller_reply(extra=seller_media)
            + inline_append('买家追评', media=append_media, extra=seller_reply(extra=seller_media)))))
        result = self.collect()
        item = self.items(result)[0]
        self.assertEqual(result['collected_count'], 1)
        self.assertEqual(item['content'], '买家初评')
        self.assertEqual(item['image_urls'], ['https://img.alicdn.com/buyer.jpg'])
        self.assertEqual(item['video_urls'], [])
        self.assertEqual(len(item['append_reviews']), 1)
        self.assertEqual(item['append_reviews'][0]['content'], '买家追评')
        self.assertEqual(item['append_reviews'][0]['time_raw'], '32天后追评：')
        self.assertEqual(item['append_reviews'][0]['image_urls'], [])
        self.assertEqual(item['append_reviews'][0]['video_urls'], ['https://cloud.video.taobao.com/buyer-append.mp4'])
        self.assertEqual(self.page.evaluate('window.sellerClicks||0'), 0)
        for path in self.folder().rglob('*'):
            if path.suffix in {'.json', '.csv'}:
                text = path.read_text(encoding='utf-8-sig')
                self.assertNotIn('PRIVATE_SELLER_REPLY', text)
                self.assertNotIn('private-seller', text)

    def test_seller_reply_inside_content_node_is_removed_without_losing_buyer_text(self):
        body = review(1, '买家第一行').replace('买家第一行</div>', '买家第一行<br>买家第二行' + seller_reply() + '</div>')
        self.reload(drawer(body))
        item = self.items(self.collect())[0]
        self.assertEqual(item['content'], '买家第一行\n买家第二行')

    def test_wrapped_seller_reply_with_multiple_classes_is_ignored(self):
        self.reload(drawer(review(1, '保留买家正文', extra='<div>' + seller_reply(css_class='neutral reply--fixture') + '</div>')))
        item = self.items(self.collect())[0]
        self.assertEqual(item['content'], '保留买家正文')

    def test_buyer_text_mentioning_seller_reply_is_preserved(self):
        text = '商家回复：这是买家引用的文字，我不认同商家的说明。'
        self.reload(drawer(review(1, text, media=seller_reply())))
        self.assertEqual(self.items(self.collect())[0]['content'], text)

    def test_unconfirmed_reply_class_is_not_silently_dropped(self):
        self.reload(drawer(review(1, media='<div class="reply--fixture">买家补充内容，归属尚未确认</div>')))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertNotIn('reviews', self.document())

    def test_unknown_buyer_content_next_to_seller_reply_still_pauses(self):
        self.reload(drawer(review(1, media=seller_reply() + '<div>未适配的买家补充内容</div>')))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertFalse(self.document()['reviews_last_attempt']['end_confirmed'])

    def test_seller_loading_media_append_and_time_nodes_do_not_enter_buyer_record(self):
        placeholder = 'https://img.alicdn.com/imgextra/i2/O1CN01Dqo1gd1wMhnobvRco_!!6000000006294-2-tps-145-145.png'
        extra = ('<div class="content--fixture">PRIVATE_SELLER_BODY</div>'
            '<div aria-busy="true" class="loading--fixture" style="height:20px">商家回复加载中</div>'
            '<div class="album--fixture"><img width="50" height="50" src="' + placeholder + '"></div>'
            + inline_append('PRIVATE_SELLER_APPEND', time='44天后追评：'))
        self.reload(drawer(review(1, '买家正文', media=seller_reply(extra=extra))))
        item = self.items(self.collect())[0]
        self.assertEqual(item['content'], '买家正文')
        self.assertEqual(item['append_reviews'], [])
        self.assertEqual(item['image_urls'], [])
        self.assertEqual(item['unresolved_media'], [])

    def test_only_buyer_fulltext_is_expanded(self):
        seller = seller_reply(extra='<button onclick="window.sellerClicks=(window.sellerClicks||0)+1">展开全文</button>')
        body = review(1, '短正文', media=seller).replace('短正文</div>', '<span id="buyer-text">短正文</span><button id="buyer-expand">展开全文</button></div>')
        self.reload(drawer(body) + '''<script>window.buyerClicks=0;document.querySelector('#buyer-expand').onclick=()=>{
            window.buyerClicks++;document.querySelector('#buyer-text').textContent='买家的完整正文';document.querySelector('#buyer-expand').remove()};</script>''')
        item = self.items(self.collect())[0]
        self.assertEqual(item['content'], '买家的完整正文')
        self.assertEqual(self.page.evaluate('[window.buyerClicks,window.sellerClicks||0]'), [1, 0])

    def test_seller_reply_cannot_turn_empty_buyer_content_into_valid_review(self):
        self.reload(drawer(review(1, '', media=seller_reply())))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertEqual(self.document()['reviews_last_attempt']['collected_count'], 0)

    def test_seller_media_or_append_does_not_keep_buyer_template(self):
        seller = seller_reply(extra=inline_append('商家追加说明') + '<div class="album--fixture"><img src="https://img.alicdn.com/private-seller.jpg"></div>')
        self.reload(drawer(review(1, TEMPLATE_REVIEW, media=seller)))
        result = self.collect()
        self.assertEqual((result['collected_count'], result['skipped_template_count']), (0, 1))
        self.assertTrue(result['all_filtered'])

    def test_nested_append_separates_initial_text_and_inline_time_and_keeps_meaningful_append(self):
        self.reload(drawer(review(1, '该用户未及时主动评价，系统默认评价', media=inline_append('才用两周，就没有粘力，'))
            + review(2, TEMPLATE_REVIEW, media=inline_append('挺好的挺好的', time='3天后追评：'))))
        result = self.collect()
        items = self.items(result)
        self.assertEqual((result['collected_count'], result['skipped_template_count']), (2, 0))
        self.assertEqual(items[0]['content'], '该用户未及时主动评价，系统默认评价')
        self.assertEqual(items[0]['append_reviews'][0]['content'], '才用两周，就没有粘力，')
        self.assertEqual(items[0]['append_reviews'][0]['time_raw'], '32天后追评：')
        self.assertEqual(items[1]['content'], TEMPLATE_REVIEW)
        self.assertEqual(items[1]['append_reviews'][0]['content'], '挺好的挺好的')
        self.assertEqual(items[1]['append_reviews'][0]['time_raw'], '3天后追评：')
        with (self.folder() / '用户评价/评价明细.csv').open(encoding='utf-8-sig', newline='') as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0]['追加评价'])[0]['content'], '才用两周，就没有粘力，')

    def test_nested_multiple_appends_keep_their_own_image_and_video_links(self):
        main_media = '<div class="album--fixture"><img src="https://img.alicdn.com/initial.jpg"></div>'
        append_media = '<div class="album--fixture"><img src="https://img.alicdn.com/append.jpg"><video src="https://cloud.video.taobao.com/append.mp4"></video></div>'
        self.reload(drawer(review(1, '首次正文', media=main_media + inline_append('第一次补充', media=append_media)
            + inline_append('第二次补充', time='60天后追评：'))))
        item = self.items(self.collect())[0]
        self.assertEqual(item['content'], '首次正文')
        self.assertEqual(item['image_urls'], ['https://img.alicdn.com/initial.jpg'])
        self.assertEqual(item['video_urls'], [])
        self.assertEqual([a['content'] for a in item['append_reviews']], ['第一次补充', '第二次补充'])
        self.assertEqual(item['append_reviews'][0]['image_urls'], ['https://img.alicdn.com/append.jpg'])
        self.assertEqual(item['append_reviews'][0]['video_urls'], ['https://cloud.video.taobao.com/append.mp4'])
        self.assertEqual(item['append_reviews'][1]['image_urls'], [])

    def test_append_time_can_be_an_absolute_date_in_both_layouts(self):
        append = '<div class="append--fixture"><span class="appendTime--fixture">2026年10月22日</span><div class="content--fixture">使用后补充评价</div></div>'
        self.reload(drawer(review(1, append=append) + review(2, media=append)))
        items = self.items(self.collect())
        for item in items:
            self.assertEqual(item['append_reviews'][0]['time_raw'], '2026年10月22日')
            self.assertEqual(item['append_reviews'][0]['content'], '使用后补充评价')

    def test_nested_media_only_append_waits_for_its_placeholder_image(self):
        placeholder = 'https://img.alicdn.com/imgextra/i2/O1CN01Dqo1gd1wMhnobvRco_!!6000000006294-2-tps-145-145.png'
        media = '<div class="album--fixture"><img id="lazy" width="160" height="160" src="' + placeholder + '"></div>'
        self.reload(drawer(review(1, '', media=inline_append('', media=media)))
            + '''<script>const pane=document.querySelector('#review-list'),img=document.querySelector('#lazy');
            document.querySelector('.contentWrapper--fixture>.content--fixture').style.minHeight='700px';
            const observer=new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting){img.src='https://img.alicdn.com/append-real.jpg';observer.disconnect()}}),{root:pane});observer.observe(img);</script>''')
        result = self.collect()
        item = self.items(result)[0]
        self.assertEqual(result['collected_count'], 1)
        self.assertGreater(result['scroll_count'], 0)
        self.assertEqual(item['image_urls'], [])
        self.assertEqual(item['append_reviews'][0]['image_urls'], ['https://img.alicdn.com/append-real.jpg'])
        self.assertEqual(item['append_reviews'][0]['content'], '')
        self.assertEqual(item['append_reviews'][0]['unresolved_media'], [])

    def test_unknown_nested_append_content_still_pauses(self):
        self.reload(drawer(review(1, media=inline_append('真实追评', extra='<div>未识别的额外追评区域</div>'))))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertNotIn('reviews', self.document())
        self.assertEqual(self.document()['reviews_last_attempt']['status'], 'partial')

    def test_nested_append_fulltext_expands_once_and_retains_initial_and_time(self):
        append = inline_append('短追评').replace('短追评</span>', '<span id="append-text">短追评</span><button id="expand-append">展开全文</button></span>')
        self.reload(drawer(review(1, '首次正文', media=append)) + '''<script>window.appendClicks=0;
            document.querySelector('#expand-append').onclick=()=>{window.appendClicks++;document.querySelector('#append-text').textContent='追评的完整正文';document.querySelector('#expand-append').remove()};</script>''')
        item = self.items(self.collect())[0]
        self.assertEqual(item['content'], '首次正文')
        self.assertEqual(item['append_reviews'][0]['content'], '追评的完整正文')
        self.assertEqual(item['append_reviews'][0]['time_raw'], '32天后追评：')
        self.assertEqual(self.page.evaluate('window.appendClicks'), 1)

    def test_media_only_review_date_sku_and_original_text_are_preserved(self):
        self.reload(drawer(review(1, '', media='<div class="album--fixture"><img src="https://img.alicdn.com/review.jpg"></div>') + review(2, '第一行\n第二行🙂')))
        result = self.collect()
        items = self.items(result)
        self.assertEqual(items[0]['content'], '')
        self.assertEqual(items[0]['date'], '2026-10-01')
        self.assertEqual(items[0]['sku'], '规格A')
        self.assertEqual(items[1]['content'], '第一行\n第二行🙂')

    def test_same_text_different_users_and_identical_sibling_rows_retained(self):
        self.reload(drawer(review(1, '很好', identity=False) + review(2, '很好', identity=False)
            + review(3, '相同记录', identity=False) + review(3, '相同记录', identity=False)))
        result = self.collect()
        self.assertEqual(result['collected_count'], 4)
        self.assertTrue(all(not r['identity_exact'] for r in self.items(result)))

    def test_expands_full_text_once_before_persisting(self):
        body = review(1, '较短的评价').replace('较短的评价</div>', '较短的评价<button id="expand">展开全文</button></div>')
        self.reload(drawer(body) + '<script>window.expandClicks=0;document.querySelector("#expand").onclick=()=>{expandClicks++;document.querySelector("#expand").parentElement.textContent="展开后的完整评价正文"}</script>')
        result = self.collect()
        self.assertEqual(self.items(result)[0]['content'], '展开后的完整评价正文')
        self.assertEqual(self.page.evaluate('expandClicks'), 1)

    def test_failed_expansion_is_not_replayed(self):
        body = review(1, '较短的评价').replace('较短的评价</div>', '较短的评价<button id="expand" onclick="window.expandClicks++">展开全文</button></div>')
        self.reload(drawer(body) + '<script>window.expandClicks=0</script>')
        with self.assertRaises(TaskStopped):
            self.collect(lambda message: '停止')
        self.assertEqual(self.page.evaluate('expandClicks'), 1)

    def test_scroll_loads_next_batch_and_captures_recycled_nodes_before_removal(self):
        batches = [''.join(review(i) for i in range(start, start+5)) for start in (0, 5, 10)]
        # 每次到底后回收整个旧列表；最后只留下结束提示。
        script = '''<script>window.batch=0;const pane=document.querySelector('#review-list');
          const batches=PAYLOAD;pane.onscroll=()=>{if(pane.scrollTop+pane.clientHeight<pane.scrollHeight-3||pane.dataset.loading)return;
          pane.dataset.loading='1';pane.setAttribute('aria-busy','true');setTimeout(()=>{window.batch++;pane.innerHTML=window.batch<batches.length?batches[window.batch]:'<div>没有更多评价</div>';pane.scrollTop=0;pane.removeAttribute('aria-busy');delete pane.dataset.loading},120)};</script>'''.replace('PAYLOAD', json.dumps(batches, ensure_ascii=False))
        self.reload(drawer(''.join(review(i) for i in range(5)), tail='') + script)
        result = self.collect()
        self.assertEqual([r['review_id'] for r in self.items(result)], [str(i) for i in range(15)])
        self.assertEqual(self.page.evaluate('window.scrollY'), 0)
        self.assertGreater(result['scroll_count'], 0)

    def test_lazy_placeholder_is_loaded_before_save_without_inflating_count(self):
        placeholder = 'https://img.alicdn.com/imgextra/i2/O1CN01Dqo1gd1wMhnobvRco_!!6000000006294-2-tps-145-145.png'
        rows = ''.join(review(i, identity=False, media=f'<div class="album--fixture"><img width="160" height="160" src="{placeholder}" data-real="https://img.alicdn.com/real-{i}.jpg"></div>') for i in range(12))
        self.reload(drawer(rows) + '''<script>const pane=document.querySelector('#review-list');
          const observer=new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting)e.target.src=e.target.dataset.real}),{root:pane});
          document.querySelectorAll('[data-real]').forEach(e=>observer.observe(e));</script>''')
        result = self.collect()
        items = self.items(result)
        self.assertEqual(result['collected_count'], 12)
        self.assertEqual([i['image_urls'] for i in items], [[f'https://img.alicdn.com/real-{i}.jpg'] for i in range(12)])
        self.assertNotIn(placeholder, (self.folder() / '用户评价/评价明细.json').read_text(encoding='utf-8'))

    def test_media_address_change_updates_same_review_without_counting_again(self):
        rows = ''.join(review(i, identity=False, media=f'<div class="album--fixture"><img width="160" height="160" src="https://img.alicdn.com/old-{i}.jpg"></div>') for i in range(5))
        self.reload(drawer(rows, tail='') + '''<script>let changed=false;const pane=document.querySelector('#review-list');pane.onscroll=()=>{
          if(changed)return;changed=true;pane.querySelector('img').src='https://img.alicdn.com/updated.jpg';pane.insertAdjacentHTML('beforeend','<div>没有更多评价</div>')};</script>''')
        result = self.collect()
        self.assertEqual(result['collected_count'], 5)
        self.assertEqual(self.items(result)[0]['image_urls'], ['https://img.alicdn.com/updated.jpg'])

    def video_viewer_page(self, *, correct=True):
        media = '<div class="album--fixture"><div class="photo--fixture" id="video-tile"><img width="100" height="100" src="https://img.alicdn.com/poster.jpg"><div class="playerIcon--fixture"></div></div></div>'
        rows = review(1, media=media) + ''.join(review(i) for i in range(2, 6))
        viewer = ('<div class="BlackLightPage--fixture" style="position:fixed;inset:0;background:white">'
            '<div class="iconWrap--fixture" id="close-viewer" style="width:30px;height:30px"><img class="closeIcon--fixture" width="24" height="24"></div>'
            '<div class="itemVideo--fixture"><video preload="none" src="https://cloud.video.taobao.com/review-video.mp4"></video></div>'
            '<div class="itemTextWrap--fixture">' + ('用户1' if correct else '其他买家') + ' 2026年10月1日 · 规格A 实际评价1</div></div>')
        script = '''<script>window.videoOpens=0;window.videoCloses=0;document.querySelector('#video-tile').onclick=()=>{
          videoOpens++;document.body.insertAdjacentHTML('beforeend',PAYLOAD);document.querySelector('#close-viewer').onclick=()=>{videoCloses++;document.querySelector('[class^="BlackLightPage--"]').remove()}};
          document.querySelector('#review-list').onscroll=()=>{if(!document.querySelector('#end'))document.querySelector('#review-list').insertAdjacentHTML('beforeend','<div id="end">没有更多评价</div>')};</script>'''.replace('PAYLOAD', json.dumps(viewer, ensure_ascii=False))
        return drawer(rows, tail='') + '<div id="picGalleryEle"><video preload="none" src="https://cloud.video.taobao.com/PRODUCT-MAIN.mp4"></video></div>' + script

    def test_video_cover_opens_correct_review_player_once_and_records_link_only(self):
        self.reload(self.video_viewer_page())
        result = self.collect()
        items = self.items(result)
        self.assertEqual(items[0]['video_urls'], ['https://cloud.video.taobao.com/review-video.mp4'])
        self.assertEqual(items[0]['video_poster_urls'], ['https://img.alicdn.com/poster.jpg'])
        self.assertEqual(items[0]['image_urls'], [])
        self.assertEqual(items[0]['unresolved_media'], [])
        self.assertEqual(self.page.evaluate('[videoOpens,videoCloses]'), [1, 1])
        self.assertEqual(result['collected_count'], 5)
        self.assertFalse(list(self.folder().rglob('*.mp4')))

    def test_video_player_wrong_review_requires_manual_correction_without_reopening(self):
        self.reload(self.video_viewer_page(correct=False))
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('[class^="itemTextWrap--"]').evaluate("e=>e.textContent='用户1 2026年10月1日 · 规格A 实际评价1'")
            return '继续'
        result = self.collect(attention)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.page.evaluate('[videoOpens,videoCloses]'), [1, 1])
        self.assertEqual(self.items(result)[0]['video_urls'], ['https://cloud.video.taobao.com/review-video.mp4'])

    def test_folded_notice_at_stable_bottom_finishes_only_visible_scope(self):
        self.reload(drawer(review(1) + folded_notice(), count='2000+', tail=''))
        result = self.collect()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['stop_reason'], 'folded_visible_end')
        self.assertEqual((result['collected_count'], result['folded_count']), (1, 1978))
        self.assertEqual(result['bottom_confirmation_rounds'], 3)
        self.assertTrue(result['end_confirmed'])
        self.assertEqual(result['scope'], 'page_available_reviews')
        self.assertEqual(result['review_count_display']['raw'], '2000+')
        self.assertIsNone(result['review_count_display']['value'])
        self.assertEqual(result['review_count_display']['lower_bound'], 2000)
        self.assertIn('当前页面可见评价采集完成', result['message'])
        exported = json.loads((self.folder() / '用户评价/评价明细.json').read_text(encoding='utf-8'))
        self.assertEqual(exported['summary']['folded_count'], 1978)
        self.assertEqual(exported['summary']['collected_count'], 1)
        self.assertEqual(exported['summary']['completion_evidence']['method'], 'folded_visible_end')
        with (self.folder() / '用户评价/评价明细.csv').open(encoding='utf-8-sig', newline='') as source:
            self.assertEqual(len(list(csv.DictReader(source))), 1)

    def test_stall_without_end_or_folded_notice_still_pauses(self):
        self.reload(drawer(review(1), tail=''))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message: '停止')
        last = self.document()['reviews_last_attempt']
        self.assertEqual(last['status'], 'partial')
        self.assertFalse(last['end_confirmed'])
        self.assertEqual(last['collected_count'], 1)

    def test_folded_notice_before_actual_bottom_still_pauses(self):
        self.reload(drawer(review(1).replace('min-height:110px', 'min-height:1800px') + folded_notice(), tail=''))
        with patch('ecommerce.modules.collection.detail_reviews.scroll_reviews'), self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        last = self.document()['reviews_last_attempt']
        self.assertFalse(last['end_confirmed'])
        self.assertEqual(last['bottom_confirmation_rounds'], 0)

    def test_folded_notice_with_expand_or_unknown_icon_entry_never_finishes(self):
        for extra in ('<button>展开评价</button>', '<button aria-label="未知入口"></button>',
                      '<span style="cursor:pointer" onclick="void(0)"></span>'):
            with self.subTest(extra=extra):
                self.reload(drawer(review(1) + folded_notice(extra=extra), tail=''))
                with self.assertRaises(TaskStopped):
                    self.collect(lambda _: '停止')
                self.assertFalse(self.document()['reviews_last_attempt']['end_confirmed'])
                self.assertNotIn('reviews', self.document())

    def test_folded_notice_does_not_hide_pending_review_image(self):
        placeholder = 'https://img.alicdn.com/imgextra/i2/O1CN01Dqo1gd1wMhnobvRco_!!6000000006294-2-tps-145-145.png'
        media = '<div class="album--fixture"><img width="50" height="50" src="' + placeholder + '"></div>'
        self.reload(drawer(review(1, media=media) + folded_notice(), tail=''))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        last = self.document()['reviews_last_attempt']
        self.assertFalse(last['end_confirmed'])
        self.assertEqual(last['collected_count'], 0)

    def test_folded_notice_does_not_hide_loading_state(self):
        self.reload(drawer(review(1) + folded_notice(), tail='').replace('id="review-list"', 'aria-busy="true" id="review-list"'))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertFalse(self.document()['reviews_last_attempt']['end_confirmed'])

    def test_folded_notice_late_review_resets_bottom_confirmation(self):
        from ecommerce.platforms.taobao.detail_reviews import scroll_reviews
        self.reload(drawer(review(1) + folded_notice(), tail=''))
        calls = []
        def scroll(result, value, timeout_ms):
            scroll_reviews(result, value, timeout_ms)
            calls.append(1)
            if len(calls) == 2:
                self.page.locator('[class*="foldInfoWrap--"]').evaluate('(e,html)=>e.insertAdjacentHTML("beforebegin",html)', review(2))
        with patch('ecommerce.modules.collection.detail_reviews.scroll_reviews', side_effect=scroll):
            result = self.collect()
        self.assertEqual(result['collected_count'], 2)
        self.assertEqual(result['bottom_confirmation_rounds'], 3)
        self.assertGreaterEqual(len(calls), 5)

    def test_folded_notice_with_resolved_video_finishes_without_reopening_player(self):
        self.reload(self.video_viewer_page())
        self.page.locator('#review-list').evaluate('(e,html)=>{e.onscroll=null;e.insertAdjacentHTML("beforeend",html)}', folded_notice())
        result = self.collect()
        self.assertEqual(result['stop_reason'], 'folded_visible_end')
        self.assertEqual(result['collected_count'], 5)
        self.assertEqual(self.page.evaluate('[videoOpens,videoCloses]'), [1, 1])
        self.assertEqual(self.items(result)[0]['video_urls'], ['https://cloud.video.taobao.com/review-video.mp4'])

    def test_folded_notice_with_all_reviews_filtered_is_not_no_reviews(self):
        self.reload(drawer(review(1, TEMPLATE_REVIEW) + folded_notice('1,978'), tail=''))
        result = self.collect()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['stop_reason'], 'folded_visible_end')
        self.assertTrue(result['all_filtered'])
        self.assertEqual(result['review_availability'], 'present')
        self.assertEqual((result['collected_count'], result['skipped_template_count'], result['folded_count']), (0, 1, 1978))

    def test_unknown_structure_preserves_previous_complete_result(self):
        first = self.collect()
        self.reload(drawer(review(1, extra='<div>未识别的追加内容</div>')))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message: '停止')
        self.assertEqual(self.document()['reviews']['batch_id'], first['batch_id'])

    def test_wrong_filter_requires_explicit_manual_restore(self):
        self.reload(drawer(review(1), selected='有图'))
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('#filter').evaluate("e=>e.textContent='全部'")
            return '继续'
        result = self.collect(attention)
        self.assertEqual(result['filter'], '全部')
        self.assertEqual(len(prompts), 1)

    def test_verification_during_scroll_waits_and_does_not_duplicate_records(self):
        self.reload(drawer(''.join(review(i) for i in range(8)), tail='') + '''<script>
          let fired=false;document.querySelector('#review-list').onscroll=()=>{if(fired)return;fired=true;
          const e=document.createElement('div');e.className='J_MIDDLEWARE_FRAME_WIDGET';e.textContent='请完成安全验证';document.body.append(e)};</script>''')
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate('e=>e.remove()')
            self.page.locator('#review-list').evaluate("e=>e.insertAdjacentHTML('beforeend','<div>没有更多评价</div>')")
            return '继续'
        result = self.collect(attention)
        self.assertEqual(result['collected_count'], 8)
        self.assertEqual(len(prompts), 1)

    def test_csv_formula_like_text_is_displayed_as_text_and_json_unchanged(self):
        self.reload(drawer(review(1, '=1+1', author='@用户')))
        result = self.collect()
        self.assertEqual(self.items(result)[0]['content'], '=1+1')
        with (self.folder() / result['files'][1]['history_file']).open(encoding='utf-8-sig', newline='') as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(rows[0]['评价正文'], "'=1+1")
        self.assertEqual(rows[0]['用户显示名称'], "'@用户")
        self.assertRegex(rows[0]['采集时间'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_repeat_preserves_other_sections_and_publishes_shorter_files_cleanly(self):
        for key in ('video', 'main_images', 'product_info', 'sku_details', 'parameters', 'detail_images', 'review_summary'):
            save_detail_section(self.folder(), ITEM_ID, key, {'unchanged': key})
        first = self.collect()
        self.reload(drawer(review(1)))
        second = self.collect()
        latest = json.loads((self.folder() / '用户评价/评价明细.json').read_text(encoding='utf-8'))
        self.assertEqual(len(latest['reviews']), 1)
        self.assertTrue((self.folder() / first['history_record_file']).is_file())
        self.assertNotEqual(first['batch_id'], second['batch_id'])
        for key in ('video', 'main_images', 'product_info', 'sku_details', 'parameters', 'detail_images', 'review_summary'):
            self.assertEqual(self.document()[key], {'unchanged': key})

    def test_publish_json_failure_restores_previous_folder_and_json(self):
        first = self.collect()
        prior = (self.folder() / '用户评价/评价明细.json').read_bytes()
        self.reload(drawer(review(99)))
        original = save_detail_section
        def save(directory, item_id, section, value):
            if section == 'reviews':
                raise OSError('disk full')
            return original(directory, item_id, section, value)
        with patch('ecommerce.modules.collection.detail_reviews.save_detail_section', side_effect=save):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual((self.folder() / '用户评价/评价明细.json').read_bytes(), prior)
        self.assertEqual(self.document()['reviews']['batch_id'], first['batch_id'])
        self.assertFalse(list(self.folder().glob('.reviews-*')))

    def test_unregistered_user_file_is_preserved(self):
        first = self.collect()
        user_file = self.folder() / '用户评价/手动备注.txt'
        user_file.write_text('用户资料', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.collect()
        self.assertEqual(user_file.read_text(encoding='utf-8'), '用户资料')
        self.assertEqual(self.document()['reviews']['batch_id'], first['batch_id'])

    def test_new_no_reviews_replaces_latest_with_empty_files_and_keeps_history(self):
        first = self.collect()
        self.reload(drawer('<div>暂无评价</div>', count='0', tail=''))
        second = self.collect()
        self.assertEqual(second['status'], 'no_reviews')
        self.assertEqual(json.loads((self.folder() / '用户评价/评价明细.json').read_text(encoding='utf-8'))['reviews'], [])
        self.assertEqual(len(self.items(first)), 5)

    def test_open_button_clicked_once_and_service_only_runs_reviews(self):
        from ecommerce.modules.collection.detail_service import open_detail_input
        from ecommerce.platforms.taobao import session
        self.settings = replace(self.settings, timeout_ms=1000)
        panel = drawer(review(1))
        self.reload('<div class="Comments--entry"><div class="footer--entry"><div class="ShowButton--entry" id="open">查看全部评价</div></div></div><main id="target"></main>'
            '<script>window.openClicks=0;document.querySelector("#open").onclick=()=>{openClicks++;document.querySelector("#target").innerHTML='
            + json.dumps(panel, ensure_ascii=False) + '}</script>')
        @contextmanager
        def browser(settings):
            yield self.context, self.page
        with patch.object(session, 'base_browser_session', browser):
            message = open_detail_input(self.settings, ITEM_ID, wait=False, reviews=True)
        self.assertIn('已采集1条有效评价', message)
        self.assertEqual(self.page.evaluate('openClicks'), 1)
        self.assertNotIn('sku_details', self.document())


if __name__ == '__main__':
    unittest.main()
