"""只读解析已加载的搜索列表卡片，不请求详情或跟随推广链接。"""
import re
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ecommerce.platforms.taobao.ad_markers import AD_RECOMMENDATION_IMAGE_SELECTOR, AD_RECOMMENDATION_IMAGE_URL

CARD_SELECTOR = '[class*="doubleCardWrapperAdapt--"],[class*="doubleCardWrapper--"]'
LIST_SELECTOR = "#content_items_wrapper"

PAGE_METADATA = r"""() => {
 const shown=e=>{const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
  for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false;}return true;};
 const text=e=>(e.innerText||'').trim(),u=new URL(location.href);
 const tabs=[...document.querySelectorAll('.next-tabs-nav .next-tabs-tab[class*="customTabItem--"]')].filter(shown);
 const selected=tabs.filter(e=>e.classList.contains('active')||e.getAttribute('aria-selected')==='true');
 const legacy=[...document.querySelectorAll('[class*="sort" i]')].filter(e=>shown(e)&&/(selected|active|current)/i.test(e.className||''));
 const labels=(tabs.length?selected:legacy).map(text).filter(t=>/^(综合|销量|价格|信用|人气)$/.test(t));
 const sales=tabs.filter(e=>text(e)==='销量');
 const groups=[...document.querySelectorAll('.next-pagination')].filter(shown);
 const normal=groups.filter(e=>e.classList.contains('next-normal')),simple=groups.filter(e=>e.classList.contains('next-simple'));
 const chosen=normal.length===1?normal:(normal.length===0&&simple.length===1?simple:[]);
 const next=chosen.flatMap(e=>[...e.querySelectorAll('button.next-next')].filter(shown));
 const totals=[...new Set(groups.flatMap(e=>[...e.querySelectorAll('.next-pagination-display')].map(n=>(text(n).match(/\/\s*(\d+)/)||[])[1]).filter(Boolean)))];
 return {origin:u.origin,path:u.pathname,query_keywords:u.searchParams.getAll('q'),
  inputs:[...document.querySelectorAll('input[name="q"],input#q')].filter(shown).map(e=>e.value),
  paging:Object.fromEntries(['page','pageNo','pageNum','s'].map(k=>[k,u.searchParams.getAll(k)])),
  sort_parameter:u.searchParams.getAll('sort'),sort_labels:[...new Set(labels)],
  page_numbers:[...new Set([...document.querySelectorAll('.next-pagination-display em,.next-pagination-item.next-current')].filter(shown).map(text))],
  sales_control_count:sales.length,sales_control_enabled:sales.length===1&&!sales[0].classList.contains('disabled')&&!sales[0].classList.contains('next-disabled')&&sales[0].getAttribute('aria-disabled')!=='true',
  list_busy:[...document.querySelectorAll('[aria-busy="true"],.next-loading-indicator')].some(shown),
  empty_message_present:[...document.querySelectorAll('p,div,span,h1,h2,h3')].some(e=>shown(e)&&!e.children.length&&/(?:没有找到|未找到|没有搜索到).{0,15}(?:相关|符合).{0,12}(?:宝贝|商品)|抱歉.{0,12}没有找到.{0,12}(?:宝贝|商品)/.test(e.innerText||'')),
  document_started:performance.timeOrigin,
  pagination:{group_count:chosen.length,variant:normal.length===1?'normal':(normal.length===0&&simple.length===1?'simple':null),
   next_count:next.length,next_disabled:next.length===1?(next[0].disabled||next[0].getAttribute('aria-disabled')==='true'||next[0].classList.contains('next-disabled')):null,total_pages:totals}};
}"""

