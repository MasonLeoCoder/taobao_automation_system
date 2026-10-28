"""前50名单第11～30件的描述性分析；不重新选样或产生经营评分。"""
from collections import Counter
from datetime import datetime
from decimal import Decimal

from ecommerce.modules.analysis.composition import quantity_tiers
from ecommerce.modules.analysis.prices import quantile
from ecommerce.shared.time import BEIJING


MIDDLE_FINDINGS = {'middle','middle_shared','middle_boundary10','middle_boundary30'}


def _summary(rows):
    """生成头部/腰部的同口径摘要；参数：已选名单；返回：人数、价格覆盖及店铺摘要。"""
    quantities=sorted(Decimal(row['sales_count']) for row in rows)
    prices=sorted(row['price'] for row in rows if row['price_usable'])
    total=sum(row['sales_count'] for row in rows)
    return {'count':len(rows),'quantity_total':total,
            'quantity_mean':Decimal(total)/len(rows) if rows else None,
            'quantity_median':quantile(quantities,Decimal('.5')),
            'price_count':len(prices),'price_median':quantile(prices,Decimal('.5')),
            'price_q1':quantile(prices,Decimal('.25')),'price_q3':quantile(prices,Decimal('.75')),
            'shop_count':len({row['shop_key'] for row in rows if row['shop_key']}),
            'missing_shops':sum(not row['shop_key'] for row in rows)}


def middle_performance(concentration):
    """分析固定腰部区间；参数：完整范围的集中度结果；返回：摘要、档位、店铺及边界依据。"""
    members=concentration['members']
    head_rows=members[:10]
    rows=members[10:30]
    head=_summary(head_rows)
    summary=_summary(rows)
    head_shops=Counter(row['shop_key'] for row in head_rows if row['shop_key'])
    shops=Counter(row['shop_key'] for row in rows if row['shop_key'])
    overlap=set(shops)&set(head_shops)
    shop_rows=[]
    for key,count in sorted(shops.items(),key=lambda item:(-item[1],item[0])):
        first=next(row for row in rows if row['shop_key']==key)
        shop_rows.append({'key':key,'name':first['shop_name'],'basis':first['shop_basis'],'count':count,
                          'percent':Decimal(count)*100/len(rows),'head_count':head_shops[key]})
    boundaries=[]
    for edge in (10,30):
        if len(members)>edge and members[edge-1]['sales_count']==members[edge]['sales_count']:
            value=members[edge]['sales_count']
            boundaries.append({'edge':edge,'value':value,'tied':members[edge]['tie_count'],
                               'included':sum(row['sales_count']==value for row in rows)})
    times=[row['captured_at'].replace(tzinfo=BEIJING) if row['captured_at'].tzinfo is None
           else row['captured_at'].astimezone(BEIJING) for row in rows if isinstance(row.get('captured_at'),datetime)]
    return {'head':head,'summary':summary,'members':rows,
            'first_rank':11 if rows else None,'last_rank':rows[-1]['rank'] if rows else None,
            'quantity_share':Decimal(summary['quantity_total'])*100/concentration['total'] if rows and concentration['total'] else None,
            'tiers':quantity_tiers(rows,'received_people:last_30_days'),'shops':shop_rows,
            'multi_shop_count':sum(count>1 for count in shops.values()),
            'overlap_shop_count':len(overlap),
            'overlap_shop_percent':Decimal(len(overlap))*100/len(shops) if shops else None,
            'overlap_product_count':sum(row['shop_key'] in overlap for row in rows),
            'boundaries':boundaries,'earliest':min(times) if times else None,
            'latest':max(times) if times else None,'missing_times':len(rows)-len(times)}
