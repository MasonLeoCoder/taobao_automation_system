"""已有结果回放和静态页面核对；禁止创建AI客户端，不写业务数据库。"""
import argparse
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch
from playwright.sync_api import sync_playwright,expect
from ecommerce.infrastructure.ai.config import load_config,api_key
from tests.prompt_trials_v1_2 import runner as frozen
from tests.report_closeout import core,runner


def check(root,revision):
    """核对真实旧返回、新派生、离线测试及页面；参数：根目录、修订；返回：保存结果。"""
    baseline,binding=runner.verify_baseline(root)
    gate=subprocess.run([sys.executable,'-m','unittest','tests.report_closeout.test_core','-v'],capture_output=True,text=True,encoding='utf-8',errors='replace')
    if (revision/'offline-tests.log').exists():raise ValueError('本修订验证日志已固定，不原地覆盖')
    (revision/'offline-tests.log').write_text(gate.stdout+gate.stderr,encoding='utf-8')
    if gate.returncode:raise ValueError('离线检查失败')
    tests=int(re.search(r'Ran (\d+) tests',gate.stdout+gate.stderr).group(1))
    with patch('ecommerce.infrastructure.ai.client.create_client',side_effect=AssertionError('本轮禁止AI请求')),patch.object(frozen,'create_client',side_effect=AssertionError('本轮禁止AI请求')):
        replay=runner.reports(root)
    receipts=runner.read_receipts(root)
    counts={};quote_count=0;unique_quotes=set();pending_count=0
    real_source=frozen.load(baseline/'real-input.json')
    source={r['id']:r for r in real_source['reviews']}
    for phase,value in replay.items():
        reviewed=core.apply_decisions(value,receipts)
        assert reviewed==frozen.load(revision/(phase+'.json'))
        pending_count+=sum(i['review_status']=='待复核' for i in reviewed['review_items'])
        original=frozen.load(baseline/(phase+'-input.json'))
        assert value['scope']==original['scope']
        assert {v['review_id'] for v in value['views']}=={r['id'] for r in original['extraction']['reviews']}
        assert not any(v['review_id']>=900000 for v in value['views'])
        lookup={s['id']:s for s in value['statistics']}
        for stat in original['statistics']:
            actual=lookup[stat['id']]
            for key in ('count','review_ids','observation_refs'):assert actual[key]==stat[key]
        for view in value['views']:
            segments={'initial':source[view['review_id']]['text'],**{a['segment']:a['text'] for a in source[view['review_id']]['appends']}}
            for e in view['evidence']:
                assert e['quote'] in segments[e['segment']];quote_count+=1
                unique_quotes.add((view['review_id'],e['evidence_id']))
        assert all(i['review_status']=='待复核' for i in value['review_items'])
        assert not any(i['review_id']==696 and i['kind']=='原提取标记' and '局部难以理解' in i['description'] for i in value['program_issues'])
        assert any(i['review_id']==694 and '局部难以理解' in i['description'] for i in value['program_issues'])
        counts[phase]={'views':len(value['views']),'review_items':len(value['review_items']),
                       'canonical_findings':sum(f['id']==f['canonical_id'] for f in value['findings']),
                       'program_issues':len(value['program_issues'])}
    complete=replay['complete_report'];partial=replay['partial_report']
    thick=next(s for s in complete['statistics'] if s['label']=='厚度' and s['kind']=='主题')
    assert thick['count']==2 and thick['sentiments']['正面']['review_ids']==[696] and thick['sentiments']['负面']['review_ids']==[663]
    assert not {658,672,679}&{v['review_id'] for v in partial['views']}
    assert all(c['pending_review_ids']==[658,672,679] for c in partial['cards'])
    expressions=frozen.load(revision/'expression-views.json')
    for uid in (900001,900002):
        v=next(v for v in expressions['synthetic'] if v['review_id']==uid)
        assert not v['eligibility']['basic_fact'] and '未独立验证' in v['performance_status']
    key=api_key(load_config(frozen.PROJECT))
    for path in root.rglob('*'):
        if path.is_file() and path.suffix in ('.json','.html','.txt','.py','.log') and key:assert key not in path.read_text(encoding='utf-8')
    checks=[];errors=[];requests=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.on('pageerror',lambda err:errors.append(str(err)))
            def guard(route):
                """允许本地文件且阻止网络；参数：请求；返回：放行或拦截。"""
                if route.request.url.startswith('file:///'):route.continue_()
                else:requests.append(route.request.url);route.abort()
            page.route('**/*',guard)
            for width,height in ((1440,1000),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                for name in ('index.html','complete-report.html','partial-report.html','expressions.html'):
                    page.goto((revision/name).as_uri());expect(page.locator('h1')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(name,width)
                    broken=page.locator('a.ref').evaluate_all('(nodes)=>nodes.filter(n=>!document.getElementById(decodeURIComponent(n.hash.slice(1)))).map(n=>n.hash)')
                    assert not broken,(name,broken)
                    assert page.locator('script,iframe').count()==0
                    assert not page.locator('[onerror], [onclick]').count()
                    page.screenshot(path=str(revision/(name[:-5]+f'-{width}.png')))
                    checks.append({'file':name,'width':width,'overflow':False,'broken_anchors':0})
                    if name=='complete-report.html':
                        expect(page.get_by_text('厚度 · 当前购买 · 提及 2 条',exact=True)).to_be_visible()
                        assert '正面 1条' in page.locator('#'+thick['id']).inner_text()
                        assert '负面 1条' in page.locator('#'+thick['id']).inner_text()
                    if name=='partial-report.html':assert page.locator('#p1').get_by_text('本阶段暂缺评价',exact=False).count()==1
        finally:browser.close()
    assert not errors and not requests
    runner.verify_baseline(root)
    output={'offline_tests':tests,'offline_passed':True,'baseline_unchanged':True,'new_ai_calls':0,'added_tokens':0,
        'replay_client_creation_blocked':True,'phase_checks':counts,'quote_reference_checks':quote_count,
        'unique_real_evidence':len(unique_quotes),'pending_review_items':pending_count,
        'thickness_example':thick,'browser_checks':checks,'page_errors':errors,'external_requests':requests,
        'review_status':f'仍有{pending_count}项待复核；本地人工采用不代表实物验证或正式接入',
        'code_hashes':{p.name:frozen.checksum(p) for p in Path(__file__).parent.glob('*.py')}}
    frozen.write(revision/'qa.json',output)
    print(f'offline={tests}; browser={len(checks)}; quote-reference checks={quote_count}; added AI calls=0; pending review items={pending_count}')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--revision',type=Path,required=True)
    args=parser.parse_args();check(args.root.resolve(),args.revision.resolve())
