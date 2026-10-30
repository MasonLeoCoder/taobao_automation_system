"""收尾报告静态展示；只读、无脚本、转义全部输入，原稿单独保留。"""
import html
from pathlib import Path
from tests.prompt_trials_v1_2.runner import load
from tests.report_closeout.core import FIELD_LABELS


def esc(value):
    """转义模型和人工文字；参数：对象；返回：安全HTML。"""
    return html.escape(str(value if value is not None else '未提供'))


def lines(values):
    """输出安全列表；参数：文字数组；返回：HTML。"""
    return '<ul>'+''.join('<li>'+esc(v)+'</li>' for v in values)+'</ul>' if values else '<p class="muted">暂无</p>'


def refs(values):
    """输出观点锚点；参数：引用数组；返回：HTML。"""
    return ' '.join('<a class="ref" href="#obs-'+esc(str(r['review_id'])+'-'+r['observation_id'])+'">评价'+esc(r['review_id'])+' · '+esc(r['observation_id'])+'</a>' for r in values)


def review_summary(reports):
    """汇总当前修订人工决定，不沿用旧状态；参数：报告数组；返回：三种状态数量。"""
    counts={'待复核':0,'已采用':0,'不采用':0}
    for report in reports:
        for item in report['review_items']:counts[item['review_status']]+=1
    return counts


def shell(title,body):
    """封装电脑手机自适应页面；参数：标题、安全正文；返回：HTML。"""
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+esc(title)+'''</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f7f5;color:#203e38;font:16px/1.8 "Microsoft YaHei",sans-serif}main{max-width:1120px;margin:auto;padding:28px 24px 60px}h1{font-size:29px;line-height:1.4}h2{font-size:23px}h3{font-size:19px}nav{display:flex;gap:18px;flex-wrap:wrap}a{color:#146f61;overflow-wrap:anywhere}section,article.card{background:#fff;padding:24px;border:1px solid #dbe8e1;border-radius:15px;margin:20px 0}.banner{background:#fff3d7;border-left:4px solid #b48021;border-radius:8px;padding:16px 20px;margin:14px 0;color:#74531d}.muted{color:#657d74;font-size:14px}.badge,.ref{display:inline-block;padding:2px 9px;margin:3px;background:#eaf3ed;border-radius:8px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}.grid>article.card{margin:0}.stat{background:#f1f7f3;padding:16px;border-radius:9px;margin:12px 0}details{border-top:1px solid #dae7e1;padding:12px 0}summary{cursor:pointer;font-weight:600}blockquote{border-left:3px solid #91b4a4;background:#f4f8f5;margin:10px 0;padding:10px 15px;white-space:pre-wrap;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere}p,li{overflow-wrap:anywhere}article{scroll-margin-top:12px}.check{border-left:3px solid #86aa99;padding-left:16px;margin:20px 0}footer{font-size:13px;color:#657d74;margin-top:30px}@media(max-width:600px){main{padding:18px 12px 45px}h1{font-size:25px}section,article.card{padding:17px}.grid{grid-template-columns:1fr}}
</style><main><nav><a href="index.html">收尾总览</a><a href="complete-report.html">完整样例</a><a href="partial-report.html">阶段样例</a><a href="expressions.html">表达与依据</a></nav><h1>'''+esc(title)+'''</h1>'''+body+'<footer>本地离线派生 · 新增AI调用0次 · 未接入正式页面。买家自述和人工采用均不等于实物已验证。</footer></main></html>'


def evidence_view(view):
    """渲染唯一观点及全部原文；参数：观点视图；返回：HTML。"""
    body='<article id="obs-'+esc(str(view['review_id'])+'-'+view['observation_id'])+'"><h3>评价 '+esc(view['review_id'])+' · '+esc(view['topic'])+'</h3>'
    body+='<p>'+esc(view['fact_text'])+'</p><p class="muted">'+esc(view['source_type'])+' · '+esc(view['event_scope'])+' · '+esc(view['sentiment'])+' · '+esc(view['expression_status'])+'</p>'
    body+='<p class="muted">'+esc(view['performance_status'])+'</p>'
    if view['usage_stages']:
        stages=[]
        for stage in view['usage_stages']:
            segment='初评' if stage['segment']=='initial' else '追评'+stage['segment'].split(':')[-1]
            stages.append(segment+'：'+stage['stage']+('，原文时间/次数：'+stage['duration_text'] if stage['duration_text'] else '，时长未说明'))
        body+='<p class="muted">本条阶段记录：'+esc('；'.join(stages))+'</p>'
    for e in view['evidence']:body+='<blockquote>'+esc(e['quote'])+'</blockquote><p class="muted">'+esc(e['segment'])+' · '+esc(e['evidence_id'])+'</p>'
    if view['uncertainties']:
        body+='<div class="banner">保留原疑点：'+lines(['、'.join(FIELD_LABELS.get(f,f) for f in u['fields'])+'：'+u['reason'] for u in view['uncertainties']])+'</div>'
    return body+'</article>'


