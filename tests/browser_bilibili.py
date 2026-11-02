"""真实缓存的浏览器验收：模拟原生文件句柄，批量状态明确为隔离夹具。"""
import copy
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

sys.stdout.reconfigure(encoding='utf-8')
root = Path(__file__).resolve().parents[1]
report = json.loads((root / 'workspace/bilibili_integration.json').read_text(encoding='utf-8'))
samples = report['samples']
output = root / 'workspace/output/reports/bilibili'
output.mkdir(parents=True, exist_ok=True)
results = {'saves': [], 'layouts': [], 'simulated_batch': [], 'writes': []}
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel='chrome', headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    first = samples[0]['cache_id']
    context.add_init_script('if(!localStorage.getItem("bilibili.active"))localStorage.setItem("bilibili.active",' + json.dumps(first) + ');')
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: results['writes'].append(request.url) if request.method == 'POST' else None)
    response = page.goto('http://127.0.0.1:8000/tools/bilibili')
    assert response.status == 200
    expect(page.locator('#bv-result')).to_be_visible(timeout=30000)
    expect(page.locator('#bv-output')).to_be_visible()
    assert page.locator('#bv-scope').input_value() == 'single'
    assert page.locator('#bv-scope option[value=collection]').count() == 1
    video_key = next(k for k,a in samples[0]['result']['resources'].items() if a['kind'] == 'video')
    page.locator('#bv-asset').select_option(video_key)
    page.wait_for_function('()=>document.querySelector("#bv-video").readyState>=2')
    expect(page.locator('#bv-save')).to_be_disabled()
    page.locator('#bv-reviewed').check(); expect(page.locator('#bv-save')).to_be_enabled()
    video_key = next(k for k,a in samples[0]['result']['resources'].items() if a['kind'] == 'video')
    for width in (1440, 1024, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), width
        page.locator('#bv-result').screenshot(path=str(output / f'card-{width}.png'))
        results['layouts'].append({'width': width, 'overflow': False})
    page.locator('#bv-format').select_option(index=1)
    expect(page.locator('#bv-reviewed')).not_to_be_checked()
    original = samples[0]['result']['catalogs'][samples[0]['result']['selected']]['videos'][0]['id']
    page.locator('#bv-format').select_option(original)
    page.locator('#bv-reviewed').check()
    # 原生选择器取消：不发送导出请求、不创建已保存摘要。
    page.evaluate('''() => {window.showSaveFilePicker=async()=>{throw new DOMException('取消','AbortError')};}''')
    before = len(results['writes']); page.locator('#bv-save').click()
    expect(page.locator('#bv-status')).to_contain_text('已停止保存')
    assert len(results['writes']) == before
    assert not page.evaluate('localStorage.getItem("bilibili.saves."+' + json.dumps(first) + ')')
    # 保存中止：写入后请求停止，不能关闭文件或记成功。
    page.evaluate('''() => {window.testClosed=false;window.testAborted=false;window.showSaveFilePicker=async options=>({name:options.suggestedName,createWritable:async()=>({write:async()=>document.querySelector('#bv-stop-save').click(),close:async()=>{window.testClosed=true},abort:async()=>{window.testAborted=true}})});}''')
    page.locator('#bv-save').click()
    page.wait_for_function('()=>window.testAborted')
    assert not page.evaluate('window.testClosed')
    assert not page.evaluate('localStorage.getItem("bilibili.saves."+' + json.dumps(first) + ')')
    # 成功关闭后核对实际传输的大小和摘要，不在用户目录落盘。
    page.evaluate('''() => {window.savedResult=null;window.showSaveFilePicker=async options=>({name:options.suggestedName,createWritable:async()=>{const parts=[];return {write:async chunk=>parts.push(chunk),abort:async()=>{},close:async()=>{const bytes=new Uint8Array(await new Blob(parts).arrayBuffer());window.savedResult={filename:options.suggestedName,bytes:bytes.length,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(b=>b.toString(16).padStart(2,'0')).join('')};}};}});}''')
    for key, asset in samples[0]['result']['resources'].items():
        if key in {'cover','avatar'}:
            continue
        page.locator('#bv-asset').select_option(key)
        if asset['kind'] == 'video':
            page.wait_for_function('()=>document.querySelector("#bv-video").readyState>=2')
            page.locator('#bv-reviewed').check()
        page.evaluate('window.savedResult=null')
        page.locator('#bv-save').click()
        page.wait_for_function('()=>window.savedResult!==null', timeout=60000)
        value = page.evaluate('window.savedResult')
        assert value['bytes'] == asset['bytes'], value
        assert value['sha256'] == asset['sha256'], value
        results['saves'].append({'key': key, **value})
    page.reload(); expect(page.locator('#bv-result')).to_be_visible(timeout=20000)
    expect(page.locator('#bv-reviewed')).not_to_be_checked()
    expect(page.locator('#bv-saved')).to_contain_text('本浏览器曾完成保存')
    # 第二个真实链接验证合集11集与当前集默认，未发送任何准备POST。
    page.evaluate('localStorage.setItem("bilibili.active",' + json.dumps(samples[1]['cache_id']) + ')')
    page.reload(); expect(page.locator('#bv-result')).to_be_visible(timeout=20000)
    assert page.locator('#bv-episode option').count() == 11
    assert page.locator('#bv-scope').input_value() == 'single'
    page.locator('#bv-scope').select_option('collection')
    expect(page.locator('#bv-range-note')).to_contain_text('11 集')
    page.screenshot(path=str(output / 'course-320.png'), full_page=True)
    # 批量保存夹具：组合两个真实文件为模拟的两集范围，不声称真实下载整个合集。
    fixture = copy.deepcopy(samples[0]['result'])
    fixture['id'] = first
    second_video_key, second_video = next((k,a) for k,a in samples[1]['result']['resources'].items() if a['kind']=='video')
    fixture['resources'][second_video_key] = copy.deepcopy(second_video)
    fixture['resources'][second_video_key]['filename'] = fixture['resources'][video_key]['filename']
    fixture['operation'] = {'scope': 'collection', 'kind': 'video'}
    fixture['metadata']['collection'] = {'title': '隔离两集批量夹具', 'declared_count': 2, 'items': [copy.deepcopy(samples[0]['result']['metadata']['parts'][0]), copy.deepcopy(samples[1]['result']['metadata']['parts'][0])]}
    fixture['plan'] = [{'episode_id': 'fixture1', 'title': '隔离批量第1集', 'state': 'prepared', 'message': '隔离测试已准备', 'resource_key': video_key}, {'episode_id': 'fixture2', 'title': '隔离批量第2集', 'state': 'prepared', 'message': '隔离测试已准备', 'resource_key': second_video_key}]
    def snapshots(route):
        if route.request.method == 'GET':
            route.fulfill(json=fixture)
        else:
            body = route.request.post_data_json
            assert body['action'] == 'prepare' and body['scope'] == 'collection'
            results['simulated_prepare'] = body
            route.fulfill(json={'key': video_key}, status=202)
    page.route('**/api/tools/bilibili/' + first, snapshots)
    def second_asset(route):
        response = context.request.get('http://127.0.0.1:8000/api/tools/bilibili/' + samples[1]['cache_id'] + '/assets/' + second_video_key)
        route.fulfill(response=response)
    page.route('**/api/tools/bilibili/' + first + '/assets/' + second_video_key, second_asset)
    page.evaluate('localStorage.setItem("bilibili.active",' + json.dumps(first) + ')')
    page.reload(); expect(page.locator('#bv-output')).to_be_visible(timeout=20000)
    page.locator('#bv-scope').select_option('collection')
    page.locator('#bv-prepare').click()
    page.wait_for_function('()=>!document.querySelector("#bv-prepare").disabled')
    assert results['simulated_prepare']['episode_id'] == fixture['selected']
    assert results['simulated_prepare']['video_id'] == original
    page.locator('#bv-asset').select_option(video_key)
    page.wait_for_function('()=>document.querySelector("#bv-video").readyState>=2')
    page.locator('#bv-reviewed').check(); expect(page.locator('#bv-save-all')).to_be_enabled()
    page.evaluate('''() => {window.batchSaved=[];const existing=new Set();window.showDirectoryPicker=async()=>({getFileHandle:async(name,options)=>{if(!options?.create){if(existing.has(name))return {};throw new DOMException('不存在','NotFoundError')};existing.add(name);return {name,createWritable:async()=>{const parts=[];return {write:async value=>parts.push(value),abort:async()=>{},close:async()=>{const bytes=new Uint8Array(await new Blob(parts).arrayBuffer());window.batchSaved.push({filename:name,bytes:bytes.length,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(b=>b.toString(16).padStart(2,'0')).join('')});}};}};}});}''')
    page.locator('#bv-save-all').click()
    page.wait_for_function('()=>window.batchSaved.length===2', timeout=60000)
    values = page.evaluate('window.batchSaved')
    assert values[0]['filename'] != values[1]['filename']
    assert '(1)' in values[1]['filename']
    assert [v['sha256'] for v in values] == [fixture['resources'][video_key]['sha256'], second_video['sha256']]
    results['simulated_batch'] = values
    # 隔离的未准备、下载中、暂停与部分失败状态，不发送实际控制/准备请求。
    for asset in fixture['resources'].values():
        if asset['kind'] in {'video', 'audio'}:
            asset['ready'] = False
    fixture.update(phase='downloading', loaded=500, total=1000, speed=100, eta=5, stage='视频轨')
    for entry in fixture['plan']:
        entry.update(state='pending', message='隔离状态等待准备')
    page.reload(); expect(page.locator('#bv-result')).to_be_visible(timeout=20000)
    expect(page.locator('#bv-video')).to_be_hidden()
    expect(page.locator('#bv-pause')).to_be_enabled()
    assert page.locator('#bv-progress').get_attribute('value') == '50'
    page.screenshot(path=str(output / 'simulated-progress-320.png'), full_page=True)
    fixture.update(phase='paused', message='隔离模拟暂停')
    page.reload(); expect(page.locator('#bv-resume')).to_be_enabled(timeout=20000)
    expect(page.locator('#bv-pause')).to_be_disabled()
    fixture.update(phase='partial', message='隔离模拟部分失败')
    page.reload(); expect(page.locator('#bv-resume')).to_be_enabled(timeout=20000)
    page.unroute('**/api/tools/bilibili/' + first, snapshots)
    page.unroute('**/api/tools/bilibili/' + first + '/assets/' + second_video_key, second_asset)
    for width in (1440, 1024, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto('http://127.0.0.1:8000/tools')
        assert page.locator('.it-tool-grid').count() == 1
        assert page.locator('.it-tool-grid>.ui-tool-card').count() == 3
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.locator('main').screenshot(path=str(output / f'toolbox-{width}.png'))
    for path in ('/tools/images', '/tools/douyin'):
        assert page.goto('http://127.0.0.1:8000' + path).status == 200
    assert not errors, errors
    # 本轮所有写请求仅为文件导出，没有解析、准备或采集任务。
    assert sum('/assets/' not in url for url in results['writes']) == 1, results['writes']
    results['errors'] = errors
    (output / 'browser-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False))
    browser.close()
