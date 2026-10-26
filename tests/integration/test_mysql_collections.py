"""显式启用的独立库采集验证；只使用临时脱敏来源，清理仅限本测试归属。"""
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.storage.collection_store import CollectionStore,collection_store
from ecommerce.infrastructure.storage.collection_queries import batches,records,products,detail_document
from ecommerce.infrastructure.tasks.events import current_events,replay_events
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.modules.collection.detail_storage import save_detail_section
from ecommerce.shared.config import load_settings
from ecommerce.shared.logging import setup_logging
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from tests.collection_fixture import create_sources,save_fixture
from ecommerce.web.app import create_app


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','须显式启用独立 MySQL 验证库')
class MySQLCollectionTests(unittest.TestCase):
    def setUp(self):
        """创建临时配置与专用身份；参数：无；返回：无。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base=replace(load_settings(root=Path(temporary.name)),task_backend='mysql')
        self.assertEqual(self.base.mysql.database,'taobao_automation_verify')
        self.store=collection_store(self.base)
        self.store.tasks.initialize()
        self.task_ids=set()
        self.external_id=str(int(uuid4().hex[:14],16))
        self.external_ids=[self.external_id,str(int(self.external_id)+1)]
        self.addCleanup(self.cleanup_records)

    def cleanup_records(self):
        """按实际归属先删子记录后删父记录；参数：无；返回：无。"""
        logger=logging.getLogger('ecommerce')
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        for path in (self.base.log_dir/'events').glob('*.jsonl'):
            for line in path.read_text(encoding='utf-8').splitlines():
                row=json.loads(line)
                if row.get('task_id'):
                    self.task_ids.add(row['task_id'])
        t=self.store.tables
        with self.store.engine.begin() as connection:
            product_ids=list(connection.execute(sa.select(t['products'].c.id).where(t['products'].c.platform_product_id.in_(self.external_ids))).scalars())
            batch_ids=list(connection.execute(sa.select(t['collection_batches'].c.id).where(t['collection_batches'].c.task_id.in_(self.task_ids))).scalars())
            collection_runs=list(connection.execute(sa.select(t['collection_runs'].c.id).where(t['collection_runs'].c.task_id.in_(self.task_ids))).scalars())
            media=t['media_references']
            # 仅解除本测试拥有的新来源边，再按旧顺序清理；不碰其他任务数据。
            connection.execute(t['collection_runs'].update().where(t['collection_runs'].c.id.in_(collection_runs)).values(entry_record_id=None))
            connection.execute(t['search_records'].update().where(t['search_records'].c.task_id.in_(self.task_ids)).values(source_list_observation_id=None))
            media_where=sa.or_(media.c.batch_id.in_(batch_ids),media.c.task_id.in_(self.task_ids))
            file_ids=set(connection.execute(sa.select(media.c.file_asset_id).where(media_where,media.c.file_asset_id.is_not(None))).scalars())
            files=t['file_assets']
            owned_paths=sa.or_(*(files.c.relative_path.startswith('output/raw/taobao_details/'+identifier+'/',autoescape=True) for identifier in self.external_ids))
            file_ids.update(connection.execute(sa.select(files.c.id).where(owned_paths)).scalars())
            connection.execute(t['products'].update().where(t['products'].c.id.in_(product_ids)).values(title_source_batch_id=None))
            connection.execute(t['product_current_sections'].delete().where(t['product_current_sections'].c.product_id.in_(product_ids)))
            connection.execute(t['task_events'].delete().where(t['task_events'].c.task_id.in_(self.task_ids)))
            connection.execute(media.delete().where(media_where))
            for name in ['product_info_observations','collection_checkpoints','answer_observations','question_observations','review_observations',
                         'sku_observations','parameter_observations','list_observations','review_summaries']:
                connection.execute(t[name].delete().where(t[name].c.batch_id.in_(batch_ids)))
            connection.execute(t['collection_batches'].update().where(t['collection_batches'].c.id.in_(batch_ids)).values(derived_from_batch_id=None))
            connection.execute(t['collection_batches'].delete().where(t['collection_batches'].c.id.in_(batch_ids)))
            connection.execute(t['collection_steps'].delete().where(t['collection_steps'].c.run_id.in_(collection_runs)))
            connection.execute(t['collection_runs'].delete().where(t['collection_runs'].c.id.in_(collection_runs)))
            connection.execute(t['search_records'].delete().where(t['search_records'].c.task_id.in_(self.task_ids)))
            connection.execute(t['file_assets'].delete().where(t['file_assets'].c.id.in_(file_ids)))
            connection.execute(t['products'].delete().where(t['products'].c.id.in_(product_ids)))
            connection.execute(t['tasks'].delete().where(t['tasks'].c.id.in_(self.task_ids)))

    def fixture(self):
        """生成已登记任务下的历史样例；参数：无；返回：临时来源。"""
        task_id=database.create_task(self.store.tasks,'example.collection.fixture')
        self.task_ids.add(task_id)
        database.finish_task(self.store.tasks,task_id,'succeeded','本地样例生成入口')
        return create_sources(self.base,task_id,self.external_id)

    def test_all_modules_roundtrip_idempotence_and_scope(self):
        """数量、价格空值、模糊总量、重复位置、参数重名及问答父子关系保持一致。"""
        fixture=self.fixture()
        with self.store.engine.begin() as connection:
            saved=save_fixture(self.base,connection,fixture)
        self.assertEqual(len(saved),9)
        before=batches(self.base,product_id=self.external_id)['batches']
        with self.store.engine.begin() as connection:
            save_fixture(self.base,connection,fixture)
        self.assertEqual([row['id'] for row in before],[row['id'] for row in batches(self.base,product_id=self.external_id)['batches']])
        rows=batches(self.base,product_id=self.external_id)['batches']
        by_module={row['module']:row for row in rows}
        self.assertEqual((by_module['reviews']['coverage_status'],by_module['reviews']['end_confirmed']),('limit_reached',False))
        self.assertEqual(by_module['questions']['requested_limit'],50)
        self.assertEqual(by_module['questions']['coverage_status'],'visible_end_confirmed')
        self.assertEqual(by_module['skus']['status'],'partial')
        sku=records(self.base,by_module['skus']['id'])['records'][0]
        self.assertIsNone(sku['price_value'])
        summary=records(self.base,by_module['review_summary']['id'])['records'][0]
        self.assertIsNone(summary['count_value'])
        self.assertEqual(summary['count_lower_bound'],10000)
        self.assertEqual(summary['positive_rate_percent'],'99.800000')
        answers=records(self.base,by_module['questions']['id'],kind='answers')['records']
        self.assertEqual(len({answer['question_id'] for answer in answers}),2)
        self.assertEqual(len(answers),2)
        document=detail_document(self.base,self.external_id)
        self.assertNotIn('sku_details',document)
        self.assertEqual(document['sku_details_last_attempt']['status'],'partial')
        self.assertEqual(len(products(self.base)['products']),2)
        with self.store.engine.connect() as connection:
            listing=self.store.tables['list_observations']
            self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(listing)),2)

    def test_missing_media_is_quarantined_and_published_conflict_is_visible(self):
        """缺失文件不制造可用性，部分隔离可见；同批异文不覆盖。"""
        fixture=self.fixture()
        image=fixture['records']['main_images']['images'][0]
        Path(image['image_path']).unlink()
        with self.store.engine.begin() as connection:
            save_fixture(self.base,connection,fixture)
        self.assertNotIn('main_images',detail_document(self.base,self.external_id))
        isolated=batches(self.base,product_id=self.external_id,module='main_images',include_quarantined=True)['batches']
        self.assertEqual(isolated[0]['visibility'],'quarantined')
        path=fixture['paths']['parameters']
        record=json.loads(path.read_text(encoding='utf-8'))
        record['items'][0]['value']='冲突的新值'
        path.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        with self.assertRaises(MySQLStorageError):
            with self.store.engine.begin() as connection:
                save_fixture(self.base,connection,fixture)
        self.assertEqual(detail_document(self.base,self.external_id)['parameters']['items'][0]['value'],'值1')


    def test_live_derivation_cycle_is_rejected(self):
        """同任务的部分批次也不能形成环，失败后原派生关系保持不变。"""
        from ecommerce.infrastructure.storage.collection_store import CollectionWriter,CollectionDataError
        from ecommerce.modules.collection.database_records import build_snapshot
        fixture=self.fixture()
        original=fixture['records']['parameters']
        keys=[uuid4().hex,uuid4().hex]
        references=[]
        for key,parent in [(keys[0],None),(keys[1],keys[0]),(keys[0],keys[1])]:
            path=fixture['root']/'history'/key/'parameters.json'
            path.parent.mkdir(parents=True,exist_ok=True)
            record={**original,'batch_id':key,'status':'partial','resumed_from_batch_id':parent,
                    'history_record_file':path.relative_to(fixture['root']).as_posix()}
            path.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
            references.append(build_snapshot(self.base,'batch',record,path,module='parameters'))
        with self.store.engine.begin() as connection:
            CollectionWriter(self.base,connection).apply(references[0])
            CollectionWriter(self.base,connection).apply(references[1])
            with self.assertRaises(CollectionDataError):
                CollectionWriter(self.base,connection).apply(references[2])
            table=self.store.tables['collection_batches']
            parent=connection.scalar(sa.select(table.c.derived_from_batch_id).where(table.c.source_batch_key==keys[0]))
            self.assertIsNone(parent)

    def _parameter_action(self,settings,*,value='当前任务实际保存的样例',status='complete'):
        """创建实际可确认的脱敏参数观察；参数：临时配置；返回：任务结语。"""
        session=current_events()
        task_id=session.task_id
        self.task_ids.add(task_id)
        batch=task_id+'-'+uuid4().hex[:8]
        root=settings.workspace/'output/raw/taobao_details'/self.external_id
        path=root/'history'/batch/'parameters.json'
        path.parent.mkdir(parents=True)
        record={'schema_version':1,'task_id':task_id,'batch_id':batch,'product_id':self.external_id,'status':status,
            'scope':'product_parameters','captured_at':'2026-10-20T04:00:00+00:00',
            'history_record_file':path.relative_to(root).as_posix(),
            'items':[{'index':1,'name':'参数','value':value,'status':'read'}]}
        path.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        save_detail_section(root,self.external_id,'parameters' if status=='complete' else 'parameters_last_attempt',record)
        return '脱敏观察已保存'

    def test_recollection_updates_valid_section_and_partial_attempt_keeps_it(self):
        """同商品同范围重采切换有效批次，部分尝试不替换且三次历史均保留。"""
        settings=replace(self.base,collection_backend='mysql')
        def action():
            """经实际持久流水保存三轮参数；参数：无；返回：结束说明。"""
            for value,status in [('第一版','complete'),('第二版','complete'),('未完成版本','partial')]:
                self._parameter_action(settings,value=value,status=status)
                current_events().checkpoint('本地参数结果已确认')
            return '重采与部分结果保留验证完成'
        result=run_task(settings,'example.collection.recollect',action)
        self.assertEqual(result.status,'succeeded',result.message)
        rows=batches(settings,product_id=self.external_id,module='parameters')['batches']
        self.assertEqual(len(rows),3)
        self.assertCountEqual([row['status'] for row in rows],['partial','complete','complete'])
        document=detail_document(settings,self.external_id)
        self.assertEqual(document['parameters']['items'][0]['value'],'第二版')
        self.assertEqual(document['parameters_last_attempt']['items'][0]['value'],'未完成版本')
        self.assertEqual({records(settings,row['id'])['records'][0]['value'] for row in rows},{'第一版','第二版','未完成版本'})

    def test_runtime_collection_is_ordered_before_finish_and_does_not_write_old_task_db(self):
        """完成事件必须排在采集事务之后，新的任务结果不写旧任务 SQLite。"""
        settings=replace(self.base,collection_backend='mysql')
        setup_logging(settings)
        result=run_task(settings,'example.collection.runtime',lambda:self._parameter_action(settings))
        self.assertEqual(result.status,'succeeded')
        events=self.store.tasks.list_events(task_id=result.task_id)['events']
        types=[event['event_type'] for event in events]
        self.assertLess(types.index('collection_saved'),types.index('task_finished'))
        self.assertFalse(settings.database.exists())
        self.assertEqual(detail_document(settings,self.external_id)['parameters']['items'][0]['value'],'当前任务实际保存的样例')

    def test_commit_response_loss_preserves_snapshot_and_replays_without_duplicate(self):
        """采集提交回执丢失后不报全部保存成功，恢复仅补写一份真实结果。"""
        settings=replace(self.base,collection_backend='mysql')
        actual=CollectionStore.apply_operation
        lost={'once':True}
        def disrupt(store,operation):
            """在真实提交后丢失一次响应；参数：存储、操作；返回：提交结果或模拟异常。"""
            result=actual(store,operation)
            if lost['once']:
                lost['once']=False
                raise MySQLStorageError('模拟采集提交回执丢失')
            return result
        with patch.object(CollectionStore,'apply_operation',disrupt):
            result=run_task(settings,'example.collection.pending',lambda:self._parameter_action(settings))
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('待入库',result.message)
        replay_events(settings,self.store.tasks)
        task=next(row for row in self.store.tasks.list_tasks() if row['id']==result.task_id)
        self.assertEqual(task['status'],'succeeded')
        self.assertEqual(len(batches(settings,product_id=self.external_id)['batches']),1)
        events=self.store.tasks.list_events(task_id=result.task_id)['events']
        self.assertEqual(sum(row['event_type']=='collection_saved' for row in events),1)

    def test_incremental_review_watermarks_and_final_projection_remain_consistent(self):
        """倍增水位和最终导出共用同一批次，保留内部身份并确认全部最终记录。"""
        from ecommerce.modules.collection.detail_reviews import ReviewStore,atomic_json,export_reviews,publish_reviews
        from ecommerce.modules.collection.persistence import capture_progress
        settings=replace(self.base,collection_backend='mysql')
        def action():
            """逐条写入本地样例并确认采集屏障；参数：无；返回：结论。"""
            session=current_events()
            self.task_ids.add(session.task_id)
            batch=session.task_id+'-'+uuid4().hex[:8]
            root=settings.workspace/'output/raw/taobao_details'/self.external_id
            directory=root/'history'/batch
            work=ReviewStore(directory,self.external_id,5)
            try:
                for count in range(1,6):
                    value={'index':count,'product_id':self.external_id,'review_key':'fp:row:'+str(count),'review_id':None,
                        'identity_method':'fingerprint_occurrence','identity_exact':False,'author':'样例用户',
                        'date_raw':'昨天','sku':None,'content':'评价'+str(count),'image_urls':[],'video_urls':[],
                        'append_reviews':[],'unresolved_media':[],'captured_at':'2026-10-20T04:00:00+00:00'}
                    with work.connection:
                        work.connection.execute('INSERT INTO seen VALUES (?,0)',(value['review_key'],))
                        work.connection.execute('INSERT INTO reviews VALUES (?,?,?)',(count,value['review_key'],json.dumps(value)))
                    record={'schema_version':1,'task_id':session.task_id,'batch_id':batch,'product_id':self.external_id,
                        'captured_at':value['captured_at'],'status':'running','scope':'page_available_reviews','mode':'limited',
                        'requested_limit':5,'collected_count':count,'scanned_count':count,'skipped_template_count':0,
                        'end_confirmed':False,'stop_reason':'collecting'}
                    progress=directory/'reviews-progress.json'
                    atomic_json(progress,record)
                    capture_progress(settings,'reviews',record,progress)
                    session.checkpoint('模拟下一个页面动作')
                saved=export_reviews(work,directory,dict(record,status='complete',stop_reason='limit_reached'))
                publish_reviews(root,saved)
            finally:
                work.close()
            return '五条真实样例均已保存'
        result=run_task(settings,'example.collection.incremental',action)
        self.assertEqual(result.status,'succeeded')
        batch=batches(settings,product_id=self.external_id,module='reviews')['batches'][0]
        self.assertEqual(len(records(settings,batch['id'])['records']),5)
        events=self.store.tasks.list_events(task_id=result.task_id)['events']
        self.assertEqual(sum(row['event_type']=='collection_saved' for row in events),4)
        from ecommerce.infrastructure.storage.collection_snapshot import digest_file
        with self.store.engine.connect() as connection:
            for asset in connection.execute(sa.select(self.store.tables['file_assets'])).mappings():
                if '/'+self.external_id+'/' in asset['relative_path']:
                    self.assertEqual(digest_file(settings.workspace/asset['relative_path']),asset['sha256'])

    def test_web_uses_same_history_filters_and_escapes_review_text(self):
        """网页和接口共用查询，参数错误明确，评价正文不执行脚本。"""
        fixture=self.fixture()
        with self.store.engine.begin() as connection:
            save_fixture(self.base,connection,fixture)
        settings=replace(self.base,collection_backend='mysql')
        client=create_app(settings).test_client()
        rows=client.get('/api/collections/batches',query_string={'product_id':self.external_id}).get_json()['batches']
        review=next(row for row in rows if row['module']=='reviews')
        self.assertEqual(client.get('/collections').status_code,200)
        page=client.get('/collections/batches/'+str(review['id'])).data.decode('utf-8')
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('<script>unsafe()',page)
        response=client.get('/api/collections/batches/'+str(review['id'])+'/records')
        self.assertNotIn('signature=private',response.data.decode('utf-8'))
        for path in ['/api/collections/products?after=-1','/api/collections/batches?limit=0',
                     '/api/collections/batches?module=unknown','/api/collections/batches?include_quarantined=bad']:
            self.assertEqual(client.get(path).status_code,400)
        self.assertEqual(client.get('/api/collections/batches/999999999999/records').status_code,404)

    def test_runtime_two_products_keep_distinct_entries_runs_and_source_context(self):
        """统一持久流水中两商品分别关联入口与参数，逐商品上下文恢复且来源不串写。"""
        from unittest.mock import MagicMock
        from ecommerce.modules.collection import detail_service
        from ecommerce.modules.collection.persistence import capture_list
        from ecommerce.platforms.taobao.detail import DetailPage, resolve_detail_input
        settings=replace(self.base,collection_backend='mysql')
        outputs=[]
        def action():
            """用本地参数结果验证实际业务保存链；参数：无；返回：结束说明。"""
            session=current_events()
            self.task_ids.add(session.task_id)
            listing={'schema_version':4,'task_id':session.task_id,'batch_id':session.task_id+'-list',
                'keyword':'脱敏样例','search_method':'keyword','page_number':1,'sort_label':'销量','search_options':None,
                'collection_status':'limit_reached','scope':'search_page_loaded_cards','requested_limit':2,
                'items':[{'product_id':identifier,'product_index':i,'slot_index':i,'title':'脱敏商品','price_text':'¥2','price_value':'2'}
                    for i,identifier in enumerate(self.external_ids,1)]}
            path=settings.workspace/'list.json'
            path.write_text(json.dumps(listing),encoding='utf-8')
            capture_list(settings,listing,path)
            session.checkpoint('本地列表已保存')
            for index,identifier in enumerate(self.external_ids,1):
                page=MagicMock()
                page.url='https://item.taobao.com/item.htm?id='+identifier
                page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'local-screenshot-fixture')
                detail=DetailPage(page,resolve_detail_input('https://item.taobao.com/item.htm?id='+identifier),'脱敏详情')
                guard=MagicMock()
                guard.task_id=session.task_id
                guard.perform.side_effect=lambda step,action,**kw:action()
                guard.checkpoint.side_effect=lambda step:session.checkpoint(step)
                def collect_parameters(config,result):
                    """保存带独立批次的本地参数；参数：配置、详情；返回：已保存结果。"""
                    directory=config.workspace/'output/raw/taobao_details'/result.target.item_id
                    batch=session.task_id+'-'+uuid4().hex[:8]
                    record={'product_id':result.target.item_id,'task_id':session.task_id,'batch_id':batch,'status':'complete',
                            'items':[{'index':1,'name':'品牌','value':'本地样例','status':'read'}],
                            'history_record_file':'history/'+batch+'/parameters.json'}
                    destination=directory/record['history_record_file']
                    destination.parent.mkdir(parents=True)
                    destination.write_text(json.dumps(record),encoding='utf-8')
                    save_detail_section(directory,result.target.item_id,'parameters',record)
                    return dict(record,message='本地参数已保存',record_path=str(destination))
                source={k:listing[k] for k in ['keyword','search_method','page_number','sort_label','search_options']}
                source['list_source']={'source_batch_key':listing['batch_id'],'product_index':index,'product_id':identifier}
                with patch.object(detail_service,'active_guard',return_value=guard),patch.object(detail_service,'inspect_detail_entry',return_value=detail),\
                     patch.object(detail_service,'save_session'),patch('ecommerce.modules.collection.detail_parameters.collect_parameters',collect_parameters):
                    outputs.append(detail_service.collect_detail_page(settings,object(),detail,source=source,basic_info=False,video=False,main_images=False,skus=False,
                        parameters=True,detail_images=False,review_summary=False,reviews=False,questions=False))
                self.assertIsNone(getattr(session,'detail_source',None))
                self.assertIsNone(getattr(session,'collection_run_key',None))
            return '两商品来源关联完成'
        result=run_task(settings,'example.collection.list_details',action)
        self.assertEqual(result.status,'succeeded',result.message)
        t=self.store.tables
        with self.store.engine.connect() as connection:
            rows=connection.execute(sa.select(t['collection_runs']).where(t['collection_runs'].c.task_id==result.task_id)).mappings().all()
            self.assertEqual(len(rows),2)
            self.assertEqual(len({r['entry_record_id'] for r in rows}),2)
            for run in rows:
                entry=connection.execute(sa.select(t['search_records']).where(t['search_records'].c.id==run['entry_record_id'])).mappings().one()
                self.assertEqual(entry['product_id'],run['product_id'])
                self.assertIsNotNone(entry['source_list_observation_id'])
                batch=connection.execute(sa.select(t['collection_batches']).where(t['collection_batches'].c.run_id==run['id'])).mappings().one()
                self.assertEqual((batch['product_id'],batch['search_keyword']),(run['product_id'],'脱敏样例'))
        self.assertNotEqual(outputs[0]['entry_key'],outputs[1]['entry_key'])

    def test_basic_info_stays_effective_when_same_run_sku_is_partial(self):
        """统一任务内基本信息独立入库，后续SKU部分结果不能覆盖它。"""
        from ecommerce.modules.collection.detail_report import DetailCollectionReport
        from ecommerce.modules.collection.detail_basic_info import collect_basic_info
        from ecommerce.platforms.taobao.detail import resolve_detail_input
        from ecommerce.shared.exceptions import NeedsAttention
        from unittest.mock import MagicMock
        settings=replace(self.base,collection_backend='mysql')
        def action():
            session=current_events();self.task_ids.add(session.task_id)
            fixture=create_sources(settings,session.task_id,self.external_id)
            requested={name:name in {'basic_info','skus'} for name in ('basic_info','video','main_images','skus','parameters','detail_images','review_summary','reviews','questions')}
            report=DetailCollectionReport(settings,self.external_id,session.task_id,requested)
            guard=MagicMock(task_id=session.task_id)
            guard.perform.side_effect=lambda label,callback,**kw:callback()
            guard.checkpoint.side_effect=lambda label:session.checkpoint(label)
            page=MagicMock(url='https://item.taobao.com/item.htm?id='+self.external_id);page.is_closed.return_value=False
            detail=MagicMock(page=page,target=resolve_detail_input(self.external_id))
            report.start('basic_info')
            with patch('ecommerce.modules.collection.detail_basic_info.active_guard',return_value=guard),patch(
                    'ecommerce.modules.collection.detail_basic_info.read_basic_info',return_value={'title':'独立入库标题','sales_text':'已售 20+',
                        'field_states':{'title':'provided','sales_text':'provided'}}):
                basic=collect_basic_info(settings,detail)
            report.finish('basic_info',basic,True)
            sku={k:v for k,v in fixture['records']['skus'].items() if k!='product_info'}
            sku.update(schema_version=3,scope='product_skus')
            fixture['paths']['skus'].write_text(json.dumps(sku,ensure_ascii=False),encoding='utf-8')
            report.start('skus')
            save_detail_section(fixture['root'],self.external_id,'sku_details_last_attempt',sku)
            report.finish('skus',sku,False);report.finalize('SKU部分完成')
            session.checkpoint('所有基本信息和SKU尝试已入库')
            raise NeedsAttention('SKU部分完成，基本信息已保存')
        result=run_task(settings,'example.collection.basic_then_skus',action)
        self.assertEqual(result.status,'needs_attention',result.message)
        document=detail_document(settings,self.external_id)
        self.assertEqual(document['product_info']['title'],'独立入库标题')
        self.assertEqual(document['sku_details_last_attempt']['status'],'partial')
        self.assertNotIn('product_info',document['sku_details_last_attempt'])
        self.assertNotIn('sku_details',document)

    def test_runtime_full_report_retains_sections_and_links_events(self):
        """同任务内逐模块保存后仍保留汇总、有效分区和部分SKU，事件关联真实运行。"""
        from ecommerce.modules.collection.detail_report import DetailCollectionReport
        settings=replace(self.base,collection_backend='mysql')
        def action():
            """模拟各模块已确认的本地采集结果；参数：无；返回：汇总结论。"""
            session=current_events()
            self.task_ids.add(session.task_id)
            fixture=create_sources(settings,session.task_id,self.external_id)
            report=DetailCollectionReport(settings,self.external_id,session.task_id,{name:True for name in fixture['records']})
            for module,value in fixture['records'].items():
                report.start(module)
                section='sku_details_last_attempt' if module=='skus' else module
                save_detail_section(fixture['root'],self.external_id,section,value)
                report.finish(module,value,True)
            report.finalize()
            return '模块与汇总已按真实状态保存'
        result=run_task(settings,'example.collection.full',action)
        self.assertEqual(result.status,'succeeded')
        document=detail_document(settings,self.external_id)
        self.assertEqual(document['collection_summary']['status'],'complete_with_warnings')
        self.assertEqual(len(document['collection_summary']['steps']),9)
        self.assertNotIn('sku_details',document)
        self.assertEqual(document['sku_details_last_attempt']['status'],'partial')
        events=self.store.tasks.list_events(task_id=result.task_id,limit=200)['events']
        self.assertTrue(any(row['run_id'] is not None and row['batch_id'] is not None for row in events))

    @unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','须额外启用本地浏览器守卫测试')
    def test_mysql_task_manual_callback_keeps_context_and_does_not_repeat_click(self):
        """Playwright事件回调保留当前任务，原生人工决定后只执行一次原动作。"""
        from playwright.sync_api import sync_playwright
        from ecommerce.platforms.taobao.verification import TaobaoGuard
        settings=replace(self.base,collection_backend='mysql')
        observed=[]
        def action():
            """访问本地模拟响应；参数：无；返回：已核对状态。"""
            self.task_ids.add(current_events().task_id)
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True)
                context=browser.new_context()
                context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='<span class="site-nav-login-info-nick">样例</span><button onclick="window.clicks=(window.clicks||0)+1">执行</button>'))
                page=context.new_page()
                page.goto('https://www.taobao.com/')
                page.on('console',lambda message:observed.append(current_events() is not None))
                page.evaluate("console.log('local callback')")
                self.assertEqual(observed,[True])
                def attention(message):
                    """明确决定本地原生弹窗；参数：提示；返回：确认或继续。"""
                    observed.append(current_events() is not None)
                    return '确认' if message.startswith('【需要人工决定原生弹窗】') else '继续'
                try:
                    page.evaluate("document.querySelector('button').onclick=()=>{if(confirm('local decision')) window.clicks=(window.clicks||0)+1}")
                    with TaobaoGuard(context,settings.log_dir,attention) as guard:
                        guard.perform('本地点击验证',lambda:page.get_by_role('button').click())
                        self.assertEqual(page.evaluate('window.clicks'),1)
                finally:
                    context.close()
                    browser.close()
            return '本地拦截与任务关联验证通过'
        result=run_task(settings,'collection.taobao.context.test',action)
        self.assertEqual(result.status,'succeeded')
        self.assertTrue(all(observed))
        self.assertEqual(len(observed),3)


if __name__=='__main__':
    unittest.main()
