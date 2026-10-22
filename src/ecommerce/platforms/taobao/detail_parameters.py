"""动态读取参数模块：突出参数和值位置反向，普通参数按名称和值读取。"""
import json
import re
import time

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import active_guard

PARAMETER_ROOT = '.paramsInfoArea:visible'
PARAMETER_NAV = '[class*="tabTitle--"]:visible'

PARAMETER_SNAPSHOT = r"""() => {
 const shown=e=>{for(let n=e;n&&n.nodeType===1;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false}return true};
 const visible=e=>{const r=e.getBoundingClientRect();return shown(e)&&r.width>0&&r.height>0};
 const parts=(root,p)=>[...root.querySelectorAll('[class*="'+p+'--"]')].filter(e=>[...e.classList].some(c=>c.startsWith(p+'--'))&&shown(e));
 const text=e=>(e?.innerText||'').trim();
 const roots=[...document.querySelectorAll('.paramsInfoArea')].filter(visible);
 const navigation=parts(document,'tabTitle').filter(e=>visible(e)&&text(e)==='参数信息').length;
 if(roots.length!==1)return {module_present:false,navigation,reason:roots.length?'参数模块不唯一':'参数模块尚未加载',
   state:roots.length?'unrecognized_structure':'not_loaded',items:[],unknown_fragments:[],expanders:[],raw_html:null};
 const root=roots[0],headings=parts(root,'tabDetailItemTitle').filter(e=>text(e)==='参数信息'),wraps=parts(root,'paramsWrap');
 if(headings.length!==1||wraps.length!==1)return {module_present:true,navigation,reason:'参数标题或内容容器结构尚未确认',
   state:'unrecognized_structure',items:[],unknown_fragments:[],expanders:[],raw_html:root.outerHTML};
 const wrap=wraps[0];
 const expand=/^(展开全部(?:参数|信息)?|查看全部(?:参数)?|全部参数|更多参数|查看更多(?:参数)?|展开(?:更多)?(?:参数)?)$/;
 const collapse=/^(收起(?:全部)?(?:参数|信息)?|折叠(?:参数)?)$/;
 const path=e=>{const steps=[];for(let n=e;n&&n!==root;n=n.parentElement)steps.unshift(n.tagName.toLowerCase()+':nth-child('+([...n.parentElement.children].indexOf(n)+1)+')');return steps.join(' > ')};
 const controls=[...root.querySelectorAll('*')].filter(e=>visible(e)&&(expand.test(text(e))||collapse.test(text(e))))
   .filter(e=>![...e.children].some(c=>visible(c)&&text(c)===text(e)));
 const expanders=controls.filter(e=>expand.test(text(e))&&e.getAttribute('aria-expanded')!=='true').map(e=>({text:text(e),path:path(e),
   disabled:e.hasAttribute('disabled')||e.getAttribute('aria-disabled')==='true'}));
 const rows=[...new Set([...parts(root,'emphasisParamsInfoItem'),...parts(root,'generalParamsInfoItem')])]
   .sort((a,b)=>a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1);
 const readCell=e=>{const title=e.getAttribute('title');return {raw:title===null?(e.innerText||''):title,source:title===null?'inner_text':'title_attribute'}};
 const items=rows.map((row,i)=>{
  const emphasis=[...row.classList].some(c=>c.startsWith('emphasisParamsInfoItem--'));
  const prefix=emphasis?'emphasisParamsInfoItem':'generalParamsInfoItem';
  const names=parts(row,prefix+(emphasis?'SubTitle':'Title')),values=parts(row,prefix+(emphasis?'Title':'SubTitle'));
  const name=names.length===1?readCell(names[0]):null,value=values.length===1?readCell(values[0]):null;
  let status='read';
  if(rows.some(other=>other!==row&&row.contains(other)))status='unrecognized_structure';
  else if(!name||!name.raw.trim())status='missing_name';
  else if(!value)status='missing_value_node';
  else if(!value.raw.trim()&&[...values[0].querySelectorAll('img,canvas,iframe,video,svg')].some(shown))status='unrecognized_value';
  else if(!value.raw.trim())status='empty_value';
  return {index:i+1,name:name?name.raw.trim():null,name_raw:name?.raw??null,value:value?.raw??null,
    source:emphasis?'emphasis':'general',name_source:name?.source??null,value_source:value?.source??null,
    status,dom_path:path(row),raw_text:row.innerText||''};
 });
 const emptyPattern=/^(暂无(?:相关)?(?:商品)?参数(?:信息)?|无参数信息|该商品暂无参数信息|暂无相关信息)$/;
 const explicitEmpty=rows.length===0&&emptyPattern.test(text(wrap));
 const covered=e=>rows.some(row=>row.contains(e))||controls.some(control=>control.contains(e))||headings.some(h=>h.contains(e));
 const unknown=[];
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
 while(walker.nextNode()){
   const node=walker.currentNode,parent=node.parentElement,raw=node.nodeValue;
   if(!raw.trim()||!shown(parent)||covered(parent)||['SCRIPT','STYLE','NOSCRIPT'].includes(parent.tagName)||(explicitEmpty&&wrap.contains(parent)))continue;
   unknown.push({kind:'unparsed_text',text:raw,dom_path:path(parent),html:parent.outerHTML.slice(0,2000)});
 }
 for(const media of root.querySelectorAll('img,canvas,iframe,video,svg')){
   if(shown(media)&&!covered(media))unknown.push({kind:'unparsed_media',dom_path:path(media),html:media.outerHTML.slice(0,2000)});
 }
 for(const node of root.querySelectorAll('[title]')){
   if(shown(node)&&!covered(node)&&!text(node)&&node.getAttribute('title')?.trim())unknown.push({kind:'unparsed_title',text:node.getAttribute('title'),dom_path:path(node),html:node.outerHTML.slice(0,2000)});
 }
 const loading=root.getAttribute('aria-busy')==='true'||[...root.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="loading--"],[class*="Loading--"],[class*="skeleton"],[class*="Skeleton"]')].some(visible);
 const errors=items.filter(i=>!['read','empty_value'].includes(i.status)).length+unknown.length;
 const state=loading?'not_loaded':expanders.length?'collapsed':errors?'unrecognized_structure':items.length?'ready':explicitEmpty?'no_parameters':'not_loaded';
 return {module_present:true,navigation,state,reason:loading?'参数仍在加载':expanders.length?'参数尚未展开':errors?'存在未解析参数或内容':!items.length&&!explicitEmpty?'尚未确认参数或明确空态':'',
   items,unknown_fragments:unknown,expanders,raw_html:root.outerHTML,explicit_empty_text:explicitEmpty?text(wrap):null,
   summary:{total_count:items.length,read_count:items.filter(i=>i.status==='read').length,
    empty_value_count:items.filter(i=>i.status==='empty_value').length,error_count:errors,
    unrecognized_fragment_count:unknown.length,total_count_confirmed:errors===0&&['ready','no_parameters'].includes(state)}};
}"""


