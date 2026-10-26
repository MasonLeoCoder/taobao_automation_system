"""只读详情页基本信息；不点击、枚举或下载任何SKU内容。"""
from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention

BASIC_INFO_SNAPSHOT=r"""() => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'};
 const parts=(root,p)=>[...root.querySelectorAll('[class*="'+p+'--"]')].filter(n=>[...n.classList].some(c=>c.startsWith(p+'--'))&&visible(n));
 const text=e=>(e?.innerText||'').trim();
 const unique=a=>[...new Set(a.filter(Boolean))];
 const titles=unique(parts(document,'ItemTitle').flatMap(e=>parts(e,'mainTitle').map(text)));
 const headers=parts(document,'ItemTitle').map(text);
 const sales=unique(headers.flatMap(t=>t.split('\n').map(s=>s.trim()).filter(s=>/^已售\s*/.test(s))));
 const shops=unique([...document.querySelectorAll('[class*="shopName--"],[class*="ShopName--"],[class*="shopNameText--"]')].filter(visible).map(text));
 const prices=unique(parts(document,'highlightPrice').map(e=>text(e).replace(/\s+/g,' ')));
 const candidates={title:titles,shop_name:shops,sales_text:sales,price_text:prices};
 const out={field_states:{},evidence:{selector:'ItemTitle/mainTitle, shopName, highlightPrice',candidates}};
 for(const [key,values] of Object.entries(candidates)){
   out[key]=values.length===1?values[0]:null;
   out.field_states[key]=values.length===1?'provided':values.length?'ambiguous':'not_displayed';
 }
 return out;
}"""


def read_basic_info(result):
    """读取当前商品可见字段；参数：已核对详情页；返回：原文、字段状态及选择器依据。"""
    guard=active_guard()
    if guard is None:
        raise RuntimeError('基本信息采集必须在淘宝守卫中运行')
    guard.checkpoint('读取详情基本信息')
    if not matches_detail_url(result.page,result.target.item_id):
        raise NeedsAttention('基本信息采集前目标商品发生变化')
    value=result.page.evaluate(BASIC_INFO_SNAPSHOT)
    guard.checkpoint('复核详情基本信息')
    if not matches_detail_url(result.page,result.target.item_id):
        raise NeedsAttention('读取基本信息后目标商品发生变化')
    return value
