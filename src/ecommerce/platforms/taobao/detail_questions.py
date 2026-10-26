"""问大家可见 DOM 适配：独立侧栏、逐题展开、仅操作侧栏滚动区域。"""
import json
import re
import time

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import active_guard

ROOT = '[class*="AskAnswersWrap--"]:visible'
PREVIEW = '[class*="askAnswerWrap--"]:visible'

SNAPSHOT = r"""() => {
 const has=(e,p)=>[...e.classList].some(c=>c.startsWith(p+'--'));
 const shown=e=>{for(let n=e;n&&n.nodeType===1;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false}return true};
 const visible=e=>shown(e)&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>has(n,p)&&shown(n));
 const txt=e=>(e?.innerText||'').trim();
 const text=e=>{if(!e)return null;if(e.getAttribute('title'))return e.getAttribute('title').trim();if(!e.querySelector('img[alt]'))return txt(e)||null;const out=[],w=document.createTreeWalker(e,NodeFilter.SHOW_TEXT|NodeFilter.SHOW_ELEMENT);while(w.nextNode()){const n=w.currentNode;if(n.nodeType===3&&shown(n.parentElement))out.push(n.nodeValue);else if(n.nodeType===1&&shown(n)&&n.tagName==='IMG')out.push(n.getAttribute('alt')||'');else if(n.nodeType===1&&shown(n)&&n.tagName==='BR')out.push('\n')}return out.join('').trim()||null};
 const path=e=>{const s=[];for(let n=e;n&&n.nodeType===1;n=n.parentElement)s.unshift(n.tagName.toLowerCase()+':nth-child('+(n.parentElement?[...n.parentElement.children].indexOf(n)+1:1)+')');return s.join(' > ')};
 const busy=e=>e.getAttribute('aria-busy')==='true'||[...e.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="loading--"],[class*="Loading--"],[class*="skeleton"],[class*="Skeleton"]')].some(visible);
 const leaves=e=>[e,...e.querySelectorAll('*')].filter(n=>shown(n)&&!n.children.length).map(txt);
 const empty=/^(?:暂无问答|暂无提问|还没有人提问|暂无问题|该商品暂无问答)[。！!]?$/;
 const end=/^(?:没有更多(?:问题|问答|了)?|已显示全部问题|全部问题已加载|已经?到底了)[。！!]?$/;
 const blank={state:'not_loaded',reason:'问大家尚未加载',panel_present:false,rows:[],count_raw:null,filter:[],search:'',geometry:null,empty_text:null,end_text:null};
 const roots=[...document.querySelectorAll('[class*="AskAnswersWrap--"]')].filter(e=>has(e,'AskAnswersWrap')&&visible(e));
 if(!roots.length){
   const ps=[...document.querySelectorAll('[class*="askAnswerWrap--"]')].filter(e=>has(e,'askAnswerWrap')&&visible(e));
   if(ps.length!==1)return blank;
   const heads=parts(ps[0],'askAnswerTitle'),count=heads.length===1?txt(heads[0]).match(/^问大家\s*[·•・]?\s*(.+)$/)?.[1]?.trim():null;
   const no=leaves(ps[0]).find(t=>empty.test(t));
   const entry=leaves(ps[0]).some(t=>t==='查看全部问答'),previews=parts(ps[0],'askAnswerItem').length;
   const zero=count==='0'&&!entry&&!previews;
   return {...blank,count_raw:count,state:zero||no?'ready':'not_loaded',reason:'全部问答侧栏尚未打开',empty_text:no||(zero?'问大家数量为0且没有问答入口或预览':null)};
 }
 if(roots.length!==1)return {...blank,state:'unrecognized_structure',reason:'问大家侧栏不唯一'};
 const root=roots[0],heads=parts(root,'askAnswerTitle'),lists=parts(root,'qaListContainer'),areas=parts(root,'ContentArea');
 if(heads.length!==1||lists.length!==1||areas.length!==1)return {...blank,panel_present:true,state:'unrecognized_structure',reason:'问大家标题或列表结构未确认'};
 const filterLabel=e=>{const clone=e.cloneNode(true);for(const n of clone.querySelectorAll('[class*="conditionItemCount--"]'))n.remove();return clone.textContent.trim()};
 const list=lists[0],area=areas[0],filters=parts(root,'conditionItem').filter(n=>n.getAttribute('aria-selected')==='true'||[...n.classList].some(c=>/^(?:isSelected|selected|active)--/.test(c))).map(filterLabel);
 const search=root.querySelector('input')?.value||'';
 const geometry=e=>({path:path(e),top:e.scrollTop,height:e.clientHeight,scroll_height:e.scrollHeight,overflow:getComputedStyle(e).overflowY});
 const controls=(e,pattern)=>[...e.querySelectorAll('button,a,span,div')].filter(n=>visible(n)&&pattern.test(txt(n))&&![...n.children].some(c=>txt(c)===txt(n))).map(n=>({text:txt(n),path:path(n)}));
 const expand=/^(?:展开全文|查看全文|全文|展开)$/;
 const url=s=>{if(typeof s!=='string'||!s.trim())return null;try{const u=new URL(s,location.href);return /^https?:$/.test(u.protocol)?u.href:null}catch{return null}};
 function media(a){
   const images=[],videos=[],posters=[],unresolved=[];
   for(const img of a.querySelectorAll('img')){
     if(!shown(img)||img.closest('[class*="userInfo--"],[class*="convertEmoji--"]'))continue;
     const u=url(img.getAttribute('data-src')||img.currentSrc||img.getAttribute('src'));
     if(u)images.push(u);else unresolved.push('回答图片链接未展示');
   }
   for(const v of a.querySelectorAll('video')){const u=url(v.currentSrc||v.getAttribute('src')||v.querySelector('source[src]')?.getAttribute('src'));if(u)videos.push(u);else unresolved.push('回答视频链接未展示');if(v.poster)posters.push(v.poster)}
   for(const n of a.querySelectorAll('[data-video-url],a[href]')){const u=url(n.getAttribute('data-video-url')||n.getAttribute('href'));if(u&&(/\.(mp4|webm|m3u8)(?:[?#]|$)/i.test(u)||n.hasAttribute('data-video-url')))videos.push(u)}
   if([...a.querySelectorAll('[class]')].some(n=>shown(n)&&/^(?:playerIcon|videoIcon|playIcon)--/.test(n.className))&&!videos.length)unresolved.push('回答视频仅展示封面，原链接未展示');
   return {image_urls:[...new Set(images)],video_urls:[...new Set(videos)],video_poster_urls:[...new Set(posters)],unresolved_media:[...new Set(unresolved)]};
 }
 const rows=[...list.children].filter(n=>has(n,'qaItem')&&shown(n)).map((q,index)=>{
   const titles=parts(q,'questionTitle'),sections=parts(q,'answerSection'),bottoms=parts(q,'bottomActionsWrap');
   const title=titles.length===1?titles[0]:null,a=sections.length===1?sections[0]:null,bottom=bottoms.length===1?bottoms[0]:null;
   const hasCollapse=!!bottom&&parts(bottom,'collapseBtn').some(n=>txt(n)==='收起');
   const open=bottom?parts(bottom,'viewAllBtnWrap').filter(visible).map(n=>({text:txt(n),path:path(n)})):[];
   const more=bottom?controls(bottom,/^(?:展开更多|加载更多(?:回答)?|查看更多回答|更多回答|展开更多回答|查看剩余.*回答)$/):[];
   const items=a?[...a.children].filter(n=>has(n,'answerItem')&&shown(n)):[];
   // 少量回答会直接显示完整用户、正文和时间，没有“查看全部回答/收起”按钮。
   // initContent 是预览布局，不能因为没有按钮就当作完整回答。
   const fullWithoutToggle=!hasCollapse&&!open.length&&items.length>0&&items.every(n=>parts(n,'userNick').length===1&&parts(n,'answerContent').length===1&&!parts(n,'initContent').length);
   const expanded=hasCollapse||fullWithoutToggle;
   const noAnswer=a?leaves(a).find(t=>/^(?:暂无回答|还没有回答|暂无人回答)[。！!]?$/ .test(t)):null;
   const answerEnd=a?leaves(a).find(t=>/^(?:没有更多回答|已显示全部回答|全部回答已加载)[。！!]?$/ .test(t)):null;
   const errors=[];
   if(titles.length!==1||sections.length!==1||bottoms.length>1||!text(title))errors.push('问题或回答区域结构未确认');
   const questionControls=title?controls(title,expand):[];
   const answers=expanded?items.map((n,i)=>{
     const names=parts(n,'userNick'),bodies=parts(n,'answerContent'),times=parts(n,'answerTime'),tags=parts(n,'userTag'),purchases=parts(n,'timeAgo');
     const body=bodies.length===1?bodies[0]:null,ext=body?controls(body,expand):[];
     const m=media(n),errs=[];
     if(names.length!==1||bodies.length!==1||times.length>1||!text(body)&&!m.image_urls.length&&!m.video_urls.length)errs.push('回答基础内容尚未确认');
     if(body&&!body.getAttribute('title')&&!ext.length&&(body.scrollHeight>body.clientHeight+2||body.scrollWidth>body.clientWidth+2))errs.push('回答正文可能仍被截断');
     if([...n.querySelectorAll('iframe,canvas')].some(shown))errs.push('回答包含未识别媒体');
     return {answer_id:n.getAttribute('data-answer-id'),author:names.length===1?text(names[0]):null,content:text(body),meta_raw:times.length===1?text(times[0]):null,
       identity_labels:tags.map(txt),purchase_labels:purchases.map(txt),...m,errors:errs,expanders:ext,dom_index:i};
   }):[];
   const unknown=a?[...a.children].some(n=>shown(n)&&!has(n,'answerItem')&&txt(n)&&!busy(n)&&!leaves(n).some(t=>/^(暂无回答|还没有回答|暂无人回答|没有更多回答|已显示全部回答|全部回答已加载)[。！!]?$/ .test(t))):false;
   if(unknown)errors.push('回答区域含未识别内容');
   if(noAnswer&&items.length)errors.push('暂无回答提示与实际回答同时存在');
   if(bottom){
     const known=[...parts(bottom,'collapseBtn'),...parts(bottom,'viewAllBtnWrap')];
     const walker=document.createTreeWalker(bottom,NodeFilter.SHOW_TEXT);let unrecognized=false;
     while(walker.nextNode()){const n=walker.currentNode;if(n.nodeValue.trim()&&shown(n.parentElement)&&!known.some(k=>k.contains(n))&&!more.some(c=>document.querySelector(c.path)?.contains(n)))unrecognized=true}
     if(unrecognized&&!busy(bottom))errors.push('回答底部存在未识别操作入口');
   }
   const clipped=!!a&&a.scrollHeight>a.clientHeight+2;
   const answerCount=a?.getAttribute('data-answer-count')||q.getAttribute('data-answer-count');
   return {dom_index:index,path:path(q),question_id:q.getAttribute('data-question-id'),question:text(title),question_author:null,question_date_raw:null,
     preview_text:!expanded&&a?txt(a):null,expanded,answer_display_mode:hasCollapse?'expanded':fullWithoutToggle?'full_without_toggle':'preview',open_controls:open,more_controls:more,expanders:questionControls,
     answers,answer_count_raw:answerCount,answer_empty_text:noAnswer||null,answer_end_text:answerEnd||null,
     answers_complete:!!noAnswer||expanded&&!busy(q)&&!more.length&&!clipped&&!unknown&&answers.length>0,
     answer_geometry:a?geometry(a):null,loading:busy(q),errors};
 });
 const direct=[...list.children].filter(n=>shown(n)&&!has(n,'qaItem'));
 const texts=direct.flatMap(leaves),no=texts.find(t=>empty.test(t)),endText=texts.find(t=>end.test(t));
 const unknown=direct.some(n=>txt(n)&&!busy(n)&&!leaves(n).some(t=>empty.test(t)||end.test(t)));
 const count=txt(heads[0]).match(/^问大家\s*[·•・]?\s*(.+)$/)?.[1]?.trim()||null;
 const loading=busy(area),broken=unknown||(no&&rows.length);
 return {...blank,panel_present:true,state:loading?'not_loaded':broken?'unrecognized_structure':rows.length||no||endText?'ready':'not_loaded',
   reason:loading?'问答仍在加载':broken?'问答列表存在未识别结构':'',rows,count_raw:count,filter:filters,search,
   geometry:geometry(area),empty_text:no||null,end_text:endText||null};
}"""


