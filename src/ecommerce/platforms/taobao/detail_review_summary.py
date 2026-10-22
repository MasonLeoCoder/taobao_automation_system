"""只读取详情页评价概览；不打开全部评价，不读取逐条评论。"""
import json
import re
import time
from decimal import Decimal

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import active_guard

REVIEW_ROOT = '[class*="Comments--"]:visible'
REVIEW_NAV = '[class*="tabTitle--"]:visible'

REVIEW_SNAPSHOT = r"""() => {
 const shown=e=>{for(let n=e;n&&n.nodeType===1;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false}return true};
 const visible=e=>{const r=e.getBoundingClientRect();return shown(e)&&r.width>0&&r.height>0};
 const has=(e,p)=>[...e.classList].some(c=>c.startsWith(p+'--'));
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>has(n,p)&&shown(n));
 const text=e=>(e?.innerText||'').trim();
 const navigation=parts(document,'tabTitle').filter(e=>visible(e)&&text(e)==='用户评价').length;
 const roots=[...document.querySelectorAll('[class*="Comments--"]')].filter(e=>has(e,'Comments')&&visible(e));
 const modules=roots.map(root=>({root,titles:[...root.children].filter(e=>has(e,'titleWrapper')&&shown(e))}))
   .filter(m=>m.titles.some(t=>parts(t,'tabDetailItemTitle').some(e=>/^用户评价/.test(text(e)))));
 const blank={module_present:false,navigation,state:'not_loaded',reason:'评价概览尚未加载',
   heading_raw:null,count_raw:null,rate_raw:null,tags:[],tags_status:'not_loaded',raw_html:null,all_reviews_entry_present:false};
 if(modules.length!==1)return {...blank,state:modules.length?'unrecognized_structure':'not_loaded',reason:modules.length?'评价概览不唯一':'评价概览尚未加载'};
 const {root,titles}=modules[0],conditions=[...root.children].filter(e=>has(e,'condition')&&shown(e));
 if(titles.length!==1||conditions.length!==1)return {...blank,module_present:true,state:'unrecognized_structure',reason:'评价标题或总结区域结构尚未确认'};
 const title=titles[0],condition=conditions[0],heads=parts(title,'tabDetailItemTitle'),rates=parts(title,'rateTextTitle');
 const expressions=parts(condition,'expressionWrap');
 const heading=heads.length===1?text(heads[0]):null;
 const count=heading?.match(/^用户评价\s*[·•・]?\s*(.+)$/)?.[1]?.trim()??null;
 const rows=expressions.length===1?parts(expressions[0],'imprItem'):[];
 const tags=rows.map((row,index)=>{
   const counts=parts(row,'imprItemCount');
   // 逐个可见文本节点排除计数，保留标签名称里的数字。
   const labelNodes=[],walker=document.createTreeWalker(row,NodeFilter.SHOW_TEXT);
   while(walker.nextNode()){const n=walker.currentNode;if(shown(n.parentElement)&&!counts.some(c=>c.contains(n)))labelNodes.push(n.nodeValue)}
   const label=labelNodes.join('').trim(),countRaw=counts.length===1?text(counts[0]):null;
   return {index:index+1,label,count_raw:countRaw,raw_text:text(row),
     status:label&&countRaw&&counts.length===1?'read':'unrecognized_structure'};
 });
 const covered=e=>heads.some(n=>n.contains(e))||rates.some(n=>n.contains(e))||rows.some(n=>n.contains(e));
 let unknown=false;
 for(const area of [title,condition]){
   const walker=document.createTreeWalker(area,NodeFilter.SHOW_TEXT);
   while(walker.nextNode()){const n=walker.currentNode;if(n.nodeValue.trim()&&shown(n.parentElement)&&!covered(n.parentElement)&&!['SCRIPT','STYLE','NOSCRIPT'].includes(n.parentElement.tagName))unknown=true}
 }
 const loading=[root,title,condition].some(e=>e.getAttribute('aria-busy')==='true')||
   [title,condition].some(e=>[...e.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="loading--"],[class*="Loading--"],[class*="skeleton"],[class*="Skeleton"]')].some(visible));
 const broken=heads.length!==1||!count||rates.length>1||expressions.length>1||tags.some(t=>t.status!=='read')||unknown;
 // 只保留概览标题、好评率、标签的片段；不序列化包含买家评论的 Comments 外层。
 const fragment=document.createElement('section');fragment.setAttribute('data-scope','review-summary');
 for(const node of [...heads,...rates,...expressions])fragment.append(node.cloneNode(true));
 const footers=[...root.children].filter(e=>has(e,'footer')&&shown(e));
 return {...blank,module_present:true,state:loading?'not_loaded':broken?'unrecognized_structure':'ready',
   reason:loading?'评价概览仍在加载':broken?'评价标题或总结标签存在未识别结构':'',
   heading_raw:heading,count_raw:count,rate_raw:rates.length===1?(rates[0].getAttribute('title')||text(rates[0])):null,
   tags,tags_status:tags.length?'displayed':'not_displayed',raw_html:fragment.outerHTML,
   all_reviews_entry_present:footers.some(e=>parts(e,'ShowButton').some(n=>text(n)==='查看全部评价'))};
}"""


