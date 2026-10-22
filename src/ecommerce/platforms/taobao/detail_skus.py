"""从可见商品规格 DOM 枚举组合；单次选择后核对价格和对应图片。"""
import itertools
import json
import math
import re
import time
from decimal import Decimal
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.storage.database import now
from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.detail_images import image_asset_key
from ecommerce.platforms.taobao.verification import active_guard

OUT_OF_STOCK_PRICE = "当前缺货，无法识别价格"
SKU_ROOT = "#skuOptionsArea"
GROUP_SELECTOR = SKU_ROOT + ' [class*="skuItem--"]:visible'
OPTION_SELECTOR = '[class*="valueItem--"]:visible'

SKU_SNAPSHOT = r"""() => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'};
 const parts=(e,p)=>[...e.querySelectorAll('[class*="'+p+'--"]')].filter(n=>[...n.classList].some(c=>c.startsWith(p+'--'))&&visible(n));
 const text=e=>(e?.innerText||'').trim();
 const asset=raw=>{if(!raw)return null;try{return new URL(raw,location.href).href}catch{return null}};
 const roots=[...document.querySelectorAll('#skuOptionsArea')].filter(visible);
 const titles=parts(document,'ItemTitle'),mainTitles=[...new Set(titles.flatMap(e=>parts(e,'mainTitle').map(text)).filter(Boolean))];
 const header=titles.length===1?text(titles[0]):'';
 const prices=parts(document,'highlightPrice').map(e=>({display:text(e).replace(/\s+/g,' '),
   label:parts(e,'title').map(text).join(' / '),amount_text:parts(e,'text').map(text).join(''),
   symbol:parts(e,'symbol').map(text).join('')}));
 const uniquePrices=prices.filter((p,i,a)=>a.findIndex(n=>JSON.stringify(n)===JSON.stringify(p))===i);
 const quantity=[...document.querySelectorAll('input[class*="countValue--"]')].filter(visible).map(e=>e.value);
 const loading=[...document.querySelectorAll('[aria-busy="true"],[class*="loading--"],[class*="Loading--"]')].some(visible);
 const root=roots.length===1?roots[0]:null;
 const groups=root?parts(root,'skuItem').map((group,gi)=>({index:gi,name:parts(group,'ItemLabel').map(text).join(' / '),
  options:parts(group,'valueItem').map((e,oi)=>{
   const label=parts(e,'valueItemText'),name=label.length===1?(label[0].getAttribute('title')||text(label[0])):text(e);
   const image=parts(e,'valueItemImg').filter(n=>n.tagName==='IMG')[0];
   const attrs=[e.getAttribute('title'),e.getAttribute('aria-label'),text(e)].filter(Boolean).join(' ');
   const stock=e.getAttribute('data-stock')??e.getAttribute('data-quantity');
   const noStock=/缺货|无货|售罄|库存不足|暂无库存/.test(attrs)||stock==='0';
   const disabled=e.hasAttribute('disabled')||e.getAttribute('aria-disabled')==='true'||e.getAttribute('data-disabled')==='true'
     ||[...e.classList].some(c=>/^(?:isDisabled|disabled|soldOut|outOfStock)--/i.test(c));
   const disabledEvidence=[e.hasAttribute('disabled')?'disabled属性':null,
     e.getAttribute('aria-disabled')==='true'?'aria-disabled=true':null,
     e.getAttribute('data-disabled')==='true'?'data-disabled=true':null,
     ...[...e.classList].filter(c=>/^(?:isDisabled|disabled|soldOut|outOfStock)--/i.test(c)).map(c=>'class='+c)].filter(Boolean);
   return {index:oi,name,value_id:e.getAttribute('data-vid'),sku_id:e.getAttribute('data-sku-id')||e.getAttribute('data-skuid'),
    selected:[...e.classList].some(c=>c.startsWith('isSelected--'))||e.getAttribute('aria-checked')==='true',
    disabled,disabled_evidence:disabledEvidence,no_stock:noStock,stock_evidence:noStock?(stock==='0'?'页面库存属性为0':attrs):null,
    image_url:asset(image?.currentSrc||image?.getAttribute('src')),
    large_image_url:asset(e.getAttribute('data-large-image')||image?.getAttribute('data-large-image'))};
  })})):[];
 const main=document.querySelector('#picGalleryEle #mainPicImageEl');
 const currentImage=main&&visible(main)&&main.complete&&main.naturalWidth>0?
   {url:asset(main.currentSrc||main.getAttribute('src')),width:main.naturalWidth,height:main.naturalHeight}:null;
 const lines=header.split('\n').map(s=>s.trim()).filter(Boolean);
 const sales=lines.filter(s=>/^已售\s*/.test(s));
 const globalStock=root?text(root).split('\n').filter(s=>/^(?:该规格|当前规格|所选规格|当前商品|商品)?(?:已)?(?:缺货|无货|售罄|暂无库存)[！!。\s]*$/.test(s.trim())).join(' '):'';
 return {ready:roots.length===1&&mainTitles.length===1&&groups.length>0&&groups.every(g=>g.name&&g.options.length&&g.options.every(o=>o.name)),
  title:mainTitles.length===1?mainTitles[0]:null,sales_text:sales.length===1?sales[0]:null,
  evaluation_hint:lines.find(s=>/人评价/.test(s))||null,returning_customer_hint:lines.find(s=>/回头客/.test(s))||null,
  cart_hint:lines.find(s=>/加购/.test(s))||null,groups,prices:uniquePrices,quantity,loading,current_image:currentImage,global_stock:globalStock};
}"""