def report_page(root,revision,phase,title):
    """显示经程序核对的来源与待确认建议；参数：目录、修订、阶段、标题；返回：页面正文。"""
    data=load(revision/(phase+'.json'));scope=data['scope'];items={i['id']:i for i in data['review_items']}
    baseline=Path(load(root/'manifest.json')['baseline'])
    body='<div class="banner"><b>助手修订建议 · '+esc(data['review_status'])+'</b><p>原AI稿与本次派生分开保存。编号、范围和统计由程序生成；方向与反证需逐项核对后采用，没有自动标记人工通过。</p></div>'
    body+='<section><h2>本次范围</h2><p>选样 '+esc(scope['sample_count'])+' 条，纳入 '+esc(scope['analyzed_count'])+' 条，待处理 '+esc(scope['pending_count'])+' 条。</p>'+lines(scope['limitations'])+'</section>'
    body+='<section id="inquiry"><h2>先询价与核对规格，再决定拿样</h2><p>'+esc(data['business_gate']['description'])+'</p>'
    for item in data['business_checks']:body+='<details><summary>'+esc(item['item'])+' · '+esc(item['status'])+'</summary><p>已有：'+esc(item['known_information'])+'</p><p>待补：'+esc(item['missing_information'])+'</p><p>影响：'+esc(item['decision_impact'])+'</p></details>'
    body+='</section><section><h2>优先验证清单 · 待确认</h2><p class="muted">先完成上方基本资料核对。下列顺序是助手提出的验证安排，不是程序频次排名或采购结论。</p>'
    for card in data['cards']:
        proposal=card['proposal'];body+='<article class="card" id="'+esc(card['id'])+'"><h3>'+esc(proposal['name'])+'</h3><span class="badge">'+esc(card['review_status'])+'</span><p>'+esc(proposal['reason'])+'</p>'+refs(proposal['basis'])
        body+='<div class="banner">'+esc(card['limitation'])+' 涉及本方向的反证、修订建议及来源限制见本卡片与下方资料说明。</div>'
        if card['pending_review_ids']:body+='<div class="banner">本阶段暂缺评价 '+esc(card['pending_review_ids'])+'；当前来源不完整，不能补写缺失原文或认定未提及的问题不存在。尺寸与适配等结论需保留本阶段缺口限制。</div>'
        for check in proposal['checks']:
            body+='<div class="check"><p><b>需要核对：</b>'+esc(check['problem'])+'</p><p><b>'+esc(check['action_type'])+'：</b>'+esc(check['action'])+'</p><p><b>继续条件：</b>'+esc(check['continue_condition'])+'</p><p><b>淘汰/暂停范围：</b>'+esc(check['stop_scope'])+'</p><p><b>触发条件：</b>'+esc(check['stop_condition'])+'</p></div>'
        body+='<h4>原AI反证与助手复核</h4>'
        for counter in card['counter_reviews']:
            item=items[counter['item_id']]
            body+='<details><summary>'+esc(item['review_status'])+' · 原反证 '+esc(counter['original']['finding_id']+'/'+counter['original']['fact_id'])+'</summary><p>原AI解释：'+esc(counter['original']['explanation'])+'</p><div class="banner">助手意见：'+esc(counter['assistant_note'])+'</div>'+refs(counter['original']['inherited_observation_refs'])+'</details>'
        body+='<p><b>复核清单：</b></p>'
        for item in data['review_items']:
            if item['card_id']!=card['id']:continue
            body+='<p><span class="badge">'+esc(item['review_status'])+'</span> '+esc(item['kind'])+'</p><details><summary>查看复核编号（登记决定用）</summary><p class="muted">'+esc(item['id'])+'</p></details>'
            if 'decision_record' in item:body+='<p>填写人：'+esc(item['decision_record']['reviewer'])+'；意见：'+esc(item['decision_record']['comment'])+'</p>'
        body+='</article>'
    body+='</section><section><h2>发现与证据索引</h2><p class="muted">每个观点在原文库只展开一次。同一组观点在不同栏目出现时引用已有内容，不增加证据分量；同义主题未自动合并。</p>'
    for f in data['findings']:
        body+='<article id="'+esc(f['id'])+'"><h3>'+esc(f['id'])+' · '+esc(f['category'])+'</h3>'
        if f['canonical_id']!=f['id']:body+='<p>与 <a class="ref" href="#'+esc(f['canonical_id'])+'">'+esc(f['canonical_id'])+'</a> 使用相同观点集合，集中查看，不重复展开。</p>'
        else:body+=refs(f['observation_refs'])
        body+='<p>相关程序统计：'+' '.join('<a class="ref" href="#'+esc(sid)+'">查看统计</a>' for sid in f['statistic_ids'])+'</p></article>'
    body+='</section><section><h2>程序统计：提及与倾向分开</h2><p>统计不是已验证性能。正面、负面等分别去重；同条可能包含不同倾向，不能相加。来源类别也可能交叉。</p>'
    selected={sid for f in data['findings'] for sid in f['statistic_ids']}
    for stat in data['statistics']:
        if stat['id'] not in selected:continue
        body+='<article class="stat" id="'+esc(stat['id'])+'"><b>'+esc(stat['label'])+' · '+esc(stat['event_scope'])+' · 提及 '+esc(stat['count'])+' 条</b><p>'+' ｜ '.join(esc(s)+' '+esc(v['count'])+'条' for s,v in stat['sentiments'].items())+'</p>'
        for s,value in stat['sentiments'].items():
            if value['count']:body+='<p class="muted">'+esc(s)+'来源：'+esc(value['review_ids'])+'</p>'
        body+='<details><summary>查看各自原文与来源类别</summary>'+refs(stat['observation_refs'])+lines([s+'：'+str(v['count'])+'条，评价编号 '+str(v['review_ids']) for s,v in stat['sources'].items()])+'</details></article>'
    body+='</section><section><h2>程序生成的资料说明</h2>'
    for issue in data['program_issues']:body+='<p>'+esc(issue['description'])+'</p>'
    body+='<p class="muted">模型自由填写的资料说明不作为此处来源说明。报告存在原始结构问题 '+esc(len(data['diagnostics']['structure_issues']))+' 项、局部关联问题 '+esc(len(data['diagnostics']['issues']))+' 项；可能对应同一处错误，不相加为失败评价数。</p></section>'
    body+='<section><h2>买家表达与原文库</h2>'
    for v in data['views']:body+=evidence_view(v)
    body+='</section><section><h2>保留原稿与比较</h2><p><a href="'+esc((baseline/('complete-report.html' if phase=='complete_report' else 'partial-report.html')).as_uri())+'">查看未覆盖的V1.2原报告</a> · <a href="'+esc((baseline/(phase+'-response.json')).as_uri())+'">原AI返回</a> · <a href="'+esc(phase+'.json')+'">本次派生与复核状态</a></p><p>本次没有重新生成AI推断；未核验的摘要、反证和行动不会自动成为已采用结论。</p></section>'
    return shell(title,body)


