"""已生成V1.2证据和只读页面核对；不联网AI、不修改正式数据、不替代内容验收。"""
import argparse
import hashlib
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from ecommerce.infrastructure.ai.config import load_config, api_key
from tests.prompt_trials_v1_2 import contracts, processing, runner


def check(root):
    """核对实际证据、提示固定和桌面手机显示；参数：目录；返回：保存QA结果。"""
    manifest=runner.load(root/'manifest.json');summary=runner.load(root/'summary.json')
    assert 0<=len(summary['calls'])<=summary['call_intents']<=4
    assert summary['database_changes']==[] and summary['versions_unchanged']
    assert runner.load(root/'database-before.json')==runner.load(root/'database-after.json')
    gate=runner.load(root/'offline-result.json')
    assert gate['passed'] and gate['code_hashes']==runner.code_hashes()
    assert gate['log_sha256']==runner.checksum(root/'offline-tests.log')
    for label,checksum in manifest['prompt_hashes'].items():assert runner.checksum(root/(label+'-instruction.txt'))==checksum
    for label,checksum in manifest['schema_hashes'].items():assert runner.checksum(root/(label+'.schema.json'))==checksum
    before=runner.load(root/'baseline-reference-hashes.json')
    assert all(runner.checksum(path)==value for group in before.values() for path,value in group.items())
    total=0;evidence_count=0;phase_checks=[]
    for phase in runner.PHASES:
        if not (root/(phase+'-intent.json')).exists():
            phase_checks.append({'phase':phase,'status':'未请求'});continue
        if not (root/(phase+'-response.json')).exists():
            assert (root/(phase+'-error.json')).exists()
            phase_checks.append({'phase':phase,'status':'已请求但用量或结果未知，不重试'});continue
        intent=runner.load(root/(phase+'-intent.json'));response=runner.load(root/(phase+'-response.json'))
        request=(root/(phase+'-request.txt')).read_text(encoding='utf-8')
        # Windows文本文件会保存CRLF；预约记录校验的是实际发送文字的UTF-8，不是磁盘换行字节。
        assert hashlib.sha256(request.encode('utf-8')).hexdigest()==intent['request_sha256']
        assert len(request)<=60000
        value,errors=processing.decode(response['text'],response['finish_reason'])
        total+=response['usage'].get('total_tokens',0)
        if value is None:
            assert runner.load(root/(phase+'-parse-issues.json'))==errors
            phase_checks.append({'phase':phase,'status':'严格解析未通过，原响应保留','parse_issues':errors});continue
        assert value==runner.load(root/(phase+'-parsed.json'))
        result=runner.load(root/(phase+'-processed.json'))
        phase_checks.append({'phase':phase,'status':'可解析','structure_issues':len(result['structure_issues']),
                             'local_issues':len(result['issues']),'adjustments':len(result.get('adjustments',[]))})
        if phase.endswith('extract'):
            source=runner.load(root/('real-input.json' if phase=='real_extract' else 'synthetic-input.json'))
            by_id={row['id']:row for row in source['reviews']}
            assert {row['id'] for row in result['reviews']}<=set(by_id)
            for row in result['reviews']:
                segments={'initial':by_id[row['id']]['text'],**{a['segment']:a['text'] for a in by_id[row['id']]['appends']}}
                for e in row['evidence']:
                    assert e['quote'] in segments[e['segment']];evidence_count+=1
        else:
            incoming=runner.load(root/(phase+'-input.json'))
            assert result['compiled']['scope']==incoming['scope']
            assert all(row['id']<900000 for row in incoming['extraction']['reviews'])
            assert result['compiled']['review_status']=='unreviewed'
    assert total==summary['known_tokens']
    reserved=sum(runner.load(path)['reserved_tokens'] for path in root.glob('*-intent.json'))
    assert reserved<=manifest['max_reserved_tokens']
    if (root/'partial_report-input.json').exists():
        partial=runner.load(root/'partial_report-input.json')
        assert partial['scope']['sample_count']==16 and partial['scope']['analyzed_count']<=13
        assert partial['scope']['analyzed_count']+partial['scope']['pending_count']==16
        assert not {658,672,679}&{row['id'] for row in partial['extraction']['reviews']}
        assert all(issue['simulation'] for issue in partial['pending_issues'] if issue['review_id'] in {658,672,679})
    key=api_key(load_config(runner.PROJECT))
    for path in root.iterdir():
        if key and path.suffix in ('.json','.txt','.html','.log'):assert key not in path.read_text(encoding='utf-8')
    errors=[];external=[];checks=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.on('pageerror',lambda error:errors.append(str(error)))
            def guard(route):
                """只允许本机文件；参数：路由；返回：放行或阻止。"""
                if not route.request.url.startswith('file:///'):external.append(route.request.url);route.abort()
                else:route.continue_()
            page.route('**/*',guard)
            for width,height in ((1440,1000),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                for name in ('index.html','complete-report.html','partial-report.html','extraction.html'):
                    page.goto((root/name).as_uri())
                    expect(page.locator('h1')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),(name,width)
                    invalid=page.locator('a.ref').evaluate_all('(nodes)=>nodes.filter(n=>!document.getElementById(decodeURIComponent(n.hash.slice(1)))).map(n=>n.hash)')
                    assert not invalid,(name,invalid)
                    page.screenshot(path=str(root/(name.removesuffix('.html')+f'-{width}.png')))
                    checks.append({'file':name,'width':width,'horizontal_overflow':False,'broken_reference_links':0})
        finally:browser.close()
    assert not errors and not external
    value={'evidence_checks':'调用证据、派生原文、提示及核心固定、V1.1未改、来源隔离、统计范围、密钥排除和双库不变核对通过；模型格式及关联问题单列，不把失败当通过',
           'calls':len(summary['calls']),'known_tokens':total,'reserved_tokens':reserved,'exact_evidence_count':evidence_count,'phase_checks':phase_checks,
           'browser_checks':checks,'page_errors':errors,'external_requests':external,
           'quality_status':'存在已记录语义问题，未通过正式接入或用户人工验收；结构和页面检查不代表语义通过'}
    runner.write(root/'qa.json',value)
    print(json.dumps({k:v for k,v in value.items() if k!='browser_checks'},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    check(parser.parse_args().root.resolve())
