"""任务历史分页行为验证，不修改正式库。"""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

import sqlalchemy as sa

from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.storage.task_store import MySQLTaskStore
from ecommerce.modules.monitoring.service import task_history
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app


class TaskPaginationTests(unittest.TestCase):
    def setUp(self):
        """创建临时配置和网页客户端；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings=load_settings(root=Path(temp.name))
        self.app=create_app(self.settings)
        self.client=self.app.test_client()

    def seed(self,count):
        """建立同一时间的多条任务以验证排序边界；参数：数量；返回：无。"""
        with database.connect(self.settings.database) as c:
            c.executemany("INSERT INTO tasks(id,name,status,started_at) VALUES (?,?,'succeeded',?)",
                [(f'{i:032x}',f'task-{i}','2026-10-25T12:00:00+08:00') for i in range(count)])

    def test_all_records_are_reachable_in_twenty_record_pages(self):
        """超过原50条上限仍全部可达，时间相同也不重复或漏项。"""
        self.seed(53)
        pages=[task_history(self.settings,p) for p in (1,2,3)]
        self.assertEqual([len(p['tasks']) for p in pages],[20,20,13])
        self.assertEqual([p['pages'] for p in pages],[3,3,3])
        ids=[r['id'] for page in pages for r in page['tasks']]
        self.assertEqual(ids,[f'{i:032x}' for i in reversed(range(53))])
        self.assertEqual((pages[-1]['first_record'],pages[-1]['last_record']),(41,53))
        first=self.client.get('/tasks').get_data(as_text=True)
        last=self.client.get('/tasks?page=3').get_data(as_text=True)
        self.assertIn('全部 53 条任务，每页 20 条',first)
        self.assertIn('page=2',first)
        self.assertIn('task-0',last)
        self.assertNotIn('rel="next"',last)
        self.assertIn('page=2',last)

    def test_empty_exact_boundary_and_invalid_pages(self):
        """空库、整页末尾和无效页码明确处理。"""
        self.assertEqual(self.client.get('/tasks').status_code,200)
        self.assertEqual(task_history(self.settings)['total'],0)
        self.seed(20)
        self.assertEqual(task_history(self.settings)['pages'],1)
        self.assertNotIn('rel="next"',self.client.get('/tasks').get_data(as_text=True))
        for page in ('0','-1','bad','1.5'):
            self.assertEqual(self.client.get('/tasks?page='+page).status_code,400)
        self.assertEqual(self.client.get('/tasks?page=2').status_code,404)

    def test_sorting_is_global_stable_and_missing_finish_times_stay_last(self):
        """时间排序在分页之前执行，相同时间稳定，未结束任务不会排在有结束时间之前。"""
        self.seed(53)
        asc=[task_history(self.settings,p,sort_by='started_at',order='asc') for p in (1,2,3)]
        self.assertEqual([r['id'] for page in asc for r in page['tasks']],[f'{i:032x}' for i in range(53)])
        with database.connect(self.settings.database) as c:
            c.execute('UPDATE tasks SET finished_at=? WHERE id=?',('2026-10-25T13:00:00+08:00',f'{5:032x}'))
            c.execute('UPDATE tasks SET finished_at=? WHERE id=?',('2026-10-25T14:00:00+08:00',f'{10:032x}'))
        for order,expected in [('asc',[5,10]),('desc',[10,5])]:
            data=task_history(self.settings,sort_by='finished_at',order=order)
            self.assertEqual([r['id'] for r in data['tasks'][:2]],[f'{i:032x}' for i in expected])
        body=self.client.get('/tasks?page=2&sort=started_at&order=asc').get_data(as_text=True)
        self.assertIn('aria-sort="ascending"',body)
        self.assertIn('sort=started_at',body)
        self.assertIn('order=desc',body)
        for path in ['/tasks?sort=unknown','/tasks?order=DROP','/tasks?sort=started_at%3BDROP']:
            self.assertEqual(self.client.get(path).status_code,400)

    def test_mysql_store_offset_and_tied_time_order(self):
        """通过临时SQL连接验证MySQL存储查询的偏移和稳定排序。"""
        engine=sa.create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        metadata=sa.MetaData()
        table=sa.Table('tasks',metadata,sa.Column('id',sa.String,primary_key=True),sa.Column('name',sa.String),
            sa.Column('status',sa.String),sa.Column('message',sa.String),sa.Column('started_at',sa.DateTime),sa.Column('finished_at',sa.DateTime),
            sa.Column('owner_process_started_at',sa.DateTime),sa.Column('origin',sa.String))
        metadata.create_all(engine)
        with engine.begin() as c:
            c.execute(table.insert(),[dict(id=f'{i:032x}',name='test',status='running',origin='live',
                started_at=datetime(2026,10,25,12)) for i in range(53)])
        store=MySQLTaskStore.__new__(MySQLTaskStore)
        store.engine=engine
        store.tables={'tasks':table}
        results=[database.list_tasks(store,20,offset=offset) for offset in (0,20,40)]
        self.assertEqual([len(r) for r in results],[20,20,13])
        self.assertEqual([r['id'] for page in results for r in page],[f'{i:032x}' for i in reversed(range(53))])
        self.assertEqual(len(store.list_tasks(active=True)),53)
        self.assertEqual(len(database.list_tasks(store)),50)


if __name__=='__main__':
    unittest.main()
