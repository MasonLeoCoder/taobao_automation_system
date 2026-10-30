"""独立试验的中文只读报告；模型内容全部转义，页面不加载脚本或外部资源。"""
import argparse
import html
import json
from pathlib import Path
from tests.prompt_trials.runner import load


def esc(value):
    """转义文字和属性；参数：对象；返回：安全HTML文字。"""
    if value is None:return '未提供'
    if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False)
    return html.escape(str(value))


def ul(values):
    """渲染安全列表；参数：数组；返回：HTML。"""
    return '<ul>'+''.join('<li>'+esc(v)+'</li>' for v in values)+'</ul>' if values else '<p class="muted">暂无</p>'


def link(ids):
    """渲染已有报告编号；参数：编号数组；返回：锚点链接。"""
    return ' '.join('<a class="ref" href="#'+esc(uid)+'">'+esc(uid)+'</a>' for uid in ids) or '<span class="muted">暂无</span>'


def erefs(refs):
    """渲染原文依据定位；参数：引用数组；返回：链接。"""
    return ' '.join('<a class="ref" href="#ev-'+esc(str(r['review_id'])+'-'+r['evidence_id'])+'">评价 '+esc(r['review_id'])+' · '+esc(r['evidence_id'])+'</a>' for r in refs)


def page(root,title,body):
    """生成自包含响应式页面；参数：目录、标题和安全正文；返回：完整HTML。"""
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+esc(title)+'''</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f7f5;color:#203e38;font:16px/1.8 "Microsoft YaHei",sans-serif}main{max-width:1120px;margin:auto;padding:32px 24px 60px}h1{font-size:29px;line-height:1.4}h2{font-size:23px;margin-top:0}h3{font-size:19px}nav{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:24px}a{color:#146f61;overflow-wrap:anywhere}section,.card{background:white;padding:24px;border:1px solid #dae7e1;border-radius:15px;margin:20px 0}.banner{padding:18px 22px;border-left:4px solid #b48021;border-radius:8px;background:#fff3d7;color:#74531d}.muted{color:#657d74;font-size:14px}.ref,.badge{display:inline-block;background:#eaf3ed;border-radius:8px;padding:2px 9px;margin:4px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.grid .card{margin:0}blockquote{margin:12px 0;border-left:3px solid #79a99a;background:#f4f8f5;padding:12px 16px;white-space:pre-wrap;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}details{border-top:1px solid #e0e9e4;padding:10px 0}summary{cursor:pointer;font-weight:600}td,th{padding:10px;text-align:left;vertical-align:top;border-bottom:1px solid #e0e9e4;overflow-wrap:anywhere}th{background:#edf4ef}table{width:100%;border-collapse:collapse;font-size:14px}.table{overflow:auto}article{scroll-margin-top:12px}li{margin:6px 0}.stat{background:#eef6f1;padding:10px;border-radius:8px;margin:8px 0}.fact{border-left:3px solid #94b7a7;padding-left:14px;margin:14px 0}.interpret{background:#f6f7fa;padding:14px;border-radius:8px}footer{font-size:13px;color:#657d74;margin-top:30px}@media(max-width:600px){main{padding:20px 12px 45px}h1{font-size:25px}section,.card{padding:17px}.grid{grid-template-columns:1fr}td,th{padding:8px;min-width:90px}}
</style><main><nav><a href="index.html">V1.1验证总览</a><a href="complete-report.html">完整样例</a><a href="partial-report.html">阶段样例</a><a href="extraction.html">原文提取对照</a></nav><h1>'''+esc(title)+'''</h1>'''+body+'<footer>试验 '+esc(root.name)+' · 商品1050158363561 · 本库商品3 · 批次96。全部为本地只读文件，未接入正式报告。</footer></main></html>'


