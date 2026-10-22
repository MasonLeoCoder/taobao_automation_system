"""加载图文详情至模块末尾，固定完整图片列表，排除其他商品区域。"""
import json
import re
import time

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import active_guard

DESCRIPTION_ROOT = '#imageTextInfo-container:visible'
DESCRIPTION_NAV = '[class*="tabTitle--"]:visible'
MAX_SCROLL_STEPS = 500

DESCRIPTION_SNAPSHOT = r"""() => {
 const shown=e=>{for(let n=e;n&&n.nodeType===1;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false}return true};
 const visible=e=>{const r=e.getBoundingClientRect();return shown(e)&&r.width>0&&r.height>0};
 const text=e=>(e.innerText||'').trim();
 const asset=v=>{if(!v)return null;try{return new URL(v,location.href).href}catch{return null}};
 const roots=[...document.querySelectorAll('#imageTextInfo-container')].filter(visible);
 const navigation=[...document.querySelectorAll('[class*="tabTitle--"]')].filter(e=>visible(e)&&text(e)==='图文详情').length;
 const missing={module_present:false,navigation,state:roots.length?'unknown_structure':'not_loaded',reason:roots.length?'图文详情容器不唯一':'图文详情尚未加载',images:[],expanders:[],unknown:[],raw_html:null};
 if(roots.length!==1)return missing;
 const root=roots[0],section=root.parentElement;
 const headings=[...section.querySelectorAll('[class*="tabDetailItemTitle--"]')].filter(e=>visible(e)&&text(e)==='图文详情');
 if(headings.length!==1)return {...missing,state:'unknown_structure',reason:'图文详情标题及模块边界未确认',raw_html:section.outerHTML};
 const expand=/^(展开(?:全部)?(?:图文)?详情|查看(?:全部|完整)(?:图文)?详情|更多详情|展开全部|查看更多|展开更多)$/;
 const controls=[...section.querySelectorAll('*')].filter(e=>visible(e)&&expand.test(text(e)))
  .filter(e=>![...e.children].some(c=>visible(c)&&text(c)===text(e)));
 const path=e=>{const p=[];for(let n=e;n&&n!==section;n=n.parentElement)p.unshift(n.tagName.toLowerCase()+':nth-child('+([...n.parentElement.children].indexOf(n)+1)+')');return p.join(' > ')};
 const expanders=controls.filter(e=>e.getAttribute('aria-expanded')!=='true').map(e=>({text:text(e),path:path(e),disabled:e.hasAttribute('disabled')||e.getAttribute('aria-disabled')==='true'}));
 const rawImages=[...root.querySelectorAll('img')];
 const images=rawImages.map((e,domIndex)=>{
  const src=asset(e.currentSrc||e.getAttribute('src'));
  const lazy=e.getAttribute('data-src')||e.getAttribute('data-ks-lazyload')||e.getAttribute('data-original')||e.getAttribute('data-lazy-src');
  const lazyUrl=asset(lazy);
  const resource=u=>{try{return new URL(u).pathname.split('/').pop().replace(/(\.(?:jpg|jpeg|png|webp|gif|avif))(?:_.*)?$/i,'$1')}catch{return null}};
  const http=src&&/^https?:\/\//.test(src),placeholder=lazyUrl&&lazyUrl!==src&&resource(lazyUrl)!==resource(src);
  return {dom_index:domIndex,image_url:src,lazy_source:lazyUrl,loaded:!!(http&&e.complete&&e.naturalWidth>0&&!placeholder),
   page_width:e.naturalWidth,page_height:e.naturalHeight,rendered:shown(e),unsupported:!!(src&&!http&&!lazyUrl),
   visible:visible(e)};
 }).filter(i=>i.rendered).map((i,index)=>({...i,index:index+1}));
 const unknown=[...root.querySelectorAll('iframe,canvas,object,embed')].filter(shown).map(e=>({kind:e.tagName.toLowerCase(),path:path(e)}));
 for(const e of [root,...root.querySelectorAll('*')]){
  if(shown(e)&&(getComputedStyle(e).backgroundImage||'').includes('url('))unknown.push({kind:'background_image',path:path(e)});
 }
 const loading=section.getAttribute('aria-busy')==='true'||[...section.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="loading--"],[class*="Loading--"],[class*="skeleton"],[class*="Skeleton"]')].some(visible);
 const explicitEmpty=images.length===0&&/^(暂无图文详情|暂无商品详情|暂无详情|该商品暂无详情)$/.test(text(root));
 const rect=root.getBoundingClientRect(),viewport=innerHeight;
 const state=loading?'not_loaded':unknown.length||images.some(i=>i.unsupported)?'unknown_structure':expanders.length?'collapsed':
   images.length&&images.every(i=>i.loaded)?'ready':explicitEmpty?'no_images':'not_loaded';
 return {module_present:true,navigation,state,reason:unknown.length?'存在尚未适配的详情媒体结构':images.some(i=>i.unsupported)?'存在非普通图片来源':loading?'详情仍在加载':'详情图片或展开状态尚未完整确认',
  images,expanders,unknown,raw_html:section.outerHTML,explicit_empty_text:explicitEmpty?text(root):null,
  container_height:rect.height,viewport_height:viewport,at_end:rect.bottom<=viewport+2,remaining:rect.bottom-viewport};
}"""


