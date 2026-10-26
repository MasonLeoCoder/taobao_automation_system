"""商品评价合并页的隔离浏览器验证，不访问淘宝或真实媒体来源。"""
import hashlib
import os
import unittest

from playwright.sync_api import expect
from ecommerce.infrastructure.tasks import web_jobs as jobs
from tests.integration import test_web_collection_browser as fixtures
from tests.integration.test_image_preview_browser import png
from tests.unit.test_product_reviews import add_reviews


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用商品评价浏览器检查')
class ProductReviewsBrowserTests(unittest.TestCase):
    def setUp(self):
        """建立参数、评论及合成媒体；参数：无；返回：无。"""
        self.host=fixtures.WebCollectionBrowserTests();self.host.setUp();self.addCleanup(self.host.doCleanups)
        self.fixture=self.host.fixture;self.page=self.host.page;self.context=self.host.context
        add_reviews(self.fixture)
        self.long='\n'.join('评论第'+str(i)+'行：保留完整文字及换行。' for i in range(18))
        table=self.fixture.store.tables['review_observations']
        with self.fixture.engine.begin() as c:c.execute(table.update().where(table.c.id==123).values(content=self.long))
        data=png(160,100);path=self.fixture.settings.workspace/'output/preview/review.png'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        self.fixture.put('file_assets',id=90,root_key='workspace',relative_path='output/preview/review.png',availability='present',
            size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        for identifier,role,url,payload,file_id in [
            (90,'review_image',None,{},90),
            (91,'append_image','https://img.alicdn.com/append.jpg',{'append_index':1},None),
            (92,'review_video','https://cloud.video.taobao.com/demo.mp4',{},None),
            (93,'review_image','https://img.alicdn.com/broken.jpg',{},None)]:
            self.fixture.put('media_references',id=identifier,batch_id=31,review_observation_id=123,role=role,
                position=identifier,source_url=url,payload=payload,file_asset_id=file_id)
        self.context.route('https://img.alicdn.com/**',lambda route:route.abort() if 'broken' in route.request.url else
            route.fulfill(status=200,content_type='image/png',body=data))

    def goto(self,path='/collections/products/123?module=reviews'):
        """打开隔离合并页面；参数：路径；返回：无。"""
        self.page.goto('http://127.0.0.1'+path)
        expect(self.page.locator('#review-overview')).to_be_visible()

    def test_all_tags_wrap_and_comments_keep_sort_and_pagination(self):
        """全部标签自动换行，无分页或筛选操作；评论仍按20条翻页排序。"""
        self.goto('/collections/products/123?module=review_summary')
        expect(self.page.locator('.module-tabs a.selected')).to_have_text('商品评价')
        overview=self.page.locator('#review-overview');comments=self.page.locator('#review-comments')
        expect(overview).to_contain_text('25个评价标签')
        expect(comments).to_contain_text('25条评价')
        expect(comments).to_contain_text('采集范围未记录')
        expect(overview).to_contain_text('25个评价标签')
        expect(comments).to_contain_text('25条评价')
        expect(comments).to_contain_text('采集范围未记录')
        expect(overview.locator('.review-overview-table')).to_contain_text('99.6%')
        expect(overview.locator('.review-overview-table')).to_contain_text('近3个月')
        tags=overview.locator('.review-tag')
        expect(tags).to_have_count(25)
        expect(overview.get_by_role('navigation',name='分页')).to_have_count(0)
        expect(overview.locator('.review-tags a,.review-tags button')).to_have_count(0)
        self.assertGreater(tags.last.bounding_box()['y'],tags.first.bounding_box()['y'])
        expect(comments.locator('tbody tr')).to_have_count(20)
        expect(comments.locator('tbody tr').first).to_have_attribute('data-review-id','123')
        comments.get_by_role('link',name='下一页',exact=True).click()
        expect(comments.locator('tbody tr')).to_have_count(5)
        expect(comments).to_contain_text('25条评价')
        expect(comments).to_contain_text('25条评价')
        expect(tags).to_have_count(25)
        expect(comments.locator('.page-actions').get_by_text('下一页',exact=True)).to_have_attribute('aria-disabled','true')
        comments.get_by_role('link',name='评价时间',exact=False).click()
        expect(comments.locator('tbody tr')).to_have_count(20)
        expect(comments.locator('tbody tr').first).to_have_attribute('data-review-id','100')
        expect(tags).to_have_count(25)
        self.page.set_viewport_size({'width':900,'height':1000})
        expect(tags).to_have_count(25)
        right=overview.locator('.review-tags').bounding_box()
        self.assertTrue(all(tag['x']+tag['width']<=right['x']+right['width']+1 for tag in tags.evaluate_all('(nodes)=>nodes.map(n=>{const r=n.getBoundingClientRect();return {x:r.x,width:r.width}})')))
        self.assertEqual(self.host.errors,[])

    def test_full_text_expand_inline_media_and_page_image_preview(self):
        """长评论可展开，初评与追评图片各自显示，放大后回到原页面。"""
        self.goto();row=self.page.locator('[data-review-id="123"]');body=row.locator('.review-body').first
        text=body.locator('.review-text');toggle=body.locator('.review-toggle')
        expect(toggle).to_be_visible();collapsed=text.bounding_box()['height']
        toggle.click();self.assertEqual(text.text_content(),self.long)
        self.assertGreater(text.bounding_box()['height'],collapsed*3)
        toggle.click();expect(toggle).to_have_text('展开全文')
        expect(row.locator('.review-append img')).to_be_visible()
        expect(row.locator('video')).to_have_attribute('src','https://cloud.video.taobao.com/demo.mp4')
        image=row.locator('img').first;image.scroll_into_view_if_needed()
        self.page.wait_for_function("document.querySelector('[data-review-id=\"123\"] img').naturalWidth>0")
        location=self.page.url;image.click()
        expect(self.page.locator('#image-preview')).to_be_visible()
        expect(self.page.locator('#image-preview-photo')).to_be_visible()
        self.page.locator('#image-preview-photo').click()
        expect(self.page.locator('#image-preview')).not_to_be_visible()
        self.assertEqual(self.page.url,location);self.assertEqual(len(self.context.pages),1)
        broken=row.locator('img[src="https://img.alicdn.com/broken.jpg"]').locator('..').locator('..')
        expect(broken.locator('.review-media-error')).to_be_visible()
        self.assertEqual(self.host.errors,[])

    def test_history_selection_preserves_other_part_and_page(self):
        """切换概览历史不改变明细批次和页码，旧批次入口仍可查看两部分。"""
        self.fixture.batch(32,'review_summary',product_id=1)
        self.fixture.put('review_summaries',batch_id=32,count_raw='历史总数',positive_rate_percent=100,tags=[])
        self.goto('/collections/products/123?batch=31&page=2&tag_page=2')
        overview=self.page.locator('#review-overview');comments=self.page.locator('#review-comments')
        overview.locator('select').select_option('32');overview.get_by_role('button',name='查看',exact=True).click()
        expect(overview.locator('.review-overview-table')).to_contain_text('历史总数')
        expect(overview.locator('.review-overview-table')).to_contain_text('100%')
        expect(comments.locator('tbody tr')).to_have_count(5)
        expect(comments).to_contain_text('批次 31')
        self.assertEqual(self.host.errors,[])

    def test_collect_missing_part_uses_original_single_module_and_manual_dialog(self):
        """空明细按钮只派发当前商品的明细模块，人工提示继续沿用原入口。"""
        table=self.fixture.store.tables['product_current_sections']
        with self.fixture.engine.begin() as c:c.execute(table.delete().where(table.c.module=='reviews'))
        self.goto();comments=self.page.locator('#review-comments')
        comments.get_by_role('button',name='采集明细',exact=True).click()
        expect(self.page.locator('#collection-task-bar')).to_contain_text('启动中')
        row=jobs.rows(self.host.settings)[0]
        self.assertEqual((row['product_id'],row['module']),('123','reviews'))
        row['state']='running';jobs.save(self.host.settings,row);self.host.publish(row)
        expect(self.page.locator('#collection-attention')).to_be_visible(timeout=7000)
        expect(self.page.locator('#collection-attention')).to_contain_text('等待人工登录淘宝')
        self.assertEqual(self.host.errors,[])


if __name__=='__main__':unittest.main()