def parse_display_count(raw):
    """保留页面口径：万/亿显示可能舍入，只有普通整数能认作精确数量。"""
    result = {'raw': raw, 'value': None, 'display_value': None, 'lower_bound': None,
              'is_exact': False, 'status': 'not_displayed' if raw is None else 'unparsed'}
    if raw is None:
        return result
    match = re.fullmatch(r'\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*([万亿]?)\s*([+＋]?)\s*', raw)
    if not match:
        return result
    number = Decimal(match[1].replace(',', '')) * {'': 1, '万': 10000, '亿': 100000000}[match[2]]
    display = int(number) if number == int(number) else float(number)
    exact = not match[2] and not match[3] and number == int(number)
    result.update(display_value=display, value=int(number) if exact else None, is_exact=exact,
                  lower_bound=display if match[3] else None,
                  status='exact' if exact else 'lower_bound' if match[3] else 'approximate')
    return result


def parse_positive_rate(raw):
    result = {'raw': raw, 'percent': None, 'period_raw': None,
              'status': 'not_displayed' if raw is None else 'unparsed'}
    if raw is not None:
        match = re.fullmatch(r'\s*(.*?)好评率(?:高达|为|[:：])?\s*([0-9]+(?:\.[0-9]+)?)\s*[%％]\s*', raw)
        if match and 0 <= Decimal(match[2]) <= 100:
            result.update(percent=float(match[2]), period_raw=match[1].strip() or None, status='read')
    return result


def read_review_summary(page, item_id):
    if not matches_detail_url(page, item_id):
        raise ValueError('评价概览的目标商品发生变化')
    value = page.evaluate(REVIEW_SNAPSHOT)
    value['review_count'] = parse_display_count(value['count_raw'])
    value['positive_rate'] = parse_positive_rate(value['rate_raw'])
    if value['state'] != 'ready':
        for field in ('review_count', 'positive_rate'):
            if value[field]['raw'] is None:
                value[field]['status'] = 'not_loaded' if value['state'] == 'not_loaded' else 'unrecognized_structure'
    for tag in value['tags']:
        tag['count'] = parse_display_count(tag['count_raw'])
    return value


def snapshot_key(value):
    return json.dumps({key: value.get(key) for key in ('state', 'heading_raw', 'count_raw', 'rate_raw', 'tags', 'tags_status')},
                      ensure_ascii=False, sort_keys=True)


def collect_review_summary_data(result, timeout_ms, *, on_diagnostic=None, stable_ms=500):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('评价概览采集必须在淘宝专用守卫中运行')
    page, item_id = result.page, result.target.item_id
    same_page = lambda: matches_detail_url(page, item_id)
    guard.resume_check = same_page
    guard.pause_between('page')
    deadline, last_key, stable_since = time.monotonic() + timeout_ms / 1000, None, None
    nav_clicked, scrolled, diagnostic_count = False, False, 0

    def safe_read():
        try:
            return read_review_summary(page, item_id)
        except (ValueError, PlaywrightError):
            return {'state': 'not_loaded', 'module_present': False, 'tags': [], 'raw_html': None,
                    'reason': '商品页面或评价概览暂时无法检查'}

    def reset_wait():
        nonlocal deadline, last_key, stable_since
        deadline, last_key, stable_since = time.monotonic() + timeout_ms / 1000, None, None

    while True:
        paused = guard.paused_seconds
        guard.checkpoint('读取评价概览')
        delta = guard.paused_seconds - paused
        deadline += delta
        if delta:
            last_key, stable_since = None, None
        value = safe_read()
        if not value['module_present'] and value.get('navigation') == 1 and not nav_clicked:
            nav_clicked = True
            guard.perform('定位用户评价页签', lambda: page.locator(REVIEW_NAV).filter(
                has_text=re.compile(r'^\s*用户评价\s*$')).click(timeout=min(timeout_ms, 5000)),
                completed=lambda: safe_read()['module_present'])
            reset_wait()
            continue
        if value['module_present'] and not scrolled:
            scrolled = True
            guard.perform('将评价概览滚动至可见区域', lambda: page.locator(REVIEW_ROOT).filter(
                has=page.locator('[class*="tabDetailItemTitle--"]').filter(has_text=re.compile(r'^用户评价'))
            ).scroll_into_view_if_needed(timeout=min(timeout_ms, 5000)), completed=same_page)
            guard.pause_between('scroll')
            reset_wait()
            continue
        key = snapshot_key(value)
        if key != last_key:
            last_key, stable_since = key, time.monotonic()
        if value['state'] == 'ready' and (time.monotonic() - stable_since) * 1000 >= stable_ms:
            return {**value, 'status': 'complete', 'diagnostic_count': diagnostic_count, 'selector': REVIEW_ROOT}
        if time.monotonic() >= deadline:
            diagnostic_count += 1
            if on_diagnostic and same_page():
                guard.perform('保存评价概览异常现场', lambda: on_diagnostic(value, diagnostic_count))
            guard.request_attention(page, value.get('reason', '评价概览未确认') + '；请核对后回复继续，不会点击查看全部评价', step='复核评价概览')
            reset_wait()
            continue
        paused = guard.paused_seconds
        guard.wait(100)
        if guard.paused_seconds != paused:
            reset_wait()
