"""本地HTML验证SKU按名称匹配、数值格式及失败保护，不连接店铺。"""
import os
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from ecommerce.platforms.qianniu.product_sku import fill_sku_details
from ecommerce.platforms.qianniu.product_basic import fill_fixed_price, fill_merchant_code, enable_multi_item_discount
from ecommerce.platforms.qianniu.verification import VerificationGuard
from ecommerce.shared.exceptions import NeedsAttention


HTML = """
<div id="sell-field-sku"><table><tbody></tbody></table></div>
<script>
window.textChanges=0; window.categoryClicks=0; window.allowSingle=true;
window.seedRows=(data)=>{
  const body=document.querySelector('tbody'); body.innerHTML='';
  data.forEach((record,index)=>{
    const row=document.createElement('tr'); row.className='sku-table-row';
    const nameCell=document.createElement('td'); nameCell.className='sku-unique-title-textarea';
    const name=document.createElement('textarea'); name.dataset.real='true'; name.value=record.name;
    nameCell.append(name); row.append(nameCell);
    for(const [key,suffix] of [['price','skuPrice'],['stock','skuStock'],['code','skuOuterId']]){
      const cell=document.createElement('td'); cell.id=index+'-'+suffix;
      const field=document.createElement('input'); field.maxLength=key==='code'?64:15;
      field.value=record[key]??(key==='stock'?'0':'');
      field.oninput=()=>{textChanges++};
      field.onblur=()=>{
        if(key==='price'&&field.value) field.value=Number(field.value).toFixed(2);
        if(key==='stock'&&field.value) field.value=String(Number(field.value));
        if(key==='code') alert.hidden=field.value!=='拒绝';
      };
      cell.append(field); row.append(cell);
    }
    const category=document.createElement('td'); category.id=index+'-skuQuality';
    const values=document.createElement('span'); values.className='next-select-values';
    const combo=document.createElement('input'); combo.setAttribute('role','combobox');
    combo.readOnly=true; combo.setAttribute('aria-expanded','false');
    const select=(value)=>{
      values.innerHTML=''; const label=document.createElement('em'); label.title=value;
      label.textContent=value; values.append(label); combo.setAttribute('aria-expanded','false');
    };
    if(record.category) select(record.category);
    combo.onclick=()=>{
      categoryClicks++; combo.setAttribute('aria-expanded','true');
      const popup=document.createElement('div'); popup.className='sell-o-select-popup-overlay';
      const option=document.createElement('div'); option.title=allowSingle?'单品':'套餐';
      option.textContent=option.title; option.onclick=()=>{select(option.title);popup.remove()};
      popup.append(option); document.body.append(popup);
    };
    category.append(values,combo); row.append(category);
    const alert=document.createElement('td'); alert.setAttribute('role','alert');
    alert.textContent='商家编码不符合要求'; alert.hidden=true; row.append(alert); body.append(row);
  });
};
</script>
"""

DETAILS = [
    {"销售规格": "规格一", "价格": "11.25", "数量": "10", "商家编码": "0001"},
    {"销售规格": "规格二", "价格": "22", "数量": "20", "商家编码": "T002"},
]


