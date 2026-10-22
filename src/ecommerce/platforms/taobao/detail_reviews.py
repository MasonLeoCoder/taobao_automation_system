"""评价侧栏的可见 DOM 适配；滚动列表本身，不调用隐藏接口。"""
import json
import re
import time

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.detail_review_summary import read_review_summary
from ecommerce.platforms.taobao.verification import active_guard

DRAWER = '[class*="Drawer--"]:visible'
LIST = DRAWER + ' [class*="comments--"]:visible'
OPEN_BUTTON = '[class*="Comments--"]:visible > [class*="footer--"] [class*="ShowButton--"]'

REVIEWS_SNAPSHOT = r"""() => {
 const has=(e,p)=>[...e.classList].some(c=>c.startsWith(p+'--'));
 const shown=e=>{for(let n=e;n&&n.nodeType===1;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false}return true};
 const visible=e=>{const r=e.getBoundingClientRect();return shown(e)&&r.width>0&&r.height>0};
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>has(n,p)&&shown(n));
 const txt=e=>(e?.innerText||'').trim();
 const path=e=>{const steps=[];for(let n=e;n&&n.nodeType===1;n=n.parentElement){steps.unshift(n.tagName.toLowerCase()+':nth-child('+(n.parentElement?[...n.parentElement.children].indexOf(n)+1:1)+')')}return steps.join(' > ')};
 const cleanText=e=>{if(!e)return null;return (e.getAttribute('title')?.trim()||txt(e))};
 const drawers=[...document.querySelectorAll('[class*="Drawer--"]')].filter(e=>has(e,'Drawer')&&visible(e))
   .filter(e=>parts(e,'tabDetailItemTitle').some(n=>/^用户评价/.test(txt(n))));
 const blank={state:'not_loaded',reason:'全部评价侧栏尚未加载',panel_present:false,rows:[],expanders:[],
   filter:null,sort:null,count_raw:null,empty_text:null,end_text:null,folded_text:null,folded_count_raw:null,folded_end_candidate:false,geometry:null};
 if(drawers.length!==1)return {...blank,state:drawers.length?'unrecognized_structure':'not_loaded',reason:drawers.length?'评价侧栏不唯一':blank.reason};
 const drawer=drawers[0],heads=parts(drawer,'tabDetailItemTitle'),conditions=parts(drawer,'condition');
 const lists=parts(drawer,'comments').filter(visible);
 if(heads.length!==1||conditions.length!==1||lists.length!==1)return {...blank,panel_present:true,state:'unrecognized_structure',reason:'评价标题、筛选或滚动列表结构未确认'};
 const list=lists[0],condition=conditions[0];
 const selected=parts(condition,'imprItem').filter(e=>has(e,'isSelected'));
 const label=e=>{const n=e.cloneNode(true);for(const c of n.querySelectorAll('[class*="imprItemCount--"]'))c.remove();return n.textContent.trim()};
 const filters=selected.map(label),sorts=parts(condition,'sortByText');
 const rows=[...list.children].filter(e=>has(e,'Comment')&&shown(e));
 const isSellerReply=e=>has(e,'sellerReply')||has(e,'merchantReply')||
   has(e,'reply')&&/^(?:商家回复|店家回复)\s*[:：]/.test(txt(e));
 const replyNodes=rows.flatMap(row=>[...row.querySelectorAll('[class]')].filter(e=>shown(e)&&isSellerReply(e)));
 const sellerReplies=replyNodes.filter(e=>!replyNodes.some(parent=>parent!==e&&parent.contains(e)));
 const buyerOwned=e=>!sellerReplies.some(reply=>reply.contains(e));
 const direct=[...list.children].filter(e=>shown(e)&&!has(e,'Comment'));
 const empty=/^(?:暂无(?:用户|商品)?评价|该商品暂无评价|还没有评价|暂无相关评价)[。！!]?$/;
 const end=/^(?:没有更多(?:评价|了)?|暂无更多评价|已显示全部评价|全部评价已加载|已经?到底了)[。！!]?$/;
 const leaves=direct.flatMap(e=>[e,...e.querySelectorAll('*')]).filter(e=>shown(e)&&!e.children.length);
 const emptyText=leaves.map(txt).find(t=>empty.test(t))||null;
 const endText=leaves.map(txt).find(t=>end.test(t))||null;
 const folded=parts(list,'foldInfoWrap').map(txt).join('\n')||null;
 const busy=e=>buyerOwned(e)&&(e.getAttribute('aria-busy')==='true'||[...e.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="loading--"],[class*="Loading--"],[class*="skeleton"],[class*="Skeleton"]')].some(n=>buyerOwned(n)&&visible(n)));
 const interactive=e=>[e,...e.querySelectorAll('*')].some(n=>shown(n)&&(n.matches('button,a,input,select,textarea,[role="button"],[onclick],[tabindex]')||typeof n.onclick==='function'||getComputedStyle(n).cursor==='pointer'));
 const foldNodes=direct.filter(e=>has(e,'foldInfoWrap'));
 const foldMatch=foldNodes.length===1?txt(foldNodes[0]).match(/^已折叠\s*((?:\d{1,3}(?:,\d{3})+|\d+))\s*条对你帮助不大的评价[。！!]?$/):null;
 const foldNode=foldNodes.length===1?foldNodes[0]:null;
 // 折叠提示必须位于评价之后，且没有展开、加载或未知入口；提示本身不等同末尾。
 const foldCandidate=!!foldMatch&&rows.length>0&&[...list.children].indexOf(foldNode)>[...list.children].indexOf(rows.at(-1))&&
   !interactive(foldNode)&&direct.every(e=>e===foldNode||!txt(e)&&!busy(e)&&!interactive(e)&&!e.querySelector('img,video,iframe,canvas'));
 const loading=busy(list)||condition.getAttribute('aria-busy')==='true';
 const expanders=[];
 const url=s=>{try{const u=new URL(s,location.href);return /^(https?:)$/.test(u.protocol)?u.href:null}catch{return null}};
 function media(area,owned){
   const images=[],videos=[],posters=[],tiles=[],unresolved=[];let pending=false;
   for(const album of parts(area,'album').filter(owned)){
     for(const img of album.querySelectorAll('img')){
       if(!shown(img)||!owned(img))continue;
       const raw=img.getAttribute('data-src')||img.currentSrc||img.getAttribute('src');
       const value=raw&&url(raw);
       const placeholder=!value||value.includes('O1CN01Dqo1gd1wMhnobvRco_!!6000000006294-2-tps-145-145.png');
       const tile=img.closest('[class*="photo--"]');
       const isVideo=tile&&parts(tile,'playerIcon').length>0;
       if(placeholder){pending=true;unresolved.push('评价图片正在懒加载')}else if(isVideo)posters.push(value);else images.push(value);
       if(isVideo)tiles.push({path:path(tile),poster_url:placeholder?null:value});
     }
     for(const video of album.querySelectorAll('video')){
       if(!shown(video)||!owned(video))continue;
       const raw=video.currentSrc||video.getAttribute('src')||video.querySelector('source[src]')?.getAttribute('src');
       const value=raw&&url(raw);if(value)videos.push(value);else unresolved.push('评价视频链接未展示');
     }
     for(const n of album.querySelectorAll('[data-video-url],a[href]')){
       if(!shown(n)||!owned(n))continue;
       const raw=n.getAttribute('data-video-url')||n.getAttribute('href'),value=raw&&url(raw);
       if(value&&(/\.(mp4|webm|m3u8)(?:[?#]|$)/i.test(value)||n.hasAttribute('data-video-url')))videos.push(value);
     }
     const icon=[...album.querySelectorAll('[class]')].some(n=>shown(n)&&owned(n)&&/video|play/i.test(n.className||''));
     if(icon&&!videos.length)unresolved.push('评价视频仅展示封面，原链接未展示');
   }
   return {image_urls:[...new Set(images)],video_urls:[...new Set(videos)],video_poster_urls:[...new Set(posters)],
     video_tiles:tiles,unresolved_media:[...new Set(unresolved)],pending_media:pending};
 }
 const expandPattern=/^(展开全文|展开|查看全文|全文)$/;
 function block(area,rowIndex,excluded=[],timeNodes=[]){
   const owned=e=>buyerOwned(e)&&!excluded.some(n=>n.contains(e));
   const bodies=parts(area,'content').filter(owned);
   const controls=[...area.querySelectorAll('button,a,span,div')].filter(e=>owned(e)&&visible(e)&&expandPattern.test(txt(e)))
      .filter(e=>![...e.children].some(n=>txt(n)===txt(e)));
   for(const e of controls)expanders.push({row_index:rowIndex,text:txt(e),path:path(e)});
   const body=bodies.length===1?bodies[0]:null;
   let content=body?cleanText(body):null;
   const omit=[...controls,...timeNodes,...excluded,...sellerReplies].filter(n=>body?.contains(n));
   if(body&&omit.length){
     const clone=body.cloneNode(true),original=[body,...body.querySelectorAll('*')],copies=[clone,...clone.querySelectorAll('*')];
     original.forEach((n,i)=>{if(omit.includes(n))copies[i].remove()});
     for(const br of clone.querySelectorAll('br'))br.replaceWith('\n');
     content=clone.textContent.trim();
   }
   const albums=parts(area,'album').filter(owned),times=parts(area,'appendTime').filter(owned);
   const covered=e=>bodies.some(n=>n.contains(e))||albums.some(n=>n.contains(e))||controls.some(n=>n.contains(e))||[...times,...timeNodes].some(n=>n.contains(e));
   const walker=document.createTreeWalker(area,NodeFilter.SHOW_TEXT);let unparsed=false;
   while(walker.nextNode()){const n=walker.currentNode;if(n.nodeValue.trim()&&owned(n.parentElement)&&shown(n.parentElement)&&!covered(n.parentElement))unparsed=true}
   if([...area.querySelectorAll('iframe,canvas')].some(n=>owned(n)&&shown(n)))unparsed=true;
   return {content,...media(area,owned),body_count:bodies.length,unparsed};
 }
 const records=rows.map((row,index)=>{
   const headers=[...row.children].filter(e=>has(e,'header'));
   const bodies=[...row.children].filter(e=>has(e,'contentWrapper')&&shown(e));
   const candidates=[...row.querySelectorAll('[class]')].filter(e=>buyerOwned(e)&&shown(e)&&[...e.classList].some(c=>/^(?:append|appendComment|appendWrapper|appendContentWrapper)--/.test(c)));
   // 追评可能是初评容器的子节点；只解析最外层追评块，避免重复入库。
   const appends=candidates.filter(e=>!candidates.some(other=>other!==e&&other.contains(e)));
   const names=headers.length===1?parts(headers[0],'userName').filter(buyerOwned):[],metas=headers.length===1?parts(headers[0],'meta').filter(buyerOwned):[];
   const first=bodies.length===1?block(bodies[0],index,appends):{content:null,image_urls:[],video_urls:[],unresolved_media:[],body_count:0};
   const additional=appends.map(e=>{
     const times=[...parts(e,'appendTime'),...parts(e,'appendInternal')].filter(buyerOwned),inner=block(e,index,[],times);
     if(times.length>1||parts(e,'appendInternal').filter(buyerOwned).some(n=>!/(?:追评|追加评价)[:：]?$/.test(txt(n))))inner.unparsed=true;
     return {time_raw:times.length===1?txt(times[0]):null,...inner};
   });
   const ignored=e=>has(e,'header')||has(e,'contentWrapper')||has(e,'splitLine2')||appends.includes(e)||!buyerOwned(e);
   const hasBuyerContent=e=>{
     const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);
     while(walker.nextNode()){const n=walker.currentNode;if(n.nodeValue.trim()&&shown(n.parentElement)&&buyerOwned(n.parentElement))return true}
     return [e,...e.querySelectorAll('img,video,iframe,canvas')].some(n=>n.matches('img,video,iframe,canvas')&&shown(n)&&buyerOwned(n));
   };
   const unknown=[...row.children].some(e=>shown(e)&&!ignored(e)&&hasBuyerContent(e));
   const errors=[];
   if(headers.length!==1||names.length!==1||metas.length!==1||bodies.length!==1||first.body_count>1)errors.push('评价基础字段结构未确认');
   if(unknown||first.unparsed||additional.some(e=>e.unparsed||e.body_count>1||e.body_count===0&&!e.image_urls.length&&!e.video_urls.length&&!e.unresolved_media.length))errors.push('评价含未识别内容结构');
   if(!first.content&&!first.image_urls.length&&!first.video_urls.length&&!additional.some(e=>e.content||e.image_urls.length||e.video_urls.length||e.unresolved_media.length)&&!first.unresolved_media.length)errors.push('评价正文与媒体尚未加载');
   return {dom_index:index,review_id:row.getAttribute('data-rate-id')||row.getAttribute('data-review-id')||row.getAttribute('data-comment-id'),
     author:names.length===1?txt(names[0]):null,meta_raw:metas.length===1?txt(metas[0]):null,
     ...first,pending_media:first.pending_media||additional.some(e=>e.pending_media),append_reviews:additional,errors};
 });
 const unknownList=direct.some(e=>!has(e,'foldInfoWrap')&&!busy(e)&&txt(e)&&![e,...e.querySelectorAll('*')].some(n=>empty.test(txt(n))||end.test(txt(n))));
 const style=getComputedStyle(list);
 const geometry={top:list.scrollTop,height:list.clientHeight,scroll_height:list.scrollHeight,path:path(list),overflow:style.overflowY};
 const count=txt(heads[0]).match(/^用户评价\s*[·•・]?\s*(.+)$/)?.[1]?.trim()??null;
 const broken=filters.length!==1||sorts.length!==1||unknownList||(emptyText&&records.length>0)||(count==='0'&&records.length>0);
 return {...blank,panel_present:true,state:loading?'not_loaded':broken?'unrecognized_structure':!records.length&&!emptyText&&!endText?'not_loaded':'ready',
   reason:loading?'评价列表仍在加载':broken?'评价筛选或列表结构未确认':!records.length&&!emptyText&&!endText?'尚未确认评价或明确空态':'',
   rows:records,expanders,filter:filters.length===1?filters[0]:null,sort:sorts.length===1?txt(sorts[0]):null,
   count_raw:count,empty_text:emptyText,end_text:endText,folded_text:folded,folded_count_raw:foldMatch?.[1]||null,folded_end_candidate:foldCandidate,geometry};
}"""


