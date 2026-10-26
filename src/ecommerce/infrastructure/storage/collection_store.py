"""采集观察、媒体与发布指针的事务写入；文件快照只用于补写数据。"""
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import re

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as mysql_insert

from ecommerce.infrastructure.storage.collection_snapshot import open_snapshot
from ecommerce.infrastructure.storage.mysql import MySQLStorageError,safe_error
from ecommerce.infrastructure.storage.task_store import task_store,fingerprint,database_time,iso_value
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.storage.mysql_migrations.search_origin_v3 import enrich_origin

CHILDREN={'product_info_observations','list_observations','sku_observations','parameter_observations','review_summaries',
    'review_observations','question_observations','answer_observations','media_references','collection_checkpoints'}
PRUNE_ORDER=['media_references','collection_checkpoints','answer_observations','question_observations',
             'review_observations','sku_observations','parameter_observations','list_observations','review_summaries','product_info_observations']
SECTIONS={'product_info':'basic_info','sku_details':'skus',**{name:name for name in
    ['video','main_images','parameters','detail_images','review_summary','reviews','questions']}}


class CollectionDataError(MySQLStorageError):
    """内容或归属冲突不可通过重试解决，保留快照并停止新的业务动作。"""
    retryable=False


def now():
    """生成数据库北京时间；参数：无；返回：无时区时间。"""
    return database_now()


def database_values(table,values):
    """转换受控列类型；参数：表及值；返回：DBAPI 兼容副本，不允许未知列。"""
    if set(values)-set(table.c.keys()):
        raise CollectionDataError('采集快照含未支持的字段')
    converted=dict(values)
    for key,value in converted.items():
        column=table.c[key]
        if isinstance(column.type,sa.DateTime):
            # 字符串必须自带时区；内部 now()/数据库反射的无时区 datetime 已是北京时间。
            converted[key]=value if isinstance(value,datetime) and value.tzinfo is None else database_time(value)
        elif isinstance(column.type,sa.Numeric) and value is not None:
            converted[key]=Decimal(str(value))
            if not converted[key].is_finite():
                raise CollectionDataError('采集数值不是有限数：'+table.name+'.'+key)
        elif isinstance(column.type,sa.Integer) and value is not None:
            if isinstance(value,bool) or Decimal(str(value))!=int(value):
                raise CollectionDataError('采集整数列含非整数值')
            converted[key]=int(value)
        elif isinstance(column.type,sa.String) and value is not None:
            if not isinstance(value,str) or (column.type.length is not None and len(value)>column.type.length):
                raise CollectionDataError('采集文本超出字段容量或类型不符：'+table.name+'.'+key)
            if isinstance(column.type,sa.Text) and len(value.encode('utf-8'))>65535:
                raise CollectionDataError('采集文本超出字段容量：'+table.name+'.'+key)
            if column.type.collation=='ascii_bin' and not value.isascii():
                raise CollectionDataError('采集代码字段不是受支持的英文原值：'+table.name+'.'+key)
    return converted


