"""长期关注真实查询与接口回归；仅使用临时SQLite，不操作真实平台。"""
from datetime import timedelta
from unittest.mock import patch
import unittest
import sqlalchemy as sa
from tests.unit import test_search_analysis as fixtures
from ecommerce.modules.favorites import service
from ecommerce.infrastructure.storage import favorites as repository


class FavoriteTests(unittest.TestCase):
    def setUp(self):
        """复用隔离资料；参数：无；返回：无。"""
        self.f = fixtures.SearchAnalysisTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.observation(1); self.f.observation(2, shop_name='第二店')
        self.headers = {'X-Collection-Token': self.f.app.config['COLLECTION_TOKEN']}

    def call(self, **values):
        """调用本机写接口；参数：JSON字段；返回：响应。"""
        return self.f.client.post('/api/favorites', json=values, headers=self.headers)

    def test_identity_source_and_idempotency(self):
        """重复关注不重置依据和备注，拒绝错配、未知身份、暂存来源。"""
        original = service.add(self.f.settings, 1, 1)
        repository.change(self.f.settings, 1, note='保留备注', expected_id=original['id'])
        self.f.batch(2, search_keyword='另一个词', captured_at=self.f.now + timedelta(days=1))
        self.f.observation(3, product=1, batch=2)
        again = service.add(self.f.settings, 1, 3)
        self.assertEqual(again['id'], original['id']); self.assertEqual(again['observation_id'], 1)
        self.assertEqual(again['note'], '保留备注'); self.assertEqual(again['source_keyword'], '粘毛器')
        for product, observation in [(1,2),(9,1),(True,1)]:
            with self.assertRaises(ValueError): service.add(self.f.settings,product,observation)
        self.f.batch(3, visibility='staging'); self.f.observation(4, batch=3)
        with self.assertRaises(ValueError): service.add(self.f.settings,4,4)

    def test_database_scope_and_untrusted_old_saved_ignored(self):
        """关注由数据库决定，旧参数不导入；原广告、时间和关键词范围仍限制分析页。"""
        self.assertEqual(self.f.read(view='saved',saved='p:1')['pagination']['total'],0)
        service.add(self.f.settings,1,1)
        data = self.f.read(view='saved',saved='p:2')
        self.assertEqual([r['product_id'] for r in data['detail_rows']],[1])
        self.assertEqual(data['favorite_states'],{'1':data['favorite_states']['1']})
        self.assertEqual(service.page(self.f.settings,{})['pagination']['total'],1)

    def test_latest_whole_observation_across_keywords_and_visibility(self):
        """最近记录跨词取同一条，缺价不借旧值，暂存不覆盖；缺依据不丢关注。"""
        service.add(self.f.settings,1,1)
        self.f.batch(2, search_keyword='另一个词', captured_at=self.f.now + timedelta(days=1))
        self.f.observation(3,product=1,batch=2,price_value=None,sales_count=2000,sales_text='2000+人收货',price_label='首单价')
        self.f.batch(3, visibility='staging',captured_at=self.f.now+timedelta(days=2))
        self.f.observation(4,product=1,batch=3,sales_count=90000)
        row=service.page(self.f.settings,{})['items'][0]
        self.assertEqual(row['initial']['id'],1); self.assertEqual(row['latest']['id'],3)
        self.assertIsNone(row['latest']['price_value']); self.assertTrue(row['comparison']['new_record'])
        self.assertEqual(row['latest']['search_keyword'],'另一个词')
        self.assertEqual(service.page(self.f.settings,{'keyword':'粘毛器'})['pagination']['total'],1)
        self.assertEqual(service.page(self.f.settings,{'keyword':'另一个词'})['pagination']['total'],0)

    def test_undo_restores_original_and_rejects_forgery_expiry_and_race(self):
        """撤销恢复时间及备注，伪造/超时/重新关注均不得覆盖。"""
        added=self.call(action='add',product_id=1,observation_id=1).get_json()
        self.call(action='note',product_id=1,favorite_id=added['favorite_id'],note='保留我')
        before=repository.read_page(self.f.settings,dict(q='',keyword='',shop='',sort='created_desc',page=1))['items'][0]
        removed=self.call(action='remove',product_id=1,favorite_id=added['favorite_id']).get_json()
        self.assertEqual(service.page(self.f.settings,{})['pagination']['total'],0)
        self.assertEqual(self.call(action='undo',product_id=2,undo=removed['undo']).status_code,409)
        self.assertEqual(self.call(action='undo',product_id=1,undo=removed['undo']+'wrong').status_code,409)
        with patch('itsdangerous.timed.time.time',return_value=0):
            self.assertEqual(self.call(action='undo',product_id=1,undo=removed['undo']).status_code,409)
        self.assertEqual(self.call(action='undo',product_id=1,undo=removed['undo']).status_code,200)
        after=service.page(self.f.settings,{})['items'][0]
        self.assertEqual((after['id'],after['note'],after['created_at']),(before['id'],'保留我',before['created_at']))
        self.assertEqual(self.call(action='undo',product_id=1,undo=removed['undo']).status_code,409)
        self.call(action='remove',product_id=1,favorite_id=added['favorite_id'])
        new=self.call(action='add',product_id=1,observation_id=1).get_json()
        self.assertNotEqual(new['favorite_id'],added['favorite_id'])
        self.assertEqual(self.call(action='note',product_id=1,favorite_id=added['favorite_id'],note='过期').status_code,409)

    def test_token_origin_and_note_validation_and_html_escape(self):
        """跨站与旧页面写入拒绝，备注普通文本处理。"""
        self.assertEqual(self.f.client.post('/api/favorites',json={}).status_code,403)
        self.assertEqual(self.f.client.post('/api/favorites',json={},headers=dict(self.headers,Origin='https://other.example')).status_code,403)
        added=self.call(action='add',product_id=1,observation_id=1).get_json()
        self.assertEqual(self.call(action='note',product_id=1,favorite_id=added['favorite_id'],note='字'*501).status_code,409)
        self.assertEqual(self.call(action='note',product_id=1,favorite_id=added['favorite_id'],note='<script>危险</script>').status_code,200)
        response=self.f.client.get('/favorites')
        self.assertEqual(response.status_code,200)
        self.assertIn('&lt;script&gt;',response.get_data(as_text=True))
        self.assertEqual(service.page(self.f.settings,{})['items'][0]['note'],'<script>危险</script>')

    def test_sql_pagination_filters_and_clamp_after_cancel(self):
        """超过原200件上限使用SQL分页，最后一页取消后合理回退。"""
        for i in range(1,204):
            if i>2: self.f.observation(i,title=f'研究商品{i}')
            service.add(self.f.settings,i,i)
        data=service.page(self.f.settings,{'page':'11'})
        self.assertEqual(data['pagination']['total'],203); self.assertEqual(len(data['items']),3)
        self.assertEqual(service.page(self.f.settings,{'q':'研究商品203'})['pagination']['total'],1)
        self.assertEqual(service.page(self.f.settings,{'shop':'第二店'})['pagination']['total'],1)
        for item in data['items']: repository.change(self.f.settings,item['product_id'],remove=True,expected_id=item['id'])
        self.assertEqual(service.page(self.f.settings,{'page':'11'})['pagination']['page'],10)

    def test_comparison_does_not_claim_growth_or_fill_missing(self):
        """统计口径、价格条件和缺失值不同均明确提示。"""
        a=dict(id=1,sales_metric='received_people',sales_period='last_30_days',sales_parse_status='parsed',sales_precision='lower_bound',sales_count=1000,sales_text='1000+',price_value=10,price_label=None)
        b=dict(a,id=2)
        self.assertIn('实际人数仍可能变化',service.comparison(a,b)['quantity'])
        self.assertIn('统计口径',service.comparison(a,dict(b,sales_period='cumulative'))['quantity'])
        self.assertIn('价格条件不同',service.comparison(a,dict(b,price_label='首单价'))['price'])
        self.assertIn('不表示降至0',service.comparison(a,dict(b,sales_text=None))['quantity'])

    def test_schema_only_adds_favorites_and_preserves_frozen_tables(self):
        """当前版本只增加关注表，旧表列、约束及中文备注保持原契约。"""
        from ecommerce.infrastructure.storage.mysql_migrations.schema_v9 import build_metadata as before
        from ecommerce.infrastructure.storage.mysql_migrations.schema_v10 import build_metadata as after
        from sqlalchemy.schema import CreateTable
        from sqlalchemy.dialects import mysql
        old,new=before(),after()
        self.assertEqual(set(new.tables)-set(old.tables),{'product_favorites'})
        for name,table in old.tables.items():
            self.assertEqual(str(CreateTable(table).compile(dialect=mysql.dialect())),str(CreateTable(new.tables[name]).compile(dialect=mysql.dialect())))
        favorite=new.tables['product_favorites']
        self.assertEqual(len(favorite.foreign_key_constraints),2)
        self.assertTrue(all(c.comment for c in favorite.c))
