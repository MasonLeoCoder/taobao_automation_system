"""真实YouTube缓存浏览器验收；原生保存句柄与忙碌状态均明确隔离模拟。"""
import copy
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

sys.stdout.reconfigure(encoding='utf-8')
root=Path(__file__).resolve().parents[1];report=json.loads((root/'workspace/development/runs/undated-youtube/results/youtube-integration.json').read_text(encoding='utf-8'))
record=report['result'];identifier=report['cache_id'];out=root/'workspace/output/reports/youtube';out.mkdir(parents=True,exist_ok=True)
videos=[(k,a) for k,a in record['resources'].items() if a['kind']=='video']
first_key,first=videos[0];second_key,second=videos[1]
result={'saves':[],'layouts':[],'writes':[]};errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(channel='chrome',headless=True);context=browser.new_context(viewport={'width':1440,'height':1000})
    context.add_init_script('if(!localStorage.getItem("youtube.active"))localStorage.setItem("youtube.active",'+json.dumps(identifier)+');')
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:result['writes'].append(r.url) if r.method=='POST' else None)
    response=page.goto('http://127.0.0.1:8000/tools/youtube');assert response.status==200
    expect(page.locator('#yt-result')).to_be_visible(timeout=30000)
    assert page.locator('#yt-format option').count()>5
    assert any('1080P' in v and '60' in v for v in page.locator('#yt-format option').all_text_contents())
    assert not page.locator('[id*=scope],[id*=playlist],[id*=save-all]').count()
    assert 'list=' not in page.locator('#yt-input').input_value()
    page.locator('#yt-format').select_option(first['video_id'])
    page.wait_for_function('()=>document.querySelector("#yt-video").readyState>=2')
    assert page.locator('#yt-language').input_value()=='en-US'
    expect(page.locator('#yt-save')).to_be_disabled();page.locator('#yt-reviewed').check();expect(page.locator('#yt-save')).to_be_enabled()
    for width in (1440,1024,390,320):
        page.set_viewport_size({'width':width,'height':1000})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        page.locator('#yt-result').screenshot(path=str(out/f'card-{width}.png'))
        result['layouts'].append({'width':width,'overflow':False})
    page.locator('#yt-format').select_option(second['video_id'])
    expect(page.locator('#yt-reviewed')).not_to_be_checked();page.wait_for_function('()=>document.querySelector("#yt-video").readyState>=2&&document.querySelector("#yt-video").videoWidth===1280')
    page.locator('#yt-reviewed').check()
    unused=next(v['id'] for v in record['catalog']['videos'] if v['height']==1080)
    page.locator('#yt-format').select_option(unused);expect(page.locator('#yt-output')).to_be_hidden()
    page.locator('#yt-format').select_option(first['video_id']);expect(page.locator('#yt-output')).to_be_visible()
    page.wait_for_function('()=>document.querySelector("#yt-video").readyState>=2')
    page.locator('#yt-reviewed').check()
    page.locator('summary').filter(has_text='音轨语言与音质').click()
    page.locator('#yt-language').select_option('ja');expect(page.locator('#yt-reviewed')).not_to_be_checked();expect(page.locator('#yt-output')).to_be_hidden()
    page.locator('#yt-language').select_option('en-US');page.wait_for_function('()=>document.querySelector("#yt-video").readyState>=2');page.locator('#yt-reviewed').check()
    # 取消选择器，不应发起导出或留下保存摘要。
    page.evaluate('()=>{window.showSaveFilePicker=async()=>{throw new DOMException("取消","AbortError")}}')
    writes=len(result['writes']);page.locator('#yt-save').click();expect(page.locator('#yt-status')).to_contain_text('已取消保存')
    assert len(result['writes'])==writes
    assert not page.evaluate('localStorage.getItem("youtube.saves."+'+json.dumps(identifier)+')')
    # 中途停止不能关闭文件或宣称成功。
    page.evaluate('''()=>{window.closedFile=false;window.abortedFile=false;window.showSaveFilePicker=async o=>({name:o.suggestedName,createWritable:async()=>({write:async()=>document.querySelector('#yt-stop-save').click(),close:async()=>{window.closedFile=true},abort:async()=>{window.abortedFile=true}})})}''')
    page.locator('#yt-save').click();page.wait_for_function('()=>window.abortedFile');assert not page.evaluate('window.closedFile')
    # 保存三个真实资源，比较所有实际传输字节，不写用户保存目录。
    page.evaluate('''() => {
      window.savedFile = null;
      window.showSaveFilePicker = async o => ({
        name: o.suggestedName,
        createWritable: async () => {
          const parts = [];
          return {
            write: async b => {parts.push(b);},
            abort: async () => {},
            close: async () => {
              const bytes = new Uint8Array(await new Blob(parts).arrayBuffer());
              const digest = await crypto.subtle.digest('SHA-256', bytes);
              window.savedFile = {name: o.suggestedName, bytes: bytes.length,
                sha256: Array.from(new Uint8Array(digest)).map(v => v.toString(16).padStart(2, '0')).join('')};
            }
          };
        }
      });
    }''')
    keys=[first_key]+[k for k,a in record['resources'].items() if a['kind']=='audio' or a['kind']=='cover' and k!='cover']
    for key in keys:
        page.locator('#yt-asset').select_option(key)
        if record['resources'][key]['kind']=='video':
            page.wait_for_function('()=>document.querySelector("#yt-video").readyState>=2');page.locator('#yt-reviewed').check()
        page.evaluate('window.savedFile=null');page.locator('#yt-save').click();page.wait_for_function('()=>window.savedFile!==null',timeout=90000)
        value=page.evaluate('window.savedFile');expected=record['resources'][key]
        assert value['bytes']==expected['bytes'];assert value['sha256']==expected['sha256'];result['saves'].append({'key':key,**value})
    page.reload();expect(page.locator('#yt-result')).to_be_visible(timeout=30000)
    expect(page.locator('#yt-reviewed')).not_to_be_checked();expect(page.locator('#yt-history')).to_contain_text('本浏览器曾保存')
    # 一次真实缓存复用，不新建任务；刷新后仍保持明确的画质与音轨。
    before=context.request.get('http://127.0.0.1:8000/api/tools/youtube/'+identifier).json()
    token=page.locator('meta[name="workbench-token"]').get_attribute('content')
    response=context.request.post('http://127.0.0.1:8000/api/tools/youtube/'+identifier,
        headers={'X-Collection-Token':token},data={'action':'prepare','kind':'video','video_id':first['video_id'],'audio_id':first['audio_id'],'output_format':'original'})
    assert response.status==202
    after=context.request.get('http://127.0.0.1:8000/api/tools/youtube/'+identifier).json()
    assert len(after['attempts'])==len(before['attempts'])
    page.reload();expect(page.locator('#yt-format')).to_have_value(first['video_id'],timeout=30000)
    expect(page.locator('#yt-audio-format')).to_have_value(first['audio_id'])
    expect(page.locator('#yt-reviewed')).not_to_be_checked()
    result['real_cache_reuse']={'new_tasks':0,'video_id':first['video_id'],'audio_id':first['audio_id']}
    # 单视频准备请求被拦截，证明没有scope/list/部分下载参数；不创建真实任务。
    fixture=copy.deepcopy(record)
    def state(route):
        if route.request.method=='POST':
            body=route.request.post_data_json
            assert body['action']=='prepare' and body['kind']=='video'
            assert set(body)=={'action','kind','video_id','audio_id','output_format'}
            result['simulated_prepare']=body
            route.fulfill(json={'key':first_key},status=202)
        else:route.fulfill(json=fixture)
    page.route('**/api/tools/youtube/'+identifier,state)
    page.locator('#yt-format').select_option(first['video_id']);page.locator('#yt-prepare').click()
    page.wait_for_function('()=>!document.querySelector("#yt-prepare").disabled')
    assert result['simulated_prepare']['video_id']==first['video_id']
    for a in fixture['resources'].values():a['ready']=False
    fixture.update(phase='downloading',loaded=500,total=1000,speed=100,eta=5,stage='视频轨')
    page.reload();expect(page.locator('#yt-pause')).to_be_enabled(timeout=30000);expect(page.locator('#yt-output')).to_be_hidden()
    assert page.locator('#yt-progress').get_attribute('value')=='50'
    page.screenshot(path=str(out/'simulated-progress-320.png'),full_page=True)
    fixture.update(phase='paused',message='隔离模拟暂停')
    page.reload();expect(page.locator('#yt-resume')).to_be_enabled(timeout=30000);expect(page.locator('#yt-pause')).to_be_disabled()
    fixture.update(phase='failed',message='隔离模拟失败')
    page.reload();expect(page.locator('#yt-resume')).to_be_enabled(timeout=30000)
    fixture.update(phase='waiting',message='隔离模拟其他进程仍运行')
    page.reload();expect(page.locator('#yt-prepare')).to_be_disabled(timeout=30000);expect(page.locator('#yt-resume')).to_be_disabled()
    page.unroute('**/api/tools/youtube/'+identifier,state)
    for width in (1440,1024,768,390,320):
        page.set_viewport_size({'width':width,'height':1000});assert page.goto('http://127.0.0.1:8000/tools').status==200
        assert page.locator('.it-tool-grid').count()==1;assert page.locator('.it-tool-grid>.ui-tool-card').count()==4
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.locator('main').screenshot(path=str(out/f'toolbox-{width}.png'))
    for path in ('/tools/images','/tools/douyin','/tools/bilibili'):
        assert page.goto('http://127.0.0.1:8000'+path).status==200
    assert not errors,errors
    assert sum('/assets/' not in u for u in result['writes'])==1,result['writes']
    result['errors']=errors
    (out/'browser-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));browser.close()
