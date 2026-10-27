"""采集业务保存入口：保留导出，把确认后的观察快照排入当前任务持久流水。"""
import json
from pathlib import Path

from ecommerce.infrastructure.tasks.events import current_events
from ecommerce.modules.collection.database_records import build_snapshot,MODULE_FILES,SECTIONS


def active_settings(directory=None):
    """取得当前已启用采集库的配置；参数：可选归属目录；返回：配置或空。"""
    session=current_events()
    if session is None or session.settings.collection_backend!='mysql':
        return None
    settings=session.settings
    if directory is not None:
        Path(directory).resolve().relative_to(settings.workspace.resolve())
    return settings


def validate_publication(module,record,sections):
    """核对原模块发布条件；参数：模块、原记录、目标分区；返回：无，部分结果不得发布。"""
    if not sections:
        return
    status=record.get('video_status',record.get('status'))
    if status not in {'complete','downloaded','no_video','no_images','no_parameters','no_reviews','no_questions'}:
        raise ValueError('部分结果不能替换当前有效分区')
    if module=='main_images':
        images=record.get('images',[])
        if not images or len(images)!=record.get('expected_count',len(images)) or len(images)!=record.get('collected_count',len(images)):
            raise ValueError('主图发布数量尚未确认')
    if module=='detail_images' and status=='complete' and (not record.get('list_confirmed') or any(i.get('status')!='downloaded' for i in record.get('images',[]))):
        raise ValueError('详情图列表或下载状态尚未确认')
    if module=='basic_info':
        from ecommerce.shared.basic_info import assessment
        check=assessment(record.get('product_info') or {},record.get('source_url'),record.get('captured_at'))
        if check['status']!='complete':
            raise ValueError('基本信息完整性未确认，不能更新有效分区')
    if module=='skus':
        from ecommerce.modules.collection.detail_skus import sku_continuation_allowed
        if status!='complete' or not sku_continuation_allowed(record) or record.get('warnings') or record.get('blocking_issues'):
            raise ValueError('SKU 完整性未确认，保留最后尝试但不更新有效分区')
    if module=='questions' and record.get('completed_question_count')!=record.get('collected_count'):
        raise ValueError('问答还有回答未完成的问题，不能更新有效分区')
    if record.get('completeness_trusted') is False:
        raise ValueError('原结果完整性依据已失效，不能更新有效分区')


def capture_page(settings,record,path,record_type):
    """保存页面记录的数据库快照；参数：配置、记录、原件及类型；返回：无。"""
    if settings.collection_backend!='mysql':
        return
    session=current_events()
    if session is None or record.get('task_id')!=session.task_id:
        raise RuntimeError('采集入库必须由当前统一任务执行器保存')
    session.collection(build_snapshot(settings,'page',record,path,module=record_type),{})


def list_checkpoint(record,path):
    """生成完整列表页检查点；参数：已保存结果、原件路径；返回：含校验值的页描述。"""
    from ecommerce.infrastructure.storage.collection_snapshot import digest_file
    return {'message':f"原计划第{record['page_number']}页列表已保存，沿用原商品清单",
        'json_path':str(path),'csv_path':str(Path(path).with_suffix('.csv')),
        'collected_count':record['collected_count'],'field_gap_count':record.get('field_gap_count',0),
        'page_number':record['page_number'],'source_batch_key':record['batch_id'],
        'product_ids':[item.get('product_id') for item in record['items']],
        'image_downloads':record.get('image_downloads',{}),'empty':not record['items'],
        'json_sha256':digest_file(path)}


def capture_list(settings,record,path):
    """保存列表批次；参数：配置、结果、原件路径；返回：无，不修改导出。"""
    if settings.collection_backend=='mysql':
        session=current_events()
        if session is None or record.get('task_id')!=session.task_id:
            raise RuntimeError('列表入库缺少当前任务归属')
        tracker=getattr(session,'recovery',None)
        if tracker:
            tracker.state['pages'][str(record['page_number'])]=list_checkpoint(record,path)
            tracker.changed()
        session.collection(build_snapshot(settings,'batch',record,path,module='list'),{})


