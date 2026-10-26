"""问大家本地浏览器状态测试，不访问淘宝：展开、限额、加载、人工接管及发布回滚。"""
import csv
import json
import os
import tempfile
import unittest
from dataclasses import replace
from html import escape
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.detail_questions import QuestionPacer, collect_questions
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.detail_questions import read_questions
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID = '1050158363561'


def answer(i, text=None, *, identity=True, extra='', author=None):
    return (f'<div class="answerItem--fixture"' + (f' data-answer-id="{i}"' if identity else '') + '>'
        '<div class="userInfo--fixture"><img class="userAvatar--fixture" src="https://img.alicdn.com/avatar.jpg">'
        '<div class="userDetails--fixture"><div class="userDetailsInfo--fixture"><span class="userNick--fixture">'
        + escape(author or '用户' + str(i)) + '</span><div class="userTag--fixture">已购</div>'
        '<img class="userIcon--fixture" src="https://img.alicdn.com/level.png"></div></div></div>'
        '<div class="answerContent--fixture">' + escape(text if text is not None else '回答内容' + str(i)) + '</div>'
        '<div class="answerMeta--fixture"><span class="answerTime--fixture" title="近1个月发布 · 规格A">近1个月发布 · 规格A</span>'
        '<span class="timeAgo--fixture">半年内已购</span></div>' + extra + '</div>')


def question(i, answers=None, *, expanded=True, identity=True, text=None, more='', section_style='', empty=False):
    content = ('<div>暂无回答</div>' if empty else answers if answers is not None else answer(1))
    return (f'<div class="qaItem--fixture"' + (f' data-question-id="{i}"' if identity else '') + '>'
        '<div class="questionSection--fixture"><div class="questionTitle--fixture" title="' + escape(text or '问题' + str(i), quote=True)
        + '">' + escape(text or '问题' + str(i)) + '</div></div><div class="answerSection--fixture" style="' + section_style + '">'
        + content + '</div><div class="bottomActionsWrap--fixture">' + more
        + ('<div class="collapseBtn--fixture">收起</div>' if expanded else '<div class="viewAllBtnWrap--fixture"><div>查看全部回答</div></div>') + '</div></div>')


