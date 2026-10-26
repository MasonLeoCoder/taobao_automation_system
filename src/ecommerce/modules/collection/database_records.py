"""把现有采集结果转换为数据库观察记录，保留来源身份、范围与原状态。"""
from collections import Counter
from contextlib import closing
from decimal import Decimal,InvalidOperation
import json
import mimetypes
from pathlib import Path
import re
import sqlite3

from ecommerce.infrastructure.storage.collection_snapshot import file_record,json_array,save_snapshot,sqlite_records
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.collection_codes import stop_reason_fields

MODULE_FILES={'basic_info':'basic_info.json','video':'video.json','main_images':'main_images.json','skus':'sku_details.json',
    'parameters':'parameters.json','detail_images':'detail_images.json','review_summary':'review_summary.json',
    'reviews':'reviews.json','questions':'questions.json'}
SECTIONS={'product_info':'basic_info','sku_details':'skus',**{name:name for name in MODULE_FILES if name!='skus'}}
RUNNING={'running','in_progress','collecting','pending','detected'}


def product_id(value):
    """校验平台商品编号；参数：原值；返回：保留前导零的字符串或空。"""
    if value is None:
        return None
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,25}',value) or int(value)==0:
        raise ValueError('商品编号必须为有效的原数字字符串')
    return value


def decimal_value(value):
    """保留价格精度；参数：数值或文本；返回：六位内十进制字符串或空，不截断。"""
    if value is None:
        return None
    try:
        parsed=Decimal(str(value))
        if not parsed.is_finite() or parsed<0 or parsed.as_tuple().exponent < -6 or parsed>=Decimal('100000000000000'):
            raise ValueError('价格超出当前数据库精确存储范围')
        return str(parsed)
    except InvalidOperation:
        raise ValueError('价格不是可确认的数值') from None


def scope_contract(module,record):
    """生成可复查的范围身份；参数：模块和原记录；返回：范围键、依据及覆盖状态。"""
    fields=('scope','mode','requested_limit','answer_limit','filter','sort','keyword','page_number','sort_label')
    contract={'version':1,'module':module,**{key:record[key] if key in record else {'missing':True} for key in fields}}
    if 'search_method' in record:
        image=record.get('search_image') or {}
        image_sha=image.get('sha256')
        if record['search_method']=='image' and (not isinstance(image_sha,str) or not re.fullmatch('[a-f0-9]{64}',image_sha)):
            raise ValueError('图片搜索范围缺少已核对的输入图片SHA-256')
        contract.update(version=2,search_method=record['search_method'],search_image_sha256=image_sha,
                        search_options=record.get('search_options'))
    status=record.get('video_status',record.get('status',record.get('collection_status')))
    reason=record.get('stop_reason')
    if status in {'no_video','no_images','no_parameters','no_reviews','no_questions','empty'}:
        coverage='confirmed_empty'
    elif reason=='manual_confirmation':
        coverage='manual_count_confirmed'
    elif reason=='limit_reached' or status=='limit_reached':
        coverage='limit_reached'
    elif record.get('end_confirmed') is True:
        coverage='visible_end_confirmed'
    elif status in {'partial','failed','interrupted','needs_attention'}:
        coverage='incomplete'
    else:
        coverage='unknown'
    return fingerprint(contract),contract,coverage


def row(table,key,values,**links):
    """构造规范化观察；参数：受控表、逻辑键、列值及归属；返回：快照行。"""
    return {'table':table,'key':key,'values':values,**links}


def asset(settings,path,metadata=None):
    """核对业务文件；参数：配置、路径及原校验值；返回：文件索引。"""
    metadata=metadata or {}
    return file_record(settings.workspace,path,expected_sha=metadata.get('sha256'),expected_size=metadata.get('size_bytes'),
        content_type=metadata.get('content_type') or mimetypes.guess_type(str(path))[0],
        width=metadata.get('width'),height=metadata.get('height'),duration_seconds=metadata.get('duration_seconds'))


def media(role,position,*,url=None,file=None,owner=None,payload=None,status=None):
    """构造媒体或证据引用；参数：用途、序号、链接、文件和明细归属；返回：快照行。"""
    return row('media_references',{'role':role,'position':position,'owner':owner},
        {'role':role,'position':position,'source_url':url,'download_status':status or ('downloaded' if file else 'link_only'),
         'payload':payload},file=file,owner=owner)