def render_report(root,phase,filename,title):
    """渲染局部有效报告并保留全部错误；参数：目录、阶段、文件名及标题；返回：无。"""
    incoming=load(root/(phase+'-input.json'));scope=incoming['scope']
    response=load(root/(phase+'-response.json'))
    processed=root/(phase+'-processed.json')
    body='<div class="banner"><b>真实复测样例 · 尚未用户人工验收</b><p>展示程序可追溯的派生结果。结构检查与内容判断分开，疑点及被隔离内容保留在下方诊断中。</p></div>'
    body+='<section><h2>程序生成的范围</h2><p>选样 '+esc(scope['sample_count'])+' 条，纳入 '+esc(scope['analyzed_count'])+' 条，待处理 '+esc(scope['pending_count'])+' 条。'+esc(scope['report_stage'])+'</p>'+ul(scope['limitations'])
    if incoming['pending_issues']:body+='<p class="muted">'+esc(incoming['pending_issues'])+'</p>'
    body+='</section>'
    if not processed.exists():
        body+='<section><h2>原返回未形成可解析报告</h2><p>未自动重试或格式修复。</p><pre>'+esc(response['text'])+'</pre></section>'
        (root/filename).write_text(page(root,title,body),encoding='utf-8');return
    result=load(processed);data=result['compiled']
    body+='<section><h2>本次诊断</h2><p>原始结构问题 '+esc(len(result['structure_issues']))+' 项；局部关联问题 '+esc(len(result['issues']))+' 项；格式警示 '+esc(len(result['warnings']))+' 项。语义仍需逐项复核。</p><details><summary>展开全部诊断与未改写的原始结果</summary><pre>'+esc({k:v for k,v in result.items() if k!='compiled'})+'</pre><pre>'+esc(load(root/(phase+'-parsed.json')))+'</pre></details></section>'
    body+='<section><h2>决策摘要</h2>'
    for item in data['decision_summary']:
        body+='<article><p>'+esc(item['text'])+'</p>'+link(item['finding_ids'])+' '+link(item['opportunity_ids'])+'<p class="muted">限制：'+esc(item['limitation'])+'</p></article>'
    body+='</section>'
    obs={(r['id'],o['observation_id']):o for r in incoming['extraction']['reviews'] for o in r['observations']}
    stats={s['id']:s for s in incoming['statistics']}
    for category in ('购买需求与场景','认可表现','问题与需求','规格与取舍','竞品综合'):
        body+='<section><h2>'+category+'</h2>'
        for finding in data['findings']:
            if finding['category']!=category:continue
            body+='<article id="'+esc(finding['id'])+'"><h3>'+esc(finding['id'])+' <span class="badge">'+esc(finding['evidence_strength'])+'</span></h3>'
            for fact in finding['facts']:
                scopes=sorted({obs[(ref['review_id'],ref['observation_id'])]['event_scope'] for ref in fact['observation_refs']})
                body+='<div class="fact"><b>买家表述</b><p>'+esc(fact['text'])+'</p>'+erefs(fact['evidence_refs'])+'<p class="muted">来源事件归属：'+esc('、'.join(scopes))+'</p></div>'
            interpretation=finding['interpretation']
            if interpretation:body+='<div class="interpret"><b>分析推断</b><p>'+esc(interpretation['text'])+'</p><p class="muted">成立假设</p>'+ul(interpretation['assumptions'])+ul(interpretation['limitations'])+'</div>'
            for selected in finding['statistics']:
                stat=stats[selected['stat_id']]
                body+='<div class="stat"><b>程序统计：</b>'+esc(stat['label'])+' · '+esc(stat['event_scope'])+' · '+esc(stat['count'])+' 条评价记录<p class="muted">'+esc(selected['meaning'])+'；来源编号 '+esc(stat['review_ids'])+'；'+esc(stat['id'])+'</p></div>'
            body+='<p class="muted">证据解释：'+esc(finding['strength_reason'])+'</p>'+ul(finding['limitations'])+'</article>'
        body+='</section>'
    body+='<section><h2>选品方向</h2>'
    for card in data['opportunities']:
        body+='<article class="card" id="'+esc(card['id'])+'"><h3>'+esc(card['name'])+'</h3><span class="badge">'+esc(card['stage'])+'</span><span class="badge">'+esc(card['priority'])+'</span>'
        for key,label in [('target_need','目标需求'),('product_direction','具体方向'),('reasoning','推断理由'),('priority_reason','排序理由'),('independence_reason','方向差异')]:body+='<p><b>'+label+'：</b>'+esc(card[key])+'</p>'
        body+='<p>支持：'+link(card['supporting_finding_ids'])+'</p><h4>已有反证</h4>'
        for counter in card['counter_evidence']:body+='<p>'+link([counter['finding_id']])+' '+esc(counter['explanation'])+'</p>'
        if not card['counter_evidence']:body+='<p class="muted">没有列出已有反证，不代表已证明不存在风险。</p>'
        for key,label in [('risk_hypotheses','待验证风险假设'),('conditions','成立条件'),('missing_information','待补信息'),('validation_actions','验证动作'),('stop_conditions','放弃或降级条件')]:body+='<h4>'+label+'</h4>'+ul(card[key])
        body+='</article>'
    body+='</section><section><h2>经营资料检查</h2>'
    for item in data['business_checks']:body+='<details><summary>'+esc(item['item'])+' · '+esc(item['status'])+'</summary><p>已有：'+esc(item['known_information'])+'</p><p>待补：'+esc(item['missing_information'])+'</p><p>影响：'+esc(item['decision_impact'])+'</p></details>'
    body+='</section><section><h2>下一步验证任务</h2>'
    for item in data['next_actions']:body+='<article><h3>'+esc(item['order'])+' · '+esc(item['action'])+'</h3><p>'+esc(item['purpose'])+'</p><p>继续条件：'+esc(item['continue_condition'])+'</p><p>停止条件：'+esc(item['stop_condition'])+'</p></article>'
    body+='</section><section><h2>资料问题和限制</h2>'
    for item in data['data_issues']:body+='<details><summary>'+esc(item['description'])+'</summary><p>'+esc(item['affected_sections'])+'</p><p>'+esc(item['handling'])+'</p></details>'
    body+='</section><section><h2>原文依据</h2>'
    for row in incoming['extraction']['reviews']:
        for e in row['evidence']:body+='<article id="ev-'+esc(str(row['id'])+'-'+e['evidence_id'])+'"><p class="muted">评价 '+esc(row['id'])+' · '+esc(e['evidence_id'])+' · '+esc(e['segment'])+'</p><blockquote>'+esc(e['quote'])+'</blockquote></article>'
    body+='</section>'
    (root/filename).write_text(page(root,title,body),encoding='utf-8')


