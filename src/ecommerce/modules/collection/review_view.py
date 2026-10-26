"""合并读取商品评价资料，保持概览、明细各自批次和来源边界。"""
from decimal import Decimal,InvalidOperation

from ecommerce.modules.collection.queries import query
from ecommerce.modules.collection.media_view import media_preview
from ecommerce.shared.pagination import pagination
from ecommerce.shared.sorting import sorting


def _batch(data,module,identifier):
    """选择本模块资料；参数：商品视图、模块及显式批次；返回：所选批次或空。"""
    if type(identifier) is not int or identifier<0:
        raise ValueError('批次编号须为非负整数')
    history=[row for row in data['history'] if row['module']==module and row['product_id']==data['product']['id']]
    if identifier:
        selected=next((row for row in history if row['id']==identifier),None)
        if not selected:
            raise LookupError('评价批次不属于此商品或模块')
        return selected,history
    context=(data.get('source_context') or {}).get('context_run',{}).get('id')
    candidates=[row for row in history if row['run_id']==context] if context else [row for row in history if row['id'] in data['current_ids']]
    return next(iter(candidates),None),history


def _tags(summary):
    """整理全部标签名称和原始数量；参数：概览；返回：保留原顺序的完整标签列表。"""
    rows=[]
    for tag in summary.get('tags') or []:
        if not isinstance(tag,dict):
            rows.append({'label':str(tag),'count_text':'未记录'})
            continue
        count=tag.get('count') or {}
        raw=tag.get('count_raw')
        if raw is None or str(raw).strip()=='':
            raw=count.get('raw')
        rows.append({'label':tag.get('label') or tag.get('raw_text') or '标签未记录',
                     'count_text':str(raw) if raw is not None and str(raw).strip() else '未记录'})
    return rows


def _review(row):
    """组织正文、追评及对应媒体；参数：单条评论；返回：展示副本。"""
    media=[media_preview(item) for item in row.get('media',[])]
    main=[item for item in media if item['role'].startswith('review_')]
    appends=[]
    for index,value in enumerate(row.get('append_reviews') or [],1):
        if not isinstance(value,dict): continue
        appends.append({**value,'media':[item for item in media if item['role'].startswith('append_') and
            (item.get('payload') or {}).get('append_index')==index]})
    return {**row,'date_text':row.get('date_raw') or (row.get('payload') or {}).get('date') or '未记录',
            'media':main,'appends':appends}


def review_page(settings,data,*,summary_batch=0,reviews_batch=0,page=1,sort_by=None,order=None):
    """读取同商品的两份评价资料；参数：配置、商品上下文及独立选择；返回：合并展示视图。"""
    selected,history=_batch(data,'review_summary',summary_batch)
    summary_detail=query(settings,'records',batch_id=selected['id'],page=1) if selected else None
    summary=next(iter(summary_detail['records']),{}) if summary_detail else {}
    rate=summary.get('positive_rate_percent')
    try:
        if rate is not None:
            rate=format(Decimal(str(rate)),'f')
            rate=(rate.rstrip('0').rstrip('.') if '.' in rate else rate)+'%'
        else:
            rate=summary.get('positive_rate_raw') or '未记录'
    except (InvalidOperation,ValueError):
        rate=summary.get('positive_rate_raw') or '未记录'
    overview={'selected':selected,'history':history,'summary':summary,'rate_text':rate,
        'tags':_tags(summary)}
    selected,history=_batch(data,'reviews',reviews_batch)
    spec=sorting(sort_by,order,{'position','content','author','sku','date','captured_at'},'date','desc')
    detail=query(settings,'records',batch_id=selected['id'],page=page,sort_by=spec['sort'],order=spec['order'],with_context=True) if selected else {
        'records':[],**pagination(0,page),'sort_spec':spec}
    detail['records']=[_review(row) for row in detail['records']]
    comments={'selected':selected,'history':history,'detail':detail}
    return {'overview':overview,'comments':comments}