def _links(value,owner,prefix):
    """保留评价问答链接及追评归属；参数：原值、父观察键、用途前缀；返回：媒体迭代。"""
    for field,suffix in [('image_urls','image'),('video_urls','video'),('video_poster_urls','poster')]:
        for position,url in enumerate(value.get(field,[]),1):
            yield media(prefix+'_'+suffix,position,url=url,owner=owner)
    for append_index,append in enumerate(value.get('append_reviews',[]),1):
        for field,suffix in [('image_urls','image'),('video_urls','video'),('video_poster_urls','poster')]:
            for position,url in enumerate(append.get(field,[]),1):
                yield media('append_'+suffix,append_index*1000000+position,url=url,owner=owner,
                    payload={'append_index':append_index})


def _identity(value,kind):
    """映射保守身份；参数：原明细和类型；返回：身份字段，不升级指纹为精确编号。"""
    exact=value.get('identity_exact') is True
    identifier=value.get(kind+'_id')
    if exact and not identifier:
        raise ValueError('精确明细身份缺少平台编号')
    return {'position':value['index'],kind+'_key':value[kind+'_key'],'platform_'+kind+'_id':identifier,
            'identity_method':value['identity_method'],'identity_exact':exact,'author':value.get('author'),
            'date_raw':value.get('date_raw')}


def _long_rows(settings,module,record,directory):
    """流式比对 JSON 与工作库；参数：配置、模块、元数据、历史目录；返回：明细与检查点。"""
    kind='reviews' if module=='reviews' else 'questions'
    filename='评价明细.json' if module=='reviews' else '问答明细.json'
    file_info=next((item for item in record.get('files',[]) if item['name']==filename),None)
    db_path=directory/(kind+'-progress.sqlite3')
    if file_info is None:
        # 运行中只有工作库时仍保留逐批观察，不把没有导出的记录宣布为完成。
        if record.get('status') in {'complete','no_reviews','no_questions'} or not db_path.is_file():
            raise ValueError('评价或问答缺少已登记的明细文件')
        values=(json.loads(raw) for raw, in sqlite_records(db_path,'SELECT payload FROM '+kind+' ORDER BY position'))
    else:
        path=directory.parent.parent/file_info['history_file']
        asset(settings,path,file_info)
        values=json_array(path,kind)
    count=answer_count=0
    with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)) as connection:
        connection.execute('BEGIN')
        for value in values:
            count+=1
            if value['index']!=count or value.get('product_id')!=record['product_id']:
                raise ValueError('评价或问答顺序、商品归属不一致')
            expected=connection.execute('SELECT payload FROM '+kind+' WHERE position=?',(count,)).fetchone()
            if expected is None:
                raise ValueError('明细与模块工作库记录数量不一致')
            expected=json.loads(expected[0])
            if module=='reviews':
                if value!=expected:
                    raise ValueError('评价明细与模块工作库内容不一致')
                key={'position':count}
                yield row('review_observations',key,{**_identity(value,'review'),'sku_text':value.get('sku'),
                    'content':value['content'],'append_reviews':value.get('append_reviews'),'captured_at':value.get('captured_at'),
                    'payload':{k:v for k,v in value.items() if k not in {'content','append_reviews'}}})
                yield from _links(value,{'table':'review_observations',**key},'review')
            else:
                answers=value.get('answers')
                question={k:v for k,v in value.items() if k!='answers'}
                if question!=expected:
                    raise ValueError('问题明细与模块工作库内容不一致')
                key={'position':count}
                yield row('question_observations',key,{**_identity(question,'question'),'question':question['question'],
                    'answers_complete':bool(question['answers_complete']),
                    'answer_stop_reason':stop_reason_fields(question['answer_stop_reason'],field='answer_stop_reason')['answer_stop_reason'],
                    'captured_at':question.get('captured_at'),'payload':question})
                actual_answers=connection.execute('SELECT payload FROM answers WHERE question_key=? ORDER BY position',(question['question_key'],))
                if answers is None:
                    answers=(json.loads(raw) for raw, in connection.execute('SELECT payload FROM answers WHERE question_key=? ORDER BY position',(question['question_key'],)))
                this_count=0
                for this_count,answer in enumerate(answers,1):
                    actual=actual_answers.fetchone()
                    if actual is None or answer!=json.loads(actual[0]) or answer['index']!=this_count or answer['question_key']!=question['question_key']:
                        raise ValueError('回答明细的内容、顺序或问题归属不一致')
                    answer_count+=1
                    answer_key={'question_position':count,'position':this_count}
                    yield row('answer_observations',answer_key,{**_identity(answer,'answer'),'content':answer['content'],
                        'sku_text':answer.get('sku'),'captured_at':answer.get('captured_at'),'payload':answer})
                    yield from _links(answer,{'table':'answer_observations',**answer_key},'answer')
                if actual_answers.fetchone() is not None or this_count!=question.get('answer_count',0):
                    raise ValueError('问题回答数量与工作库不一致')
        actual_count=connection.execute('SELECT COUNT(*) FROM '+kind).fetchone()[0]
        if count!=actual_count or count!=record.get('collected_count',count):
            raise ValueError('明细数量与批次计数不一致')
        if module=='questions' and answer_count!=record.get('answer_count',answer_count):
            raise ValueError('回答数量与批次计数不一致')
        if module=='reviews':
            for sequence,key,skipped in connection.execute('SELECT rowid,review_key,skipped FROM seen ORDER BY rowid'):
                yield row('collection_checkpoints',{'checkpoint_type':'review_seen','checkpoint_key':key},
                    {'checkpoint_type':'review_seen','checkpoint_key':key,'source_sequence':sequence,'payload':{'skipped':bool(skipped)}})