class CollectionWriter:
    """在同一事务内写入实时采集快照并核对归属，支持幂等补写。"""
    def __init__(self,settings,connection):
        self.settings,self.connection=settings,connection
        self.tables=task_store(settings.mysql).tables
        self.issues=[]
        self.root_id=None
        self.batch_id=None
        self.task_id=None
        self.ids={}
        self.digest=None
        self.mutable=False

    def _select(self,table,key):
        """按明确唯一键取行；参数：表及键；返回：行或空。"""
        statement=sa.select(table).where(*[table.c[k]==v for k,v in key.items()])
        statement=statement.with_for_update()
        return self.connection.execute(statement).mappings().one_or_none()


    def _upsert(self,table,key,values,*,mutable=False):
        """保留内部主键并核对不可变内容；参数：表、键、列值及可变标记；返回：内部编号或批次键。"""
        values=database_values(table,values)
        old=self._select(table,key)
        sales_bootstrap=False
        if table.name=='list_observations':
            from ecommerce.shared.sales import FIELDS,parse_sales
            supplied=set(FIELDS)&set(values)
            if supplied and supplied!=set(FIELDS):
                raise CollectionDataError('列表销量快照字段不完整')
            if not supplied:
                sales_bootstrap=bool(old and old['sales_parse_status']=='pending' and old['sales_text']==values.get('sales_text'))
                # 旧不可变快照不含结构化销量：原文相同时保留已回填结果，不清空、不修改旧快照指纹。
                sales=({name:old[name] for name in FIELDS} if old and old['sales_text']==values.get('sales_text')
                       and old['sales_parse_status']!='pending' else parse_sales(values.get('sales_text')))
                values.update(database_values(table,sales))
        if old is None:
            pk=self.connection.execute(table.insert().values(**values)).inserted_primary_key[0]
        else:
            changed=any(old[k]!=v for k,v in values.items())
            bootstrap_only=sales_bootstrap and all(old[k]==v for k,v in values.items() if k not in FIELDS)
            if changed and not mutable and not bootstrap_only:
                raise CollectionDataError('采集记录与已发布内容冲突：'+table.name)
            if changed:
                self.connection.execute(table.update().where(*[table.c[k]==v for k,v in key.items()]).values(**values))
            pk=old['id'] if 'id' in table.c else old['batch_id']
        return pk

    def _product(self,external):
        """获取已核对的商品身份；参数：平台原编号；返回：内部编号或空。"""
        if external is None:
            return None
        if not isinstance(external,str) or not re.fullmatch(r'[0-9]{1,25}',external) or int(external)==0:
            raise CollectionDataError('采集商品身份无效')
        table=self.tables['products']
        key={'platform':'taobao','platform_product_id':external}
        old=self._select(table,key)
        if old is None:
            statement=mysql_insert(table).values(**key).on_duplicate_key_update(id=sa.func.last_insert_id(table.c.id))
            pk=self.connection.execute(statement).inserted_primary_key[0]
        else:
            pk=old['id']
        return pk

    def _file(self,record,source_key):
        """核对文件身份与已知内容；参数：规范文件及引用键；返回：文件内部编号。"""
        table=self.tables['file_assets']
        relative=record['relative_path']
        if relative.startswith('/') or '..' in relative.split('/') or ':' in relative:
            raise CollectionDataError('文件索引不是工作目录内的相对路径')
        key={'root_key':'workspace','path_key':hashlib.sha256(relative.casefold().encode('utf-8')).hexdigest()}
        values={k:record.get(k) for k in ['relative_path','size_bytes','sha256','content_type','width','height','duration_seconds','availability']}
        old=self._select(table,key)
        if old and old['relative_path'].casefold()!=relative.casefold():
            raise CollectionDataError('文件规范路径键冲突，未合并不同文件')
        if old and any(old[k]!=v for k,v in database_values(table,values).items()):
            # 当前任务的工作文件可更新；已结束批次引用的原件不能被静默改写。
            references=self.tables['media_references']
            other=self.connection.scalar(sa.select(sa.func.count()).select_from(references).where(
                references.c.file_asset_id==old['id'],sa.or_(references.c.batch_id!=self.batch_id,
                    references.c.batch_id.is_(None) if self.batch_id else references.c.task_id!=self.task_id)))
            if not self.mutable or other:
                raise CollectionDataError('已登记历史文件的属性改变，未覆盖原索引')
        if record['availability']!='present':
            self.issues.append({'severity':'warning','code':'missing_file','message':'文件不存在：'+relative})
        values['verified_at']=old['verified_at'] if old else now()
        pk=self._upsert(table,key,{**key,**values},mutable=self.mutable)
        # 同一文件可被多处来源引用，额外的引用位置由媒体映射标识。
        return pk

    def _owner_task(self,identifier):
        """检查任务引用；参数：原任务编号；返回：已存在编号或空，缺失时登记原因。"""
        if not identifier:
            return None
        if not re.fullmatch('[a-f0-9]{32}',identifier):
            raise CollectionDataError('采集来源任务编号格式无效')
        exists=self.connection.scalar(sa.select(self.tables['tasks'].c.id).where(self.tables['tasks'].c.id==identifier))
        if exists is None:
            self.issues.append({'severity':'warning','code':'unknown_task','message':'来源任务不存在，原编号保留于源快照'})
        return exists

    def _list_observation(self,header,product):
        """解析可移植列表来源键；参数：快照头、商品内部编号；返回：经归属核对的观察编号。"""
        source=header['list_source']
        if (not isinstance(source,dict) or set(source)!={'source_batch_key','product_index','product_id'} or
            type(source['product_index']) is not int or source['product_index']<1 or source['product_id']!=header['product_id']):
            raise CollectionDataError('详情入口的列表来源格式或商品编号不一致')
        if header['values']['record_type']!='detail_open':
            raise CollectionDataError('只有详情入口可以关联列表观察')
        batch=self._select(self.tables['collection_batches'],{'platform':'taobao','module':'list','source_batch_key':source['source_batch_key']})
        if (batch is None or batch['task_id']!=self.task_id or batch['visibility']!='published' or
            (batch['result_payload'].get('record') or {}).get('task_id')!=header.get('task_id')):
            raise CollectionDataError('详情入口的来源列表批次不存在、未发布或不属于同一任务')
        observation=self._select(self.tables['list_observations'],{'batch_id':batch['id'],'product_index':source['product_index']})
        if observation is None or observation['product_id']!=product:
            raise CollectionDataError('详情入口与来源列表观察的商品归属不一致')
        record=header['values']['payload']['record']
        for key,expected in [('keyword',batch['search_keyword']),('search_method',batch['search_method']),
                             ('page_number',batch['page_number']),('search_options',batch['search_options'])]:
            if record.get(key)!=expected:
                raise CollectionDataError('详情入口与列表搜索来源冲突：'+key)
        return observation['id']

    def _entry_record(self,header,product):
        """解析详情入口稳定键；参数：快照头、商品内部编号；返回：同任务同商品入口编号。"""
        table=self.tables['search_records']
        rows=self.connection.execute(sa.select(table).where(table.c.task_id==self.task_id,table.c.product_id==product,
            table.c.record_type=='detail_open',table.c.payload['record']['entry_key'].as_string()==header['entry_key'])).mappings().all()
        if len(rows)!=1:
            raise CollectionDataError('运行汇总的详情入口不存在或稳定键不唯一')
        entry=rows[0]
        if entry['record_type']!='detail_open' or entry['product_id']!=product or entry['task_id']!=self.task_id:
            raise CollectionDataError('运行汇总的详情入口类型、任务或商品不一致')
        return entry['id']

    def apply(self,reference,context=None):
        """在调用方事务内应用快照；参数：引用和发布依据；返回：根归属、计数及问题。"""
        context=context or {}
        header,records=open_snapshot(self.settings,reference)
        missing=False
        actual_records=0
        for item in records:
            actual_records+=1
            missing=missing or (item.get('file') or {}).get('availability')=='missing'
        if actual_records!=reference['record_count']:
            raise CollectionDataError('采集快照行数与清单不一致')
        header,records=open_snapshot(self.settings,reference)
        self.digest=reference['sha256']
        self.task_id=self._owner_task(header.get('task_id'))
        product=self._product(header.get('product_id'))
        kind=header['kind']
        values=dict(header['values'])
        if missing and kind=='batch':
            values['visibility']='quarantined'
            context={**context,'sections':[]}
        values['task_id']=self.task_id
        values['product_id']=product
        if header.get('search_image'):
            values['search_image_file_id']=self._file(header['search_image'],{'role':'search_input_image'})
        if kind=='batch':
            table=self.tables['collection_batches']
            key={k:values[k] for k in ['platform','module','source_batch_key']}
            old=self._select(table,key)
            if old and (old['product_id']!=product or old['task_id']!=self.task_id):
                raise CollectionDataError('同批次编号的商品或任务归属冲突')
            try:
                values=enrich_origin(table.name,values,old)
            except ValueError as error:
                raise CollectionDataError(str(error)) from None
            if values['search_method']=='image' and values['search_image_file_id'] is None:
                raise CollectionDataError('图片搜索缺少实际输入图片文件')
            old_payload=(old['result_payload'] or {}) if old else {}
            self.mutable=old_payload.get('_current_sections') is None
            payload={**values['result_payload'],'_fingerprint':self.digest}
            for control in ['_current_sections','_export_sections','_last_attempt_selected']:
                if control in old_payload:
                    payload[control]=old_payload[control]
            values['result_payload']=payload
            values['run_id']=values['step_id']=None
            if context.get('report_key'):
                run=self._select(self.tables['collection_runs'],{'source_run_key':context['report_key']})
                if not run or run['product_id']!=product or run['task_id']!=self.task_id:
                    raise CollectionDataError('模块批次与运行汇总归属不一致')
                if run['entry_record_id'] is not None:
                    entry=self._select(self.tables['search_records'],{'id':run['entry_record_id']})
                    origin=(entry['payload'] or {}).get('record') or {}
                    expected={'search_method':entry['search_method'],'search_keyword':entry['keyword'],
                              'page_number':entry['page_number'],'search_image_file_id':entry['search_image_file_id'],
                              'search_options':origin.get('search_options')}
                    if any(values.get(k)!=v for k,v in expected.items()):
                        raise CollectionDataError('模块批次与详情入口的搜索来源不一致')
                step=self._select(self.tables['collection_steps'],{'run_id':run['id'],'module':values['module']})
                if step is None:
                    raise CollectionDataError('模块批次的汇总步骤不存在')
                values.update(run_id=run['id'],step_id=step['id'])
            parent_key=values.get('upstream_batch_key')
            if parent_key:
                if parent_key==values['source_batch_key']:
                    raise CollectionDataError('采集批次不能派生自自身')
                parent=self._select(table,{'platform':values['platform'],'module':values['module'],'source_batch_key':parent_key})
                if parent:
                    if parent['product_id']!=product:
                        raise CollectionDataError('派生批次与来源商品不一致')
                    ancestor=parent
                    visited=set()
                    while ancestor:
                        if ancestor['source_batch_key']==values['source_batch_key'] or ancestor['id'] in visited:
                            raise CollectionDataError('采集派生关系出现循环，未保存')
                        visited.add(ancestor['id'])
                        ancestor=self._select(table,{'id':ancestor['derived_from_batch_id']}) if ancestor['derived_from_batch_id'] else None
                    values['derived_from_batch_id']=parent['id']
                    if values.get('derivation_kind')=='manual_confirmation':
                        values['observed_at']=iso_value(parent['observed_at'])
                else:
                    self.issues.append({'severity':'warning','code':'missing_upstream_batch','message':'来源批次缺失，外部编号已保留'})
        elif kind=='report':
            table=self.tables['collection_runs']
            key={'source_run_key':values['source_run_key']}
            old=self._select(table,key)
            if old and (old['task_id']!=self.task_id or old['product_id']!=product):
                raise CollectionDataError('运行汇总编号与任务或商品冲突')
            self.mutable=old is None or old['finished_at'] is None
            values['entry_record_id']=self._entry_record(header,product) if header.get('entry_key') else (old['entry_record_id'] if old else None)
            if old and old['entry_record_id'] is not None and values['entry_record_id']!=old['entry_record_id']:
                raise CollectionDataError('运行汇总的详情入口关联冲突')
        elif kind=='page':
            table=self.tables['search_records']
            statement=sa.select(table).where(table.c.payload['_logical_key'].as_string()==header['logical_key'])
            old=self.connection.execute(statement).mappings().one_or_none()
            key={'id':old['id']} if old else None
            values['source_list_observation_id']=(self._list_observation(header,product) if header.get('list_source') else
                                                   (old['source_list_observation_id'] if old else None))
            try:
                values=enrich_origin(table.name,values,old)
            except ValueError as error:
                raise CollectionDataError(str(error)) from None
            if values['search_method']=='image' and values['search_image_file_id'] is None:
                raise CollectionDataError('图片搜索缺少实际输入图片文件')
            values['payload']={**values['payload'],'_logical_key':header['logical_key'],'_fingerprint':self.digest}
        else:
            raise CollectionDataError('采集快照根类型不支持')
        if kind=='page' and old is None:
            self.root_id=self.connection.execute(table.insert().values(**database_values(table,values))).inserted_primary_key[0]
        else:
            self.root_id=self._upsert(table,key,values,mutable=self.mutable)
        self.batch_id=self.root_id if kind=='batch' else None
        counts={}
        try:
            for record in records:
                name=record['table']
                if name not in CHILDREN|{'collection_steps'}:
                    raise CollectionDataError('快照子表不在允许范围')
                child=self.tables[name]
                child_key=dict(record['key'])
                child_values=dict(record['values'])
                if name=='collection_steps':
                    if kind!='report':
                        raise CollectionDataError('模块步骤必须属于运行汇总')
                    child_key['run_id']=self.root_id
                    child_values['run_id']=self.root_id
                elif name=='media_references':
                    owner=record.get('owner')
                    identity=[self.batch_id,header['logical_key'],record['key']]
                    child_key={'reference_key':fingerprint(identity)}
                    child_values.update(reference_key=child_key['reference_key'],task_id=self.task_id,batch_id=self.batch_id)
                    if not self.task_id and not self.batch_id:
                        self.issues.append({'severity':'warning','code':'unlinked_page_media','message':'页面证据无可靠任务归属，原文件仍登记于页面来源'})
                        continue
                    if owner:
                        owner_name=owner['table']
                        owner_key={k:v for k,v in owner.items() if k!='table'}
                        identifier=self.ids.get((owner_name,fingerprint(owner_key)))
                        owner_fields={'sku_observations':'sku_observation_id','review_observations':'review_observation_id',
                                      'question_observations':'question_observation_id','answer_observations':'answer_observation_id'}
                        if owner_name not in owner_fields or identifier is None:
                            raise CollectionDataError('媒体所属明细尚未登记或归属不符')
                        child_values[owner_fields[owner_name]]=identifier
                    if record.get('file'):
                        child_values['file_asset_id']=self._file(record['file'],record['key'])
                else:
                    if self.batch_id is None:
                        raise CollectionDataError('采集明细缺少所属批次')
                    child_values['batch_id']=self.batch_id
                    child_key['batch_id']=self.batch_id
                    if 'module' in child.c:
                        child_values['module']=header['module']
                    if name=='product_info_observations':
                        child_values['product_id']=product
                    if name=='list_observations':
                        child_values['product_id']=self._product(record.get('product_id'))
                    if name=='answer_observations':
                        question_position=child_key.pop('question_position')
                        question_id=self.ids.get(('question_observations',fingerprint({'position':question_position})))
                        if question_id is None:
                            raise CollectionDataError('回答所属问题未登记')
                        child_key['question_id']=question_id
                        child_values['question_id']=question_id
                if 'payload' in child.c and kind=='batch':
                    child_values['payload']={**(child_values.get('payload') or {}),'_snapshot_sha':self.digest}
                if kind=='page':
                    child_values['payload']={**(child_values.get('payload') or {}),'page_logical_key':header['logical_key']}
                pk=self._upsert(child,child_key,child_values,mutable=self.mutable)
                if name in {'sku_observations','review_observations','question_observations','answer_observations'}:
                    self.ids={k:v for k,v in self.ids.items() if k[0]!=name and not (name=='question_observations' and k[0]=='answer_observations')}
                    self.ids[(name,fingerprint(record['key']))]=pk
                counts[name]=counts.get(name,0)+1
        finally:
            records.close()
        if kind=='batch':
            if self.mutable:
                for name in PRUNE_ORDER:
                    child=self.tables[name]
                    self.connection.execute(child.delete().where(child.c.batch_id==self.batch_id,
                        sa.or_(child.c.payload['_snapshot_sha'].as_string()!=self.digest,child.c.payload['_snapshot_sha'].as_string().is_(None))))
            for name in counts:
                actual=self.connection.scalar(sa.select(sa.func.count()).select_from(self.tables[name]).where(self.tables[name].c.batch_id==self.batch_id))
                if actual!=counts[name]:
                    raise CollectionDataError('模块快照包含重复身份或未替换旧明细：'+name)
            if self.issues and any(issue['code']=='missing_file' for issue in self.issues):
                self.connection.execute(table.update().where(table.c.id==self.batch_id).values(visibility='quarantined'))
                context={**context,'sections':[]}
            self._publish(header,values,context,product)
            self._title(header,values,product)
            if header['module']=='skus' and (values.get('schema_version') or 0)<=2:
                from ecommerce.infrastructure.storage.mysql_migrations.basic_info_v7 import split_legacy_info
                split_legacy_info(self.connection,self.tables,batch_id=self.batch_id)
        return {'table':table.name,'id':self.root_id,'task_id':self.task_id,'batch_id':self.batch_id,'counts':counts,
                'issues':self.issues,'logical_key':header['logical_key'],
                'run_id':self.root_id if kind=='report' else values.get('run_id') if kind=='batch' else None}

    def _publish(self,header,values,context,product):
        """核对完整性并更新对应范围指针；参数：头、根值、发布依据及商品；返回：无。"""
        sections=context.get('sections',[])
        if header['module']=='skus' and (values.get('schema_version') or 0)<=2:
            sections=[section for section in sections if section!='product_info']
        status=values['status']
        if sections and (product is None or values['visibility']!='published' or status not in
            {'complete','downloaded','no_video','no_images','no_parameters','no_reviews','no_questions'}):
            raise CollectionDataError('未通过完整性核对的批次不能进入当前有效分区')
        if sections and header['module']=='basic_info':
            from ecommerce.shared.basic_info import assessment
            info=self.connection.execute(sa.select(self.tables['product_info_observations']).where(
                self.tables['product_info_observations'].c.batch_id==self.batch_id)).mappings().one_or_none()
            if info is None or assessment(info,info['source_url'],info['captured_at'])['status']!='complete':
                raise CollectionDataError('基本信息观察尚未通过独立完整性检查')
        for section in sections:
            if SECTIONS.get(section)!=header['module']:
                raise CollectionDataError('发布分区与采集模块不匹配')
            key={'product_id':product,'section':section,'scope_key':values['scope_key']}
            table=self.tables['product_current_sections']
            previous=self._select(table,key)
            data={**key,'module':header['module'],'batch_id':self.batch_id,
                  'published_at':now(),
                  'publication_basis':'module_completeness_checked'}
            if previous:
                self.connection.execute(table.update().where(*[table.c[k]==v for k,v in key.items()]).values(**data))
            else:
                self.connection.execute(table.insert().values(**data))
        if sections or context.get('last_attempt'):
            table=self.tables['collection_batches']
            selected=self.connection.execute(sa.select(table.c.id,table.c.result_payload).where(
                table.c.product_id==product,table.c.module==header['module'],table.c.id!=self.batch_id,
                sa.or_(table.c.result_payload['_last_attempt_selected'].as_boolean()==True,
                       sa.func.JSON_LENGTH(table.c.result_payload['_export_sections'])>0))).mappings().all()
            for previous in selected:
                old_payload=dict(previous['result_payload'] or {})
                if context.get('last_attempt'):
                    old_payload['_last_attempt_selected']=False
                if sections:
                    old_payload['_export_sections']=[s for s in old_payload.get('_export_sections',[]) if s not in sections]
                self.connection.execute(table.update().where(table.c.id==previous['id']).values(result_payload=old_payload))
            payload=dict(self.connection.scalar(sa.select(table.c.result_payload).where(table.c.id==self.batch_id)) or {})
            if sections:
                payload['_current_sections']=sections
                payload['_export_sections']=sections
            if context.get('last_attempt'):
                payload['_last_attempt_selected']=True
            self.connection.execute(table.update().where(table.c.id==self.batch_id).values(result_payload=payload))

    def _title(self,header,values,product):
        """维护带来源的显示标题；参数：根、值和商品；返回：无，不更改历史观察原文。"""
        products=self.tables['products']
        candidates=[]
        if header['module']=='list':
            listing=self.tables['list_observations']
            candidates=self.connection.execute(sa.select(listing.c.product_id,listing.c.title).where(listing.c.batch_id==self.batch_id,listing.c.product_id.is_not(None)).order_by(listing.c.slot_index)).all()
        elif header['module']=='basic_info' and product:
            title=(values['result_payload']['record'].get('product_info') or {}).get('title')
            if title:
                candidates=[(product,title)]
        seen=set()
        for identifier,title in candidates:
            if identifier in seen:
                continue
            seen.add(identifier)
            previous=self._select(products,{'id':identifier})
            previous_batch=self._select(self.tables['collection_batches'],{'id':previous['title_source_batch_id']}) if previous['title_source_batch_id'] else None
            incoming=database_time(values.get('captured_at'))
            newer=previous_batch and incoming and previous_batch['captured_at'] and incoming>previous_batch['captured_at']
            if not previous['display_title'] or newer:
                self.connection.execute(products.update().where(products.c.id==identifier).values(display_title=title,title_source_batch_id=self.batch_id))


