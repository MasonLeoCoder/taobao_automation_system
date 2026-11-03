"""来源报告返回上下文；只接受同商品的综合页、交叉报告或设置页。"""
import re
from urllib.parse import urlsplit, parse_qs, unquote
import sqlalchemy as sa
from flask import request, url_for
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.web.navigation import safe_return


def target(settings, product, value):
    """校验并重建同商品返回地址，失效时回综合页；参数：地址；返回：地址、来源标签、有效性。"""
    fallback = url_for('pages.collection_product', product_id=product, module='composite')
    if not isinstance(value, str) or not value or len(value) > 8192:
        return fallback, '交叉分析', False
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if any(ord(char) < 32 for char in decoded) or '\\' in decoded:
        return fallback, '交叉分析', False
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=50)
    except ValueError:
        return fallback, '交叉分析', False
    if parsed.scheme or parsed.netloc or not parsed.path.startswith('/'):
        return fallback, '交叉分析', False
    if any(len(values) != 1 for values in query.values()):
        return fallback, '交叉分析', False
    def get(key):
        """读取唯一查询项；参数：键；返回：文字或空。"""
        return query.get(key, [''])[0]
    if parsed.path == f'/collections/products/{product}' and get('module') == 'composite':
        values = {'product_id': product, 'module': 'composite'}
        if get('analysis'):
            if get('analysis') not in ('reviews','images'): return fallback, '交叉分析', False
            values['analysis'] = get('analysis')
        if get('observation'):
            if not re.fullmatch(r'[0-9]{1,19}', get('observation')): return fallback, '交叉分析', False
            if int(get('observation')):
                with repository.connection(settings) as (conn, tables):
                    p, observation = tables['products'], tables['list_observations']
                    exists = conn.scalar(sa.select(observation.c.id).join(p, observation.c.product_id == p.c.id).where(
                        observation.c.id == int(get('observation')), p.c.platform == 'taobao', p.c.platform_product_id == product))
                    if not exists: return fallback, '交叉分析', False
            values['observation'] = get('observation')
        if get('return_to'):
            values['return_to'] = safe_return(get('return_to'), '/collections/details')
        return url_for('pages.collection_product', **values), '图片分析' if get('analysis')=='images' else '交叉分析', True
    image_match = re.fullmatch(r'/analysis/images/tasks/([a-f0-9]{32})', parsed.path)
    if image_match:
        version=get('version')
        if version and not re.fullmatch(r'[1-9][0-9]{0,8}',version): return fallback,'图片分析',False
        with repository.connection(settings) as (conn,tables):
            p,t,r=(tables[n] for n in ('products','analysis_tasks','analysis_reports'))
            exists=conn.scalar(sa.select(t.c.id).join(p,t.c.product_id==p.c.id).where(t.c.id==image_match[1],
                t.c.analysis_type=='images',p.c.platform=='taobao',p.c.platform_product_id==product))
            if not exists or version and not conn.scalar(sa.select(r.c.id).where(r.c.analysis_id==image_match[1],r.c.version==int(version))):
                return fallback,'图片分析',False
        return url_for('image_analysis.result',task_id=image_match[1],version=version or None),'图片报告',True
    match = re.fullmatch(r'/analysis/composite/tasks/([a-f0-9]{32})', parsed.path)
    if match:
        version = get('version')
        if version and not re.fullmatch(r'[1-9][0-9]{0,8}', version): return fallback, '交叉分析', False
        with repository.connection(settings) as (conn, tables):
            p, t, r = (tables[n] for n in ('products', 'analysis_tasks', 'analysis_reports'))
            exists = conn.scalar(sa.select(t.c.id).join(p, t.c.product_id == p.c.id).where(t.c.id == match[1],
                t.c.analysis_type == 'cross', t.c.rule_version == 'cross-v1', p.c.platform == 'taobao', p.c.platform_product_id == product))
            if not exists: return fallback, '交叉分析', False
            if version and not conn.scalar(sa.select(r.c.id).where(r.c.analysis_id == match[1], r.c.version == int(version))):
                return fallback, '交叉分析', False
        return url_for('composite_analysis.result', task_id=match[1], version=version or None), '交叉报告', True
    if parsed.path == f'/analysis/composite/products/{product}/new':
        from ecommerce.modules.cross_analysis.kinds import KINDS, ordered
        from ecommerce.modules.cross_analysis.store import identity, load_source
        values = {key: get(key) for key in ('model','output_tokens') if get(key)}
        selection = {k:get(k+'_report') for k in KINDS if get(k+'_report')}
        try:
            # 尚不足两类的只读设置也可往返；付费启动仍强制至少两类。
            if get('sources') != '1': raise ValueError('来源选择标识无效')
            ordered(selection,minimum=0)
            with repository.connection(settings) as (conn,tables):
                item = identity(conn,tables,product)
                for kind, report_id in selection.items(): load_source(conn,tables,item['id'],report_id,kind)
        except (ValueError,LookupError): return fallback, '交叉分析', False
        values.update({k+'_report':v for k,v in selection.items()})
        values['sources'] = '1'
        if values.get('output_tokens') and not re.fullmatch(r'[0-9]{1,8}',values['output_tokens']): return fallback,'交叉分析',False
        if values.get('model') and not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',values['model']): return fallback,'交叉分析',False
        return url_for('composite_analysis.new',product=product,**values),'交叉分析设置',True
    return fallback, '交叉分析', False


def origin(settings, product):
    """派生报告顶部来源提示及受控返回入口；参数：商品；返回：只读模型或None。"""
    raw = request.args.get('return_to')
    if raw is None: return None
    destination, label, valid = target(settings, product, raw)
    mode = 'settings' if request.args.get('return_mode') == 'settings' else None
    if valid and mode and label == '交叉分析': label = '交叉分析设置'
    state = request.args.get('return_state', '')
    if not re.fullmatch(r'[a-f0-9]{32}', state): state = None
    return {'label': label, 'valid': valid, 'href': url_for('composite_analysis.source_return', product=product,
        target=destination if valid else raw, state=state), 'return_to': destination, 'state': state, 'mode': mode}
