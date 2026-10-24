"""独立验证库的事务、约束与连接故障验证；所有合成业务记录回滚。"""
from dataclasses import replace
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import logging
import re
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql import MySQLStorageError,create_mysql_engine,inspect_schema,safe_error
from ecommerce.infrastructure.storage.mysql_migrations.schema_v5 import build_metadata
from ecommerce.infrastructure.storage.time_values import BEIJING,database_now

log=logging.getLogger(__name__)


def verify_foundation(settings):
    """验证基础能力；参数：名称以 _verify 结尾的独立验证库配置；返回：无凭据报告。"""
    if not settings.database.endswith('_verify'):
        raise MySQLStorageError("合成写入验证仅允许名称以 _verify 结尾的独立验证库")
    report=inspect_schema(settings)
    if report['schema_errors']:
        raise MySQLStorageError("实际结构尚未通过核对，请先运行 database check")
    metadata=build_metadata()
    tables=metadata.tables
    engine=create_mysql_engine(settings)
    token=uuid4().hex
    checks={}
    now=database_now()

    def rejected(connection,statement,values,expected):
        """验证真实拒绝结果；参数：连接、语句、数据、错误码；返回：是否匹配。"""
        try:
            with connection.begin_nested():
                connection.execute(statement,values)
        except sa.exc.DBAPIError as error:
            return getattr(error.orig,'args',(None,))[0] in expected
        return False

    try:
        with engine.connect() as connection:
            transaction=connection.begin()
            try:
                product={'platform':'foundation_check','platform_product_id':token,'display_title':'中文读写校验🧪'}
                product_id=connection.execute(tables['products'].insert(),product).inserted_primary_key[0]
                second_id=connection.execute(tables['products'].insert(),dict(product,platform_product_id=token+'B')).inserted_primary_key[0]
                read=connection.execute(sa.select(tables['products'].c.display_title).where(tables['products'].c.id==product_id)).scalar_one()
                checks['chinese_and_emoji_roundtrip']=read==product['display_title']
                connection.execute(tables['products'].update().where(tables['products'].c.id==product_id).values(display_title='已更新的中文🧪'))
                checks['update']=connection.execute(sa.select(tables['products'].c.display_title).where(tables['products'].c.id==product_id)).scalar_one()=='已更新的中文🧪'
                checks['unique_identity_rejected']=rejected(connection,tables['products'].insert(),product,{1062})
                task={'id':token,'name':'foundation.validation','status':'running','started_at':now,'origin':'live'}
                connection.execute(tables['tasks'].insert(),task)
                run_id=connection.execute(tables['collection_runs'].insert(),{'source_run_key':token+'-summary','task_id':token,'product_id':product_id,'status':'running'}).inserted_primary_key[0]
                step_id=connection.execute(tables['collection_steps'].insert(),{'run_id':run_id,'module':'skus','requested':True,'status':'running'}).inserted_primary_key[0]
                scope=hashlib.sha256(b'foundation').hexdigest()
                batch={'platform':'foundation_check','source_batch_key':token+'-batch','task_id':token,'run_id':run_id,'step_id':step_id,
                       'product_id':product_id,'module':'skus','status':'complete','scope_key':scope,'coverage_status':'unknown'}
                batch_id=connection.execute(tables['collection_batches'].insert(),batch).inserted_primary_key[0]
                checks['batch_run_product_mismatch_rejected']=rejected(connection,tables['collection_batches'].insert(),dict(batch,source_batch_key=token+'-wrong',product_id=second_id),{1452})
                sku={'batch_id':batch_id,'position':1,'sku_name':'中文规格','price_value':Decimal('13.800001'),'price_status':'collected','combination_confirmed':True,
                     'properties':[{'group':'颜色','name':'蓝色','value_id':'1'}]}
                connection.execute(tables['sku_observations'].insert(),sku)
                value=connection.execute(sa.select(tables['sku_observations'].c.price_value,tables['sku_observations'].c.properties).where(tables['sku_observations'].c.batch_id==batch_id)).one()
                checks['decimal_and_json_roundtrip']=value[0]==sku['price_value'] and value[1]==sku['properties']
                checks['wrong_module_rejected']=rejected(connection,tables['parameter_observations'].insert(),{'batch_id':batch_id,'position':1,'name':'参数','value':'合成值','status':'read'},{1452})
                pointer={'product_id':second_id,'section':'sku_details','scope_key':scope,'module':'skus','batch_id':batch_id,'publication_basis':'validation_only'}
                checks['current_pointer_wrong_product_rejected']=rejected(connection,tables['product_current_sections'].insert(),pointer,{1452})
                media={'reference_key':scope,'role':'screenshot','position':1,'download_status':'link_only'}
                checks['media_without_owner_rejected']=rejected(connection,tables['media_references'].insert(),media,{3819})
                checks['parent_delete_restricted']=rejected(connection,tables['products'].delete().where(tables['products'].c.id==product_id),{}, {1451})
                event={'task_id':token,'task_sequence':1,'event_type':'validation','message':'合成事件','recorded_at':now,'time_basis':'utc','origin':'live','event_key':token+'-event'}
                connection.execute(tables['task_events'].insert(),event)
                checks['event_duplicate_rejected']=rejected(connection,tables['task_events'].insert(),event,{1062})
                checks['event_sequence_duplicate_rejected']=rejected(connection,tables['task_events'].insert(),dict(event,event_key=token+'-another'),{1062})
                checks['beijing_session']=connection.execute(sa.text("SELECT @@SESSION.time_zone")).scalar_one()=='+08:00'
                removed=connection.execute(tables['products'].delete().where(tables['products'].c.id==second_id))
                checks['delete']=removed.rowcount==1
            finally:
                transaction.rollback()
        with engine.connect() as connection:
            checks['transaction_rollback']=connection.execute(sa.select(sa.func.count()).select_from(tables['products']).where(tables['products'].c.platform_product_id==token)).scalar_one()==0
            privileges=[]
            for grant in report['grants']:
                match=re.match(r'GRANT (.*?) ON ',grant)
                if match:
                    privileges.extend(p.strip() for p in match[1].split(','))
            checks['runtime_privileges_limited']=set(privileges)<= {'USAGE','SELECT','INSERT','UPDATE','DELETE'} and {'SELECT','INSERT','UPDATE','DELETE'}<=set(privileges)
        failed_engine=create_mysql_engine(replace(settings,port=1,connect_timeout=1))
        try:
            with failed_engine.connect() as connection:
                connection.execute(sa.text('SELECT 1'))
            checks['connection_failure_reported']=False
        except sa.exc.SQLAlchemyError as error:
            log.warning("MySQL 断库模拟（预期失败，本地日志继续记录）：%s",safe_error(error))
            checks['connection_failure_reported']=True
        finally:
            failed_engine.dispose()
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
    finally:
        engine.dispose()
    return {'checked_at':datetime.now(BEIJING).isoformat(),'database':settings.database,'version':report['version'],
            'revision':report['revision'],'business_table_count':report['business_table_count'],'table_count':report['table_count'],
            'column_count':report['column_count'],'foreign_key_count':report['foreign_key_count'],
            'all_comments_present':report['all_comments_present'],'checks':checks,
            'passed':all(checks.values()),'synthetic_records_rolled_back':checks.get('transaction_rollback',False),
            'schema':report}
