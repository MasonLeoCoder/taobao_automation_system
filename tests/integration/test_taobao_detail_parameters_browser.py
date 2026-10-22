"""参数的动态字段、反向布局、展开、完整性及原 HTML 保存检查。"""
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

from ecommerce.modules.collection.detail_parameters import collect_parameters
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
from ecommerce.platforms.taobao.detail_parameters import PARAMETER_SNAPSHOT
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

ITEM_ID = '1050158363561'


def row(name, value, source='general', display=None):
    prefix = 'emphasisParamsInfoItem' if source == 'emphasis' else 'generalParamsInfoItem'
    name_class = prefix + ('SubTitle' if source == 'emphasis' else 'Title')
    value_class = prefix + ('Title' if source == 'emphasis' else 'SubTitle')
    name_html = f'<div class="{name_class}--fixture" title="{escape(name, quote=True)}">{escape(name)}</div>'
    value_html = '' if value is None else f'<div class="{value_class}--fixture" title="{escape(value, quote=True)}">{escape(value if display is None else display)}</div>'
    cells = value_html + name_html if source == 'emphasis' else name_html + value_html
    return f'<div class="{prefix}--fixture">{cells}</div>'


def module(content):
    return '<div class="tabDetailItem--fixture paramsInfoArea"><p class="tabDetailItemTitle--fixture">参数信息</p><div class="paramsWrap--fixture">' + content + '</div></div>'