def read_reviews(page, item_id):
    if not matches_detail_url(page, item_id):
        raise ValueError('评价采集目标商品发生变化')
    return page.evaluate(REVIEWS_SNAPSHOT)


def snapshot_key(value):
    return json.dumps({k: value.get(k) for k in ('state', 'rows', 'expanders', 'filter', 'sort', 'empty_text', 'end_text', 'folded_text', 'folded_count_raw', 'folded_end_candidate', 'geometry')},
                      ensure_ascii=False, sort_keys=True)


def wait_snapshot(result, timeout_ms, *, stable_ms=500, changed_from=None):
    """超时返回未确认状态；等待不重复滚动，人工暂停时间不计入预算。"""
    guard, page, item_id = active_guard(), result.page, result.target.item_id
    deadline, previous, since = time.monotonic() + timeout_ms / 1000, None, None
    while True:
        paused = guard.paused_seconds
        guard.checkpoint('等待评价列表就绪')
        delta = guard.paused_seconds - paused
        deadline += delta
        if delta:
            previous, since = None, None
        try:
            value = read_reviews(page, item_id)
        except (ValueError, PlaywrightError):
            value = {'state': 'not_loaded', 'reason': '商品或评价侧栏暂时无法检查', 'rows': [], 'expanders': []}
        key = snapshot_key(value)
        if key != previous:
            previous, since = key, time.monotonic()
        if value['state'] == 'ready' and (changed_from is None or key != changed_from) and (time.monotonic()-since)*1000 >= stable_ms:
            return {**value, 'stable': True}
        if time.monotonic() >= deadline:
            return {**value, 'stable': value['state'] == 'ready' and (time.monotonic()-since)*1000 >= stable_ms}
        paused = guard.paused_seconds
        guard.wait(100)
        if guard.paused_seconds != paused:
            deadline = time.monotonic() + timeout_ms / 1000
            previous, since = None, None