@unittest.skipUnless(os.environ.get("QIANNIU_BROWSER_TESTS") == "1", "显式启用本地浏览器测试")
class SkuDetailsBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_content(HTML)

    def seed(self, records):
        self.page.evaluate("records => seedRows(records)", records)

    def test_reversed_page_order_matches_name_and_repeat_preserves_codes(self):
        self.seed([{"name": "规格二"}, {"name": "规格一"}])
        with VerificationGuard(self.context):
            fill_sku_details(self.page, DETAILS)
            changes = self.page.evaluate("textChanges")
            fill_sku_details(self.page, DETAILS)
        result = self.page.locator('tr').evaluate_all("rows => rows.map(row => ({name:row.querySelector('textarea').value,price:row.querySelector('[id$=skuPrice] input').value,stock:row.querySelector('[id$=skuStock] input').value,code:row.querySelector('[id$=skuOuterId] input').value,category:row.querySelector('em').textContent}))")
        self.assertEqual(result, [
            {"name": "规格二", "price": "22.00", "stock": "20", "code": "T002", "category": "单品"},
            {"name": "规格一", "price": "11.25", "stock": "10", "code": "0001", "category": "单品"}])
        self.assertEqual(self.page.evaluate("textChanges"), changes)
        self.assertEqual(self.page.evaluate("categoryClicks"), 2)

    def test_missing_or_duplicate_page_names_do_not_write_any_fields(self):
        for names in (["规格一"], ["规格一", "规格一"], ["规格一", "未知规格"]):
            with self.subTest(names=names):
                self.seed([{"name": name} for name in names])
                with VerificationGuard(self.context), self.assertRaises(NeedsAttention):
                    fill_sku_details(self.page, DETAILS)
                self.assertEqual(self.page.evaluate("textChanges"), 0)

    def test_later_field_length_error_is_found_before_first_row_write(self):
        self.seed([{"name": "规格一"}, {"name": "规格二"}])
        details = [DETAILS[0], {**DETAILS[1], "商家编码": "x" * 65}]
        with VerificationGuard(self.context), self.assertRaisesRegex(ValueError, "64字符"):
            fill_sku_details(self.page, details)
        self.assertEqual(self.page.evaluate("textChanges"), 0)

    def test_missing_single_category_stops_without_fallback(self):
        self.seed([{"name": "规格一"}, {"name": "规格二"}])
        self.page.evaluate("allowSingle=false")
        with VerificationGuard(self.context), self.assertRaisesRegex(NeedsAttention, "单品"):
            fill_sku_details(self.page, DETAILS)
        self.assertEqual(self.page.locator('em').count(), 0)

    def test_platform_error_is_not_reported_as_completed(self):
        self.seed([{"name": "规格一"}, {"name": "规格二"}])
        details = [{**DETAILS[0], "商家编码": "拒绝"}, DETAILS[1]]
        with self.assertLogs('ecommerce.platforms.qianniu.product_sku', level="INFO") as logs:
            with VerificationGuard(self.context), self.assertRaisesRegex(NeedsAttention, "不符合要求"):
                fill_sku_details(self.page, details)
        self.assertFalse(any("全部SKU明细填写完成" in message for message in logs.output))

    def test_fixed_price_replaces_existing_value_and_accepts_decimal_formatting(self):
        self.page.set_content("""<div id="sell-field-price"><input maxlength="15" value="999.99"
            onblur="this.value=Number(this.value).toFixed(2)"></div>""")
        with VerificationGuard(self.context):
            fill_fixed_price(self.page, "11.5")
        self.assertEqual(self.page.get_by_role("textbox").input_value(), "11.50")

    def test_fixed_price_platform_error_does_not_log_success(self):
        self.page.set_content("""<div id="sell-field-price"><input maxlength="15" value="99">
            <div class="sell-component-info-wrapper-msg-content error">低于类目最低价</div></div>""")
        with patch("ecommerce.platforms.qianniu.product_basic.getLogger") as logger:
            with VerificationGuard(self.context), self.assertRaisesRegex(NeedsAttention, "最低价"):
                fill_fixed_price(self.page, "0.01")
            logger.return_value.info.assert_not_called()

    def test_product_merchant_code_is_scoped_separately_from_sku_codes(self):
        self.seed([{"name": "规格一", "code": "T001"}, {"name": "规格二", "code": "T002"}])
        self.page.evaluate("""() => {
            const group=document.createElement('div'); group.id='sell-field-outerId';
            group.innerHTML='<input maxlength="64" value="OLD"><span class="next-input-len">3/64</span><div class="sell-o-message"></div>';
            const field=group.querySelector('input');
            field.oninput=()=>{group.querySelector('.next-input-len').textContent=field.value.length+'/64'};
            document.body.append(group);
        }""")
        with VerificationGuard(self.context):
            fill_merchant_code(self.page, "0001")
        self.assertEqual(self.page.locator('#sell-field-outerId input').input_value(), "0001")
        self.assertEqual(self.page.locator('#sell-field-sku td[id$="-skuOuterId"] input').evaluate_all("els=>els.map(e=>e.value)"), ["T001", "T002"])

    def test_product_merchant_code_platform_error_does_not_log_success(self):
        self.page.set_content("""<div id="sell-field-outerId"><input maxlength="64" value="0001">
            <span class="next-input-len">4/64</span><div class="sell-o-message">编码不符合要求</div></div>""")
        with patch("ecommerce.platforms.qianniu.product_basic.getLogger") as logger:
            with VerificationGuard(self.context), self.assertRaisesRegex(NeedsAttention, "编码不符合要求"):
                fill_merchant_code(self.page, "0001")
            logger.return_value.info.assert_not_called()

    def test_multi_discount_icon_name_and_repeat_do_not_toggle_other_checkbox(self):
        self.page.set_content("""
            <label><input id="other" type="checkbox">启用</label>
            <div id="sell-field-multiDiscountPromotion">
              <label> <input id="discount" type="checkbox">启用</label>
              <input role="combobox" aria-valuetext="满2件打折" readonly disabled>
              <input value="9.5" disabled>
              <div class="sell-component-info-wrapper-msg-content error">必填项不能为空</div>
            </div>
            <script>
              window.discountChanges=0;
              document.querySelector('#discount').onchange=()=>{
                discountChanges++;
                document.querySelectorAll('#sell-field-multiDiscountPromotion input:not([type=checkbox])').forEach(e=>e.disabled=false);
                document.querySelector('.error').hidden=true;
              };
            </script>""")
        with VerificationGuard(self.context):
            enable_multi_item_discount(self.page)
            enable_multi_item_discount(self.page)
        self.assertTrue(self.page.locator('#discount').is_checked())
        self.assertFalse(self.page.locator('#other').is_checked())
        self.assertEqual(self.page.evaluate('discountChanges'), 1)
        self.assertEqual(self.page.get_by_role('textbox').input_value(), '9.5')
