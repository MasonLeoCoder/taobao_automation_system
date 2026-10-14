"""本地HTML规格创建回归，不连接店铺；显式启用QIANNIU_BROWSER_TESTS运行。"""
import os
import unittest
from playwright.sync_api import sync_playwright
from ecommerce.platforms.qianniu.product_sku import create_sales_specifications
from ecommerce.platforms.qianniu.verification import VerificationGuard
from ecommerce.shared.exceptions import NeedsAttention


HTML = """
<div id="sell-field-sku"><button onclick="openDrawer()">+ 创建规格</button></div>
<div class="sku-decouple-drawer" role="dialog" style="display:none">
  <label><input type="radio" name="mode" onclick="customMode()">自定义填写规格</label>
  <label><input type="radio" name="mode" checked>选择标准属性构建规格</label>
  <div class="sale-props-value"><span class="props-label">颜色分类(0)</span>
    <div id="fields"><input placeholder="主色(必选)" oninput="update()"></div>
    <button class="add" disabled onclick="addField(); addClicks++">+</button>
    <div class="sell-o-message errorMessage-test" hidden>规格名称不符合要求</div>
  </div>
  <button onclick="confirmCreation()">确认创建</button>
</div>
<script>
window.addClicks=0; window.creations=0;
function openDrawer(){document.querySelector('.sku-decouple-drawer').style.display='block'}
function customMode(){document.querySelector('#fields input').placeholder='输入规格'; update()}
function update(){
  const fields=[...document.querySelectorAll('#fields input')];
  document.querySelector('.props-label').textContent='商品规格('+fields.filter(e=>e.value).length+')';
  document.querySelector('button.add').disabled=fields.some(e=>!e.value);
  document.querySelector('.errorMessage-test').hidden=!fields.some(e=>e.value==='平台拒绝');
}
function addField(){
  const field=document.createElement('input'); field.placeholder='输入规格';
  field.oninput=update; document.querySelector('#fields').append(field); update();
}
function confirmCreation(){
  const names=[...document.querySelectorAll('#fields input')].map(e=>e.value);
  document.querySelector('#sell-field-sku').innerHTML='<table><tbody></tbody></table>';
  const body=document.querySelector('tbody');
  for(const name of names){
    const row=document.createElement('tr'); row.className='sku-table-row';
    const cell=document.createElement('td'); cell.className='sku-unique-title-textarea';
    const field=document.createElement('textarea'); field.dataset.real='true'; field.value=name;
    const fake=document.createElement('textarea'); fake.dataset.fake='true'; fake.value=name; fake.hidden=true;
    cell.append(field,fake); row.append(cell); body.append(row);
  }
  document.querySelector('.sku-decouple-drawer').style.display='none'; creations++;
}
</script>
"""


@unittest.skipUnless(os.environ.get("QIANNIU_BROWSER_TESTS") == "1", "显式启用本地浏览器测试")
class SalesSpecificationsBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def test_variable_count_order_and_repeat_do_not_add_extra_rows(self):
        for names in (["单规格"], ["规格1", "规格10", "规格2"]):
            with self.subTest(names=names):
                self.page.set_content(HTML)
                with VerificationGuard(self.context):
                    self.assertEqual(create_sales_specifications(self.page, names), names)
                    self.assertEqual(create_sales_specifications(self.page, names), names)
                self.assertEqual(self.page.evaluate("addClicks"), len(names) - 1)
                self.assertEqual(self.page.evaluate("creations"), 1)

    def test_partial_drawer_reuses_existing_matching_row(self):
        self.page.set_content(HTML)
        self.page.get_by_role("button", name="创建规格", exact=False).click()
        self.page.get_by_role("radio", name="自定义填写规格", exact=True).check()
        self.page.get_by_placeholder("输入规格").fill("规格1")
        names = ["规格1", "规格2", "规格3"]
        with VerificationGuard(self.context):
            self.assertEqual(create_sales_specifications(self.page, names), names)
        self.assertEqual(self.page.evaluate("addClicks"), 2)

    def test_different_created_specifications_are_not_overwritten(self):
        self.page.set_content(HTML)
        with VerificationGuard(self.context):
            create_sales_specifications(self.page, ["原规格"])
            with self.assertRaisesRegex(NeedsAttention, "不一致"):
                create_sales_specifications(self.page, ["新规格"])
        self.assertEqual(self.page.locator("textarea[data-real='true']").input_value(), "原规格")
        self.assertEqual(self.page.evaluate("creations"), 1)

    def test_platform_rejection_does_not_confirm_creation(self):
        self.page.set_content(HTML)
        with VerificationGuard(self.context):
            with self.assertRaisesRegex(NeedsAttention, "不符合要求"):
                create_sales_specifications(self.page, ["平台拒绝"])
        self.assertEqual(self.page.evaluate("creations"), 0)