def parse_sales(text):
    result = {"sales_text": text, "sales_value": None, "sales_lower_bound": None, "sales_relation": "not_displayed"}
    match = re.fullmatch(r"已售\s*([0-9]+(?:\.[0-9]+)?)\s*([万亿]?)\s*(\+)?\s*(?:件)?", text or "")
    if not match:
        return result
    value = Decimal(match[1]) * {"": 1, "万": 10000, "亿": 100000000}[match[2]]
    if match[3]:
        result.update(sales_lower_bound=int(value), sales_relation="at_least")
    elif match[2] or "." in match[1]:
        result.update(sales_relation="rounded_display")
    else:
        result.update(sales_value=int(value), sales_relation="exact_display")
    return result


def parse_prices(prices):
    result = []
    for price in prices:
        amount = price.get("amount_text", "").replace(",", "").strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount):
            result.append({**price, "amount": str(Decimal(amount)),
                           "currency": "CNY" if price.get("symbol") in {"￥", "¥"} else None})
    return result


def signature(value):
    return [(g["name"], [(o["name"], o["value_id"]) for o in g["options"]]) for g in value["groups"]]


class SkuReader:
    def __init__(self, result, timeout_ms, *, settle_ms=1200, on_progress=None):
        self.page, self.item_id = result.page, result.target.item_id
        self.timeout_ms, self.settle_ms = timeout_ms, settle_ms
        self.guard = active_guard()
        if self.guard is None:
            raise RuntimeError("SKU 采集必须在淘宝专用守卫中运行")
        self.initial_signature = None
        self.pending = set()
        self.network_failure = False
        self.on_progress = on_progress

    def request(self, request):
        parsed = urlsplit(request.url)
        host = parsed.hostname or ""
        if (request.resource_type in {"xhr", "fetch"}
                and any(host == d or host.endswith('.' + d) for d in ("taobao.com", "tmall.com", "alibaba.com"))
                and re.search(r"mtop|/api/|sku", parsed.path, re.I)):
            self.pending.add(request)

    def finished(self, request):
        if request in self.pending:
            response = request.response()
            if response is None or response.status >= 400:
                self.network_failure = True
        self.pending.discard(request)

    def failed(self, request):
        if request in self.pending:
            self.network_failure = True
        self.pending.discard(request)

    def read(self):
        if not matches_detail_url(self.page, self.item_id):
            raise ValueError("SKU 目标商品发生变化")
        value = self.page.evaluate(SKU_SNAPSHOT)
        if self.initial_signature is not None and signature(value) != self.initial_signature:
            raise ValueError("规格组、选项数量或顺序发生变化")
        return value

    def safe(self):
        try:
            return self.read().get('ready', False)
        except (ValueError, PlaywrightError):
            return False

    def wait(self, predicate, reason, *, stable_ms=0, minimum_ms=0):
        deadline = time.monotonic() + self.timeout_ms / 1000
        started, stable_since, last = time.monotonic(), None, None
        while True:
            paused = self.guard.paused_seconds
            self.guard.checkpoint("核对 SKU 及价格")
            delta = self.guard.paused_seconds - paused
            deadline += delta
            started += delta
            if delta:
                stable_since, last = None, None
            try:
                value = self.read()
                key = json.dumps({"groups": value['groups'], "prices": value['prices'], "quantity": value['quantity'],
                                  "loading": value['loading'], "stock": value['global_stock']}, ensure_ascii=False, sort_keys=True)
                valid = value.get('ready') and predicate(value)
                if valid and key == last:
                    stable_since = stable_since or time.monotonic()
                elif valid:
                    stable_since, last = time.monotonic(), key
                else:
                    stable_since, last = None, None
                if (valid and stable_since is not None and (time.monotonic()-stable_since)*1000 >= stable_ms
                        and (time.monotonic()-started)*1000 >= minimum_ms):
                    return value
            except (ValueError, PlaywrightError):
                stable_since, last = None, None
            if time.monotonic() >= deadline:
                self.guard.request_attention(self.page, reason + "；请人工核对后回复继续，不会自动重放点击", step="复核 SKU 采集")
                self.network_failure = False
                deadline = time.monotonic() + self.timeout_ms / 1000
                started, stable_since, last = time.monotonic(), None, None
            paused = self.guard.paused_seconds
            self.guard.wait(100)
            delta = self.guard.paused_seconds - paused
            deadline += delta
            started += delta
            if delta:
                stable_since, last = None, None

    @staticmethod
    def selected(value, combination):
        return all([o['index'] for o in g['options'] if o['selected']] == ([] if i is None else [i])
                   for g, i in zip(value['groups'], combination))

    def click(self, group_index, option_index, expected):
        self.guard.resume_check = self.safe
        self.guard.pause_between('page')
        current = self.wait(lambda v: not v['loading'], "规格区域未就绪")
        option = current['groups'][group_index]['options'][option_index]
        if option['disabled']:
            return False

        def complete():
            try:
                return self.selected(self.read(), expected)
            except (ValueError, PlaywrightError):
                return False

        self.guard.perform("选择 SKU 规格：" + option['name'],
                           lambda: self.page.locator(GROUP_SELECTOR).nth(group_index).locator(OPTION_SELECTOR).nth(option_index).click(
                               timeout=min(self.timeout_ms, 5000)), completed=complete)
        self.wait(lambda v: self.selected(v, expected) and not v['loading'], "规格选中状态与目标不一致")
        return True

    def collect(self):
        self.guard.resume_check = self.safe
        self.guard.pause_between('page')
        initial = self.wait(lambda v: not v['loading'], "尚未识别商品标题或规格区域")
        self.initial_signature = signature(initial)
        if len(set(g['name'] for g in initial['groups'])) != len(initial['groups']):
            raise ValueError("规格组名称重复，无法可靠生成完整组合")
        for group in initial['groups']:
            keys = [(o['name'], o['value_id']) for o in group['options']]
            if len(keys) != len(set(keys)):
                raise ValueError("同一规格组存在无法区分的重复选项")
        if len(initial['quantity']) != 1 or initial['quantity'][0] != '1':
            self.guard.request_attention(self.page, "请将商品购买数量手动设置为 1 后回复继续", step="核对采集价格数量")
        self.wait(lambda v: v['quantity'] == ['1'], "购买数量尚未确认为 1")
        product = {"title": initial['title'], **parse_sales(initial['sales_text']),
                   "evaluation_hint": initial['evaluation_hint'], "returning_customer_hint": initial['returning_customer_hint'],
                   "cart_hint": initial['cart_hint'], "captured_at": now()}
        records = []
        ranges = [range(len(g['options'])) for g in initial['groups']]
        inventory = [{"name": g['name'], "option_count": len(g['options']),
                      "options": [{"name": o['name'], "value_id": o['value_id']} for o in g['options']]} for g in initial['groups']]
        for indexes in itertools.product(*ranges):
            self.guard.checkpoint("采集下一个 SKU")
            self.network_failure = False
            properties = [{"group": g['name'], "name": g['options'][i]['name'], "value_id": g['options'][i]['value_id']}
                          for g, i in zip(initial['groups'], indexes)]
            name = properties[0]['name'] if len(properties) == 1 else '_'.join(p['group'] + '-' + p['name'] for p in properties)
            record = {"index": len(records)+1, "name": name, "properties": properties, "sku_id": None,
                      "stock_status": "unknown", "stock_evidence": None, "price": None, "price_value": None,
                      "price_label": None, "currency": None, "prices": [], "price_status": "not_read", "price_reason": None,
                      "unavailability_evidence": [], "sku_identity_status": "not_provided",
                      "quantity": 1, "image_url": None, "image_source": None, "image_status": "not_read"}
            current = self.wait(lambda v: not v['loading'], "规格区域仍在加载")
            # 多组规格先清空旧组合，避免上一个尺寸使目标颜色暂时不可选。
            if len(indexes) > 1 and not self.selected(current, indexes):
                for gi in reversed(range(len(indexes))):
                    current = self.read()
                    selected = [o for o in current['groups'][gi]['options'] if o['selected']]
                    if len(selected) > 1:
                        raise ValueError("同一规格组出现多个选中项")
                    if selected:
                        expected = [next((o['index'] for o in g['options'] if o['selected']), None) for g in current['groups']]
                        expected[gi] = None
                        if not self.click(gi, selected[0]['index'], expected):
                            raise ValueError("已选中的规格无法解除，停止组合枚举")
            blocked = None
            for gi, oi in enumerate(indexes):
                current = self.wait(lambda v: not v['loading'], "等待规格联动更新")
                option = current['groups'][gi]['options'][oi]
                if option['disabled'] or option['no_stock']:
                    blocked = option
                    break
                if not option['selected']:
                    expected = [next((o['index'] for o in g['options'] if o['selected']), None) for g in current['groups']]
                    expected[gi] = oi
                    if not self.click(gi, oi, expected):
                        blocked = self.read()['groups'][gi]['options'][oi]
                        break
            if blocked:
                record.update(stock_status='out_of_stock' if blocked['no_stock'] else 'unavailable_unconfirmed',
                              stock_evidence=blocked['stock_evidence'], price_status='out_of_stock' if blocked['no_stock'] else 'unavailable_unconfirmed',
                              unavailability_evidence=blocked.get('disabled_evidence',[]),
                              price_reason='页面已明确显示缺货' if blocked['no_stock'] else '规格不可选，页面未提供明确缺货依据，无法识别价格')
                if blocked['no_stock']:
                    record['price'] = OUT_OF_STOCK_PRICE
                current = self.read()
            else:
                self.guard.resume_check = lambda: self.safe() and self.selected(self.read(), indexes)
                current = self.wait(lambda v: self.selected(v, indexes) and v['quantity'] == ['1'] and not v['loading']
                                    and not self.pending and not self.network_failure
                                    and (v['global_stock'] or parse_prices(v['prices'])),
                                    "选中 SKU 的价格、数量或请求完成状态尚未确认", stable_ms=350, minimum_ms=self.settle_ms)
                if current['global_stock']:
                    record.update(stock_status='out_of_stock', stock_evidence=current['global_stock'],
                                  price=OUT_OF_STOCK_PRICE, price_status='out_of_stock',price_reason='页面已明确显示缺货')
                else:
                    prices = parse_prices(current['prices'])
                    primary = prices[0]
                    record.update(stock_status='selectable', price=primary['symbol'] + primary['amount_text'], price_value=primary['amount'],
                                  price_label=primary['label'], currency=primary['currency'], prices=prices, price_status='collected')
            options = [g['options'][i] for g, i in zip(current['groups'], indexes)]
            record['combination_confirmed'] = not blocked or len(indexes) == 1 or gi == len(indexes)-1
            explicit_ids = {o['sku_id'] for o in options if re.fullmatch(r'[1-9][0-9]*',o['sku_id'] or '')}
            if len(explicit_ids) == 1 and record['combination_confirmed']:
                record['sku_id'] = next(iter(explicit_ids))
                record['sku_identity_status'] = 'explicit_dom_id'
            option_images = [o for o in options if o['image_url']]
            if option_images:
                chosen = option_images[-1]
                record.update(image_url=chosen['large_image_url'] or chosen['image_url'], image_source='sku_option', image_status='pending')
                if chosen['large_image_url']:
                    record['image_source'] = 'explicit_large_image'
                main = current['current_image']
                if not blocked and main and image_asset_key(main['url']) == image_asset_key(chosen['image_url']):
                    record.update(image_url=main['url'], image_source='matched_main_image')
                record['option_image_url'] = chosen['image_url']
            else:
                record['image_status'] = 'no_image'
            record['captured_at'] = now()
            record['price_checked_at'] = record['captured_at']
            records.append(record)
            self.guard.resume_check = self.safe
            if self.on_progress:
                self.guard.perform('保存已识别的 SKU 进度', lambda: self.on_progress({
                    'product_info': product, 'groups': inventory, 'skus': records,
                    'candidate_count': math.prod(len(r) for r in ranges), 'processed_count': len(records)}))
        return {"product_info": product, "groups": inventory,
                "skus": records, "candidate_count": len(records)}


def collect_sku_data(result, timeout_ms, *, settle_ms=1200, on_progress=None):
    reader = SkuReader(result, timeout_ms, settle_ms=settle_ms, on_progress=on_progress)
    page = result.page
    callbacks = [('request', reader.request), ('requestfinished', reader.finished), ('requestfailed', reader.failed)]
    for event, callback in callbacks:
        page.on(event, callback)
    try:
        return reader.collect()
    finally:
        for event, callback in callbacks:
            page.remove_listener(event, callback)