def read_parameters(page, item_id):
    if not matches_detail_url(page, item_id):
        raise ValueError('参数采集的目标商品发生变化')
    return page.evaluate(PARAMETER_SNAPSHOT)


def snapshot_key(value):
    return json.dumps({key: value.get(key) for key in ('state', 'items', 'unknown_fragments', 'expanders', 'explicit_empty_text')},
                      ensure_ascii=False, sort_keys=True)


def collect_parameter_data(result, timeout_ms, *, on_diagnostic=None, stable_ms=500):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('参数采集必须在淘宝专用守卫中运行')
    page, item_id = result.page, result.target.item_id

    def same_page():
        return matches_detail_url(page, item_id)

    def safe_read():
        try:
            return read_parameters(page, item_id)
        except (ValueError, PlaywrightError):
            return {'state': 'not_loaded', 'module_present': False, 'items': [], 'unknown_fragments': [],
                    'expanders': [], 'raw_html': None, 'reason': '商品页面或参数区域暂时无法检查'}

    guard.resume_check = same_page
    guard.pause_between('page')
    deadline = time.monotonic() + timeout_ms / 1000
    last_key, stable_since = None, None
    nav_clicked, scrolled = False, False
    expanded_controls = set()
    diagnostic_count = 0

    def reset_wait():
        nonlocal deadline, last_key, stable_since
        deadline = time.monotonic() + timeout_ms / 1000
        last_key, stable_since = None, None

    def diagnose(value, reason):
        nonlocal diagnostic_count
        diagnostic_count += 1
        if on_diagnostic and same_page():
            guard.perform('保存参数异常现场', lambda: on_diagnostic(value, diagnostic_count))
        guard.request_attention(page, reason + '；请人工核对后回复继续，不会自动重复点击或重新打开商品', step='复核参数信息')
        reset_wait()

    while True:
        paused = guard.paused_seconds
        guard.checkpoint('读取参数信息')
        delta = guard.paused_seconds - paused
        deadline += delta
        if delta:
            last_key, stable_since = None, None
        value = safe_read()
        if not value['module_present'] and value['state'] == 'not_loaded' and value.get('navigation') == 1 and not nav_clicked:
            guard.pause_between('page')
            current = safe_read()
            if not current['module_present'] and current.get('navigation') == 1:
                nav_clicked = True
                guard.perform('定位参数信息页签', lambda: page.locator(PARAMETER_NAV).filter(
                    has_text=re.compile(r'^\s*参数信息\s*$')).click(timeout=min(timeout_ms, 5000)),
                    completed=lambda: safe_read()['module_present'])
            reset_wait()
            continue
        if value['module_present'] and not scrolled:
            scrolled = True
            guard.perform('将参数信息滚动至可见区域', lambda: page.locator(PARAMETER_ROOT).scroll_into_view_if_needed(
                timeout=min(timeout_ms, 5000)), completed=same_page)
            guard.pause_between('scroll')
            reset_wait()
            continue
        key = snapshot_key(value)
        if key != last_key:
            last_key, stable_since = key, time.monotonic()
        stable = stable_since is not None and (time.monotonic()-stable_since)*1000 >= stable_ms
        if stable and value['state'] == 'collapsed':
            controls = value['expanders']
            if len(controls) != 1:
                diagnose(value, '展开入口不唯一，尚未确认全部参数')
                continue
            control = controls[0]
            control_key = (control['text'], control['path'])
            if control_key in expanded_controls and time.monotonic() < deadline:
                paused = guard.paused_seconds
                guard.wait(100)
                if guard.paused_seconds != paused:
                    reset_wait()
                continue
            if control['disabled'] or control_key in expanded_controls:
                diagnose(value, '参数展开入口仍未完成或不可操作，已阻止重复点击')
                continue
            guard.pause_between('page')
            current = safe_read()
            if current['expanders'] != controls:
                reset_wait()
                continue
            expanded_controls.add(control_key)
            guard.perform('展开全部参数：' + control['text'], lambda: page.locator(PARAMETER_ROOT).locator(control['path']).click(
                timeout=min(timeout_ms, 5000)), completed=lambda: safe_read()['module_present'] and not safe_read()['expanders'])
            # 等待展开结果；同一入口若仍存在只交给人工，不重放。
            reset_wait()
            continue
        if stable and value['state'] in {'ready', 'no_parameters'}:
            return {**value, 'status': 'complete' if value['state'] == 'ready' else 'no_parameters',
                    'expansion_count': len(expanded_controls), 'diagnostic_count': diagnostic_count,
                    'selector': PARAMETER_ROOT}
        if time.monotonic() >= deadline:
            diagnose(value, value.get('reason') or '参数内容尚未加载完成或结构未确认')
            continue
        paused = guard.paused_seconds
        guard.wait(100)
        if guard.paused_seconds != paused:
            reset_wait()
