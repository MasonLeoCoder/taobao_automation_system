"""同一筛选样本的收货人数前50件集中度；使用展示下限，不写入采集记录。"""
from collections import Counter
from datetime import datetime
from decimal import Decimal

from ecommerce.shared.time import BEIJING


FINDINGS = {'top3':3, 'top10':10, 'top50':50}


def _time_key(value):
    """生成可比较的北京时间键；参数：观察采集时间；返回：时间元组，缺失时排在最后。"""
    if not isinstance(value,datetime):
        return (0,)*7
    local=value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)
    return (local.year,local.month,local.day,local.hour,local.minute,local.second,local.microsecond)


def competition_concentration(rows):
    """计算前三/前十及HHI；参数：已去重且已按广告条件筛选的完整商品；返回：前50名单和依据。"""
    reasons=Counter(identity=0,missing=0,group=0,approximate=0)
    eligible=[]
    for row in rows:
        if row.get('product_id') is None or not row.get('platform_product_id'):
            reasons['identity']+=1
        elif not row['quantity_usable']:
            reasons['missing']+=1
        elif row['sales_group']!='received_people:last_30_days':
            reasons['group']+=1
        elif row['sales_precision'] not in {'exact','lower_bound'}:
            reasons['approximate']+=1
        else:
            eligible.append(row)
    # 稳定排序由最低优先级开始；同值时页码、页内位置优先于采集时间。
    ranked=sorted(eligible,key=lambda row:row['id'])
    ranked.sort(key=lambda row:_time_key(row.get('captured_at')),reverse=True)
    ranked.sort(key=lambda row:(row.get('page_number') is None,row.get('page_number') or 0,
                               row.get('slot_index') is None,row.get('slot_index') or 0))
    ranked.sort(key=lambda row:row['sales_count'],reverse=True)
    selected=ranked[:50]
    count=len(selected)
    total=sum(row['sales_count'] for row in selected)
    ties=Counter(row['sales_count'] for row in ranked)
    members=[]
    cumulative=0
    for rank,row in enumerate(selected,start=1):
        cumulative+=row['sales_count']
        members.append({**row,'rank':rank,'tie_count':ties[row['sales_count']],
                        'share':Decimal(row['sales_count'])*100/total if total else None,
                        'cumulative':Decimal(cumulative)*100/total if total else None})
    metrics=[]
    for size in (3,10):
        numerator=sum(row['sales_count'] for row in selected[:size]) if count>=size else None
        metrics.append({'key':'top'+str(size),'label':'CR'+str(size),'size':size,'numerator':numerator,
                        'value':Decimal(numerator)*100/total if numerator is not None and total else None,
                        'reason':'样本不足'+str(size)+'件' if count<size else '人数计算值合计为0' if not total else ''})
    boundary=None
    if len(ranked)>50 and ranked[49]['sales_count']==ranked[50]['sales_count']:
        value=ranked[49]['sales_count']
        included=sum(row['sales_count']==value for row in selected)
        boundary={'value':value,'tied':ties[value],'included':included,'excluded':ties[value]-included}
    return {'source_count':len(rows),'eligible_count':len(eligible),'count':count,'total':total,
            'excluded':sum(reasons.values()),'reasons':dict(reasons),'members':members,'metrics':metrics,
            'hhi':Decimal(sum(row['sales_count']**2 for row in selected))*10000/(Decimal(total)**2) if total else None,
            'uniform_hhi':Decimal(10000)/count if count and total else None,
            'exact_count':sum(row['sales_precision']=='exact' for row in selected),
            'lower_bound_count':sum(row['sales_precision']=='lower_bound' for row in selected),
            'zero_count':sum(row['sales_count']==0 for row in selected),'boundary':boundary}