def read_description(page, item_id):
    if not matches_detail_url(page, item_id):
        raise ValueError('图文详情目标商品发生变化')
    return page.evaluate(DESCRIPTION_SNAPSHOT)


def image_list_signature(value):
    return [(i['index'], i['dom_index'], i['image_url'], i['page_width'], i['page_height']) for i in value['images']]


def collect_description_links(result, timeout_ms, *, on_diagnostic=None, stable_ms=700):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('详情图采集必须在淘宝专用守卫中运行')
    page, item_id = result.page, result.target.item_id
    guard.resume_check = lambda: matches_detail_url(page, item_id)
    guard.pause_between('page')
    deadline = time.monotonic() + timeout_ms / 1000
    navigation_done, positioned = False, False
    scroll_steps, diagnostic_count = 0, 0
    expanded, visited = set(), set()
    last_key, since = None, None

    def safe_read():
        try:
            return read_description(page, item_id)
        except (ValueError, PlaywrightError):
            return {'module_present': False, 'navigation': 0, 'state': 'not_loaded', 'images': [],
                    'expanders': [], 'unknown': [], 'raw_html': None, 'reason': '商品页面或图文详情暂时无法检查'}

    def reset():
        nonlocal deadline, last_key, since
        deadline, last_key, since = time.monotonic()+timeout_ms/1000, None, None

    def attention(value, reason):
        nonlocal diagnostic_count
        diagnostic_count += 1
        if on_diagnostic and matches_detail_url(page, item_id):
            guard.perform('保存详情图诊断', lambda: on_diagnostic(value, diagnostic_count))
        guard.request_attention(page, reason+'；请人工检查后回复继续，不会自动刷新或重复展开', step='复核详情图加载')
        reset()

    def scroll(action):
        nonlocal scroll_steps
        if scroll_steps >= MAX_SCROLL_STEPS:
            attention(safe_read(), '分段滚动已到保护上限，尚未确认详情模块末尾')
            scroll_steps = 0
        guard.perform('分段加载详情图', action)
        scroll_steps += 1
        guard.pause_between('scroll')
        reset()

    while True:
        paused = guard.paused_seconds
        guard.checkpoint('确认完整详情图片列表')
        deadline += guard.paused_seconds-paused
        if guard.paused_seconds != paused:
            last_key, since = None, None
        value = safe_read()
        if not value['module_present'] and value['state']=='not_loaded' and value['navigation']==1 and not navigation_done:
            guard.pause_between('page')
            current = safe_read()
            if not current['module_present'] and current['navigation']==1:
                navigation_done = True
                guard.perform('定位图文详情页签', lambda: page.locator(DESCRIPTION_NAV).filter(has_text=re.compile(r'^\s*图文详情\s*$')).click(timeout=min(timeout_ms,5000)),
                              completed=lambda: safe_read()['module_present'])
            reset()
            continue
        if value['module_present'] and not positioned:
            positioned = True
            scroll(lambda: page.locator(DESCRIPTION_ROOT).scroll_into_view_if_needed(timeout=min(timeout_ms,5000)))
            continue
        if value['module_present'] and value['state']=='collapsed':
            controls = value['expanders']
            if len(controls)==1:
                control = controls[0]
                key = (control['text'],control['path'])
                if not control['disabled'] and key not in expanded:
                    guard.pause_between('page')
                    if safe_read()['expanders'] != controls:
                        reset()
                        continue
                    expanded.add(key)
                    guard.perform('展开图文详情', lambda: page.locator(DESCRIPTION_ROOT).locator('..').locator(control['path']).click(timeout=min(timeout_ms,5000)),
                                  completed=lambda: safe_read()['module_present'] and not safe_read()['expanders'])
                    reset()
                    continue
        if value['module_present'] and value['state'] not in {'unknown_structure','collapsed'}:
            pending = [i for i in value['images'] if not i['loaded']]
            target = next((i for i in pending if (i['dom_index'],i['lazy_source']) not in visited),None)
            if target:
                visited.add((target['dom_index'],target['lazy_source']))
                scroll(lambda: page.locator(DESCRIPTION_ROOT).locator('img').nth(target['dom_index']).scroll_into_view_if_needed(timeout=min(timeout_ms,5000)))
                continue
            if not pending and value['state'] in {'ready','no_images'} and not value['at_end']:
                distance = max(1, min(value['viewport_height']*.75, value['remaining']))
                scroll(lambda: page.mouse.wheel(0,distance))
                continue
        key = json.dumps({'state':value['state'],'images':image_list_signature(value),'height':value.get('container_height'),
                          'expanders':value['expanders'],'unknown':value['unknown']},ensure_ascii=False,sort_keys=True)
        if key!=last_key:
            last_key,since=key,time.monotonic()
        if (since is not None and (time.monotonic()-since)*1000>=stable_ms and value.get('at_end')
                and value['state'] in {'ready','no_images'}):
            return {**value,'status':'complete' if value['state']=='ready' else 'no_images',
                    'scroll_steps':scroll_steps,'expansion_count':len(expanded),'diagnostic_count':diagnostic_count,'selector':DESCRIPTION_ROOT}
        if time.monotonic()>=deadline:
            attention(value,value.get('reason') or '详情图片列表尚未稳定')
            continue
        paused=guard.paused_seconds
        guard.wait(100)
        if guard.paused_seconds!=paused:
            reset()
