"""日志网页筛选的北京时间、快捷范围和分页验证。"""
from dataclasses import replace
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch
from urllib.parse import parse_qs, urlsplit

from ecommerce.shared.config import Settings, load_settings
from ecommerce.web.app import create_app


class LogFilterTests(unittest.TestCase):
    def setUp(self):
        """建立临时网页和日志查询替身；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings=load_settings(root=Path(temp.name))
        self.app=create_app(settings)
        self.app.config['SETTINGS']=replace(settings,task_backend='mysql')
        self.client=self.app.test_client()
        self.store=Mock()
        self.store.list_events.return_value={'events':[{'id':1,'occurred_at':'2026-10-25T12:00:00+08:00',
            'task_id':'a'*32,'task_name':'collection.taobao.detail.open','step':'parameters',
            'level':'INFO','event_type':'test','message':'样例日志'}],'next_cursor':17,
            'has_more':True,'has_previous':False,'previous_cursor':1,
            'total':21,'page':1,'pages':2,'page_size':20,'sort_spec':{'sort':'occurred_at','order':'desc'}}
        task_source=patch.object(Settings,'task_database',new_callable=PropertyMock,return_value=self.store)
        task_source.start()
        self.addCleanup(task_source.stop)

    def test_local_time_gets_beijing_offset_and_aware_links_are_compatible(self):
        """本地控件时间自动补北京时间；旧链接按时区转换回显。"""
        result=self.client.get('/logs',query_string={'since':'2026-10-25T10:30','until':'2026-10-25T11:30'})
        self.assertEqual(result.status_code,200)
        args=self.store.list_events.call_args.kwargs
        self.assertEqual(args['since'],'2026-10-25T10:30:00+08:00')
        self.assertEqual(args['until'],'2026-10-25T11:30:00+08:00')
        page=self.client.get('/logs',query_string={'since':'2026-10-25T02:30:00+00:00'}).get_data(as_text=True)
        self.assertIn('value="2026-10-25T10:30:00"',page)
        self.assertIn('type="datetime-local"',page)
        self.assertIn('北京时间',page)
        self.assertIn('全部级别',page)
        self.assertIn('>信息</option>',page)

    def test_quick_ranges_keep_task_and_pagination_freezes_times(self):
        """快捷范围保留任务和级别，翻页不重新计算当前时间。"""
        for preset,seconds in [('24h',86400),('7d',604800),('today',None)]:
            result=self.client.get('/logs',query_string={'range':preset,'task_id':'a'*32,'level':'ERROR','limit':'50'})
            self.assertEqual(result.status_code,200)
            args=self.store.list_events.call_args.kwargs
            start,end=map(datetime.fromisoformat,[args['since'],args['until']])
            self.assertEqual(end.utcoffset(),timedelta(hours=8))
            if seconds:
                self.assertEqual((end-start).total_seconds(),seconds)
            else:
                self.assertEqual((start.hour,start.minute,start.second),(0,0,0))
                self.assertEqual(start.date(),end.date())
            page=result.get_data(as_text=True)
            href=unescape(re.search(r'href="([^"]+)"[^>]*>下一页',page).group(1))
            parsed=parse_qs(urlsplit(href).query)
            self.assertNotIn('range',parsed)
            self.assertEqual(parsed['since'],[args['since']])
            self.assertEqual(parsed['until'],[args['until']])
            self.assertEqual(parsed['task_id'],['a'*32])
            self.assertEqual(parsed['level'],['ERROR'])
            self.assertNotIn('limit',parsed)
            self.assertNotIn('after',parsed)
            self.assertEqual(parsed['page'],['2'])
            self.assertEqual(args['limit'],20)
            self.assertEqual(self.client.get(href).status_code,200)
            self.assertEqual(self.store.list_events.call_args.kwargs['since'],args['since'])

    def test_invalid_ranges_are_rejected_before_query(self):
        """无效日期、倒置范围和未知快捷项不触发查询。"""
        for params in [{'since':'bad'},{'since':'2026-10-25'},
                       {'since':'2026-10-25T12:00','until':'2026-10-24T12:00'},
                       {'since':'2026-02-30T12:00'},{'range':'unknown'}]:
            self.store.list_events.reset_mock()
            self.assertEqual(self.client.get('/logs',query_string=params).status_code,400)
            self.store.list_events.assert_not_called()

    def test_reset_and_api_keep_existing_contract(self):
        """空筛选清除时间，接口仍传递原带时区参数。"""
        page=self.client.get('/logs').get_data(as_text=True)
        args=self.store.list_events.call_args.kwargs
        self.assertIsNone(args['since'])
        self.assertIsNone(args['until'])
        self.assertIn('class="button action-button action-neutral" href="/logs"',page)
        self.client.get('/api/events',query_string={'since':'2026-10-25T01:00:00+00:00'})
        self.assertEqual(self.store.list_events.call_args.kwargs['since'],'2026-10-25T01:00:00+00:00')

    def test_readable_labels_full_time_and_safe_unknown_values(self):
        """中文标签、完整北京时间和级别样式准确，未知代码保留原文且安全转义。"""
        result=self.client.get('/logs')
        body=result.get_data(as_text=True)
        self.assertIn('淘宝商品详情采集',body)
        self.assertIn('商品参数采集',body)
        self.assertIn('2026-10-25 12:00:00',body)
        self.assertIn('event-level-info',body)
        self.assertIn('a'*32,body)
        self.assertTrue(self.store.list_events.call_args.kwargs['include_task_name'])
        from ecommerce.web.presentation import present_log_event
        for code,css in [('DEBUG','debug'),('INFO','info'),('WARNING','warning'),('ERROR','error'),('CRITICAL','critical')]:
            self.assertEqual(present_log_event({'level':code})['level_class'],css)
        self.store.list_events.return_value['events']=[{'occurred_at':'2026-10-25T04:00:00+00:00',
            'task_id':'a'*32,'task_name':'<script>未知任务</script>','step':'未知步骤',
            'level':'<bad>','event_type':'test','message':'<script>内容</script>'}]
        body=self.client.get('/logs').get_data(as_text=True)
        self.assertIn('2026-10-25 12:00:00',body)
        self.assertIn('&lt;script&gt;未知任务&lt;/script&gt;',body)
        self.assertIn('未知步骤',body)
        self.assertIn('event-level-unknown',body)
        self.assertNotIn('<script>',body)
        self.assertEqual(present_log_event({'event_type':'task_started'})['step_label'],'任务开始')
        self.assertEqual(present_log_event({})['task_label'],'无可靠任务归属')

    def test_event_type_translation_preserves_original_and_unknown_values(self):
        """事件类型中文展示，悬停保留原代码，未知类型安全回退。"""
        from ecommerce.web.presentation import present_log_event
        original={'event_type':'task_finished','state':'cancelled'}
        shown=present_log_event(original)
        self.assertEqual(shown['event_type_label'],'任务结束')
        self.assertEqual(shown['event_type'],'task_finished')
        self.assertNotIn('event_type_label',original)
        self.store.list_events.return_value['events']=[original]
        page=self.client.get('/logs').get_data(as_text=True)
        self.assertIn('<span>任务结束</span>',page)
        self.assertIn('class="event-type-label" tabindex="0"',page)
        self.assertIn('role="tooltip" id="event-code-1">task_finished</span>',page)
        self.assertIn('aria-describedby="event-code-1"',page)
        self.assertEqual(present_log_event({'event_type':'future_event'})['event_type_label'],'future_event')
        self.assertEqual(present_log_event({})['event_type_label'],'类型未记录')
        self.store.list_events.return_value['events']=[{'event_type':'<script>未知</script>'}]
        page=self.client.get('/logs').get_data(as_text=True)
        self.assertIn('&lt;script&gt;未知&lt;/script&gt;',page)
        self.assertNotIn('<script>未知',page)

    def test_real_event_queries_stop_at_end_and_can_go_back(self):
        """用真实SQL分页验证不足一页、整页末尾、反向读取及空页恢复。"""
        import sqlalchemy as sa
        from ecommerce.infrastructure.storage.task_store import MySQLTaskStore
        engine=sa.create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        metadata=sa.MetaData()
        table=sa.Table('task_events',metadata,sa.Column('id',sa.Integer,primary_key=True),
            sa.Column('task_id',sa.String),sa.Column('task_sequence',sa.Integer),
            sa.Column('level',sa.String),sa.Column('occurred_at',sa.DateTime),
            sa.Column('recorded_at',sa.DateTime),sa.Column('message',sa.String),
            sa.Column('event_type',sa.String),sa.Column('details',sa.JSON))
        tasks=sa.Table('tasks',metadata,sa.Column('id',sa.String,primary_key=True),sa.Column('name',sa.String))
        metadata.create_all(engine)
        with engine.begin() as c:
            c.execute(tasks.insert(),dict(id='a'*32,name='collection.taobao.collect'))
            c.execute(table.insert(),[dict(id=i*10,task_id='a'*32,task_sequence=i*2,level='INFO',
                occurred_at=datetime(2026,10,25,12),recorded_at=datetime(2026,10,25,12),
                message='日志'+str(i),event_type='test',details={'private':'hidden'}) for i in range(1,6)])
            c.execute(table.insert(),dict(id=60,task_id='b'*32,task_sequence=1,level='DEBUG',
                occurred_at=datetime(2026,10,25,12),recorded_at=datetime(2026,10,25,12)))
        store=MySQLTaskStore.__new__(MySQLTaskStore)
        store.engine=engine
        store.tables={'task_events':table,'tasks':tasks}
        orphan=store.list_events(level='DEBUG',page=1,include_task_name=True)
        self.assertEqual(orphan['total'],1)
        self.assertIsNone(orphan['events'][0]['task_name'])
        for task in (None,'a'*32):
            first=store.list_events(task_id=task,limit=2,level='INFO')
            second=store.list_events(task_id=task,limit=2,level='INFO',after=first['next_cursor'])
            last=store.list_events(task_id=task,limit=2,level='INFO',after=second['next_cursor'])
            self.assertEqual([len(p['events']) for p in (first,second,last)],[2,2,1])
            self.assertEqual([p['has_more'] for p in (first,second,last)],[True,True,False])
            self.assertFalse(first['has_previous'])
            back=store.list_events(task_id=task,limit=2,level='INFO',before=second['previous_cursor'])
            self.assertEqual(back['events'],first['events'])
            self.assertFalse(back['has_previous'])
            self.assertTrue(back['has_more'])
            full=store.list_events(task_id=task,limit=5,level='INFO')
            self.assertFalse(full['has_more'])
            self.assertNotIn('details',full['events'][0])
        with engine.begin() as c:
            c.execute(table.insert(),[dict(id=i*10,task_id='a'*32,task_sequence=i*2,level='INFO',
                occurred_at=datetime(2026,10,25,12),recorded_at=datetime(2026,10,25,12),
                message='日志'+str(i),event_type='test') for i in range(7,47)])
        numbered=[store.list_events(level='INFO',page=p) for p in (1,2,3)]
        self.assertEqual([len(r['events']) for r in numbered],[20,20,5])
        self.assertEqual([r['total'] for r in numbered],[45,45,45])
        self.assertEqual(len({r['id'] for page in numbered for r in page['events']}),45)
        asc=[store.list_events(level='INFO',page=p,sort_by='occurred_at',order='asc') for p in (1,2,3)]
        descending=[r['id'] for page in numbered for r in page['events']]
        self.assertEqual(descending,list(reversed([r['id'] for page in asc for r in page['events']])))
        severity=store.list_events(page=1,sort_by='level',order='asc')
        self.assertEqual(severity['events'][0]['level'],'DEBUG')
        with patch.object(Settings,'task_database',new_callable=PropertyMock,return_value=store):
            page=self.client.get('/logs?page=3&level=INFO&limit=500').get_data(as_text=True)
            self.assertIn('淘宝列表采集',page)
            self.assertIn('rel="prev"',page)
            self.assertIn('第 3 / 3 页',page)
            self.assertIn('aria-disabled="true">下一页',page)
            self.assertNotIn('rel="next"',page)
            back_href=unescape(re.search(r'rel="prev" href="([^"]+)"',page).group(1))
            restored=self.client.get(back_href).get_data(as_text=True)
            self.assertIn('第 2 / 3 页',restored)
            empty=self.client.get('/logs?level=ERROR').get_data(as_text=True)
            self.assertNotIn('rel="next"',empty)
            self.assertNotIn('rel="prev"',empty)
            self.assertEqual(self.client.get('/logs?page=0').status_code,400)
            self.assertEqual(self.client.get('/logs?page=4').status_code,404)
            self.assertEqual(self.client.get('/logs?sort=invalid').status_code,400)
            self.assertEqual(self.client.get('/logs?order=invalid').status_code,400)


if __name__=='__main__':
    unittest.main()