def ensure_reviews_open(result, timeout_ms):
    guard, page = active_guard(), result.page
    if read_reviews(page, result.target.item_id)['panel_present']:
        return
    # 零评价且没有入口时也可明确返回空态，不能将入口缺失认作零评价。
    summary = read_review_summary(page, result.target.item_id)
    if summary['state'] == 'ready' and summary['review_count']['value'] == 0 and not summary['all_reviews_entry_present']:
        return {'state': 'ready', 'stable': True, 'rows': [], 'expanders': [], 'filter': '全部', 'sort': None,
                'count_raw': '0', 'empty_text': '用户评价数量明确为0', 'end_text': None, 'folded_text': None, 'geometry': None}
    button = page.locator(OPEN_BUTTON).filter(has_text=re.compile(r'^\s*查看全部评价\s*$'))
    if button.count() != 1:
        return {'state': 'not_loaded', 'reason': '查看全部评价入口未确认', 'rows': [], 'expanders': []}
    guard.pause_between('page')
    guard.perform('打开全部评价', lambda: button.click(timeout=min(timeout_ms, 5000)),
                  completed=lambda: read_reviews(page, result.target.item_id)['panel_present'])
    return None


def expand_review(result, control, timeout_ms):
    guard, page = active_guard(), result.page
    guard.perform('展开评价全文', lambda: page.locator(control['path']).click(timeout=min(timeout_ms, 5000)),
                  completed=lambda: all(c['path'] != control['path'] for c in read_reviews(page, result.target.item_id)['expanders']))