def read_questions(page, item_id):
    if not matches_detail_url(page, item_id):
        raise ValueError('问大家目标商品发生变化')
    return page.evaluate(SNAPSHOT)


def snapshot_key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def all_questions_selected(value):
    """兼容无显式全部标签，以及唯一选中“全部”的两种布局；数量不属于标签名称。"""
    return value.get('filter') in ([], ['全部']) and not value.get('search')


def wait_snapshot(result, timeout_ms, *, stable_ms=500, changed_from=None):
    guard = active_guard()
    deadline, previous, since = time.monotonic() + timeout_ms / 1000, None, time.monotonic()
    while True:
        paused = guard.paused_seconds
        guard.checkpoint('等待问答区域加载稳定')
        if guard.paused_seconds != paused:
            deadline = time.monotonic() + timeout_ms / 1000
            previous = None
        try:
            value = read_questions(result.page, result.target.item_id)
        except (ValueError, PlaywrightError):
            value = {'state': 'not_loaded', 'reason': '商品或问答侧栏暂时无法检查', 'rows': []}
        key = snapshot_key(value)
        if key != previous:
            previous, since = key, time.monotonic()
        stable = value['state'] == 'ready' and (time.monotonic() - since) * 1000 >= stable_ms
        if stable and (changed_from is None or key != changed_from) or time.monotonic() >= deadline:
            return {**value, 'stable': stable}
        paused = guard.paused_seconds
        guard.wait(100)
        deadline += guard.paused_seconds - paused