def drawer(content, *, count='5', tail='', selected=False, search=''):
    return ('<div class="askAnswerInfoDrawer--fixture" style="position:fixed;right:0;top:0;width:700px;height:700px;background:white">'
        '<div class="AskAnswersWrap--fixture"><div class="headerWrap--fixture"><div class="askAnswerTitle--fixture">问大家·' + count
        + '</div><input value="' + search + '"></div><div class="ConditionWrap--fixture">'
        + ''.join('<div class="conditionItem--fixture' + (' selected--fixture' if selected and i == 0 else '') + '">标签' + str(i) + ' 1</div>' for i in range(6))
        + '</div><div id="questions-area" class="ContentArea--fixture" style="height:530px;overflow:auto">'
        '<div class="qaListContainer--fixture">' + content + ('<div id="end">' + tail + '</div>' if tail else '') + '</div></div></div></div>')


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用问大家本地浏览器检查')
class TaobaoDetailQuestionsBrowserTests(unittest.TestCase):
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
        self.settings = replace(load_settings(root=Path(self.temp.name)).for_platform('taobao'), headless=True, timeout_ms=350,
            taobao_timing={k: (0, 0) for k in DEFAULT_TAOBAO_TIMING})
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.body = drawer(question(1), count='1')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8',
            body='<!doctype html><meta charset="utf-8"><span class="site-nav-login-info-nick">账号</span>' + self.body))
        self.page = self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id=' + ITEM_ID)
        self.result = DetailPage(self.page, resolve_detail_input(ITEM_ID), '问答测试商品')

    def reload(self, body):
        self.body = body
        self.page.reload()

    def collect(self, attention=None, *, all_questions=False):
        with TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing) as guard:
            guard.login_mode = False
            return collect_questions(self.settings, self.result, all_questions=all_questions, stable_ms=20)

    def folder(self):
        return product_directory(self.settings, ITEM_ID)

    def items(self, result):
        return json.loads((self.folder() / result['files'][0]['history_file']).read_text(encoding='utf-8'))['questions']

    def document(self):
        return json.loads((self.folder() / (ITEM_ID + '.json')).read_text(encoding='utf-8'))

    def test_default_50_finishes_every_answer_of_last_question(self):
        # 大型DOM包含55题和第50题的65条回答；350ms的异常场景预算不足以稳定读取两次。
        self.settings = replace(self.settings, timeout_ms=1500)
        rows = ''.join(question(i, ''.join(answer(j) for j in range(65)) if i == 49 else None) for i in range(55))
        self.reload(drawer(rows, count='55'))
        result = self.collect()
        items = self.items(result)
        self.assertEqual((result['collected_count'], result['answer_count']), (50, 114))
        self.assertEqual(result['stop_reason'], 'limit_reached')
        self.assertFalse(result['end_confirmed'])
        self.assertEqual(len(items[-1]['answers']), 65)
        self.assertEqual([q['question_id'] for q in items], [str(i) for i in range(50)])
        self.assertTrue(all(q['answers_complete'] for q in items))
        self.assertEqual(result['rest_count'], 4)

    def test_all_removes_only_question_cap_and_retains_short_answers(self):
        self.settings = replace(self.settings, timeout_ms=1500)
        self.reload(drawer(''.join(question(i, answer(1, '非常好')) for i in range(53)), count='53'))
        result = self.collect(all_questions=True)
        self.assertEqual((result['collected_count'], result['answer_count']), (53, 53))
        self.assertIsNone(result['requested_limit'])
        self.assertTrue(result['end_confirmed'])
        self.assertTrue(all(q['answers'][0]['content'] == '非常好' for q in self.items(result)))

    def test_collapsed_preview_is_not_duplicate_answer(self):
        self.reload(drawer(question(1, '<div class="answerItem--fixture">已购 非常好</div>', expanded=False, section_style='height:28px;overflow:hidden'), count='1'))
        self.page.evaluate('''html=>{window.clicks=0;const q=document.querySelector('[class*="qaItem--"]');q.querySelector('[class*="viewAllBtnWrap--"]').onclick=()=>{window.clicks++;q.outerHTML=html}}''',
            question(1, answer(1, '非常好') + answer(2, '十来张吧')))
        result = self.collect()
        q = self.items(result)[0]
        self.assertEqual(result['answer_count'], 2)
        self.assertEqual([a['content'] for a in q['answers']], ['非常好', '十来张吧'])
        self.assertEqual(self.page.evaluate('window.clicks'), 1)
        self.assertIn('非常好', q['preview_text'])

    def test_answer_more_loads_until_gone_and_deduplicates_overlap(self):
        self.reload(drawer(question(1, answer(1), more='<button id="more">加载更多回答</button>'), count='1'))
        self.page.evaluate('''html=>document.querySelector('#more').onclick=()=>{document.querySelector('[class*="answerSection--"]').insertAdjacentHTML('beforeend',html);document.querySelector('#more').remove()}''', answer(2))
        result = self.collect()
        self.assertEqual(result['answer_count'], 2)
        self.assertEqual([a['answer_id'] for a in self.items(result)[0]['answers']], ['1', '2'])

    def test_expand_more_button_loads_all_answer_batches_before_complete(self):
        self.reload(drawer(question(1, answer(1), more='<div class="expandBtn--fixture"><div id="more">展开更多</div></div>'), count='1'))
        self.page.evaluate('''html=>{window.moreClicks=0;const b=document.querySelector('#more');b.onclick=()=>{window.moreClicks++;document.querySelector('[class*="answerSection--"]').insertAdjacentHTML('beforeend',html[window.moreClicks-1]);if(window.moreClicks===2)b.parentElement.remove()}}''', [answer(2), answer(3)])
        before = read_questions(self.page, ITEM_ID)['rows'][0]
        self.assertFalse(before['answers_complete'])
        self.assertEqual(before['more_controls'][0]['text'], '展开更多')
        result = self.collect()
        self.assertEqual(result['answer_count'], 3)
        self.assertEqual(self.page.evaluate('window.moreClicks'), 2)
        self.assertTrue(self.items(result)[0]['answers_complete'])

    def test_unknown_bottom_control_blocks_false_completion(self):
        self.reload(drawer(question(1, answer(1), more='<button>其他回答入口</button>'), count='1'))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertNotIn('questions', self.document())

    def test_overreported_title_finishes_after_repeated_stable_bottom(self):
        self.reload(drawer(question(1) + question(2), count='3'))
        result = self.collect()
        self.assertEqual(result['completed_question_count'], 2)
        self.assertEqual(result['question_count_display']['value'], 3)
        self.assertEqual(result['question_count_difference'], 1)
        self.assertEqual(result['stop_reason'], 'stable_bottom')
        self.assertEqual(result['bottom_confirmation_rounds'], 3)
        self.assertTrue(result['end_confirmed'])
        self.assertTrue(result['title_count_is_reference_only'])

    def test_underreported_title_never_truncates_actual_questions(self):
        self.reload(drawer(question(1) + question(2), count='1'))
        result = self.collect()
        self.assertEqual(result['collected_count'], 2)
        self.assertEqual(result['question_count_difference'], -1)
        self.assertEqual(result['stop_reason'], 'stable_bottom')

    def test_zero_title_does_not_discard_actual_questions(self):
        self.reload(drawer(question(1) + question(2), count='0'))
        result = self.collect()
        self.assertEqual((result['collected_count'], result['question_count_difference']), (2, -2))
        self.assertEqual(result['stop_reason'], 'stable_bottom')

    def test_zero_preview_with_entry_still_opens_actual_questions(self):
        self.reload('<div class="askAnswerWrap--fixture"><div class="askAnswerTitle--fixture">问大家·0</div><button id="open">查看全部问答</button></div>')
        self.page.evaluate('html=>document.querySelector("#open").onclick=()=>document.body.insertAdjacentHTML("beforeend",html)', drawer(question(1), count='0'))
        result = self.collect()
        self.assertEqual(result['collected_count'], 1)
        self.assertEqual(result['status'], 'complete')

    def test_empty_drawer_with_zero_title_needs_explicit_empty_state(self):
        self.reload(drawer('', count='0'))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertEqual(self.document()['questions_last_attempt']['status'], 'partial')

    def test_exact_total_questions_confirms_end_without_marker_and_ignores_topic_counts(self):
        self.reload(drawer(question(1) + question(2), count='2'))
        result = self.collect()
        self.assertEqual(result['collected_count'], 2)
        self.assertEqual(result['question_count_display']['value'], 2)
        self.assertEqual(result['stop_reason'], 'stable_bottom')

    def test_no_questions_is_explicit_and_logged(self):
        self.reload('<div class="askAnswerWrap--fixture"><div class="askAnswerTitle--fixture">问大家·0</div></div>')
        with self.assertLogs('ecommerce.modules.collection.detail_questions', level='INFO') as logs:
            result = self.collect()
        self.assertEqual((result['status'], result['message']), ('no_questions', '无问答'))
        self.assertIn('无问答', str(logs.output))
        self.assertEqual(self.items(result), [])

    def test_missing_entry_skips_after_ten_scrolls_without_overwriting_existing_questions(self):
        """找不到入口正常返回跳过，旧问答和有效文件不变；参数：无；返回：无。"""
        self.collect()
        before=self.document()
        old_files=sorted(str(p) for p in self.folder().rglob('*') if p.is_file())
        self.reload('<div style="height:50000px">商品介绍，没有问答入口</div>')
        def attention(message):
            self.fail('入口缺失不应请求人工处理：'+message)
        with self.assertLogs('ecommerce.modules.collection.detail_questions',level='INFO') as logs:
            result=self.collect(attention)
        self.assertEqual(result['status'],'skipped')
        self.assertEqual(result['entry_search']['scroll_count'],10)
        self.assertEqual(result['entry_search']['stop_reason'],'scroll_limit_reached')
        self.assertIn('未找到问大家入口，已跳过',str(logs.output))
        self.assertEqual(self.document(),before)
        self.assertEqual(sorted(str(p) for p in self.folder().rglob('*') if p.is_file()),old_files)

    def test_missing_entry_at_stable_bottom_skips_after_three_scrolls(self):
        """连续三次位置高度不变即提前跳过，不创建问答批次；参数：无；返回：无。"""
        self.reload('<div>商品介绍，没有问答入口</div>')
        result=self.collect(lambda message:self.fail('不应等待人工：'+message))
        self.assertEqual(result['status'],'skipped')
        self.assertEqual(result['entry_search']['scroll_count'],3)
        self.assertEqual(result['entry_search']['stop_reason'],'stable_position')
        self.assertFalse(self.folder().exists())

    def test_ambiguous_entry_does_not_use_missing_entry_skip(self):
        """两个入口属于结构冲突，继续沿用人工处理；参数：无；返回：无。"""
        self.reload('<div class="askAnswerWrap--fixture"><button>查看全部问答</button><button>查看全部问答</button></div>')
        with self.assertRaises(TaskStopped):self.collect(lambda message:'停止')

    def test_unanswered_question_remains_in_json_and_csv(self):
        self.reload(drawer(question(1, empty=True), count='1'))
        result = self.collect()
        q = self.items(result)[0]
        self.assertEqual(q['answers'], [])
        self.assertEqual(q['answer_stop_reason'], 'no_answers')
        with (self.folder() / '问大家/问答明细.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['回答状态'], '暂无回答')
        self.assertNotIn(None, rows[0])

    def test_loading_is_not_empty_and_persists_partial(self):
        self.reload(drawer('', count='5'))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        doc = self.document()
        self.assertNotIn('questions', doc)
        self.assertEqual(doc['questions_last_attempt']['status'], 'partial')
        self.assertEqual(doc['questions_last_attempt']['collected_count'], 0)

    def test_unknown_answer_structure_pauses_instead_of_silent_omission(self):
        self.reload(drawer(question(1, answer(1) + '<div>未知新增正文</div>'), count='1'))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertNotIn('questions', self.document())

    def test_selected_topic_and_search_require_manual_restore(self):
        self.reload(drawer(question(1), count='1', selected=True, search='关键词'))
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.evaluate("document.querySelector('.selected--fixture').classList.remove('selected--fixture');document.querySelector('input').value=''")
            return '继续'
        result = self.collect(attention)
        self.assertEqual(result['answer_count'], 1)
        self.assertEqual(len(prompts), 1)

    def test_explicit_selected_all_with_count_allows_open_and_question_scroll(self):
        self.reload(drawer(question(1), count='2'))
        self.page.evaluate('''html=>{const chip=document.querySelector('[class*="conditionItem--"]');chip.classList.add('isSelected--fixture');chip.innerHTML='全部<span class="conditionItemCount--fixture">2</span>';document.querySelector('[class*="qaItem--"]').style.minHeight='700px';document.querySelector('#questions-area').onscroll=()=>{if(!window.added){window.added=true;document.querySelector('[class*="qaListContainer--"]').insertAdjacentHTML('beforeend',html)}}}''', question(2))
        snapshot = read_questions(self.page, ITEM_ID)
        self.assertEqual(snapshot['filter'], ['全部'])
        result = self.collect()
        self.assertEqual((result['collected_count'], result['answer_count']), (2, 2))
        self.assertGreater(result['scroll_count'], 0)

    def test_explicit_selected_all_allows_collapsed_answer_expansion(self):
        self.reload(drawer(question(1, '<div class="answerItem--fixture">预览</div>', expanded=False), count='1'))
        self.page.evaluate('''html=>{const chip=document.querySelector('[class*="conditionItem--"]');chip.classList.add('isSelected--fixture');chip.innerHTML='全部<span class="conditionItemCount--fixture">1</span>';document.querySelector('[class*="viewAllBtnWrap--"]').onclick=()=>document.querySelector('[class*="qaItem--"]').outerHTML=html}''', question(1, answer(1) + answer(2)))
        self.assertEqual(self.collect()['answer_count'], 2)

    def test_wrong_product_must_be_restored(self):
        self.page.evaluate("history.replaceState(null,'','?id=1091534543310')")
        def attention(_):
            self.page.evaluate("history.replaceState(null,'','?id=" + ITEM_ID + "')")
            return '继续'
        self.assertEqual(self.collect(attention)['collected_count'], 1)

    def test_approximate_title_is_reference_only_at_confirmed_bottom(self):
        self.reload(drawer(question(1), count='1万+'))
        result = self.collect(all_questions=True)
        self.assertEqual(result['stop_reason'], 'stable_bottom')
        self.assertIsNone(result['question_count_difference'])
        self.assertTrue(result['end_confirmed'])

    def test_scroll_stuck_before_bottom_still_pauses(self):
        self.reload(drawer(question(1), count='64'))
        self.page.evaluate('''()=>{document.querySelector('[class*="qaItem--"]').style.minHeight='1800px';Object.defineProperty(document.querySelector('#questions-area'),'scrollTop',{get:()=>0,set:v=>{}})}''')
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertNotIn('questions', self.document())
        self.assertFalse(self.document()['questions_last_attempt']['end_confirmed'])

    def test_matching_title_does_not_hide_delayed_next_question(self):
        self.reload(drawer(question(1), count='1'))
        self.page.evaluate('''html=>{let checks=0;Object.defineProperty(document.querySelector('#questions-area'),'scrollTop',{get:()=>0,set:v=>{checks++;if(checks===2&&!window.added){window.added=true;document.querySelector('[class*="qaListContainer--"]').insertAdjacentHTML('beforeend',html)}}})}''', question(2))
        result = self.collect()
        self.assertEqual(result['collected_count'], 2)
        self.assertEqual(result['question_count_difference'], -1)
        self.assertEqual(result['bottom_confirmation_rounds'], 3)

    def test_list_lazy_loads_next_question_on_its_own_scroll(self):
        self.reload(drawer(question(1, '<div style="height:700px">' + answer(1) + '</div>'), count='2'))
        # 正式卡片只接受 answerItem 作为直接子项。
        self.page.evaluate("document.querySelector('[class*=\"answerSection--\"]>div').outerHTML=document.querySelector('[class*=\"answerItem--\"]').outerHTML;document.querySelector('[class*=\"qaItem--\"]').style.minHeight='700px'")
        self.page.evaluate('''html=>document.querySelector('#questions-area').onscroll=()=>{if(!window.added){window.added=true;document.querySelector('[class*="qaListContainer--"]').insertAdjacentHTML('beforeend',html)}}''', question(2))
        result = self.collect()
        self.assertEqual(result['collected_count'], 2)
        self.assertGreater(result['scroll_count'], 0)

    def test_same_short_answer_under_different_questions_is_retained(self):
        self.reload(drawer(question(1, answer(1, '可以', identity=False, author='匿名')) + question(2, answer(1, '可以', identity=False, author='匿名')), count='2'))
        items = self.items(self.collect())
        self.assertEqual(sum(len(q['answers']) for q in items), 2)
        self.assertNotEqual(items[0]['question_key'], items[1]['question_key'])
        self.assertFalse(items[0]['answers'][0]['identity_exact'])

    def test_answer_scroll_region_loads_remaining_answers_until_explicit_end(self):
        self.reload(drawer(question(1, answer(1), section_style='height:100px;overflow:auto'), count='1'))
        self.page.evaluate('''html=>{const area=document.querySelector('[class*="answerSection--"]');area.firstElementChild.style.minHeight='700px';area.onscroll=()=>{if(!window.loaded){window.loaded=true;area.insertAdjacentHTML('beforeend',html)}}}''', answer(2) + '<div>没有更多回答</div>')
        result = self.collect()
        self.assertEqual(result['answer_count'], 2)
        # 一次回答区滚动，另加三次问题列表底部复核。
        self.assertEqual(result['scroll_count'], 1 + result['bottom_confirmation_rounds'])
        self.assertEqual(result['bottom_confirmation_rounds'], 3)
        self.assertEqual(self.items(result)[0]['answer_stop_reason'], 'explicit_end')

    def test_hidden_text_excluded_emoji_and_missing_media_preserved_without_invented_url(self):
        self.reload(drawer(question(1, answer(1, extra='<img>')), count='1'))
        self.page.evaluate('''()=>{document.querySelector('[class*="answerContent--"]').innerHTML='真实正文<img class="convertEmoji--fixture" alt="[笑]" src="https://img.alicdn.com/emoji.png"><span style="display:none">隐藏副本</span>'}''')
        result = self.collect()
        a = self.items(result)[0]['answers'][0]
        self.assertEqual(a['content'], '真实正文[笑]')
        self.assertEqual(a['image_urls'], [])
        self.assertEqual(a['unresolved_media'], ['回答图片链接未展示'])

    def test_new_empty_result_replaces_latest_but_retains_previous_history(self):
        first = self.collect()
        self.reload(drawer('', count='0', tail='暂无问答'))
        second = self.collect()
        self.assertEqual(second['status'], 'no_questions')
        self.assertEqual(self.items(second), [])
        self.assertEqual(len(self.items(first)), 1)

    def test_identical_anonymous_questions_and_answers_preserve_occurrences(self):
        a = answer(1, '可以', identity=False, author='匿名')
        q = question(1, a + a, identity=False, text='匿名同文问题')
        self.reload(drawer(q + q, count='2'))
        result = self.collect()
        self.assertEqual((result['collected_count'], result['answer_count']), (2, 4))
        self.assertFalse(self.items(result)[0]['identity_exact'])

    def test_media_links_and_relative_date_saved_without_avatars_or_download(self):
        media = '<div class="answerAlbum--fixture"><img src="https://img.alicdn.com/photo.jpg"><video src="https://cloud.video.taobao.com/video.mp4" poster="https://img.alicdn.com/poster.jpg"></video></div>'
        self.reload(drawer(question(1, answer(1, '=hello', extra=media), text='+问题'), count='1'))
        result = self.collect()
        a = self.items(result)[0]['answers'][0]
        self.assertEqual(a['image_urls'], ['https://img.alicdn.com/photo.jpg'])
        self.assertEqual(a['video_urls'], ['https://cloud.video.taobao.com/video.mp4'])
        self.assertEqual((a['date_raw'], a['sku']), ('近1个月发布', '规格A'))
        self.assertEqual(a['content'], '=hello')
        with (self.folder() / '问大家/问答明细.csv').open(encoding='utf-8-sig', newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual((row['问题内容'], row['回答内容']), ("'+问题", "'=hello"))
        self.assertRegex(row['采集时间'], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$')
        self.assertEqual({p.suffix for p in (self.folder() / '问大家').iterdir()}, {'.json', '.csv'})

    def test_no_replay_when_expand_does_not_change_state(self):
        self.reload(drawer(question(1, '<div class="answerItem--fixture">预览</div>', expanded=False), count='1'))
        self.page.evaluate("()=>{window.clicks=0;document.querySelector('[class*=\"viewAllBtnWrap--\"]').onclick=()=>window.clicks++}")
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertEqual(self.page.evaluate('window.clicks'), 1)
        self.assertEqual(self.document()['questions_last_attempt']['answer_count'], 0)

    def test_explicit_end_after_questions_and_approximate_count(self):
        self.reload(drawer(question(1), count='1万+', tail='没有更多问题'))
        result = self.collect(all_questions=True)
        self.assertTrue(result['end_confirmed'])
        self.assertEqual(result['collected_count'], 1)

    def test_fulltext_expander_runs_before_storing_answer(self):
        self.reload(drawer(question(1, answer(1)), count='1'))
        self.page.evaluate('''()=>{const e=document.querySelector('[class*="answerContent--"]');e.innerHTML='短预览<button>展开全文</button>';e.querySelector('button').onclick=()=>e.textContent='完整回答，保留全部正文'}''')
        result = self.collect()
        self.assertEqual(self.items(result)[0]['answers'][0]['content'], '完整回答，保留全部正文')

    def test_complete_answers_without_expand_or_collapse_button_are_collected(self):
        content = question(1, answer(1, '有区别，贵的就是好，品质高')) + question(2, answer(1) + answer(2))
        self.reload(drawer(content, count='2'))
        self.page.evaluate('''()=>{for(const n of document.querySelectorAll('[class*="collapseBtn--"]'))n.remove()}''')
        result = self.collect()
        items = self.items(result)
        self.assertEqual(result['answer_count'], 3)
        self.assertTrue(all(q['answers_complete'] for q in items))
        self.assertTrue(all(q['answer_stop_reason'] == 'fully_visible_without_toggle' for q in items))
        self.assertTrue(all(q['answer_display_mode'] == 'full_without_toggle' for q in items))

    def test_clipped_answer_without_toggle_is_not_reported_complete(self):
        self.reload(drawer(question(1, answer(1, '正文'), section_style='height:20px;overflow:hidden'), count='1'))
        self.page.evaluate('''()=>document.querySelector('[class*="collapseBtn--"]').remove()''')
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertNotIn('questions', self.document())

    def test_previous_complete_and_other_sections_survive_failure(self):
        save_detail_section(self.folder(), ITEM_ID, 'parameters', {'items': ['保留参数']})
        first = self.collect()
        previous = (self.folder() / '问大家/问答明细.json').read_bytes()
        self.reload(drawer('', count='5'))
        with self.assertRaises(TaskStopped):
            self.collect(lambda _: '停止')
        self.assertEqual(self.document()['questions']['batch_id'], first['batch_id'])
        self.assertEqual(self.document()['parameters'], {'items': ['保留参数']})
        self.assertEqual((self.folder() / '问大家/问答明细.json').read_bytes(), previous)

    def test_publish_rolls_back_on_root_json_error(self):
        first = self.collect()
        previous = (self.folder() / '问大家/问答明细.json').read_bytes()
        from ecommerce.modules.collection.detail_questions import save_detail_section as save
        def fail(directory, item_id, section, value):
            if section == 'questions':
                raise OSError('fixture publication failed')
            return save(directory, item_id, section, value)
        with patch('ecommerce.modules.collection.detail_questions.save_detail_section', side_effect=fail), self.assertRaises(OSError):
            self.collect()
        self.assertEqual((self.folder() / '问大家/问答明细.json').read_bytes(), previous)
        self.assertEqual(self.document()['questions']['batch_id'], first['batch_id'])

    def test_unmanaged_or_modified_files_are_not_overwritten(self):
        self.collect()
        path = self.folder() / '问大家/个人笔记.txt'
        path.write_text('保留', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.collect()
        self.assertEqual(path.read_text(encoding='utf-8'), '保留')

    def test_review_drawer_is_closed_before_opening_questions(self):
        preview = '<div class="askAnswerWrap--fixture"><div class="askAnswerTitle--fixture">问大家·1</div><div id="open">查看全部问答</div></div>'
        review = '<div class="Drawer--fixture" style="position:fixed"><div class="tabDetailItemTitle--fixture">用户评价·1</div><div class="closeWrap--fixture" onclick="this.parentElement.remove()">关闭</div></div>'
        self.reload(preview + review)
        self.page.evaluate('''html=>document.querySelector('#open').onclick=()=>document.body.insertAdjacentHTML('beforeend',html)''', drawer(question(1), count='1'))
        self.assertEqual(self.collect()['answer_count'], 1)
        self.assertEqual(self.page.locator('.Drawer--fixture').count(), 0)

    def test_main_page_lazy_loads_entry_before_opening_and_service_routes_questions(self):
        from contextlib import contextmanager
        from ecommerce.modules.collection.detail_service import open_detail_input
        from ecommerce.platforms.taobao import session
        self.settings = replace(self.settings, timeout_ms=1000)
        panel = drawer(question(1), count='1')
        entry = '<div class="askAnswerWrap--fixture"><div class="askAnswerTitle--fixture">问大家·1</div><div id="open">查看全部问答</div></div>'
        script = '''<script>window.openClicks=0;window.onscroll=()=>{if(!window.loaded){window.loaded=true;document.querySelector('#target').innerHTML=ENTRY;document.querySelector('#open').onclick=()=>{window.openClicks++;document.querySelector('#target').innerHTML=PANEL}}}</script>'''
        script = script.replace('ENTRY', json.dumps(entry, ensure_ascii=False)).replace('PANEL', json.dumps(panel, ensure_ascii=False))
        self.reload('<div style="height:1800px">商品主体</div><main id="target"></main>' + script)
        @contextmanager
        def browser(settings):
            yield self.context, self.page
        with patch.object(session, 'base_browser_session', browser):
            message = open_detail_input(self.settings, ITEM_ID, wait=False, questions=True)
        self.assertIn('已采集1个问题、1条回答', message)
        self.assertEqual(self.page.evaluate('openClicks'), 1)
        self.assertNotIn('reviews', self.document())


class QuestionPacerTests(unittest.TestCase):
    def test_rest_threshold_and_manual_pause_exclusion(self):
        guard = MagicMock(paused_seconds=0)
        with patch('ecommerce.modules.collection.detail_questions.time.monotonic', return_value=0) as clock:
            pacer = QuestionPacer(guard)
            clock.return_value = 500
            guard.paused_seconds = 490
            pacer.before_action()
            guard.pause_between.assert_not_called()
            clock.return_value = 791
            pacer.before_action()
            guard.pause_between.assert_called_once_with('question_rest')
            self.assertEqual(pacer.rests, 1)
            pacer.completed = 10
            pacer.before_action(next_question=True)
            self.assertEqual(pacer.rests, 2)
            self.assertEqual(guard.pause_between.call_args.args, ('question',))
