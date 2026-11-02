"""核对已准备的真实素材记录；另存为句柄模拟，不向用户目录写文件。"""
import json,hashlib,sys,re
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
sys.stdout.reconfigure(encoding='utf-8')
root=Path(__file__).resolve().parents[1]/'workspace'
identifier=sys.argv[1]
assert re.fullmatch('[a-f0-9]{32}',identifier),'请输入已准备的缓存编号'
output=root/'output/reports/douyin_studio';output.mkdir(parents=True,exist_ok=True)
results=[]
with sync_playwright() as p:
    browser=p.chromium.launch(channel='chrome',headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1000})
    context.add_init_script('localStorage.setItem("douyin.active",'+json.dumps(identifier)+');')
    page=context.new_page();errors=[];writes=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:writes.append(r.url) if r.method=='POST' else None)
    response=page.goto('http://127.0.0.1:8000/tools/douyin');assert response.status==200
    expect(page.locator('#dv-result')).to_be_visible(timeout=15000)
    report={'result':context.request.get('http://127.0.0.1:8000/api/tools/douyin/'+identifier).json()}
    selected=page.locator('#dv-format').input_value()
    required=['video_'+selected,'music','cover_png','audio_'+selected]
    assert all(report['result'].get('resources',{}).get(key,{}).get('ready') for key in required),'请先准备默认视频版本、音乐、PNG封面和完整声音'
    page.wait_for_function('()=>document.querySelector("#dv-cover").naturalWidth>0')
    assert page.locator('#dv-format option').count()>1,'本检查需要至少两种版本以核对确认重置'
    for width in (1440,1024,390,320):
        page.set_viewport_size({'width':width,'height':1000})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        page.locator('#dv-result').screenshot(path=str(output/f'card-{width}.png'))
    page.locator('#dv-prepare-video').click()
    page.wait_for_function('()=>document.querySelector("#dv-video").readyState>=2')
    expect(page.locator('#dv-save')).to_be_disabled()
    page.locator('#dv-reviewed').check();expect(page.locator('#dv-save')).to_be_enabled()
    original=page.locator('#dv-format').input_value()
    other=page.locator('#dv-format option').nth(1).get_attribute('value')
    page.locator('#dv-format').select_option(other)
    if not report['result']['resources'].get('video_'+other,{}).get('ready'):
        expect(page.locator('#dv-preview')).to_be_hidden()
        expect(page.locator('#dv-prepare-video')).to_have_text('准备并预览')
    expect(page.locator('#dv-reviewed')).not_to_be_checked();expect(page.locator('#dv-save')).to_be_disabled()
    page.locator('#dv-format').select_option(original)
    expect(page.locator('#dv-preview')).to_be_visible()
    expect(page.locator('#dv-prepare-video')).to_have_text('预览并保存')
    page.wait_for_function('()=>document.querySelector("#dv-video").readyState>=2')
    page.locator('#dv-reviewed').check()
    page.evaluate('''() => {window.showSaveFilePicker=async(options)=>({name:options.suggestedName,createWritable:async()=>{const parts=[];return {write:async chunk=>parts.push(chunk),abort:async()=>{window.aborted=true},close:async()=>{const bytes=new Uint8Array(await new Blob(parts).arrayBuffer());window.savedResult={name:options.suggestedName,size:bytes.length,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(b=>b.toString(16).padStart(2,'0')).join('')};}};}});}''')
    def save(button,key):
        """使用模拟文件句柄核对实际文件；参数：按钮与资源编号；返回：无。"""
        page.evaluate('()=>{window.savedResult=null}')
        page.locator(button).click()
        page.wait_for_function('()=>window.savedResult!==null')
        result=page.evaluate('window.savedResult')
        expected=report['result']['resources'][key]
        assert result['size']==expected['bytes'],(key,result)
        assert result['sha256']==expected['sha256'],(key,result)
        results.append({'key':key,**result})
    save('#dv-save','video_'+original)
    page.locator('#dv-music').click();expect(page.locator('#dv-music-player')).to_be_visible();save('#dv-save-music','music')
    page.locator('#dv-cover-open').click();page.locator('#dv-cover-format').select_option('png');save('#dv-save-cover','cover_png')
    page.locator('#dv-audio').click();expect(page.locator('#dv-audio-player')).to_be_visible();save('#dv-save-audio','audio_'+original)
    page.reload();expect(page.locator('#dv-result')).to_be_visible()
    expect(page.locator('#dv-save')).to_be_disabled();expect(page.locator('#dv-saved')).to_contain_text('本浏览器曾完成保存')
    page.locator('#dv-prepare-video').click()
    page.screenshot(path=str(output/'full-320.png'),full_page=True)
    # 明确标注的隔离标题，核对窄屏截断后可展开，不把此标题当成真实平台资料。
    isolated=json.loads(json.dumps(report['result']));isolated['items'][0]['title']='标题'*20
    target='http://127.0.0.1:8000/api/tools/douyin/'+identifier
    page.route(target,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(isolated)))
    page.reload();expect(page.locator('#dv-title-toggle')).to_be_visible()
    page.locator('#dv-title-toggle').click()
    assert page.locator('#dv-title').evaluate('(h)=>h.scrollHeight<=h.clientHeight+1')
    page.set_viewport_size({'width':1440,'height':1000});page.locator('#dv-title-toggle').click()
    expect(page.locator('#dv-title-toggle')).to_be_hidden()
    assert not errors,errors
    assert len(writes)==4,writes
    print(json.dumps({'saves':results,'errors':errors,'writes':len(writes),'native_picker':'模拟文件句柄，原生弹窗待人工体验'},ensure_ascii=False))
    browser.close()