def ensure_questions_open(result, timeout_ms):
    """寻找并打开问答；参数：商品页面、超时；返回：正常为空，入口缺失返回跳过依据。"""
    guard, page, item_id = active_guard(), result.page, result.target.item_id
    def inspect():
        """确认页面及问答结构仍可识别；参数：无；返回：当前快照。"""
        snapshot=read_questions(page,item_id)
        if snapshot.get('state')=='unrecognized_structure':
            raise ValueError(snapshot.get('reason') or '问大家结构未确认')
        return snapshot
    guard.checkpoint('准备打开问大家')
    value = inspect()
    if value['panel_present'] or value.get('empty_text'):
        return
    # 支持在 --reviews 后串行执行；只关闭已识别的评价侧栏。
    reviews = page.locator('[class*="Drawer--"]:visible').filter(has=page.locator('[class*="tabDetailItemTitle--"]', has_text=re.compile('^用户评价')))
    if reviews.count() == 1:
        close = reviews.locator('[class*="closeWrap--"]:visible')
        if close.count() != 1:
            raise ValueError('评价侧栏关闭入口未确认')
        guard.pause_between('page')
        guard.perform('关闭评价侧栏后进入问大家', lambda: close.click(timeout=min(timeout_ms, 5000)), completed=lambda: reviews.count() == 0)
    button = page.locator(PREVIEW).get_by_text('查看全部问答', exact=True)
    # 刚打开详情时下方模块尚未挂载。以普通页面滚动触发懒加载；入口出现即停止。
    # 仅入口缺失可以跳过；页面、验证和结构错误仍由原守卫处理。
    unchanged = scroll_count = 0
    for _ in range(10):
        value = inspect()
        if value.get('empty_text') or value['panel_present']:
            return
        count=button.count()
        if count>1:
            raise ValueError('查看全部问答入口不唯一')
        if count==1:
            break
        guard.pause_between('scroll')
        before = page.evaluate('()=>[scrollY,document.documentElement.scrollHeight,innerHeight]')
        def scroll():
            if not matches_detail_url(page, item_id):
                raise ValueError('寻找问大家入口时目标商品发生变化')
            page.evaluate('()=>window.scrollBy(0,Math.max(80,innerHeight*0.75))')
        guard.perform('分段滚动加载问大家入口', scroll)
        scroll_count += 1
        guard.wait(min(timeout_ms, 500))
        after = page.evaluate('()=>[scrollY,document.documentElement.scrollHeight,innerHeight]')
        unchanged = unchanged + 1 if before == after else 0
        if unchanged >= 3:
            break
    # 最后一次滚动可能刚挂载入口或明确空态，必须再读一次。
    guard.checkpoint('复核问大家入口寻找结果')
    value=inspect()
    if value.get('empty_text') or value['panel_present']:
        return
    count=button.count()
    if count>1:
        raise ValueError('查看全部问答入口不唯一')
    if count==0:
        search={'scroll_count':scroll_count,'max_scrolls':10,'unchanged_count':unchanged,
                'stop_reason':'stable_position' if unchanged>=3 else 'scroll_limit_reached'}
        return {'status':'skipped','stop_reason':'entry_not_found','entry_search':search,
                'message':f'未找到问大家入口，已跳过（滚动{scroll_count}次；'+
                    ('连续3次页面位置和高度不变）' if unchanged>=3 else '达到10次滚动上限）')}
    guard.pause_between('page')
    guard.perform('打开全部问答', lambda: button.click(timeout=min(timeout_ms, 5000)),
                  completed=lambda: read_questions(page, item_id)['panel_present'])


