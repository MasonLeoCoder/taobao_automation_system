"""独立本地HTML商品提交结果回归，不访问店铺。"""
import os
import tempfile
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
from ecommerce.modules.product import service
from ecommerce.platforms.qianniu.product_submit import submit_product_to_warehouse, inspect_submission_result
from ecommerce.platforms.qianniu.verification import VerificationGuard
from ecommerce.shared.config import load_settings


HTML = """
<div id="sell-field-title">商品编辑区</div>
<div id="sell-field-startTime"><label><input type="radio" checked>放入仓库</label></div>
<button onclick="submitCount++; action()">提交宝贝信息</button>
<script>window.submitCount=0;window.action=()=>{};</script>
"""
DETAIL_INSTRUCTION = "宝贝详情图【高/宽≤ 2】，超出将被裁剪。建议宽度≥1440像素以确保清晰。"


@unittest.skipUnless(os.environ.get("QIANNIU_BROWSER_TESTS") == "1", "显式启用本地浏览器测试")
class ProductSubmitBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_content(HTML)

    def test_field_error_is_failure_and_only_one_click(self):
        self.page.evaluate("() => { action=()=>document.body.insertAdjacentHTML('beforeend','<div class=\"sell-component-info-wrapper-wrap\"><span class=\"sell-component-info-wrapper-label\" title=\"宝贝标题\"></span><div class=\"sell-component-info-wrapper-msg-content error\">必填项不能为空</div></div>'); }")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=500)
        self.assertEqual(result.status, "failed")
        self.assertIn("宝贝标题", result.message)
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_real_success_view_is_confirmed(self):
        self.page.evaluate("() => { action=()=>{document.querySelector('#sell-field-title').remove();document.body.insertAdjacentHTML('beforeend','<h1>宝贝发布成功，已放入仓库</h1>')}; }")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=500)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_success_keeps_item_id_and_supplemental_failures(self):
        self.page.evaluate("""() => { action=()=>{
            document.querySelector('#sell-field-title').remove();
            document.body.insertAdjacentHTML('beforeend',
                '<h1>商品提交成功</h1><div>商品ID：123456，您可以在商品列表中搜索。</div>' +
                '<div class="bodySuccess"><span>商品多件优惠创建失败，商品不存在</span></div>' +
                '<div>导购标题存在异常（流量受限）</div>');
        }; }""")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=500)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.item_id, "123456")
        self.assertEqual(result.issues, ("商品多件优惠创建失败，商品不存在", "导购标题存在异常（流量受限）"))
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_material_success_without_editor_is_not_product_success(self):
        self.page.evaluate("() => { action=()=>{document.querySelector('#sell-field-title').remove();document.body.insertAdjacentHTML('beforeend','<div>素材发布成功</div>')}; }")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=100)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_material_success_in_editor_is_not_product_success(self):
        self.page.evaluate("() => { action=()=>document.body.insertAdjacentHTML('beforeend','<div>素材发布成功</div>'); }")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=100)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_manual_feedback_rechecks_without_another_click(self):
        baseline = tuple(self.context.pages)
        with VerificationGuard(self.context):
            self.assertEqual(submit_product_to_warehouse(self.page, wait_ms=100).status, "unknown")
            self.page.evaluate("document.querySelector('#sell-field-title').remove();document.body.insertAdjacentHTML('beforeend','<h1>提交成功</h1>')")
            self.assertEqual(inspect_submission_result(self.page, baseline, wait_ms=100).status, "succeeded")
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_wrong_listing_choice_never_clicks_submit(self):
        self.page.locator('input').evaluate("e=>e.checked=false")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=100)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.page.evaluate("submitCount"), 0)

    def test_informational_message_does_not_interrupt_pending_submission(self):
        self.page.evaluate("""() => {
            document.body.insertAdjacentHTML('beforeend','<div class="sell-o-error-hoc"><div class="sell-o-message">详情图建议宽度≥1440像素</div></div>');
            action=()=>setTimeout(()=>{
                document.querySelector('#sell-field-title').remove();
                document.body.insertAdjacentHTML('beforeend','<h1>宝贝发布成功</h1>');
            },150);
        }""")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=1000)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_detail_instruction_in_alert_is_not_failure(self):
        self.page.evaluate("""text => {
            document.body.insertAdjacentHTML('beforeend', '<div role="alert">' + text + '</div>');
            action=()=>setTimeout(()=>{
                document.querySelector('#sell-field-title').remove();
                document.body.insertAdjacentHTML('beforeend','<h1>商品提交成功</h1>');
            },350);
        }""", DETAIL_INSTRUCTION)
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=1500)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_detail_instruction_in_shared_error_class_is_not_failure(self):
        self.page.evaluate("""text => {
            document.body.insertAdjacentHTML('beforeend', '<div class="sell-o-message errorMessage-test">' + text + '</div>');
            action=()=>setTimeout(()=>{
                document.querySelector('#sell-field-title').remove();
                document.body.insertAdjacentHTML('beforeend','<h1>商品提交成功</h1>');
            },350);
        }""", DETAIL_INSTRUCTION)
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=1500)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.page.evaluate("submitCount"), 1)

    def test_real_error_before_submission_blocks_the_click(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div class=\"sell-component-info-wrapper-msg-content error\">宝贝标题不能为空</div>')")
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=500)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.submit_attempted)
        self.assertEqual(self.page.evaluate("submitCount"), 0)

    def test_real_error_is_kept_alongside_detail_instruction(self):
        self.page.evaluate("""text => {
            document.body.insertAdjacentHTML('beforeend', '<div role="alert">' + text + '</div>' +
                '<div role="alert">提交失败，价格超出允许范围</div>');
        }""", DETAIL_INSTRUCTION)
        with VerificationGuard(self.context):
            result = submit_product_to_warehouse(self.page, wait_ms=500)
        self.assertEqual(result.status, "failed")
        self.assertIn("价格超出允许范围", result.message)
        self.assertNotIn("建议宽度", result.message)
        self.assertEqual(self.page.evaluate("submitCount"), 0)

    def test_preflight_review_finishes_before_first_and_only_click(self):
        self.page.evaluate("""() => {
            document.body.insertAdjacentHTML('beforeend','<div class="sell-o-message errorMessage-test">宝贝标题不能为空</div>');
            action=()=>{
                document.querySelector('#sell-field-title').remove();
                document.body.insertAdjacentHTML('beforeend','<h1>商品提交成功</h1>');
            };
        }""")
        reviews = []
        def review(message):
            reviews.append(message)
            self.assertEqual(self.page.evaluate("submitCount"), 0)
            self.page.locator('.errorMessage-test').evaluate('e=>e.remove()')
        with tempfile.TemporaryDirectory() as directory, VerificationGuard(self.context), self.assertLogs(service.__name__, level="ERROR"):
            message = service.submit_with_review(load_settings(root=Path(directory)), self.page, review)
        self.assertEqual(message, "商品提交成功")
        self.assertEqual(len(reviews), 1)
        self.assertIn("尚未点击提交", reviews[0])
        self.assertEqual(self.page.evaluate("submitCount"), 1)
