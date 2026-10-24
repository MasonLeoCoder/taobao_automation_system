"""逐页列表到详情的串行协作；列表观察全部保留，同任务成功详情按商品复用。"""
import json
from pathlib import Path

from ecommerce.infrastructure.tasks.events import emit_event
from ecommerce.modules.collection.detail_service import collect_detail_page
from ecommerce.platforms.taobao.list_details import confirm_search_page, open_list_detail, return_to_search
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped


def collect_page_details(settings, context, result, collected, seen, attempts):
    """处理已入库列表；参数：会话、列表批次、成功缓存及汇总列表；返回：无，中断不跳过后续商品。"""
    path = Path(collected['json_path'])
    path.resolve().relative_to(settings.workspace.resolve())
    payload = json.loads(path.read_text(encoding='utf-8'))
    guard = active_guard()
    if (payload['task_id'] != guard.task_id or payload['batch_id'] != collected['source_batch_key'] or
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
        if item['product_id'] in seen:
            previous = seen[item['product_id']]
            attempt.update(status='reused_in_task', detail_status=previous['detail_status'], entry_key=previous['entry_key'], source_run_key=previous['source_run_key'])
            emit_event('list_detail_reused', '同任务商品详情已成功采集，保留本次列表观察并复用详情运行', details=attempt)
            continue
        emit_event('list_detail_started', '准备采集当前列表商品的默认八个详情模块', details=attempt)
        try:
            confirm_search_page(result, payload['page_number'])
            detail = open_list_detail(context, item['product_url'], item['product_id'], settings.timeout_ms)
            attempt['detail_status']='running'
            value = collect_detail_page(settings, context, detail, source={**source_fields, 'list_source': source})
            if value['status'] not in {'complete', 'complete_with_warnings'}:
                raise NeedsAttention('当前商品详情未完成，未继续下一商品')
            attempt.update(status=value['status'], detail_status=value['status'], entry_key=value['entry_key'], source_run_key=value['source_run_key'])
            seen[item['product_id']] = dict(attempt)
            emit_event('list_detail_saved', '当前列表商品详情及汇总已入库', details=attempt)
            return_to_search(result, detail, payload['page_number'])
        except (Exception, KeyboardInterrupt) as error:
            attempt.update(status='interrupted' if isinstance(error, (TaskStopped, KeyboardInterrupt)) else
                           'needs_attention' if isinstance(error, NeedsAttention) else 'failed', error_type=type(error).__name__)
            if attempt['detail_status']=='running':
                attempt['detail_status']=attempt['status']
            emit_event('list_detail_interrupted', '列表到详情流程中断，已保存记录保留，后续商品未执行', level='WARNING', details=attempt)
            raise
