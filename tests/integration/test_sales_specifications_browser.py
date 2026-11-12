"""本地HTML规格创建回归，不连接店铺；显式启用QIANNIU_BROWSER_TESTS运行。"""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from ecommerce.platforms.qianniu.product_sku import create_layered_specifications, create_sales_specifications, layered_sku_rows
from ecommerce.platforms.qianniu.popups import PopupGuard
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

LAYERED_HTML = """
<div id="sell-field-sku"><button onclick="drawer.hidden=false">创建规格</button></div>
<div id="drawer" class="sku-decouple-drawer next-drawer" role="dialog" hidden>
  <label><input type="radio" name="mode">单层展示·自定义填写规格</label>
  <label><input type="radio" name="mode" checked>分层展示·选择标准属性构建规格</label>
  <div>属性 2/2 <span>颜色分类</span><span>适用尺码</span></div>
  <section id="color"><header><span>颜色分类 (0)</span><label><input type="checkbox">添加图片</label></header>
    <div><input placeholder="主色(必选)"><input placeholder="备注(可选)"></div>
    <button onclick="add(color)">＋</button>
  </section>
  <section id="size"><header><span>适用尺码（0）</span><label><input type="checkbox">添加图片</label></header>
    <div><input placeholder="请输入"></div>
    <button onclick="add(size)">+</button>
  </section>
  <button onclick="create()">确认创建</button>
</div>
<script>
window.creations = 0;
window.additions = 0;
function add(section) {
    const list = section.querySelector(':scope > div');
    const field = document.createElement('input');
    field.placeholder = list.querySelector('input').placeholder;
    list.append(field);
    additions++;
}
function create() {
    const values = section => Array.from(section.querySelectorAll(':scope > div input'))
        .filter(input => !input.placeholder.includes('备注')).map(input => input.value);
    const rows = values(color).flatMap(color => values(size).map(size => [color, size]));
    const table = document.createElement('table');
    table.innerHTML = '<thead><tr><th>颜色分类</th><th>适用尺码</th></tr></thead><tbody></tbody>';
    for (const pair of rows) {
        const row = table.tBodies[0].insertRow();
        row.className = 'sku-table-row';
        for (const value of pair) row.insertCell().textContent = value;
    }
    document.querySelector('#sell-field-sku').replaceChildren(table);
    drawer.hidden = true;
    creations++;
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

    def test_layered_screen_fills_fourteen_combinations_and_recovers_existing_conflict(self):
        self.page.set_content(LAYERED_HTML)
        self.page.get_by_role("button", name="创建规格").click()
        self.page.get_by_placeholder("主色(必选)").fill("已有不符颜色")
        combinations = {f"SKU-{color}-{size}": (f"颜色 {color}", size)
                        for color in range(1, 8) for size in ("标准尺码", "大件尺码【适合15斤的猫】")}
        messages = []

        def review(message):
            messages.append(message)
            self.assertIn("颜色分类", message)
            self.assertIn("已有不符颜色", message)
            self.assertIn("颜色 1", message)
            self.page.get_by_placeholder("主色(必选)").fill("颜色 1")

        with TemporaryDirectory() as folder:
            popups = PopupGuard(self.context, self.fail, Path(folder))
            with VerificationGuard(self.context, self.fail, popup_guard=popups), patch('ecommerce.platforms.qianniu.product_sku.WAIT_MS', 3500):
                create_layered_specifications(self.page, combinations, on_manual=review)
                create_layered_specifications(self.page, combinations, on_manual=self.fail)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.page.evaluate("[creations, additions]"), [1, 7])
        self.assertEqual(self.page.locator(".sku-table-row").count(), 14)

    def test_layered_extra_combination_prompts_then_checks_corrected_table(self):
        self.page.set_content(LAYERED_HTML)
        combinations = {"黑 M": ("黑色", "M 码"), "灰 L": ("灰色", "L 码")}
        messages = []

        def review(message):
            messages.append(message)
            self.assertIn("黑色 × L 码", message)
            self.assertIn("灰色 × M 码", message)
            self.page.locator(".sku-table-row").nth(2).evaluate("row => row.remove()")
            self.page.locator(".sku-table-row").nth(1).evaluate("row => row.remove()")

        with VerificationGuard(self.context), patch('ecommerce.platforms.qianniu.product_sku.WAIT_MS', 3500):
            create_layered_specifications(self.page, combinations, on_manual=review)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.page.evaluate("creations"), 1)

    def test_layered_rows_use_color_and_size_instead_of_row_order(self):
        self.page.set_content("""
          <div id="sell-field-sku"><table>
            <thead><tr><th>颜色分类</th><th>适用尺码</th></tr></thead>
            <tbody><tr class="sku-table-row" id="large"><td rowspan="2">黑色</td><td>L 码</td></tr>
            <tr class="sku-table-row" id="medium"><td>M 码</td></tr></tbody>
          </table></div>
        """)
        combinations = {"规格一": ("黑色", "M 码"), "规格二": ("黑色", "L 码")}
        with VerificationGuard(self.context):
            create_layered_specifications(self.page, combinations, on_manual=self.fail)
        group = self.page.locator("#sell-field-sku")
        rows = layered_sku_rows(group, combinations)
        self.assertEqual(rows["规格一"].get_attribute("id"), "medium")
        self.assertEqual(rows["规格二"].get_attribute("id"), "large")
        with self.assertRaisesRegex(NeedsAttention, "多出：黑色 × L 码；缺少：灰色 × L 码"):
            layered_sku_rows(group, {**combinations, "规格二": ("灰色", "L 码")})

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
