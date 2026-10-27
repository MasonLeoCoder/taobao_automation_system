"""逐页列表到详情的串行协作；列表观察全部保留，同任务成功详情按商品复用。"""
import json
from pathlib import Path

from ecommerce.infrastructure.tasks.events import emit_event
from ecommerce.modules.collection.detail_service import collect_detail_page
from ecommerce.platforms.taobao.list_details import confirm_search_page, open_list_detail, return_to_search
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped, ModuleBlocked
from ecommerce.modules.collection import recovery
from ecommerce.shared.collection_contract import MODULES


def collect_page_details(settings, context, result, collected, seen, attempts, *, on_progress=None):
    """处理已入库列表；参数：会话、列表批次、成功缓存及汇总列表；返回：无，中断不跳过后续商品。"""
    path = Path(collected['json_path'])
    path.resolve().relative_to(settings.workspace.resolve())
    payload = json.loads(path.read_text(encoding='utf-8'))
    guard = active_guard()
    tracker=recovery.current()
    from ecommerce.infrastructure.tasks.events import current_events
    session=current_events()
    modules=getattr(session,'job_modules',None) or MODULES
    allowed_tasks={guard.task_id}|(set(tracker.cleanup_tasks) if tracker else set())
    if (payload['task_id'] not in allowed_tasks or payload['batch_id'] != collected['source_batch_key'] or
        payload['page_number'] != collected['page_number'] or payload['keyword'] != result.keyword or
        len(payload['items']) != collected['collected_count']):
        raise ValueError('本页原件与已确认的任务、批次或搜索条件不一致')
    source_fields = {key: payload.get(key) for key in ('keyword', 'page_number', 'sort_label', 'search_method', 'search_options')}
    if payload.get('search_image'):
        source_fields['search_image'] = payload['search_image']
    for item in payload['items']:
        source = {'source_batch_key': payload['batch_id'], 'product_index': item['product_index'], 'product_id': item['product_id']}
        attempt = {'list_source': source, 'page_number': payload['page_number'], 'product_id': item['product_id'], 'status': 'running', 'detail_status':'not_started'}
        attempts.append(attempt)
        if tracker and all(tracker.module(item['product_id'],module).get('state') in {'complete','no_entry','failed_skipped','not_run_skipped'} for module in modules):
            for module in modules:
                tracker.should_skip(item['product_id'],module)
            attempt.update(status='reused_in_task',detail_status='reused',entry_key=None,source_run_key=None)
            seen[item['product_id']]=dict(attempt)
            emit_event('list_detail_reused','原计划中本商品各模块均已处理，直接处理下一个商品',details=attempt)
            continue
        if item['product_id'] in seen:
            previous = seen[item['product_id']]
            attempt.update(status='reused_in_task', detail_status=previous['detail_status'], entry_key=previous['entry_key'], source_run_key=previous['source_run_key'])
            emit_event('list_detail_reused', '同任务商品详情已成功采集，保留本次列表观察并复用详情运行', details=attempt)
            continue
        emit_event('list_detail_started', '准备采集当前列表商品的默认九个详情模块', details=attempt)
        if on_progress:
            on_progress({'current_product':item['product_id'],'current_module':None})
        try:
            confirm_search_page(result, payload['page_number'])
            detail = open_list_detail(context, item['product_url'], item['product_id'], settings.timeout_ms)
            attempt['detail_status']='running'
            value = collect_detail_page(settings, context, detail, source={**source_fields, 'list_source': source},
                **({name:name in modules for name in MODULES} if getattr(session,'job_modules',None) else {}),
                **({'on_module_event':lambda module,stage:on_progress({'current_module':module if stage=='running' else None})} if on_progress else {}))
            if value['status'] not in {'complete', 'complete_with_warnings', 'complete_with_errors'}:
                raise NeedsAttention('当前商品详情未完成，未继续下一商品')
            attempt.update(status=value['status'], detail_status=value['status'], entry_key=value['entry_key'], source_run_key=value['source_run_key'])
            seen[item['product_id']] = dict(attempt)
            if on_progress:
                on_progress({'detail_completed_count':len(seen),'current_product':None,'current_module':None})
            emit_event('list_detail_saved', '当前列表商品详情及汇总已入库', details=attempt)
            return_to_search(result, detail, payload['page_number'])
        except ModuleBlocked as error:
            attempt.update(status='failed_skipped',detail_status='failed_skipped',entry_key=None,source_run_key=None,message=str(error))
            seen[item['product_id']]=dict(attempt)
            if tracker:
                for module in modules:
                    if tracker.module(item['product_id'],module).get('state') not in {'complete','no_entry','failed_skipped','not_run_skipped'}:
                        tracker.finish_module(item['product_id'],module,{'status':'not_run_skipped','message':'详情入口无法恢复，本模块未执行'},failed=True)
                        tracker.state['products'][item['product_id']][module]['state']='not_run_skipped'
                tracker.persist('商品详情入口失败，剩余模块未执行，继续下一商品')
            emit_event('collection_page_unrecoverable','商品详情入口失败，已记录未执行模块，继续下一商品',level='ERROR',details=attempt)
            if on_progress:on_progress({'detail_completed_count':len(seen),'current_product':None,'current_module':None})
        except (Exception, KeyboardInterrupt) as error:
            attempt.update(status='interrupted' if isinstance(error, (TaskStopped, KeyboardInterrupt)) else
                           'needs_attention' if isinstance(error, NeedsAttention) else 'failed', error_type=type(error).__name__)
            if attempt['detail_status']=='running':
                attempt['detail_status']=attempt['status']
            emit_event('list_detail_interrupted', '列表到详情流程中断，已保存记录保留，后续商品未执行', level='WARNING', details=attempt)
            raise