def scroll_reviews(result, value, timeout_ms):
    guard, page = active_guard(), result.page
    geometry = value['geometry']
    def scroll():
        if not matches_detail_url(page, result.target.item_id):
            raise ValueError('滚动前目标商品发生变化')
        current = read_reviews(page, result.target.item_id)
        if (current.get('filter'), current.get('sort')) != (value['filter'], value['sort']):
            raise ValueError('滚动前评价筛选或排序发生变化')
        pane = page.locator(geometry['path'])
        pane.hover(timeout=min(timeout_ms, 5000))
        page.mouse.wheel(0, max(1, geometry['height'] * 0.65))
    guard.perform('滚动评价列表', scroll, completed=lambda: matches_detail_url(page, result.target.item_id))
    guard.pause_between('scroll')


VIEWER = '[class*="BlackLightPage--"]:visible'
VIEWER_SNAPSHOT = r"""() => {
 const roots=[...document.querySelectorAll('[class*="BlackLightPage--"]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'});
 if(roots.length!==1)return {present:roots.length>0,valid:false};
 const root=roots[0],texts=root.querySelectorAll('[class*="itemTextWrap--"]'),videos=root.querySelectorAll('[class*="itemVideo--"] video');
 const video=videos.length===1?videos[0]:null;
 return {present:true,valid:texts.length===1&&videos.length===1,text:texts.length===1?texts[0].innerText:null,
   url:video?(video.currentSrc||video.getAttribute('src')||video.querySelector('source[src]')?.getAttribute('src')):null};
}"""


