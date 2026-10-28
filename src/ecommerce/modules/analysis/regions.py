"""列表展示发货地区的只读归类，不从店铺、标题或单独城市名猜省份。"""
from collections import defaultdict
from decimal import Decimal
import re


PROVINCES = {
    **{name:name+'省' for name in ('河北','山西','辽宁','吉林','黑龙江','江苏','浙江','安徽','福建','江西',
                                  '山东','河南','湖北','湖南','广东','海南','四川','贵州','云南','陕西','甘肃','青海','台湾')},
    **{name:name+'市' for name in ('北京','天津','上海','重庆')},
    '内蒙古':'内蒙古自治区','广西':'广西壮族自治区','西藏':'西藏自治区',
    '宁夏':'宁夏回族自治区','新疆':'新疆维吾尔自治区',
    '香港':'香港特别行政区','澳门':'澳门特别行政区',
}
SPECIAL = {'missing':'地区未记录','unrecognized':'地区待识别'}
CITY_SPECIAL = {'missing':'城市未记录','unrecognized':'城市待识别'}
_MISSING = {'','—','-','未知','未提供','未记录','地区未记录','暂无','暂无信息'}
_ALIASES = sorted([(alias,name) for name,full in PROVINCES.items() for alias in {name,full}],
                  key=lambda pair:len(pair[0]),reverse=True)


def validate_region(region, city):
    """校验地区筛选；参数：省份键及可选城市；返回：无，非法输入抛出ValueError。"""
    if not isinstance(region,str) or region not in {'',*PROVINCES,*SPECIAL}:
        raise ValueError('发货地区筛选无效')
    if not isinstance(city,str) or len(city)>40 or (city and (
            region not in PROVINCES or city not in CITY_SPECIAL and not re.fullmatch(r'[\u4e00-\u9fff·]{2,40}',city))):
        raise ValueError('发货城市筛选无效')


def region_label(region, city=''):
    """生成筛选说明；参数：已校验地区键和城市键；返回：便于查看的地区名称。"""
    label=SPECIAL.get(region,region)
    return label+(' / '+CITY_SPECIAL.get(city,city) if city else '')


def parse_location(raw):
    """保守归类地区原文；参数：列表地区；返回：省份和城市键，无法识别保留待识别分组。"""
    text=re.sub(r'\s+',' ',raw.strip()) if isinstance(raw,str) else ''
    if text in _MISSING:
        return {'region':'missing','city':''}
    matches=[(alias,name) for alias,name in _ALIASES if text.startswith(alias) and (
        alias!=name or len(text)==len(alias) or text[len(alias)] in ' ,，·/、')]
    if not matches:
        return {'region':'unrecognized','city':''}
    alias,province=matches[0]
    remainder=text[len(alias):].strip(' ,，·')
    # 两个省份、跨地发货等复合文字不能当成一个城市。
    if any(name!=province and re.search(r'(?:^|[\s/、,，])'+re.escape(other)+r'(?:$|[\s/、,，])',remainder)
           for other,name in _ALIASES):
        return {'region':'unrecognized','city':''}
    if province in {'北京','天津','上海','重庆','香港','澳门'}:
        city=province
    elif not remainder:
        city='missing'
    else:
        match=re.fullmatch(r'([\u4e00-\u9fff·]{2,25}?)(市|自治州|地区|盟)[\u4e00-\u9fff·]*',remainder)
        if match:
            city=match[1] if match[2]=='市' else match[1]+match[2]
        else:
            city=remainder if re.fullmatch(r'[\u4e00-\u9fff·]{2,10}',remainder) and not any(
                word in remainder for word in ('区','县','多地','全国','随机','发货','仓库')) else 'unrecognized'
    return {'region':province,'city':city}


def shipping_distribution(rows):
    """汇总发货地占比；参数：去重且已按广告筛选的完整样本；返回：省份及省内城市占比。"""
    groups=defaultdict(list)
    for row in rows:
        groups[row['shipping_region']['region']].append(row)
    total=len(rows)
    provinces=[]
    for key,members in sorted(groups.items(),key=lambda item:(item[0] in SPECIAL,-len(item[1]),item[0])):
        city_groups=defaultdict(int)
        if key in PROVINCES:
            for row in members:
                city_groups[row['shipping_region']['city']]+=1
        cities=[{'key':city,'label':CITY_SPECIAL.get(city,city),'count':count,
                 'percent':float(Decimal(count)*100/len(members))}
                for city,count in sorted(city_groups.items(),key=lambda item:(item[0] in CITY_SPECIAL,-item[1],item[0]))]
        provinces.append({'key':key,'label':SPECIAL.get(key,key),'count':len(members),
                          'percent':float(Decimal(len(members))*100/total),'cities':cities})
    missing=len(groups.get('missing',[]))
    return {'total':total,'recorded':total-missing,'missing':missing,
            'unrecognized':len(groups.get('unrecognized',[])),'rows':provinces}