def render(root,revision):
    """输出新修订四份静态页面；参数：根目录及新修订目录；返回：无。"""
    for phase,name,title in [('complete_report','complete-report.html','评价报告收尾 · 完整16条样例'),('partial_report','partial-report.html','评价报告收尾 · 阶段13条样例')]:
        (revision/name).write_text(report_page(root,revision,phase,title),encoding='utf-8')
    expressions=load(revision/'expression-views.json');body='<div class="banner">'+esc(expressions['note'])+'</div>'
    for key,title in [('real','真实评价表达'),('synthetic','独立合成边界表达')]:
        body+='<section><h2>'+title+'</h2>'
        for v in expressions[key]:body+=evidence_view(v)
        body+='</section>'
    (revision/'expressions.html').write_text(shell('表达、印象与性能验证分开',body),encoding='utf-8')
    counts=review_summary([load(revision/(phase+'.json')) for phase in ('complete_report','partial_report')])
    body='<div class="banner"><b>已有返回离线收尾</b><p>'+esc('；'.join(s+' '+str(n)+'项' for s,n in counts.items()))+'</p><p>新增AI调用0次，新增Tokens 0。正式页面和数据库未改；本次只生成新的本地派生修订。</p></div><section><h2>查看收尾报告</h2><div class="grid"><article class="card"><a href="complete-report.html">完整样例报告</a><p>16条样例的验证清单、倾向统计和原文。</p></article><article class="card"><a href="partial-report.html">阶段样例报告</a><p>13条纳入，3条模拟暂缺，不是正式任务新增失败。</p></article><article class="card"><a href="expressions.html">表达与依据对照</a><p>外观、转述、使用自述分别保留；合成独立。</p></article></div></section>'
    body+='<section><h2>本轮变化</h2>'+lines(['来源编号与资料问题由程序生成，不沿用AI文字中的错误归因。','提及总数与正负面分开展示，相同证据集中引用。','每个问题对应具体动作，明确淘汰当前款、供应商或暂停组合的范围。','基本询价/规格核验提前；方向与反证默认待复核，没有助手代替用户采用。'])+'</section>'
    body+='<section><h2>复核入口</h2><p>每个方向下列出条目状态及意见。用户可通过本地命令明确采用或不采用，再另存修订，不覆盖历史。当前没有新的正式Web审核入口。</p><p>采用只是对报告建议作出人工决定，不代表实物性能、利润或供应条件已经验证。</p></section>'
    (revision/'index.html').write_text(shell('竞品评价报告 · 离线收尾',body),encoding='utf-8')