def resolve_review_video(result, row, tile, timeout_ms):
    """单次打开已核对的买家视频卡片，只读取评价专用播放器，不取商品主图视频。"""
    from urllib.parse import urlsplit
    guard, page, item_id = active_guard(), result.page, result.target.item_id
    guard.checkpoint('定位评价视频')
    current = read_reviews(page, item_id)
    index = row['dom_index']
    if index >= len(current['rows']) or any(current['rows'][index].get(k) != row.get(k) for k in ('author', 'meta_raw', 'content')):
        raise ValueError('打开视频前评价归属发生变化')
    if page.evaluate(VIEWER_SNAPSHOT)['present']:
        raise ValueError('已有媒体查看器尚未关闭，未点击新的评价视频')
    guard.perform('打开买家评价视频', lambda: page.locator(tile['path']).click(timeout=min(timeout_ms, 5000)),
                  completed=lambda: page.evaluate(VIEWER_SNAPSHOT)['present'])
    deadline = time.monotonic() + timeout_ms / 1000
    normalize = lambda s: re.sub(r'\s+', '', s or '')
    expected = [row.get('author'), *(re.split(r'已购\s*[:：]', row.get('meta_raw') or '', maxsplit=1)), row.get('content')]
    while True:
        paused = guard.paused_seconds
        guard.checkpoint('读取买家评价视频链接')
        deadline += guard.paused_seconds - paused
        if not matches_detail_url(page, item_id):
            raise ValueError('读取评价视频时目标商品发生变化')
        view = page.evaluate(VIEWER_SNAPSHOT)
        belongs = view.get('valid') and all(normalize(value) in normalize(view.get('text')) for value in expected if value)
        if belongs and view.get('url') and urlsplit(view['url']).scheme in {'http', 'https'}:
            source = view['url']
            break
        if time.monotonic() >= deadline:
            guard.request_attention(page, '评价播放器尚未暴露可用链接或评价归属不符，请核对本条评价视频后回复继续，不会重复打开', step='核对评价视频链接')
            deadline = time.monotonic() + timeout_ms / 1000
        paused = guard.paused_seconds
        guard.wait(100)
        deadline += guard.paused_seconds - paused
    close = page.locator(VIEWER + ' > [class*="iconWrap--"]').filter(has=page.locator('[class*="closeIcon--"]'))
    if close.count() != 1:
        raise ValueError('评价视频关闭入口未确认')
    guard.perform('关闭买家评价视频并返回列表', lambda: close.click(timeout=min(timeout_ms, 5000)),
                  completed=lambda: not page.evaluate(VIEWER_SNAPSHOT)['present'])
    deadline = time.monotonic() + timeout_ms / 1000
    while page.evaluate(VIEWER_SNAPSHOT)['present']:
        if time.monotonic() >= deadline:
            guard.request_attention(page, '评价视频查看器尚未关闭，请关闭后回复继续，不会重复点击', step='返回评价列表')
            deadline = time.monotonic() + timeout_ms / 1000
        paused = guard.paused_seconds
        guard.wait(100)
        deadline += guard.paused_seconds - paused
    guard.pause_between('page')
    return source