SNAPSHOT = r"""() => {
 const shown=e=>{
  if(!e)return false;
  const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
  for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false;}
  return true;
 };
 const text=e=>e?(e.innerText||'').trim():null;
 const allParts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>[...n.classList].some(c=>c.startsWith(p+'--')));
 const parts=(e,p)=>allParts(e,p).filter(shown);
 const part=(e,p)=>parts(e,p)[0]||null;
 const numeric=v=>/^\d{1,25}$/.test(v||'')?v:null;
 const asset=(e,attribute='src')=>{
  const raw=e?.getAttribute(attribute);if(!raw)return null;
  try{const u=new URL(raw,location.href);return ['http:','https:'].includes(u.protocol)?u.href:null;}catch{return null;}
 };
 const roots=[...document.querySelectorAll('#content_items_wrapper')];
 const children=roots.length===1?[...roots[0].children]:[];
 const slots=children.map((e,i)=>({e,position:i+1})).filter(({e})=>shown(e)).map(({e,position})=>{
  const anchors=[...e.querySelectorAll('a[id^="item_id_"]')].filter(a=>/^item_id_\d{5,25}$/.test(a.id));
  const survey=!!e.querySelector('#pcSearchInvestigationCard_root_container,[class*="surveyCard--"],[class*="pcSearchInvestigationCard--"]');
  const suggestions=parts(e,'suggestItemCard').some(card=>[card,...card.querySelectorAll('div,span,h2,h3')].some(n=>shown(n)&&text(n)==='大家都在搜'));
  if(survey&&!suggestions&&!anchors.length)return {position,type:'search_experience_survey'};
  if(suggestions&&!survey&&!anchors.length)return {position,type:'search_suggestions'};
  if(!e.classList.contains('search-content-col')||survey||suggestions||anchors.length!==1){
   return {position,type:'unknown',reason:survey||suggestions?'非商品卡片与其他结构混合':anchors.length>1?'单格商品入口不唯一':'未识别为商品或已适配的非商品卡片'};
  }
  const a=anchors[0];
  let link=null;
  let skuReference=null;
  try{const url=new URL(a.href,location.href);
   link={scheme:url.protocol,host:url.hostname,path:url.pathname,ids:url.searchParams.getAll('id').map(numeric)};
   const direct=url.searchParams.getAll('skuId');
   const nested=new URLSearchParams(url.searchParams.get('a')||'').getAll('skuId');
   const values=[...new Set([...direct,...nested])];
   if(values.length===1)skuReference=numeric(values[0]);
  }catch{}
  const shop=part(e,'shopName');
  let shopIds=[];
  let shopLink=null;
  try{const url=new URL(shop.href,location.href);
   if(['https:','http:'].includes(url.protocol)&&url.hostname==='store.taobao.com'&&url.pathname==='/category.htm'){
    const values=url.searchParams.getAll('topIds');
    if(values.length===1&&/^\d{1,25}(?:,\d{1,25})*$/.test(values[0]))shopIds=values[0].split(',');
    const app=url.searchParams.getAll('appUid');
    if(app.length===1&&/^[A-Za-z0-9_-]{1,300}$/.test(app[0]))shopLink={app_uid:app[0],top_ids:shopIds};
   }
  }catch{}
  const dom=a.getAttribute('id')||'';
  const match=dom.match(/^(?:item_id_|item_|item-)(\d{5,25})$/);
  const title=part(e,'title');
  const amount=text(part(e,'innerPriceWrapper'));
  const adImage=e.querySelector(__AD_IMAGE_SELECTOR__)!==null;
  // 视频悬浮预览会临时隐藏这个静态主图区；按结构取链接，仍排除 imageSwitch。
  const picture=allParts(e,'mainPicAdaptWrapper')[0]||null;
  const mainImages=picture?allParts(picture,'mainPic'):[];
  const service=part(e,'subIconWrapper');
  const video=[...e.querySelectorAll('video')].filter(shown)[0]||null;
  const videoSource=video?(asset(video)||asset(video.querySelector('source'))):null;
  const videoFlag=!!part(e,'videoIcon')||!!e.querySelector('[class*="holderVideo--"]')||!!video;
  const videoIcon=e.querySelector('[class*="videoIconInner--"]');
  let videoIconUrl=null;
  try{const value=videoIcon?getComputedStyle(videoIcon).backgroundImage:'';
   const match=value.match(/^url\(["']?(.*?)["']?\)$/);if(match){const u=new URL(match[1],location.href);if(['http:','https:'].includes(u.protocol))videoIconUrl=u.href;}
  }catch{}
  const titleAttribute=title?.getAttribute('title')?.trim();
  return {position,type:'product',title:titleAttribute||text(title),title_source:titleAttribute?'title_attribute':'visible_text',
   price_number_text:amount?amount.replace(/\s+/g,''):null,price_integer_text:text(part(e,'priceInt')),price_fraction_text:text(part(e,'priceFloat')),
   currency_symbol:text(part(e,'unit')),price_label:text(part(e,'priceDesc')),sales_text:text(part(e,'realSales')),
   location_text:parts(e,'procity').map(text).filter(Boolean).join(' ')||null,
   shop_name:text(part(e,'shopNameText')),shop_link:shopLink,shop_href_present:!!shop?.getAttribute('href'),
   link,dom_item_id:match?match[1]:null,shop_top_ids:shopIds,sku_reference:skuReference,
   main_image_url:mainImages.length===1?asset(mainImages[0]):null,main_image_count:mainImages.length,
   rank_title:text(part(e,'rankTitle')),rank_position_text:text(part(e,'rankSubTitle')),
   attribute_tags:parts(e,'abstractWrapper').flatMap(n=>parts(n,'text').map(text)).filter(Boolean),
   service_tags:service?[...service.querySelectorAll('span')].filter(n=>shown(n)&&!n.children.length).map(text).filter(Boolean):[],
   video_present:videoFlag,video_url:videoSource,video_source_present:!!video?.getAttribute('src')||!!video?.querySelector('source[src]'),
   video_icon_url:videoIconUrl,
   title_icon_url:asset(part(e,'iconPic')),customer_service_icon_url:asset(part(e,'wwIcon')),
   field_elements:{main_image_url:mainImages.length>0||!!e.querySelector('[class*="mainImg--"]'),price_value:!!part(e,'innerPriceWrapper'),currency_symbol:!!part(e,'unit'),
    price_label:!!part(e,'priceDesc'),sales_text:!!part(e,'realSales'),shop_name:!!part(e,'shopNameText'),
    shop_url:!!shop?.getAttribute('href'),location_text:parts(e,'procity').length>0,rank_title:!!part(e,'rankTitle'),
    rank_position:!!part(e,'rankSubTitle'),attribute_tags:parts(e,'abstractWrapper').length>0,service_tags:!!service,
    title_icon_url:!!part(e,'iconPic'),customer_service_icon_url:!!part(e,'wwIcon'),video_icon_url:!!videoIcon},
   ad_recommendation:adImage,ad_basis:adImage?'specified_image_url':null,ad_marker_url:adImage?__AD_IMAGE_URL__:null,
   ad_label:adImage?'广告推荐位':([...e.querySelectorAll('span,div')].find(n=>!n.children.length&&shown(n)&&text(n)==='广告')?'广告':null)};
 });
 const cards=slots.filter(s=>s.type==='product');
 return {...(__PAGE_METADATA__)(),root_count:roots.length,child_count:children.length,
  hidden_slot_count:children.length-slots.length,loaded_slot_count:slots.length,loaded_card_count:cards.length,slots,cards};
}""".replace("__PAGE_METADATA__", PAGE_METADATA).replace(
    "__AD_IMAGE_SELECTOR__", json.dumps(AD_RECOMMENDATION_IMAGE_SELECTOR)).replace(
    "__AD_IMAGE_URL__", json.dumps(AD_RECOMMENDATION_IMAGE_URL))


