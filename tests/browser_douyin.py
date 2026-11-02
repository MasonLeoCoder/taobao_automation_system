"""真实本机页面核对：显式传入已准备的缓存编号；另存为句柄使用模拟实现。"""
import hashlib
import json
from pathlib import Path
import re
import sys
from playwright.sync_api import sync_playwright, expect


def check(identifier):
    """核对预览、三视口、取消和保存字节；参数：既有缓存编号；返回：检查结果。"""
    if not re.fullmatch('[a-f0-9]{32}', identifier):
        raise ValueError('请输入已准备的32位缓存编号')
    output=Path(__file__).resolve().parents[1]/'workspace/output/reports/douyin'
    output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome',headless=True)
        try:
            context=browser.new_context(viewport={'width':1440,'height':1000})
            context.add_init_script('localStorage.setItem("douyin.active",'+json.dumps(identifier)+');')
            page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto('http://127.0.0.1:8000/tools/douyin')
            expect(page.locator('#dv-result')).to_be_visible(timeout=20000)
            row=context.request.get('http://127.0.0.1:8000/api/tools/douyin/'+identifier).json()
            key='video_'+page.locator('#dv-format').input_value()
            assert row.get('ready') if row.get('legacy') else row.get('resources',{}).get(key,{}).get('ready'), '请先在页面准备默认视频版本'
            media_url='http://127.0.0.1:8000/api/tools/douyin/'+identifier+('/media' if row.get('legacy') else '/assets/'+key)
            page.locator('#dv-prepare-video').click()
            page.wait_for_function('()=>document.querySelector("#dv-video").readyState>=2')
            expected=context.request.get(media_url)
            assert expected.ok
            media=expected.body();digest=hashlib.sha256(media).hexdigest()
            expect(page.locator('#dv-save')).to_be_disabled()
            page.locator('#dv-reviewed').check()
            for width in (1440,390,320):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
                page.screenshot(path=str(output/f'page-{width}.png'),full_page=True)
            page.evaluate('() => {window.showSaveFilePicker=async()=>{throw new DOMException("取消","AbortError")};}')
            page.locator('#dv-save').click()
            expect(page.locator('#dv-status')).to_contain_text('已取消保存')
            page.evaluate('''() => {window.abortClosed=false;window.abortCalled=false;window.showSaveFilePicker=async()=>({name:'中断模拟.mp4',createWritable:async()=>({write:async()=>new Promise(r=>setTimeout(r,300)),close:async()=>{window.abortClosed=true},abort:async()=>{window.abortCalled=true}})});}''')
            page.locator('#dv-save').click()
            expect(page.locator('#dv-stop-save')).to_be_visible()
            page.locator('#dv-stop-save').click()
            expect(page.locator('#dv-status')).to_contain_text('已取消保存')
            assert page.evaluate('window.abortCalled && !window.abortClosed')
            page.evaluate('''() => {window.showSaveFilePicker=async()=>({name:'模拟另存为.mp4',createWritable:async()=>{const chunks=[];return {write:async b=>chunks.push(b),close:async()=>{const bytes=new Uint8Array(await new Blob(chunks).arrayBuffer());window.savedResult={size:bytes.length,sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(b=>b.toString(16).padStart(2,'0')).join('')};},abort:async()=>{}}}});}''')
            page.locator('#dv-save').click()
            expect(page.locator('#dv-status')).to_contain_text('已保存：模拟另存为.mp4',timeout=20000)
            result=page.evaluate('window.savedResult')
            assert result=={'size':len(media),'sha256':digest},result
            page.reload();expect(page.locator('#dv-result')).to_be_visible()
            expect(page.locator('#dv-saved')).to_contain_text('本浏览器曾完成保存')
            expect(page.locator('#dv-save')).to_be_disabled()
            assert not errors,errors
            return {'result':result,'widths':[1440,390,320],'errors':errors,'native_picker':'模拟文件句柄；原生另存为待人工体验'}
        finally:
            browser.close()


if __name__=='__main__':
    print(json.dumps(check(sys.argv[1]),ensure_ascii=False))
