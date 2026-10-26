"""规格禁用即缺货的业务口径；仅接受明确页面标记，不把点击失败当缺货。"""
import re


def disabled_stock_evidence(evidence):
    """核对禁用依据；参数：页面标记数组；返回：缺货依据文本，无可靠标记时为空。"""
    if not isinstance(evidence,list):
        return None
    accepted=[value for value in evidence if isinstance(value,str) and (
        value in {'disabled属性','disabled','aria-disabled=true','data-disabled=true'} or
        re.fullmatch(r'class=(?:isDisabled|disabled|soldOut|outOfStock)--[\w-]+',value,re.I))]
    return '页面规格禁用：'+'；'.join(accepted) if accepted else None


def classify_disabled_sku(sku):
    """按禁用依据归为缺货；参数：规格记录；返回：是否转换，原页面依据继续保留。"""
    evidence=disabled_stock_evidence(sku.get('unavailability_evidence'))
    if (not evidence or sku.get('stock_status')!='unavailable_unconfirmed' or
            sku.get('price_status')!='unavailable_unconfirmed' or
            sku.get('price') is not None or sku.get('price_value') is not None):
        return False
    sku.update(stock_status='out_of_stock',price_status='out_of_stock',stock_evidence=evidence,
               price_reason='规格不可选，按业务规则判定缺货')
    return True