DEFAULT_ROWS = row('产地', '中国大陆', 'emphasis') + row('品牌', '无品牌') + row('型号', '20撕')


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用本地参数浏览器检查')
class TaobaoDetailParametersBrowserTests(unittest.TestCase):
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
        self.body = module(DEFAULT_ROWS)
        def route(route):
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body='<!doctype html><meta charset="utf-8"><span class="site-nav-login-info-nick">测试账号</span>' + self.body)
        self.context.route('**/*', route)
        self.page = self.context.new_page()
        self.page.goto('https://item.taobao.com/item.htm?id=' + ITEM_ID)
        self.result = DetailPage(self.page, resolve_detail_input(ITEM_ID), '参数测试商品')

    def guard(self, attention=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def collect(self, attention=None):
        with self.guard(attention):
            return collect_parameters(self.settings, self.result, stable_ms=80)

    def reload(self, body):
        self.body = body
        self.page.reload()

    def document(self):
        path = product_directory(self.settings, ITEM_ID) / (ITEM_ID + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def test_emphasis_reversed_labels_and_general_parameters_are_correct(self):
        result = self.collect()
        self.assertEqual([(i['name'], i['value']) for i in result['items']], [('产地', '中国大陆'), ('品牌', '无品牌'), ('型号', '20撕')])
        self.assertEqual([i['source'] for i in result['items']], ['emphasis', 'general', 'general'])
        self.assertEqual(result['summary']['total_count'], 3)
        self.assertEqual(result['summary']['read_count'], 3)
        self.assertTrue(result['summary']['total_count_confirmed'])
        self.assertEqual(result['status'], 'complete')
        raw = Path(result['html_path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['html_sha256'])
        self.assertEqual(len(raw), result['html_size_bytes'])
        self.assertIn('中国大陆', raw.decode('utf-8'))

    def test_variable_category_fields_units_and_full_title_are_preserved(self):
        content = ''.join(row('类目自定义属性' + str(i), str(i) + ' mm') for i in range(17))
        content += row('尺寸', '10 cm × 20 cm\n±1 cm', display='10 cm…')
        self.reload(module(content))
        result = self.collect()
        self.assertEqual(result['summary']['total_count'], 18)
        self.assertEqual(result['items'][-1]['value'], '10 cm × 20 cm\n±1 cm')
        self.assertEqual(result['items'][-1]['value_source'], 'title_attribute')

    def test_same_parameter_names_and_values_are_not_overwritten_or_deduplicated(self):
        self.reload(module(row('材质', '塑料', 'emphasis') + row('材质', '金属') + row('材质', '金属')))
        result = self.collect()
        self.assertEqual([i['value'] for i in result['items']], ['塑料', '金属', '金属'])
        self.assertEqual([i['index'] for i in result['items']], [1, 2, 3])

    def test_empty_value_is_distinct_from_missing_value_node(self):
        self.reload(module(row('品牌', '') + row('型号', '20撕')))
        result = self.collect()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['items'][0]['status'], 'empty_value')
        self.assertEqual(result['items'][0]['value'], '')
        self.assertEqual(result['summary']['empty_value_count'], 1)
        self.assertEqual(result['summary']['error_count'], 0)

    def test_inner_text_used_when_title_attribute_is_absent(self):
        self.page.locator('[title]').evaluate_all('nodes=>nodes.forEach(n=>n.removeAttribute("title"))')
        result = self.collect()
        self.assertEqual(result['items'][0]['name'], '产地')
        self.assertTrue(all(i['value_source'] == 'inner_text' for i in result['items']))

    def test_only_explicit_empty_message_becomes_no_parameters(self):
        self.reload(module('暂无参数信息'))
        result = self.collect()
        self.assertEqual(result['status'], 'no_parameters')
        self.assertEqual(result['summary']['total_count'], 0)
        self.assertTrue(result['summary']['total_count_confirmed'])
        self.assertIn('暂无参数信息', Path(result['html_path']).read_text(encoding='utf-8'))

    def test_hidden_duplicates_and_other_product_parameters_are_excluded(self):
        self.reload('<div style="display:none">' + module(row('隐藏', '不要采集')) + '</div>' + module(DEFAULT_ROWS) + row('推荐商品', '不要采集'))
        result = self.collect()
        self.assertEqual(result['summary']['total_count'], 3)

    def test_expands_once_and_waits_for_slow_content(self):
        self.settings = replace(self.settings, timeout_ms=1400)
        self.reload(module(row('品牌', '无品牌') + '<div id="rest" style="display:none">' + row('型号', '20撕') + '</div><button id="more"><span>展开全部参数</span></button>') + '''<script>
          window.expandClicks=0;document.querySelector('#more').onclick=()=>{expandClicks++;setTimeout(()=>{
          document.querySelector('#rest').style.display='block';document.querySelector('#more').textContent='收起参数'},700)};
          </script>''')
        result = self.collect()
        self.assertEqual(result['summary']['total_count'], 2)
        self.assertEqual(result['expansion_count'], 1)
        self.assertEqual(self.page.evaluate('expandClicks'), 1)

    def test_failed_expand_is_not_replayed_and_is_not_reported_complete(self):
        self.reload(module(row('品牌', '无品牌') + '<button id="more" onclick="window.expandClicks++">更多参数</button>') + '<script>window.expandClicks=0</script>')
        with self.assertRaises(TaskStopped):
            self.collect(lambda message: '停止')
        self.assertEqual(self.page.evaluate('expandClicks'), 1)
        self.assertNotIn('parameters', self.document())
        self.assertEqual(self.document()['parameters_last_attempt']['state'], 'collapsed')

    def test_unknown_layout_is_archived_and_preserves_previous_complete_parameters(self):
        first = self.collect()
        self.reload(module(DEFAULT_ROWS + '<div class="unknown-row"><b>材质</b><i>塑料</i></div>'))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message:'停止')
        document = self.document()
        self.assertEqual(document['parameters']['batch_id'], first['batch_id'])
        attempt = document['parameters_last_attempt']
        self.assertEqual(attempt['status'], 'partial')
        self.assertGreater(attempt['summary']['unrecognized_fragment_count'], 0)
        self.assertFalse(attempt['summary']['total_count_confirmed'])
        self.assertIn('unknown-row', Path(attempt['html_path']).read_text(encoding='utf-8'))

    def test_missing_value_node_is_an_error_not_an_empty_value(self):
        self.reload(module(row('品牌', None)))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message:'停止')
        attempt = self.document()['parameters_last_attempt']
        self.assertEqual(attempt['items'][0]['status'], 'missing_value_node')
        self.assertIsNone(attempt['items'][0]['value'])
        self.assertEqual(attempt['summary']['empty_value_count'], 0)

    def test_image_only_parameter_is_unrecognized_not_an_empty_value(self):
        self.reload(module(row('材质', '').replace('title=""></div>', 'title=""><svg width="20" height="20"></svg></div>')))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message:'停止')
        self.assertEqual(self.document()['parameters_last_attempt']['items'][0]['status'], 'unrecognized_value')

    def test_unknown_structure_can_be_fixed_manually_and_rechecked(self):
        self.reload(module(DEFAULT_ROWS + '<div id="unknown">待加载的其他参数</div>'))
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('#unknown').evaluate('e=>e.remove()')
            return '继续'
        result = self.collect(attention)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['diagnostic_count'], 1)
        self.assertEqual(result['status'], 'complete')
        archive = Path(result['history_record_path']).parent
        self.assertTrue((archive / 'parameters-attempt-1.html').is_file())
        self.assertTrue((archive / 'parameters.html').is_file())

    def test_empty_unloaded_container_is_not_assumed_to_have_zero_parameters(self):
        self.reload(module(''))
        with self.assertRaises(TaskStopped):
            self.collect(lambda message:'停止')
        attempt = self.document()['parameters_last_attempt']
        self.assertEqual(attempt['state'], 'not_loaded')
        self.assertFalse(attempt['summary']['total_count_confirmed'])
        self.assertNotIn('parameters', self.document())

    def test_lazy_parameter_tab_is_clicked_once_and_module_scrolled_into_view(self):
        parameter_html = json.dumps(module(DEFAULT_ROWS), ensure_ascii=False)
        self.reload('<div style="height:1400px">页面其他内容</div><button class="tabTitle--fixture" id="nav">参数信息</button><main id="target"></main><script>'
                    'window.navClicks=0;document.querySelector("#nav").onclick=()=>{navClicks++;document.querySelector("#target").innerHTML=' + parameter_html + '};</script>')
        result = self.collect()
        self.assertEqual(result['summary']['total_count'], 3)
        self.assertEqual(self.page.evaluate('navClicks'), 1)
        self.assertGreater(self.page.evaluate('window.scrollY'), 0)

    def test_loading_indicator_prevents_premature_success(self):
        self.page.locator('.paramsInfoArea').evaluate('e=>e.setAttribute("aria-busy","true")')
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('.paramsInfoArea').evaluate('e=>e.removeAttribute("aria-busy")')
            return '继续'
        result = self.collect(attention)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['status'], 'complete')

    def test_verification_during_expand_requires_continue_without_duplicate_click(self):
        self.reload(module('<button id="more">更多参数</button><div id="data" style="display:none">' + DEFAULT_ROWS + '</div>') + '''<script>
          window.expandClicks=0;document.querySelector('#more').onclick=()=>{expandClicks++;document.querySelector('#data').style.display='block';
          document.querySelector('#more').textContent='收起参数';const e=document.createElement('div');e.className='J_MIDDLEWARE_FRAME_WIDGET';e.textContent='请完成安全验证';document.body.append(e)};
          </script>''')
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate('e=>e.remove()')
            return '继续'
        result = self.collect(attention)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.page.evaluate('expandClicks'), 1)
        self.assertEqual(result['summary']['total_count'], 3)

    def test_wrong_product_id_waits_for_manual_restore(self):
        self.page.evaluate("history.replaceState(null,'','?id=1007759256325')")
        prompts = []
        def attention(message):
            prompts.append(message)
            self.page.evaluate("history.replaceState(null,'','?id=" + ITEM_ID + "')")
            return '继续'
        result = self.collect(attention)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['product_id'], ITEM_ID)

    def test_repeat_keeps_history_and_other_sections(self):
        folder = product_directory(self.settings, ITEM_ID)
        other = {'video': {'video_file': ITEM_ID+'.mp4'}, 'main_images': {'images': [1, 2]}, 'sku_details': {'skus': [1]}}
        for section, value in other.items():
            save_detail_section(folder, ITEM_ID, section, value)
        first, second = self.collect(), self.collect()
        document = self.document()
        for section, value in other.items():
            self.assertEqual(document[section], value)
        self.assertNotEqual(first['batch_id'], second['batch_id'])
        self.assertTrue(Path(first['html_path']).is_file())

    def test_metadata_failure_does_not_overwrite_previous_record(self):
        first = self.collect()
        path = Path(first['record_path'])
        original = path.read_bytes()
        with patch('ecommerce.modules.collection.detail_parameters.save_detail_section', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(product_directory(self.settings, ITEM_ID).rglob('*.tmp')))

    def test_parameter_rows_outside_primary_wrapper_are_still_read(self):
        self.reload(module(DEFAULT_ROWS)[:-6] + row('材质', '塑料') + '</div>')
        result = self.collect()
        self.assertIn(('材质', '塑料'), [(item['name'], item['value']) for item in result['items']])

    def test_unknown_content_outside_wrapper_is_not_silently_ignored(self):
        self.reload(module(DEFAULT_ROWS)[:-6] + '<div>其他新布局参数</div></div>')
        with self.assertRaises(TaskStopped):
            self.collect(lambda message:'停止')
        self.assertGreater(self.document()['parameters_last_attempt']['summary']['unrecognized_fragment_count'], 0)

    def test_parameter_change_at_save_checkpoint_requires_new_snapshot(self):
        prompts = []
        with self.guard(lambda message: prompts.append(message) or '继续') as guard:
            original = guard.perform
            changed = False
            def perform(step, action, **kwargs):
                nonlocal changed
                if step == '保存参数信息与原始 HTML' and not changed:
                    changed = True
                    self.page.locator('[title="20撕"]').evaluate("e=>{e.setAttribute('title','30撕');e.textContent='30撕'}")
                return original(step, action, **kwargs)
            with patch.object(guard, 'perform', side_effect=perform):
                result = collect_parameters(self.settings, self.result, stable_ms=80)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['items'][-1]['value'], '30撕')
        self.assertIn('30撕', Path(result['html_path']).read_text(encoding='utf-8'))

    def test_detail_service_routes_parameters_without_video_or_sku_steps(self):
        from ecommerce.modules.collection.detail_service import open_detail_input
        from ecommerce.platforms.taobao import session
        @contextmanager
        def browser(settings):
            yield self.context, self.page
        with patch.object(session, 'base_browser_session', browser):
            message = open_detail_input(self.settings, ITEM_ID, wait=False, parameters=True)
        self.assertIn('参数采集完成', message)
        self.assertEqual(self.document()['parameters']['summary']['total_count'], 3)
        self.assertNotIn('sku_details', self.document())


if __name__ == '__main__':
    unittest.main()
