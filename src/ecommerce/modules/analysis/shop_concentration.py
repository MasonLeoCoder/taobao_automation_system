"""同一前50名单的店铺分组集中度；缺店铺不合并成虚拟商家。"""
from collections import defaultdict
from decimal import Decimal


SHOP_FINDINGS = {'shop_all','shop_cr3','shop_cr10'}


def shop_concentration(concentration):
    """汇总店铺及入选商品；参数：既有前50结果；返回：覆盖、CR/HHI、稳定排序与子商品。"""
    members=concentration['members']
    grouped=defaultdict(list)
    missing=[]
    for row in members:
        if row.get('shop_key'):
            grouped[row['shop_key']].append(row)
        else:
            missing.append(row)
    known_count=len(members)-len(missing)
    missing_total=sum(row['sales_count'] for row in missing)
    total=concentration['total']-missing_total
    groups=[]
    for key,rows in grouped.items():
        group_total=sum(row['sales_count'] for row in rows)
        groups.append({'key':key,'name':next((row['shop_name'] for row in rows if row.get('shop_name')),'店铺名称未记录'),
                       'basis':rows[0]['shop_basis'],'count':len(rows),'total':group_total,
                       'mean':Decimal(group_total)/len(rows),'first_rank':min(row['rank'] for row in rows),
                       'head_count':sum(row['rank']<=10 for row in rows),
                       'middle_count':sum(11<=row['rank']<=30 for row in rows),
                       'tail_count':sum(row['rank']>30 for row in rows),
                       'members':[{**row,'shop_share':Decimal(row['sales_count'])*100/group_total if group_total else None,
                                   'section':'头部' if row['rank']<=10 else '腰部' if row['rank']<=30 else '后部'}
                                  for row in sorted(rows,key=lambda row:row['rank'])]})
    groups.sort(key=lambda group:(-group['total'],group['first_rank'],group['key']))
    cumulative=0
    for rank,group in enumerate(groups,start=1):
        cumulative+=group['total']
        group.update(rank=rank,share=Decimal(group['total'])*100/total if total else None,
                     cumulative=Decimal(cumulative)*100/total if total else None)
    metrics=[]
    for size in (3,10):
        numerator=sum(group['total'] for group in groups[:size]) if len(groups)>=size else None
        metrics.append({'key':'shop_cr'+str(size),'label':'CR'+str(size),'size':size,'numerator':numerator,
                        'value':Decimal(numerator)*100/total if numerator is not None and total else None,
                        'reason':'店铺分组不足'+str(size)+'组' if len(groups)<size else '人数展示值合计为0' if not total else ''})
    return {'groups':groups,'shop_count':len(groups),'known_count':known_count,'missing_count':len(missing),
            'selected_count':len(members),'selected_total':concentration['total'],'total':total,
            'missing_total':missing_total,'metrics':metrics,
            'product_coverage':Decimal(known_count)*100/len(members) if members else None,
            'quantity_coverage':Decimal(total)*100/concentration['total'] if concentration['total'] else None,
            'reliable_count':sum(not group['key'].startswith('name:') for group in groups),
            'name_count':sum(group['key'].startswith('name:') for group in groups),
            'hhi':Decimal(sum(group['total']**2 for group in groups))*10000/(Decimal(total)**2) if total else None,
            'uniform_hhi':Decimal(10000)/len(groups) if groups and total else None}