def extraction_page(root):
    """生成真实/合成原文及局部派生对照；参数：目录；返回：无。"""
    body='<div class="banner">整体状态为程序派生；观点是模型提取。原返回保持不变，结构合格不表示内容已验收。合成样例单独标注。</div>'
    for phase,inputfile,title in [('real_extract','real-input.json','真实16条样例'),('synthetic_extract','synthetic-input.json','合成8条边界样例')]:
        source={r['id']:r for r in load(root/inputfile)['reviews']};result=load(root/(phase+'-processed.json'))
        body+='<section><h2>'+title+'</h2>'
        for row in result['reviews']:
            body+='<article class="card"><h3>评价 '+esc(row['id'])+' <span class="badge">'+esc(row['information_status'])+'</span></h3><blockquote>'+esc(source[row['id']]['text'])+'</blockquote>'
            for a in source[row['id']]['appends']:body+='<blockquote>追评：'+esc(a['text'])+'</blockquote>'
            body+='<p>使用阶段：'+esc(row['usage_stages'])+'</p><div class="table"><table><thead><tr><th>主题/类型</th><th>事实概括</th><th>来源/时间归属</th><th>疑点与统计资格</th></tr></thead><tbody>'
            for o in row['observations']:body+='<tr><td>'+esc(o['topic'])+'<br>'+esc(o['information_types'])+'</td><td>'+esc(o['fact_text'])+'</td><td>'+esc(o['source_type'])+'<br>'+esc(o['event_scope'])+'</td><td>'+esc(o['uncertainties'])+'<br>基础事实可计入：'+esc(o['eligibility']['basic_fact'])+'</td></tr>'
            body+='</tbody></table></div><details><summary>全部证据与原结构</summary><pre>'+esc(row)+'</pre></details></article>'
        body+='</section>'
    (root/'extraction.html').write_text(page(root,'V1.1 原文与提取对照',body),encoding='utf-8')


def overview(root):
    """渲染实际用量、离线及内容复核结果；参数：目录；返回：无。"""
    summary=load(root/'summary.json');gate=load(root/'offline-result.json')
    review=load(root/'quality-review.json') if (root/'quality-review.json').exists() else {'conclusion':'内容复核待完成','checks':[]}
    body='<div class="banner"><b>'+esc(review['conclusion'])+'</b><p>本轮最多四次请求，不自动重试；发现问题后先讨论下一版方案。页面展示不是用户人工验收。</p></div>'
    unchanged=not summary['database_changes'] and summary['versions_unchanged']
    body+='<section><h2>实际范围</h2><p>16条真实评价 + 8条独立合成样例；实际 '+esc(len(summary['calls']))+' 次请求，已知 '+esc(summary['known_tokens'])+' Tokens。</p><p>离线 '+esc(gate['test_count'])+' 项通过；阶段报告模拟排除三条，不是正式任务新增失败。</p><p>'+('正式及验证库业务记录均未变化，数据库版本保持0013。' if unchanged else '数据库前后存在变化，需查看核对记录。')+'</p></section>'
    body+='<section><h2>查看结果</h2><div class="grid"><a class="card" href="complete-report.html">完整样例报告<br>事实、推断和程序数字分开展示</a><a class="card" href="partial-report.html">阶段样例报告<br>模拟部分尺寸信息待处理</a><a class="card" href="extraction.html">原文与提取对照<br>真实、合成单独查看</a></div></section>'
    body+='<section><h2>与V1问题逐项对照</h2>'
    for item in review['checks']:body+='<details open><summary>'+esc(item['item'])+' · '+esc(item['status'])+'</summary><p>'+esc(item['detail'])+'</p></details>'
    body+='</section><section><h2>调用和文件证据</h2><div class="table"><table><thead><tr><th>阶段</th><th>总Token</th><th>结束原因</th></tr></thead><tbody>'
    for item in summary['calls']:body+='<tr><td>'+esc(item['phase'])+'</td><td>'+esc(item['usage'].get('total_tokens'))+'</td><td>'+esc(item['finish_reason'])+'</td></tr>'
    body+='</tbody></table></div><p><a href="summary.json">调用与数据库核对</a> · <a href="quality-review.json">内容复核原表</a></p></section>'
    (root/'index.html').write_text(page(root,'竞品评价提示词 V1.1 · 复测结果',body),encoding='utf-8')


def render(root):
    """输出全部四个只读页面；参数：证据目录；返回：无。"""
    render_report(root,'complete_report','complete-report.html','V1.1 完整样例报告')
    render_report(root,'partial_report','partial-report.html','V1.1 阶段样例报告')
    extraction_page(root);overview(root)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    render(parser.parse_args().root)
