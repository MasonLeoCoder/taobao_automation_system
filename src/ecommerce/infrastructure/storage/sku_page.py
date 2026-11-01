"""SKU页面的整批轻量概览及SQL筛选；不写入采集记录。"""
from collections import Counter
from decimal import Decimal
import sqlalchemy as sa
from ecommerce.shared.sku_stock import stock_group


def overview(connection, table, batch_id):
    """读取全批必要列；参数：同一读连接、规格表和批次；返回：状态数量与分币种价格区间。"""
    rows=connection.execute(sa.select(table.c.stock_status,table.c.price_status,table.c.price_value,table.c.currency)
                            .where(table.c.batch_id==batch_id)).mappings()
    counts=Counter();ranges={};total=0;priced=0
    for row in rows:
        total+=1;counts[stock_group(row['stock_status'])]+=1
        currency=(row['currency'] or '').strip(' ').upper()
        value=row['price_value']
        if row['price_status']!='collected' or value is None or not currency:
            continue
        value=Decimal(str(value))
        if not value.is_finite() or value<0:
            continue
        priced+=1
        item=ranges.setdefault(currency,{'currency':currency,'min':value,'max':value,'count':0})
        item['min']=min(item['min'],value);item['max']=max(item['max'],value);item['count']+=1
    return {'total':total,'counts':{key:counts[key] for key in ('selectable','out_of_stock','unavailable_unconfirmed','unknown')},
            'priced':priced,'unpriced':total-priced,'ranges':[ranges[key] for key in sorted(ranges)]}


def predicate(table, key):
    """构造已保存状态的筛选；参数：规格表和筛选键；返回：SQL条件。"""
    value=table.c.stock_status
    # 转为二进制比较，避免MySQL默认不区分大小写将异常旧状态误当成有效状态。
    raw=sa.cast(value,sa.LargeBinary)
    known=[b'selectable',b'out_of_stock',b'unavailable_unconfirmed']
    if key=='all':return sa.true()
    if key=='unknown':return sa.or_(value.is_(None),raw.not_in(known))
    return raw==key.encode('ascii')


def price_expression(table):
    """只将已确认且币种非空的价格用于排序；参数：规格表；返回：数值及币种表达式。"""
    currency=sa.func.upper(sa.func.trim(table.c.currency))
    valid=sa.and_(sa.cast(table.c.price_status,sa.LargeBinary)==b'collected',table.c.price_value.is_not(None),
                  table.c.price_value>=0,currency.is_not(None),currency!='')
    return sa.case((valid,table.c.price_value),else_=None),currency
