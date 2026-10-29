"""长期关注的MySQL约束专项；仅验证库事务写入，结束全部回滚。"""
from dataclasses import replace
from uuid import uuid4
import os
import unittest
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.mysql import create_mysql_engine
from ecommerce.infrastructure.storage.mysql_migrations.schema_v10 import build_metadata
from ecommerce.infrastructure.storage.time_values import database_now


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS') == '1','显式启用独立验证库')
class MySQLFavoriteTests(unittest.TestCase):
    def test_unique_foreign_keys_and_parent_preservation(self):
        """真实MySQL拒绝重复关注与悬空引用，依据和商品删除受限，合成记录回滚。"""
        settings=load_settings().mysql
        settings=replace(settings,database=settings.database+'_verify')
        self.assertTrue(settings.database.endswith('_verify'))
        engine=create_mysql_engine(settings); self.addCleanup(engine.dispose)
        t=build_metadata().tables; token=uuid4().hex; now=database_now()
        with engine.connect() as c:
            transaction=c.begin()
            try:
                p=c.execute(t['products'].insert().values(platform='taobao',platform_product_id=token)).inserted_primary_key[0]
                b=c.execute(t['collection_batches'].insert().values(platform='taobao',source_batch_key=token,module='list',
                    status='complete',scope_key='0'*64,coverage_status='unknown',visibility='published',
                    search_method='keyword',search_keyword='关注合成验证',captured_at=now)).inserted_primary_key[0]
                o=c.execute(t['list_observations'].insert().values(batch_id=b,product_id=p,slot_index=1,product_index=1,
                    title='仅事务内合成样例',ad_status='not_identified',identity_status='confirmed')).inserted_primary_key[0]
                row=dict(id=token,product_id=p,observation_id=o,source_keyword='关注合成验证',note='中文备注🧪',created_at=now,updated_at=now)
                c.execute(t['product_favorites'].insert().values(**row))
                self.assertEqual(c.scalar(sa.select(t['product_favorites'].c.note).where(t['product_favorites'].c.id==token)),'中文备注🧪')
                for statement in (t['product_favorites'].insert().values(**dict(row,id=uuid4().hex)),
                                  t['list_observations'].delete().where(t['list_observations'].c.id==o),
                                  t['products'].delete().where(t['products'].c.id==p)):
                    with self.assertRaises(sa.exc.IntegrityError),c.begin_nested(): c.execute(statement)
                c.execute(t['product_favorites'].delete().where(t['product_favorites'].c.id==token))
                with self.assertRaises(sa.exc.IntegrityError),c.begin_nested():
                    c.execute(t['product_favorites'].insert().values(**dict(row,observation_id=2**63)))
            finally:
                transaction.rollback()
