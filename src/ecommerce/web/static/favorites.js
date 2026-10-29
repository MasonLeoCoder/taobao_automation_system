/* 长期关注公共交互：数据库成功后更新状态，旧临时关注不导入。 */
(() => {
  'use strict';
  const node = document.getElementById('sa-data') || document.getElementById('favorite-data');
  if (!node) return;
  const data = JSON.parse(node.textContent);
  const states = new Map(Object.entries(data.favorite_states || {}));
  const rows = new Map([...(data.points || []), ...(data.details || [])].map(r => [r.key, r]));
  const busy = new Set();
  const notice = document.createElement('div');
  notice.className = 'favorite-notice'; notice.setAttribute('role', 'status'); notice.hidden = true;
  document.body.append(notice);
  let pendingUndo = 0;
  let reloadNeeded = false;
  const positionKey = 'workbench.favorite.scroll';
  try {
    const previous = JSON.parse(sessionStorage.getItem(positionKey) || 'null');
    if (previous && previous.url === location.href) { requestAnimationFrame(() => scrollTo(0, previous.y)); sessionStorage.removeItem(positionKey); }
  } catch { /* 无存储时操作仍可用。 */ }
  function reload() {
    try { sessionStorage.setItem(positionKey, JSON.stringify({url:location.href,y:scrollY})); } catch { /* 不影响刷新。 */ }
    location.reload();
  }
  function message(text, undo) {
    const entry = document.createElement('div'); entry.className = 'favorite-notice-entry';
    entry.append(document.createTextNode(text)); notice.hidden = false;
    if (!undo) { for (const old of notice.querySelectorAll('[data-regular]')) old.remove(); entry.dataset.regular = 'true'; }
    notice.append(entry);
    if (undo) pendingUndo++;
    function finish(expired) {
      entry.remove(); if (undo) pendingUndo--;
      notice.hidden = !notice.children.length;
      if (expired && undo && (data.filters?.view === 'saved' || data.favorite_page)) reloadNeeded = true;
      if (reloadNeeded && pendingUndo === 0) reload();
    }
    const timer = setTimeout(() => finish(true), undo ? 10000 : 4000);
    if (undo) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'ui-button'; button.textContent = '撤销';
      button.addEventListener('click', async () => { clearTimeout(timer); button.disabled = true; await undo(); finish(false); }, {once:true}); entry.append(button);
    }
  }
  async function send(values) {
    const response = await fetch('/api/favorites', {method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':document.querySelector('meta[name="workbench-token"]').content},body:JSON.stringify(values)});
    const result = await response.json().catch(() => ({error:'页面已失效或服务不可用，请刷新后重试'}));
    if (!response.ok) throw Error(result.error || '关注操作失败，请重试');
    return result;
  }
  function update() {
    for (const button of document.querySelectorAll('[data-save-key]')) {
      const row = rows.get(button.dataset.saveKey);
      const product = row?.product_id;
      const active = states.has(String(product));
      button.disabled = !product || !row.platform_product_id || busy.has(String(product));
      button.title = !product || !row.platform_product_id ? '商品身份未确认，暂不能长期关注' : '';
      button.textContent = button.classList.contains('sa-drawer-save') ? active ? '★ 已关注 · 点击取消' : '☆ 加入我的关注' : active ? '★' : '☆';
      button.setAttribute('aria-pressed', String(active)); button.setAttribute('aria-label', active ? '取消关注此商品' : '关注此商品');
    }
    const count = document.getElementById('sa-saved-count'); if (count) count.textContent = states.size;
  }
  async function toggle(button) {
    const row = rows.get(button.dataset.saveKey); if (!row?.product_id) return;
    const product = String(row.product_id); if (busy.has(product)) return;
    busy.add(product); update();
    try {
      if (states.has(product)) {
        const result = await send({action:'remove',product_id:row.product_id,favorite_id:states.get(product)});
        states.delete(product);
        const parent = data.filters?.view === 'saved' ? button.closest('tr') : null;
        const host = parent?.parentElement, next = parent?.nextSibling;
        if (parent) parent.remove();
        message('已取消关注', async () => {
          try { const restored = await send({action:'undo',product_id:row.product_id,undo:result.undo}); states.set(product,restored.favorite_id); if (parent) host.insertBefore(parent,next?.parentNode === host ? next : null); update(); message('已恢复原关注和备注'); }
          catch (error) { message(error.message); }
        });
      } else {
        const result = await send({action:'add',product_id:row.product_id,observation_id:row.id}); states.set(product,result.favorite_id); message('已加入长期关注');
      }
    } catch (error) { message(error.message); }
    finally { busy.delete(product); update(); }
  }
  document.addEventListener('click', event => { const button = event.target.closest('[data-save-key]'); if (button) toggle(button); });
  document.addEventListener('favorites:render', update);
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-favorite-note]'); if (!form) return;
    event.preventDefault(); const button = form.querySelector('button[type="submit"]'); button.disabled = true;
    try { await send({action:'note',product_id:Number(form.dataset.product),favorite_id:form.dataset.favoriteNote,note:form.elements.note.value}); message('备注已保存'); }
    catch (error) { message(error.message); }
    finally { button.disabled = false; }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-remove-favorite]'); if (!button || button.disabled) return;
    button.disabled = true;
    try {
      const result = await send({action:'remove',product_id:Number(button.dataset.product),favorite_id:button.dataset.removeFavorite});
      const card = button.closest('.favorite-card'); card.hidden = true;
      message('已取消关注，10秒内可撤销', async () => {
        try { await send({action:'undo',product_id:Number(button.dataset.product),undo:result.undo}); card.hidden = false; button.disabled = false; message('已恢复原关注时间、来源和备注'); }
        catch (error) { message(error.message); }
      });
    } catch (error) { button.disabled = false; message(error.message); }
  });
  update();
})();
