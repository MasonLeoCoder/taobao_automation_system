"""隔离浏览器验证参数分列、长值展开及完整原文复制；不触碰系统剪贴板。"""
import os
import unittest

from playwright.sync_api import expect
from tests.integration import test_web_collection_browser as fixtures


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用隔离参数展示检查')
class ParameterDisplayBrowserTests(unittest.TestCase):
    def setUp(self):
        """准备脱敏参数与剪贴板替身；参数：无；返回：无。"""
        self.host=fixtures.WebCollectionBrowserTests();self.host.setUp();self.addCleanup(self.host.doCleanups)
        self.page=self.host.page;self.fixture=self.host.fixture
        self.long='\n'.join(f'规格{i:02d}：伸缩杆【加宽19CM】♥<型号 & 配色>，完整原文' for i in range(1,19))+'\n  末尾保留空格  '
        for identifier,name,value in [(22,'颜色分类',self.long),(23,'空值',None),(24,'空白',' \t '),(25,'零值','0'),
                                      (26,'编码','<script>window.executed=true</script>')]:
            self.fixture.put('parameter_observations',id=identifier,batch_id=20,position=identifier-19,name=name,value=value)
        self.host.context.add_init_script("""
            window.__copied=[]; window.__copyMode='success';
            Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{
              if(window.__copyMode!=='success') throw new Error('模拟拒绝'); window.__copied.push(value);
            }}});
            document.execCommand=command=>{
              if(command==='copy' && window.__copyMode==='fallback'){
                window.__copied.push(document.activeElement.value); return true;
              } return false;
            };
        """)

    def goto(self,path='/collections/products/123?module=parameters'):
        """打开隔离页面；参数：站内路径；返回：无。"""
        self.page.goto('http://127.0.0.1'+path)
        expect(self.page.locator('.parameter-table')).to_be_visible()

    def test_long_value_toggle_and_copy_use_full_original_text(self):
        """默认三行，展开收起真实生效；复制包括折叠部分、换行和尾部空格。"""
        self.goto();text=self.page.locator('#parameter-value-22');group=text.locator('..')
        toggle=group.locator('.parameter-toggle')
        expect(toggle).to_be_visible();self.assertEqual(text.text_content(),self.long)
        collapsed=text.bounding_box()['height']
        expect(self.page.get_by_role('button',name='复制参数值',exact=True)).to_have_count(0)
        text.click()
        feedback=self.page.locator('#parameter-copy-feedback')
        expect(feedback).to_have_text('复制成功')
        self.assertEqual(self.page.evaluate('window.__copied.at(-1)'),self.long)
        expect(feedback).not_to_be_visible(timeout=3500)
        toggle.click();expect(toggle).to_have_attribute('aria-expanded','true')
        self.assertGreater(text.bounding_box()['height'],collapsed*3)
        group.get_by_role('button',name='收起',exact=True).click()
        expect(toggle).to_have_attribute('aria-expanded','false')
        self.assertAlmostEqual(text.bounding_box()['height'],collapsed,delta=1)
        self.assertEqual(self.page.evaluate('window.__copied.length'),1)
        self.assertEqual(self.host.errors,[])

    def test_short_empty_zero_duplicate_names_and_literal_html(self):
        """短值保持紧凑，空值不可复制，0支持键盘复制，原文按文本显示。"""
        self.goto('/collections/batches/20')
        headers=self.page.locator('.parameter-table thead th').all_text_contents()
        self.assertEqual(len(headers),3);self.assertIn('参数名称',headers[1]);self.assertIn('参数值',headers[2])
        expect(self.page.locator('.parameter-table tbody tr')).to_have_count(7)
        names=self.page.locator('.parameter-table tbody tr td:nth-child(2)').all_text_contents()
        self.assertEqual(names.count('同名参数'),2)
        expect(self.page.locator('#parameter-value-20').locator('..').locator('.parameter-toggle')).not_to_be_visible()
        expect(self.page.locator('#parameter-value-20').locator('..').locator('.parameter-value-actions')).not_to_be_visible()
        for identifier in [23,24]:
            text=self.page.locator(f'#parameter-value-{identifier}');expect(text).to_have_text('未采到参数值')
            self.assertIsNone(text.get_attribute('data-parameter-copy'))
            self.assertIsNone(text.get_attribute('tabindex'))
            text.click()
        self.assertEqual(self.page.evaluate('window.__copied'),[])
        expect(self.page.locator('#parameter-copy-feedback')).not_to_be_visible()
        zero=self.page.locator('#parameter-value-25');expect(zero).to_have_text('0')
        zero.focus();zero.press('Enter');expect(self.page.locator('#parameter-copy-feedback')).to_have_text('复制成功')
        zero.press('Space')
        self.assertEqual(self.page.evaluate('window.__copied'),['0','0'])
        self.assertEqual(self.page.evaluate('window.__copied.at(-1)'),'0')
        expect(self.page.locator('#parameter-value-26')).to_have_text('<script>window.executed=true</script>')
        self.assertFalse(self.page.evaluate('Boolean(window.executed)'))

    def test_compatibility_copy_and_failed_copy_restore_focus(self):
        """API拒绝时尝试兼容路径；两种方式均失败要提示，不能报告已复制。"""
        self.goto();text=self.page.locator('#parameter-value-22');feedback=self.page.locator('#parameter-copy-feedback')
        self.page.evaluate("window.__copyMode='fallback'");text.click()
        expect(feedback).to_have_text('复制成功')
        self.assertEqual(self.page.evaluate('window.__copied.at(-1)'),self.long)
        self.assertTrue(text.evaluate('e=>e===document.activeElement'))
        self.page.evaluate("window.__copyMode='failed'");text.click()
        expect(feedback).to_contain_text('复制失败')
        self.assertIsNone(text.get_attribute('aria-busy'));expect(self.page.locator('textarea')).to_have_count(0)
        self.assertEqual(self.page.evaluate('window.__copied.length'),1)
        self.assertTrue(text.evaluate('e=>e===document.activeElement'));self.assertEqual(self.host.errors,[])

    def test_sorting_pagination_and_value_copy_after_navigation(self):
        """名称和值继续排序，跨页仍20条，翻页后可复制当前行。"""
        for i in range(23):
            self.fixture.put('parameter_observations',id=100+i,batch_id=20,position=10+i,name=f'名称{i:02d}',value=f'值{i:02d}')
        self.goto();rows=self.page.locator('.parameter-table tbody tr');expect(rows).to_have_count(20)
        self.page.get_by_role('link',name='参数名称').click()
        self.assertIn('sort=content',self.page.url)
        self.page.get_by_role('link',name='参数值',exact=False).first.click()
        self.assertIn('sort=value',self.page.url)
        self.page.get_by_role('link',name='下一页',exact=True).click();expect(rows).to_have_count(10)
        first=rows.first;value=first.locator('.parameter-value-text').text_content()
        first.locator('.parameter-value-text').click()
        expect(self.page.locator('#parameter-copy-feedback')).to_have_text('复制成功')
        self.assertEqual(self.page.evaluate('window.__copied.at(-1)'),value)
        self.assertEqual(self.host.errors,[])

    def test_selecting_text_does_not_copy_entire_value(self):
        """拖选参数原文用于手动复制时，不自动覆盖剪贴板。"""
        self.goto();text=self.page.locator('#parameter-value-22')
        text.locator('..').locator('.parameter-toggle').click()
        text.evaluate("""element=>{
            const range=document.createRange();range.setStart(element.firstChild,0);range.setEnd(element.firstChild,4);
            const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
            element.dispatchEvent(new MouseEvent('click',{bubbles:true}));
        }""")
        self.assertEqual(self.page.evaluate('window.__copied'),[])
        expect(self.page.locator('#parameter-copy-feedback')).not_to_be_visible()


if __name__=='__main__':unittest.main()
