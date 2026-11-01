"""SKU页面业务：可读属性、原文异常及既有状态解释，始终保留原记录。"""
from decimal import Decimal, InvalidOperation
from ecommerce.shared.sku_stock import SKU_FILTERS, stock_group

PRICE_LABELS={'collected':'已采集','not_read':'未读取','out_of_stock':'缺货／已禁用',
              'unavailable_unconfirmed':'不可选原因未确认','unconfirmed':'未确认'}


def text(value):
    """仅接受已有文本；参数：原值；返回：字符串或空，不猜测缺失内容。"""
    return value if isinstance(value,str) else ''


def money(value):
    """格式化已确认金额；参数：数值或字符串；返回：至少两位小数的金额或空。"""
    try:
        number=Decimal(str(value))
        if not number.is_finite() or number<0:return ''
        result=format(number,'f');whole,_,fraction=result.partition('.')
        fraction=fraction.rstrip('0').ljust(2,'0')
        return whole+'.'+fraction
    except (InvalidOperation,ValueError,TypeError):return ''


def present_record(row):
    """整理一条规格的可读展示；参数：查询原行；返回：带只读显示字段的新字典。"""
    payload=row.get('payload') if isinstance(row.get('payload'),dict) else {}
    name=text(row.get('sku_name'));properties=row.get('properties')
    attributes=[]
    if isinstance(properties,list):
        for item in properties:
            if isinstance(item,dict):
                attributes.append({'group':text(item.get('group')) or '未记录属性名','name':text(item.get('name')) or '未记录属性值',
                                   'value_id':text(item.get('value_id'))})
    readable=bool(attributes) or properties is None or properties==[]
    brief=attributes if len(attributes)>1 else []
    abnormal='\ufffd' in name or any('\ufffd' in item['group'] or '\ufffd' in item['name'] for item in attributes)
    state=stock_group(row.get('stock_status'))
    currency=text(row.get('currency')).strip(' ').upper()
    amount=money(row.get('price_value')) if row.get('price_status')=='collected' else ''
    price=(currency+' '+amount) if currency and amount else '未确认'
    if currency=='CNY' and amount:price='¥'+amount
    if not amount and state=='out_of_stock':price='未取得价格'
    price_label=PRICE_LABELS.get(row.get('price_status'),'未记录或状态异常')
    if row.get('price_status')=='collected' and (not amount or not currency):
        price_label='数值或币种待核对'
    raw_evidence=payload.get('unavailability_evidence')
    unavailable=[item for item in raw_evidence if isinstance(item,str)] if isinstance(raw_evidence,list) else []
    return {**row,'display_name':name or '规格名称未记录','attributes':attributes,'brief_attributes':brief,
            'properties_readable':readable,'text_abnormal':abnormal,'stock_group':state,'stock_label':SKU_FILTERS[state],
            'stock_style':'is-ready' if state=='selectable' else 'is-error' if state=='out_of_stock' else 'is-pending',
            'price_label':price_label,'display_price':price,
            'price_original':text(row.get('price_text')),'price_reason':text(payload.get('price_reason')),
            'stock_evidence':text(payload.get('stock_evidence')),'unavailable_evidence':unavailable,
            'combination_label':'已确认' if row.get('combination_confirmed') is True else '未确认'}


def present_summary(summary):
    """整理全批概览；参数：存储层轻量统计；返回：筛选数量与分币种价格文本。"""
    result={**summary,'filters':[{'key':key,'label':label,'count':summary['total'] if key=='all' else summary['counts'][key]}
                                for key,label in SKU_FILTERS.items()]}
    ranges=[]
    for item in summary['ranges']:
        low,high=money(item['min']),money(item['max']);currency=item['currency'];prefix='¥' if currency=='CNY' else currency+' '
        ranges.append({**item,'label':prefix+low+('～'+prefix+high if low!=high else '')})
    return {**result,'ranges':ranges}