def capture_progress(settings,module,record,path):
    """按倍增水位同步工作观察；参数：配置、模块、进度和原件；返回：无，总转换量随记录数线性增长。"""
    from ecommerce.platforms.taobao.verification import active_guard
    guard=active_guard()
    if guard and hasattr(guard,'note_module_progress'):
        count=record.get('collected_count',0)+record.get('answer_count',0)
        previous=getattr(guard,'collection_seen_counts',{})
        if count>previous.get(record['batch_id'],-1):
            guard.note_module_progress(counts={'questions':record.get('collected_count',0),'answers':record.get('answer_count',0)} if module=='questions' else {'reviews':record.get('collected_count',0)})
            previous[record['batch_id']]=count
            guard.collection_seen_counts=previous
    if settings.collection_backend!='mysql':
        return
    session=current_events()
    if session is None or record.get('task_id')!=session.task_id:
        raise RuntimeError('采集进度入库缺少当前任务归属')
    state=getattr(session,'collection_progress',{})
    count=record.get('collected_count',0)+record.get('answer_count',0)
    threshold=state.get(record['batch_id'],0)
    if count<threshold:
        return
    state[record['batch_id']]=max(1,count*2)
    session.collection_progress=state
    context={'sections':[],'last_attempt':True,'report_key':getattr(session,'collection_run_key',None)}
    session.collection(build_snapshot(settings,'batch',record,path,module=module,source=getattr(session,'detail_source',None)),context)


def capture_sections(directory,item_id,sections):
    """把本次分区变更转为观察；参数：商品目录、原编号、本次分区；返回：无。"""
    settings=active_settings(directory)
    if settings is None:
        return
    session=current_events()
    directory=Path(directory)
    grouped={}
    if 'collection_summary' in sections:
        record=sections['collection_summary']
        path=directory/record['history_record_file']
        session.collection(build_snapshot(settings,'report',record,path,source=getattr(session,'detail_source',None)),{})
        session.collection_run_key=record['batch_id']
    for section,value in sections.items():
        if section=='collection_summary':
            continue
        normal=section.removesuffix('_last_attempt')
        module=SECTIONS.get(normal)
        if module is None:
            continue
        batch=value.get('batch_id')
        if not batch:
            raise ValueError('采集分区缺少独立批次编号')
        entry=grouped.setdefault((module,batch),{'value':value,'sections':[],'last_attempt':False})
        if section.endswith('_last_attempt'):
            entry['last_attempt']=True
        else:
            entry['sections'].append(section)
    for (module,batch),entry in grouped.items():
        path=directory/'history'/batch/MODULE_FILES[module]
        if not path.exists():
            alternative=entry['value'].get('progress_record_file')
            path=directory/alternative if alternative else path
        record=json.loads(path.read_text(encoding='utf-8'))
        if record.get('product_id')!=item_id or record.get('task_id')!=session.task_id or record.get('batch_id')!=batch:
            raise ValueError('模块原件与当前商品、任务或批次不一致')
        validate_publication(module,record,entry['sections'])
        context={'sections':entry['sections'],'last_attempt':entry['last_attempt'],
                 'report_key':getattr(session,'collection_run_key',None)}
        session.collection(build_snapshot(settings,'batch',record,path,module=module,source=getattr(session,'detail_source',None)),context)


def read_database_document(directory,item_id):
    """在正式采集时读取数据库有效分区与最后尝试；参数：目录、商品；返回：分区或空。"""
    settings=active_settings(directory)
    if settings is None:
        return None
    session=current_events()
    session.checkpoint('读取已确认的采集结果')
    from ecommerce.infrastructure.storage.collection_queries import detail_document
    return detail_document(settings,item_id,own_task=session.task_id)