def validate_limit(value):
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError("采集数量限制须为正整数；不传 --limit 则不限制当前页条数")
    return value


def text_or_none(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def page_signature(snapshot, keyword, *, expected_page=1):
    """核对同一关键词和目标页；参数：快照、关键词、期望页码；返回：条件签名。"""
    if type(expected_page) is not int or expected_page < 1:
        raise ValueError("目标页码须为正整数")
    if snapshot.get("origin") != "https://s.taobao.com" or snapshot.get("path", "").rstrip("/") != "/search":
        raise ValueError("当前页面不是淘宝搜索结果页")
    if snapshot.get("query_keywords") != [keyword] or not snapshot.get("inputs") or any(value != keyword for value in snapshot["inputs"]):
        raise ValueError("当前搜索关键词发生变化")
    paging = snapshot.get("paging", {})
    explicit = []
    for key in ("page", "pageNo", "pageNum"):
        values = paging.get(key, [])
        if values not in ([], [str(expected_page)]):
            raise ValueError(f"当前结果不在目标第{expected_page}页或页码不明确")
        explicit.extend(values)
    offsets = paging.get("s", [])
    if offsets and (len(offsets) != 1 or not re.fullmatch(r"\d+", offsets[0])
                    or (expected_page == 1 and int(offsets[0]) != 0)
                    or (expected_page > 1 and int(offsets[0]) == 0)):
        raise ValueError("搜索结果偏移量与目标页码矛盾")
    numbers = snapshot.get("page_numbers", [])
    if any(value != str(expected_page) for value in numbers):
        raise ValueError(f"页面显示页码不是目标第{expected_page}页")
    if expected_page > 1 and not (numbers or explicit):
        raise ValueError("没有明确的目标页码证据，不能仅按偏移量推测页数")
    labels = snapshot.get("sort_labels", [])
    parameters = snapshot.get("sort_parameter", [])
    if len(labels) > 1 or len(parameters) > 1:
        raise ValueError("当前排序条件不明确")
    return {"keyword": keyword, "page_number": expected_page, "sort_label": labels[0] if labels else None,
            "sort_parameter": parameters[0] if parameters else None}


def parse_price(value):
    value = text_or_none(value)
    if not value or not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", value):
        return None
    try:
        parsed = Decimal(value.replace(",", ""))
        return format(parsed, "f") if parsed >= 0 else None
    except InvalidOperation:
        return None


def product_identity(card):
    """推广跳转 ID 只有与卡片或店铺关联 ID 一致时才采信。"""
    link = card.get("link") or {}
    dom_id = card.get("dom_item_id")
    if not link and isinstance(dom_id, str) and re.fullmatch(r"\d{5,25}", dom_id):
        return dom_id, "https://item.taobao.com/item.htm?" + urlencode({"id": dom_id}), None
    ids = link.get("ids", [])
    if link.get("scheme") not in {"http:", "https:"} or len(ids) != 1 or not isinstance(ids[0], str) or not re.fullmatch(r"\d{1,25}", ids[0]):
        return None, None, "未识别可确认的商品链接"
    item_id = ids[0]
    if dom_id and dom_id != item_id:
        return None, None, "商品DOM标识与入口链接ID不一致"
    if link.get("host") in {"item.taobao.com", "detail.tmall.com"} and link.get("path") == "/item.htm":
        host = link["host"]
        return item_id, "https://" + host + "/item.htm?" + urlencode({"id": item_id}), None
    if link.get("host") in {"click.simba.taobao.com", "mclick.simba.taobao.com"} and link.get("path") == "/cc_im":
        related = card.get("shop_top_ids", [])
        if item_id == card.get("dom_item_id") or related == [item_id]:
            return item_id, "https://item.taobao.com/item.htm?" + urlencode({"id": item_id}), None
        return None, None, "推广链接商品ID未能交叉确认"
    return None, None, "链接格式尚未适配"


def shop_url(card):
    link = card.get("shop_link") or {}
    app_uid = link.get("app_uid")
    if not isinstance(app_uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", app_uid):
        return None
    parameters = {"appUid": app_uid}
    ids = link.get("top_ids", [])
    if ids and all(isinstance(value, str) and re.fullmatch(r"\d{1,25}", value) for value in ids):
        parameters["topIds"] = ",".join(ids)
    return "https://store.taobao.com/category.htm?" + urlencode(parameters)


def parse_cards(snapshot, keyword, limit=None, *, page_number=1):
    """按格子分类；同 ID 的不同位置保留，未知或破损商品不静默丢弃。"""
    validate_limit(limit)
    signature = page_signature(snapshot, keyword, expected_page=page_number)
    if snapshot.get("root_count") != 1 and not (snapshot.get("root_count") == 0 and snapshot.get("empty_message_present") and not snapshot.get("slots")):
        raise ValueError("未能唯一确认 content_items_wrapper 商品列表容器")
    rows, skipped, issues, seen = [], [], [], set()
    products = surveys = suggestions = unknown = invalid = repeated = 0
    for card in snapshot.get("slots", []):
        position, kind = card["position"], card["type"]
        if kind == "search_experience_survey":
            surveys += 1
            skipped.append({"position": position, "type": kind, "reason": "已确认的搜索体验调查卡"})
            continue
        if kind == "search_suggestions":
            suggestions += 1
            skipped.append({"position": position, "type": kind, "reason": "已适配的大家都在搜推荐词卡"})
            continue
        if kind != "product":
            unknown += 1
            issues.append({"position": position, "type": "unknown", "reason": card.get("reason", "未识别的格子类型")})
            continue
        products += 1
        title, amount_text = text_or_none(card.get("title")), text_or_none(card.get("price_number_text"))
        item_id, item_url, identity_note = product_identity(card)
        if not title or not amount_text or not item_id:
            invalid += 1
            issues.append({"position": position, "type": "invalid_product", "reason": identity_note or "缺少商品ID、完整标题或展示价格"})
            continue
        if item_id in seen:
            repeated += 1
        seen.add(item_id)
        label, currency = text_or_none(card.get("price_label")), text_or_none(card.get("currency_symbol"))
        price = parse_price(amount_text)
        source_host = (card.get("link") or {}).get("host")
        link_status = ("canonicalized_from_list_link" if source_host in {"item.taobao.com", "detail.tmall.com"}
                       else "constructed_from_corroborated_id" if source_host else "constructed_from_dom_id")
        notes = []
        if link_status != "canonicalized_from_list_link":
            notes.append("根据已确认商品ID生成普通入口，未打开详情验证")
        if price is None:
            notes.append("展示价格保留原文，未转换为单一数值")
        ad = card.get("ad_recommendation") is True
        ad_label = "广告推荐位" if ad else card.get("ad_label")
        rank_text = text_or_none(card.get("rank_position_text"))
        rank_match = re.search(r"第(\d+)名", rank_text) if rank_text else None
        video = card.get("video_present") is True
        row = {"position": position, "slot_index": position, "product_index": len(rows) + 1,
               "product_id": item_id, "product_url": item_url, "identity_status": "verified", "link_status": link_status,
               "title": title, "title_source": card.get("title_source"), "main_image_url": text_or_none(card.get("main_image_url")),
               "price_text": (currency or "") + amount_text + (" " + label if label else ""),
               "price_value": price, "currency_symbol": currency, "price_label": label,
               "price_integer_text": text_or_none(card.get("price_integer_text")),
               "price_fraction_text": text_or_none(card.get("price_fraction_text")),
               "sales_text": text_or_none(card.get("sales_text")), "shop_name": text_or_none(card.get("shop_name")),
               "shop_url": shop_url(card), "location_text": text_or_none(card.get("location_text")),
               "rank_title": text_or_none(card.get("rank_title")), "rank_position_text": rank_text,
               "rank_position": int(rank_match[1]) if rank_match else None,
               "attribute_tags": list(dict.fromkeys(card.get("attribute_tags", []))),
               "service_tags": list(dict.fromkeys(card.get("service_tags", []))),
               "video_present": video, "video_url": text_or_none(card.get("video_url")),
               "video_icon_url": text_or_none(card.get("video_icon_url")),
               "title_icon_url": text_or_none(card.get("title_icon_url")),
               "customer_service_icon_url": text_or_none(card.get("customer_service_icon_url")),
               "sku_reference": text_or_none(card.get("sku_reference")),
               "ad_recommendation": ad, "ad_label": ad_label, "ad_status": "identified" if ad_label else "not_identified",
               "ad_basis": "specified_image_url" if ad else None, "ad_marker_url": AD_RECOMMENDATION_IMAGE_URL if ad else None,
               "notes": notes}
        fields = ("main_image_url", "price_value", "currency_symbol", "price_label", "sales_text", "shop_name", "shop_url",
                  "location_text", "rank_title", "rank_position", "attribute_tags", "service_tags", "video_url",
                  "title_icon_url", "customer_service_icon_url", "video_icon_url", "sku_reference")
        states = {key: "provided" if row[key] is not None and row[key] != [] else "not_provided" for key in fields}
        for key, present in card.get("field_elements", {}).items():
            if present and states.get(key) == "not_provided":
                states[key] = "not_identified"
        if price is None:
            states["price_value"] = "not_identified"
        if card.get("shop_href_present") and row["shop_url"] is None:
            states["shop_url"] = "not_identified"
        if card.get("main_image_count", 0) > 1:
            states["main_image_url"] = "not_identified"
        if rank_text and row["rank_position"] is None:
            states["rank_position"] = "not_provided" if rank_text in {"榜", "榜单"} else "not_identified"
        if not video:
            states["video_url"] = "not_applicable"
            states["video_icon_url"] = "not_applicable"
        elif row["video_url"] is None and card.get("video_source_present"):
            states["video_url"] = "not_identified"
        row["field_states"] = states
        row["missing_fields"] = [key for key, state in states.items() if state in {"not_provided", "not_identified"}]
        rows.append(row)
    selected = rows[:limit]
    limit_reached = limit is not None and len(selected) == limit
    boundary = selected[-1]["position"] if limit_reached else float("inf")
    blocking = [issue for issue in issues if issue["position"] <= boundary]
    loaded = snapshot.get("loaded_slot_count", len(snapshot.get("slots", [])))
    balanced = loaded == products + surveys + suggestions + unknown and loaded == len(snapshot.get("slots", []))
    if not balanced:
        raise ValueError("列表格子分类数量核对失败")
    counts = {"list_child_count": snapshot.get("child_count", loaded), "loaded_slot_count": loaded,
              "hidden_slot_count": snapshot.get("hidden_slot_count", 0), "product_slot_count": products,
              "valid_product_count": len(rows), "survey_count": surveys, "search_suggestions_count": suggestions, "unknown_count": unknown,
              "invalid_product_count": invalid, "same_id_extra_positions": repeated,
              "exported_count": len(selected), "exported_ad_count": sum(row["ad_recommendation"] for row in selected),
              "counts_balanced": balanced}
    return {"signature": signature, "items": selected, "skipped": skipped, "issues": issues,
            "blocking_issues": blocking, "counts": counts, "duplicates": 0,
            "loaded_card_count": products, "limit": limit, "limit_reached": limit_reached,
            "field_gap_count": sum(bool(row["missing_fields"]) for row in selected)}
