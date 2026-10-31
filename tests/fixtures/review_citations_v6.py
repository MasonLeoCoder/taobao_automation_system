"""仿照真实格式变化的脱敏样例，不包含真实买家或商品内容。"""
from ecommerce.modules.review_analysis.v6_checks import SECTIONS


def report_sample():
    """生成100条来源、66处引文、65处匹配/1处待核对/33条有效来源；参数：无；返回：来源和Markdown。"""
    sources=[{'id':601+n,'text':f'脱敏反馈{n}，使用方便，具体性能待实物验证。','sku':'脱敏测试规格','appends':[]} for n in range(100)]
    citations=[]
    for n in range(66):
        source=sources[n%33];quote=source['text'] if n else '脱敏反馈0……具体性能待实物验证。'
        citations.append(f'【{source["id"]}｜初评】原文：“{quote}”')
    text='\n\n'.join('## '+name+'\n脱敏报告，仅验证程序格式兼容。'+('\n\n'+'\n\n'.join(citations) if i==0 else '') for i,name in enumerate(SECTIONS))
    return sources,text


def mixed_report_sample():
    """构造列表、混排、追评和独立依据的脱敏旧稿；参数：无；返回：七章Markdown。"""
    first='【8｜初评】原文：“刚买很好用”';second='【9｜初评】原文：“很满意”'
    append='【8｜追评1】原文：“用了几天粘不住”'
    contents=[
        '总体分析始终可见，仅用于排版验证。',
        '- **场景甲：** '+first+' '+second+'\n- **场景乙：** '+append,
        '认可表现始终可见。\n'+first+'\n'+second,
        '问题说明：'+append+' 后续分析始终可见。',
        '1. 主要任务：'+second+'\n\n    - 子场景：'+first,
        '建议始终可见。原文依据：\n\n'+first+'\n\n'+second,
        '分析文字与'+first+'，此类句中嵌入保持可见。',
    ]
    return '\n\n'.join('## '+name+'\n\n'+body for name,body in zip(SECTIONS,contents))