def question_action(result, row, control, timeout_ms, *, step):
    """执行前核对 DOM 位置仍属于同一问题；不重放不确定的点击。"""
    guard, page = active_guard(), result.page
    def action():
        value = read_questions(page, result.target.item_id)
        matches = [r for r in value['rows'] if r['path'] == row['path'] and r['question'] == row['question'] and r['question_id'] == row['question_id']]
        if len(matches) != 1 or not all_questions_selected(value):
            raise ValueError('操作前当前问题或问答筛选发生变化')
        node = page.locator(control['path'])
        if node.count() != 1 or not node.evaluate('(e,p)=>document.querySelector(p)?.contains(e)', row['path']):
            raise ValueError('回答操作入口不再属于当前问题')
        if node.inner_text().strip() != control['text']:
            raise ValueError('回答操作入口文字发生变化')
        node.click(timeout=min(timeout_ms, 5000))
    guard.perform(step, action)


def scroll_region(result, geometry, *, question=None):
    guard, page = active_guard(), result.page
    guard.pause_between('scroll')
    def action():
        value = read_questions(page, result.target.item_id)
        if not all_questions_selected(value):
            raise ValueError('滚动前问答筛选发生变化')
        if question and not any(r['path'] == question['path'] and r['question'] == question['question'] for r in value['rows']):
            raise ValueError('滚动前当前问题发生变化')
        page.locator(geometry['path']).evaluate('(e)=>{e.scrollTop+=Math.max(80,e.clientHeight*0.75)}')
    guard.perform('分段滚动当前问题的回答' if question else '分段滚动问大家列表', action)
