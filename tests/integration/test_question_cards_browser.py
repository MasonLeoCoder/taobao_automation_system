"""问大家的卡片、全部回答展开和媒体预览隔离验证。"""
import hashlib
import os
import unittest

from playwright.sync_api import expect
from ecommerce.infrastructure.tasks import web_jobs as jobs
from tests.integration import test_web_collection_browser as fixtures
from tests.integration.test_image_preview_browser import png
from tests.unit.test_question_cards import add_questions


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用问大家卡片检查')
class QuestionCardsBrowserTests(unittest.TestCase):
    def setUp(self):
        """准备超过200条回答及合成媒体；参数：无；返回：无。"""
        self.host=fixtures.WebCollectionBrowserTests();self.host.setUp();self.addCleanup(self.host.doCleanups)
        self.fixture=self.host.fixture;self.page=self.host.page;self.context=self.host.context
        add_questions(self.fixture)
        data=png(160,100);path=self.fixture.settings.workspace/'output/preview/answer.png'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        self.fixture.put('file_assets',id=90,root_key='workspace',relative_path='output/preview/answer.png',availability='present',
            size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        for identifier,owner,file_id,url in [(90,1000,90,None),(91,1224,None,'https://img.alicdn.com/answer.jpg')]:
            self.fixture.put('media_references',id=identifier,batch_id=30,answer_observation_id=owner,role='answer_image',
                position=1,file_asset_id=file_id,source_url=url)
        self.context.route('https://img.alicdn.com/**',lambda route:route.fulfill(status=200,content_type='image/png',body=data))

    def goto(self,path='/collections/products/123?module=questions'):
        """打开隔离问答页；参数：路径；返回：无。"""
        self.page.goto('http://127.0.0.1'+path);expect(self.page.locator('.qa-list')).to_be_visible()

    def test_expand_all_225_answers_and_collapse_without_navigation(self):
        """初始只显示首条，展开全部225条，不分页或跳转，再次点击收起。"""
        self.goto();card=self.page.locator('[data-question-id="300"]');location=self.page.url
        expect(self.page.locator('.qa-card')).to_have_count(20)
        expect(card.locator('.qa-answer')).to_have_count(225)
        expect(card.locator('.qa-answer:visible')).to_have_count(1)
        card.get_by_text('查看全部已采集回答（225）',exact=False).click()
        expect(card.locator('.qa-answer:visible')).to_have_count(225)
        expect(card.locator('.qa-answer').last).to_contain_text('完整回答224')
        expect(card.get_by_role('navigation')).to_have_count(0)
        card.get_by_text('收起回答',exact=False).click()
        expect(card.locator('.qa-answer:visible')).to_have_count(1)
        self.assertEqual(self.page.url,location);self.assertEqual(len(self.context.pages),1)
        self.assertEqual(self.host.errors,[])

    def test_question_pagination_sort_and_empty_answer_hint(self):
        """问题仍按20条分页，回答缺失及未完成提示独立，不展示预览假回答。"""
        self.goto()
        expect(self.page.locator('[data-question-id="301"]')).to_contain_text('暂无已采集回答')
        expect(self.page.locator('[data-question-id="301"]')).to_contain_text('回答采集未完成')
        expect(self.page.locator('[data-question-id="302"] details')).to_have_count(0)
        self.page.get_by_role('link',name='下一页',exact=True).click()
        expect(self.page.locator('.qa-card')).to_have_count(2)
        expect(self.page.locator('.page-actions').get_by_text('下一页',exact=True)).to_have_attribute('aria-disabled','true')
        self.page.get_by_role('link',name='问题内容',exact=False).click()
        self.page.get_by_role('link',name='问题内容',exact=False).click()
        expect(self.page.locator('.qa-card')).to_have_count(20)
        expect(self.page.locator('.qa-card').first).to_have_attribute('data-question-id','321')
        self.assertEqual(self.host.errors,[])

    def test_answer_images_open_here_and_batch_page_uses_same_cards(self):
        """问答独立批次页沿用卡片，图片仍可本页放大，隐藏回答图片能正常展开加载。"""
        self.goto('/collections/batches/30');card=self.page.locator('[data-question-id="300"]')
        image=card.locator('.qa-answer').first.locator('img');image.click()
        expect(self.page.locator('#image-preview-photo')).to_be_visible()
        self.page.locator('#image-preview-photo').click();expect(self.page.locator('#image-preview')).not_to_be_visible()
        card.locator('summary').focus();card.locator('summary').press('Enter')
        last_image=card.locator('.qa-answer').last.locator('img');last_image.scroll_into_view_if_needed()
        self.page.wait_for_function("() => document.querySelector('[data-answer-id=\"1224\"] img').naturalWidth>0")
        last_image.click();expect(self.page.locator('#image-preview-photo')).to_be_visible()
        self.page.keyboard.press('Escape');expect(self.page.locator('#image-preview')).not_to_be_visible()
        self.assertEqual(len(self.context.pages),1);self.assertEqual(self.host.errors,[])

    def test_missing_module_uses_current_product_collection_and_manual_prompt(self):
        """未采集问答的按钮仍只派发当前商品问答模块，保留人工提示。"""
        table=self.fixture.store.tables['product_current_sections']
        with self.fixture.engine.begin() as c:c.execute(table.delete().where(table.c.module=='questions'))
        self.page.goto('http://127.0.0.1/collections/products/123?module=questions')
        self.page.get_by_role('button',name='采集问大家',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('启动中')
        row=jobs.rows(self.host.settings)[0];self.assertEqual((row['product_id'],row['module']),('123','questions'))
        row['state']='running';jobs.save(self.host.settings,row);self.host.publish(row)
        expect(self.page.locator('#collection-attention')).to_be_visible(timeout=7000)
        self.assertEqual(self.host.errors,[])


if __name__=='__main__':unittest.main()