def batch_rows(settings,module,record,directory):
    """转换各模块观察与文件；参数：配置、模块、元数据及历史目录；返回：流式记录。"""
    product_dir=directory.parent.parent
    if module=='list':
        items=record.get('items',[])
        if len(items)!=record.get('collected_count',len(items)):
            raise ValueError('列表数量与批次记录不一致')
        for item in items:
            from ecommerce.shared.sales import parse_sales,FIELDS,PARSER_VERSION
            sales=parse_sales(item.get('sales_text'))
            supplied=set(FIELDS)&set(item)
            if supplied and (supplied!=set(FIELDS) or any(item[name]!=sales[name] for name in FIELDS)):
                raise ValueError('列表销量结构化结果与原文不一致')
            if 'sales_parser_version' in item and item['sales_parser_version']!=PARSER_VERSION:
                raise ValueError('列表销量解析规则版本不支持')
            key={'slot_index':item.get('slot_index',item.get('position'))}
            yield row('list_observations',key,{'slot_index':key['slot_index'],'product_index':item['product_index'],
                'title':item['title'],'price_text':item.get('price_text'),'price_value':decimal_value(item.get('price_value')),
                'price_label':item.get('price_label'),'sales_text':item.get('sales_text'),'shop_name':item.get('shop_name'),
                'location_text':item.get('location_text'),'ad_status':item.get('ad_status','unconfirmed'),
                'identity_status':item.get('identity_status','unconfirmed'),'field_states':item.get('field_states'),
                'payload':item,**sales},product_id=product_id(item.get('product_id')))
            image=item.get('list_image')
            if image is not None:
                file=asset(settings,image['image_path'],image) if image.get('status')=='downloaded' else None
                yield media('list_image',key['slot_index'],url=item.get('main_image_url'),file=file,
                            status=image['status'],payload={'slot_index':key['slot_index'],'download':image})
            elif item.get('main_image_url'):
                yield media('list_image',key['slot_index'],url=item['main_image_url'],payload={'slot_index':key['slot_index']})
            if item.get('video_url'):
                yield media('list_video',key['slot_index'],url=item['video_url'],payload={'slot_index':key['slot_index']})
    elif module=='basic_info':
        info=record.get('product_info') or {}
        yield row('product_info_observations',{}, {**{k:info.get(k) for k in ('title','shop_name','sales_text','price_text')},
            'source_url':record.get('source_url'),'captured_at':info.get('captured_at') or record.get('captured_at'),
            'field_states':info.get('field_states') or {},'payload':info})
    elif module=='skus':
        skus=record.get('skus',[])
        if len(skus)!=record.get('processed_count',len(skus)):
            raise ValueError('SKU 实际记录与已处理数量不一致')
        for sku in skus:
            key={'position':sku['index']}
            yield row('sku_observations',key,{'position':sku['index'],'platform_sku_id':sku.get('sku_id'),
                'sku_name':sku['name'],'properties':sku.get('properties'),'price_text':sku.get('price'),
                'price_value':decimal_value(sku.get('price_value')),'price_label':sku.get('price_label'),
                'currency':sku.get('currency'),'price_status':sku.get('price_status','unconfirmed'),
                'stock_status':sku.get('stock_status'),'combination_confirmed':bool(sku.get('combination_confirmed')),
                'image_status':sku.get('image_status'),'captured_at':sku.get('captured_at'),
                'price_checked_at':sku.get('price_checked_at'),'payload':{k:v for k,v in sku.items() if k!='image'}})
            image=sku.get('image')
            if image:
                path=directory/'sku图'/Path(image['image_file']).name
                yield media('sku_image',sku['index'],url=sku.get('image_url'),file=asset(settings,path,image),
                    owner={'table':'sku_observations',**key},payload={'record':image},
                    status='reused' if image.get('reused_from_batch_id') else 'downloaded')
            yield row('collection_checkpoints',{'checkpoint_type':'sku_item','checkpoint_key':str(sku['index'])},
                {'checkpoint_type':'sku_item','checkpoint_key':str(sku['index']),'source_sequence':sku['index'],
                 'payload':{'price_status':sku.get('price_status'),'image_status':sku.get('image_status'),'phase':record.get('phase')}})
    elif module=='parameters':
        for item in record.get('items',[]):
            yield row('parameter_observations',{'position':item['index']},{'position':item['index'],'name':item['name'],
                'name_raw':item.get('name_raw'),'value':item['value'],'status':item['status'],'payload':item})
    elif module=='review_summary':
        count=record.get('review_count') or {}
        rate=record.get('positive_rate') or {}
        yield row('review_summaries',{}, {'count_raw':count.get('raw'),'count_value':decimal_value(count.get('value')),
            'count_lower_bound':decimal_value(count.get('lower_bound')),'count_is_exact':count.get('is_exact'),
            'positive_rate_raw':rate.get('raw'),'positive_rate_percent':decimal_value(rate.get('percent')),
            'period_raw':rate.get('period_raw'),'tags':record.get('tags'),'payload':{'review_count':count,'positive_rate':rate}})
    elif module in {'reviews','questions'}:
        yield from _long_rows(settings,module,record,directory)
    if module in {'main_images','detail_images'}:
        folder='1-1主图' if module=='main_images' else '详情图'
        for image in record.get('images',[]):
            file=None
            if image.get('image_file'):
                file=asset(settings,directory/folder/Path(image['image_file']).name,image)
            yield media('main_image' if module=='main_images' else 'detail_image',image['index'],url=image.get('image_url'),
                file=file,payload={'record':image},status='reused' if image.get('reused_from_batch_id') or image.get('reused_from_index') else None)
            yield row('collection_checkpoints',{'checkpoint_type':'image_item','checkpoint_key':str(image['index'])},
                {'checkpoint_type':'image_item','checkpoint_key':str(image['index']),'source_sequence':image['index'],
                 'payload':{'sha256':image.get('sha256'),'status':image.get('status','downloaded'),'reused_from_batch_id':image.get('reused_from_batch_id')}})
    if module=='video' and record.get('video_path'):
        metadata={'sha256':record.get('video_sha256'),'size_bytes':record.get('video_size_bytes'),
                  'content_type':record.get('content_type'),'duration_seconds':record.get('duration_seconds')}
        yield media('main_video',1,url=record.get('video_url'),file=asset(settings,directory/(record.get('video_file') or (record['product_id']+'.mp4')),metadata))
    if record.get('html_file'):
        yield media('html',1,file=asset(settings,product_dir/record['html_file'],
            {'sha256':record.get('html_sha256'),'size_bytes':record.get('html_size_bytes')}))
    for index,metadata in enumerate(record.get('files',[]),1):
        yield media('export_json' if metadata['name'].endswith('.json') else 'export_csv',index,
            file=asset(settings,product_dir/metadata['history_file'],metadata))
    if record.get('screenshot'):
        yield media('screenshot',1,file=asset(settings,record['screenshot']))


