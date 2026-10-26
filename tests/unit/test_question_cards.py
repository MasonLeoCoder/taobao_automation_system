"""问大家卡片的全部回答、来源边界和问题分页验证。"""
import hashlib
import unittest

from ecommerce.modules.collection.queries import query
from tests.unit import test_collection_browser as fixtures


def add_questions(fixture):
    """准备22题及超过200条回答的单题；参数：隔离服务；返回：无。"""
    fixture.batch(30,'questions',product_id=1)
    for i in range(22):
        fixture.put('question_observations',id=300+i,batch_id=30,position=i+1,question=f'问题{i:02d}：被子能用吗？',
            answers_complete=i!=1,payload={'preview_text':'仅预览，不能当作正式回答'})
    for i in range(225):
        fixture.put('answer_observations',id=1000+i,batch_id=30,question_id=300,position=i+1,
            author=f'用户{i:03d}',content=f'完整回答{i:03d}',date_raw='近2周发布',sku_text='24cm替换芯',
            payload={'purchase_labels':['已购']})
    fixture.put('answer_observations',id=1300,batch_id=30,question_id=302,position=1,author=None,content='唯一回答',payload={})
    fixture.put('product_current_sections',product_id=1,section='questions',module='questions',batch_id=30)


class QuestionCardTests(unittest.TestCase):
    def setUp(self):
        """准备隔离数据库和服务；参数：无；返回：无。"""
        self.fixture=fixtures.CollectionBrowserTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        add_questions(self.fixture);self.settings=self.fixture.settings;self.client=self.fixture.client

    def test_question_page_has_all_225_answers_without_truncation(self):
        """问题仍20条分页，单题全部回答不受原100或200条上限影响。"""
        data=query(self.settings,'records',batch_id=30,page=1,with_context=True)
        self.assertEqual((data['total'],len(data['records'])),(22,20))
        self.assertEqual(len(data['records'][0]['answers']),225)
        self.assertEqual(data['records'][0]['answers'][-1]['content'],'完整回答224')
        second=query(self.settings,'records',batch_id=30,page=2,with_context=True)
        self.assertEqual(len(second['records']),2)
        self.assertTrue(all(not row['answers'] for row in second['records']))

    def test_cards_keep_original_text_and_do_not_use_question_preview(self):
        """卡片包含全部已存内容，不把预览当回答，不伪造头像或会员身份。"""
        for url in ['/collections/products/123?module=questions','/collections/batches/30']:
            response=self.client.get(url);self.assertEqual(response.status_code,200)
            body=response.get_data(as_text=True)
            self.assertEqual(body.count('class="qa-card"'),20)
            self.assertIn('查看全部已采集回答（225）',body);self.assertIn('完整回答224',body)
            self.assertIn('暂无已采集回答',body);self.assertIn('回答采集未完成',body)
            self.assertNotIn('仅预览，不能当作正式回答',body);self.assertNotIn('88VIP',body)
            self.assertNotIn('查看本题回答',body);self.assertNotIn('answer_page',body)
            self.assertIn('近2周发布',body);self.assertIn('24cm替换芯',body)

    def test_answers_and_media_cannot_cross_question_or_batch(self):
        """回答与媒体都同时匹配所属问题、回答和批次。"""
        f=self.fixture;f.batch(31,'questions',product_id=1)
        f.put('answer_observations',id=1400,batch_id=31,question_id=300,position=226,content='错批次回答')
        f.put('question_observations',id=400,batch_id=31,position=1,question='其他批次问题')
        f.put('answer_observations',id=1401,batch_id=30,question_id=400,position=1,content='其他问题回答')
        path=f.settings.workspace/'output/preview/answer.jpg';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'answer-image')
        f.put('file_assets',id=90,root_key='workspace',relative_path='output/preview/answer.jpg',availability='present',
            size_bytes=len(b'answer-image'),sha256=hashlib.sha256(b'answer-image').hexdigest())
        for identifier,batch,owner,url,asset in [(90,30,1000,'https://img.alicdn.com/right.jpg',90),
            (91,31,1000,'https://img.alicdn.com/wrong.jpg',None),(92,30,1300,'https://img.alicdn.com/other.jpg',None)]:
            f.put('media_references',id=identifier,batch_id=batch,answer_observation_id=owner,role='answer_image',
                position=1,source_url=url,file_asset_id=asset)
        data=query(self.settings,'records',batch_id=30,page=1,with_context=True)
        first=data['records'][0]['answers']
        self.assertEqual(len(first),225);self.assertEqual([row['id'] for row in first[0]['media']],[90])
        self.assertEqual(first[0]['media'][0]['file_id'],90)
        self.assertEqual([row['id'] for row in data['records'][2]['answers'][0]['media']],[92])
        body=self.client.get('/collections/products/123?module=questions').get_data(as_text=True)
        self.assertNotIn('错批次回答',body);self.assertNotIn('其他问题回答',body);self.assertNotIn('wrong.jpg',body)
        response=self.client.get('/collections/files/90');self.assertEqual(response.status_code,200);response.close()

    def test_escaping_missing_metadata_and_no_questions(self):
        """问题回答按文本转义，无回答时间时不借用采集时间，无问答状态明确显示。"""
        f=self.fixture;q=f.store.tables['question_observations'];a=f.store.tables['answer_observations']
        with f.engine.begin() as c:
            c.execute(q.update().where(q.c.id==300).values(question='<script>问题</script>'))
            c.execute(a.update().where(a.c.id==1000).values(content='<script>回答</script>',date_raw=None,sku_text=None,payload={}))
        body=self.client.get('/collections/products/123?module=questions').get_data(as_text=True)
        self.assertIn('&lt;script&gt;问题&lt;/script&gt;',body);self.assertIn('&lt;script&gt;回答&lt;/script&gt;',body)
        self.assertNotIn('<script>回答</script>',body)
        f.batch(31,'questions',product_id=1,status='no_questions')
        self.assertIn('本次已确认无问答',self.client.get('/collections/products/123?batch=31').get_data(as_text=True))

    def test_current_scope_and_explicit_history_remain_separate(self):
        """没有当前有效问答时允许采集，历史批次不自动冒充当前有效结果。"""
        f=self.fixture;current=f.store.tables['product_current_sections']
        with f.engine.begin() as c:c.execute(current.delete().where(current.c.module=='questions'))
        body=self.client.get('/collections/products/123?module=questions').get_data(as_text=True)
        self.assertIn('>采集问大家</button>',body);self.assertNotIn('class="qa-card"',body)
        body=self.client.get('/collections/products/123?batch=30').get_data(as_text=True)
        self.assertIn('完整回答224',body)
        self.assertEqual(self.client.get('/collections/products/123?batch=22').status_code,404)


if __name__=='__main__':unittest.main()
