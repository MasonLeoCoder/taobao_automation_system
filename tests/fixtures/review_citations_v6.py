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