def build_snapshot(settings,kind,record,source_path,*,module=None,directory=None,source=None):
    """建立可重放的数据快照；参数：配置、类型、原记录及来源；返回：不可变引用。"""
    scope_record=record
    if source:
        if any(key in record and record[key]!=value for key,value in source.items()):
            raise ValueError('模块原记录与详情来源上下文冲突')
        record={**record,**source}
    source_path=Path(source_path).resolve()
    source_relative=source_path.relative_to(settings.workspace.resolve()).as_posix()
    item_id=product_id(record.get('product_id'))
    identity=record.get('batch_id') or record.get('entry_key') or source_relative.casefold()
    header={'kind':kind,'module':module,'task_id':record.get('task_id'),'product_id':item_id,
            'logical_key':fingerprint(['collection_v1',kind,module,identity]),'source_relative_path':source_relative}
    if record.get('search_method')=='image':
        image=record.get('search_image') or {}
        if not image.get('path') or not re.fullmatch('[a-f0-9]{64}',image.get('sha256') or ''):
            raise ValueError('图片搜索缺少实际输入图片路径或SHA-256')
        image_asset=asset(settings,image['path'],image)
        if image_asset['availability']!='present':
            raise ValueError('搜索输入图片不存在，不能登记为新图片搜索来源')
        header['search_image']=image_asset
    if kind=='report':
        if record.get('entry_key'):
            header['entry_key']=record['entry_key']
        header['values']={'source_run_key':record['batch_id'],'status':record['status'],'started_at':record.get('started_at'),
            'finished_at':record.get('finished_at'),'warnings':record.get('warnings'),'error':record.get('error') or
                ({'type':record['error_type']} if record.get('error_type') else None)}
        def records():
            """逐模块保存汇总原状态；参数：无；返回：步骤记录。"""
            for index,step in enumerate(record['steps']):
                payload={'record':step}
                if index==0:
                    payload['run_metadata']={k:v for k,v in record.items() if k!='steps'}
                yield row('collection_steps',{'module':step['name']},{'module':step['name'],'requested':step['requested'],
                    'status':step['status'],'continuation_allowed':step['continuation_allowed'],
                    'started_at':step.get('started_at'),'finished_at':step.get('finished_at'),
                    'warnings':step.get('warnings'),'blocking_issues':step.get('blocking_issues'),'payload':payload})
    elif kind=='page':
        if record.get('list_source'):
            header['list_source']=record['list_source']
        header['values']={'record_type':module,'keyword':record.get('keyword'),'page_number':record.get('page_number'),
            'sort_label':record.get('sort_label'),'status':record['status'],'source_url':record.get('source_url'),
            'captured_at':record.get('checked_at'),'payload':{'record':record}}
        def records():
            """登记页面截图证据；参数：无；返回：文件引用。"""
            if header.get('search_image'):
                yield media('search_input_image',1,file=header['search_image'])
            if record.get('screenshot'):
                yield media('screenshot',1,file=asset(settings,record['screenshot']),payload={'page_logical_key':header['logical_key']})
    elif kind=='batch':
        scope_key,contract,coverage=scope_contract(module,scope_record)
        metadata={k:v for k,v in record.items() if k not in {'items','skus','images','raw_items','raw_html','image_results','unknown_fragments'}}
        # 原文件和源停止原因不改写，兼容转换只影响代码列，并补充诊断原文。
        reason_fields=stop_reason_fields(record.get('stop_reason'))
        if 'stop_reason_message' in reason_fields:
            metadata['stop_reason_message']=reason_fields['stop_reason_message']
        status=record.get('video_status',record.get('status',record.get('collection_status')))
        header['values']={'platform':'taobao','source_batch_key':record['batch_id'],'module':module,'status':status,
            'scope':record.get('scope'),'scope_key':scope_key,'collection_mode':record.get('mode'),
            'requested_limit':record.get('requested_limit'),'answer_limit':record.get('answer_limit'),
            'selection_filter':record.get('filter'),'sort_label':record.get('sort',record.get('sort_label')),
            'coverage_status':coverage,'stop_reason':reason_fields['stop_reason'],'end_confirmed':record.get('end_confirmed'),
            'completion_evidence':record.get('completion_evidence'),'captured_at':record.get('captured_at'),
            'observed_at':None if record.get('source_batch_id') else record.get('captured_at'),
            'confirmed_at':(record.get('completion_evidence') or {}).get('confirmed_at'),
            'upstream_batch_key':record.get('stock_rule_source_batch_key') or record.get('source_batch_id') or record.get('resumed_from_batch_id'),
            'derivation_kind':'sku_stock_rule' if record.get('stock_rule_source_batch_key') else 'manual_confirmation' if record.get('source_batch_id') else 'resume_reuse' if record.get('resumed_from_batch_id') else None,
            'visibility':'staging' if status in RUNNING else 'published','source_url':record.get('source_url'),
            'schema_version':record.get('schema_version'),'summary':record.get('summary') or {k:record[k] for k in record if k.endswith('_count')},
            'result_payload':{'record':metadata,'scope_contract':contract}}
        def records():
            """逐条转换明细，末尾保留源元数据文件；参数：无；返回：规范化行。"""
            if header.get('search_image'):
                yield media('search_input_image',1,file=header['search_image'])
            yield from batch_rows(settings,module,record,source_path.parent)
            # 进度文件会继续改写，不能登记为不可变证据；其当时内容已在持久快照中保存。
            if source_path.name not in {'reviews-progress.json','questions-progress.json','sku_progress.json'}:
                yield media('source_json',1,file=asset(settings,source_path))
    else:
        raise ValueError('采集快照类型不支持')
    return save_snapshot(settings,header,records(),directory=directory)