class CollectionStore:
    """复用任务连接池的采集事务入口；只处理规范快照和只读查询。"""
    def __init__(self,settings):
        self.settings=settings
        self.tasks=task_store(settings.mysql)
        self.engine=self.tasks.engine
        self.tables=self.tasks.tables

    def apply_operation(self,operation):
        """原子保存采集与对应事件；参数：已落盘操作；返回：任务序号。"""
        if operation.get('database')!=self.settings.mysql.database:
            raise CollectionDataError('采集流水目标数据库不匹配')
        tasks,events=self.tables['tasks'],self.tables['task_events']
        digest=fingerprint(operation)
        try:
            with self.engine.begin() as connection:
                task=connection.execute(sa.select(tasks).where(tasks.c.id==operation['task_id']).with_for_update()).mappings().one_or_none()
                if not task or task['origin']!='live':
                    raise CollectionDataError('实时采集写入没有已确认的当前任务')
                existing=connection.execute(sa.select(events.c.task_sequence,events.c.details).where(events.c.event_key==operation['event_key'])).mappings().one_or_none()
                if existing:
                    if (existing['details'] or {}).get('operation_fingerprint')!=digest:
                        raise CollectionDataError('采集流水事件键内容冲突')
                    return existing['task_sequence']
                if task['finished_at'] is not None:
                    raise CollectionDataError('已经结束的任务不接受新的采集写入')
                header,iterator=open_snapshot(self.settings,operation['snapshot'])
                iterator.close()
                if header.get('task_id')!=task['id']:
                    raise CollectionDataError('采集快照与执行任务不一致')
                result=CollectionWriter(self.settings,connection).apply(operation['snapshot'],operation.get('context'))
                sequence=task['last_event_sequence']+1
                connection.execute(events.insert().values(task_id=task['id'],task_sequence=sequence,
                    event_type='collection_saved',level='WARNING' if result['issues'] else 'INFO',step=header.get('module'),
                    message='采集观察已保存'+('；有文件或关联问题待核对' if result['issues'] else ''),
                    occurred_at=database_time(operation['time']),recorded_at=now(),time_raw=operation['time'],time_basis='explicit_offset',
                    details={'operation_fingerprint':digest,'snapshot_sha256':operation['snapshot']['sha256'],
                             'target':result['table'],'target_id':result['id'],'issues':result['issues']},
                    origin='live',event_key=operation['event_key'],batch_id=result['batch_id'],run_id=result['run_id']))
                connection.execute(tasks.update().where(tasks.c.id==task['id']).values(last_event_sequence=sequence))
                return sequence
        except sa.exc.SQLAlchemyError as error:
            arguments=getattr(getattr(error,'orig',None),'args',())
            if arguments and arguments[0] in {1062,1451,1452,3819,1406,1366,1264,1292,1048,1054,1146}:
                raise CollectionDataError(safe_error(error)) from None
            raise MySQLStorageError(safe_error(error)) from None


def collection_store(settings):
    """建立轻量采集入口并复用已有连接池；参数：配置；返回：采集存储。"""
    return CollectionStore(settings)
