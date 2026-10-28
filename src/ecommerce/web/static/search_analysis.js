/* 搜索词分析交互：图形与侧栏只展示业务层结果，统计和筛选由服务统一执行。 */
(() => {
  'use strict';
  const dataNode = document.getElementById('sa-data');
  if (!dataNode) return;
  const data = JSON.parse(dataNode.textContent);
  const byId = new Map([...data.points, ...data.details].map(row => [String(row.id), row]));
  const $ = id => document.getElementById(id);
  const ns = 'http://www.w3.org/2000/svg';
  const savedStorageKey = 'ecommerce.search-analysis.saved.v1';
  let saved = new Set();
  let storedSaved = false;
  try {
    const stored = sessionStorage.getItem(savedStorageKey);
    const previous = JSON.parse(stored || '[]');
    if (Array.isArray(previous)) {
      saved = new Set(previous.filter(key => typeof key === 'string' && /^[po]:[1-9][0-9]{0,19}$/.test(key)).slice(0, 200));
      storedSaved = stored !== null;
    }
  } catch { /* 浏览器禁用存储时，关注仍可在本次页面中使用。 */ }
  if (!storedSaved) for (const key of data.filters.saved.slice(0, 200)) saved.add(key);
  if (data.filters.view === 'saved' && [...saved].sort().join(',') !== [...data.filters.saved].sort().join(',')) {
    const current = new URL(location.href); current.searchParams.set('saved', [...saved].join(',')); current.searchParams.set('page', '1'); location.replace(current.href); return;
  }
  let lastFocus = null;
  let toastTimer;

  function svgElement(name, attributes = {}, text = null) {
    const element = document.createElementNS(ns, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text !== null) element.textContent = text;
    return element;
  }
  function money(value) { return value === null || value === undefined ? '—' : '¥' + Number(value).toFixed(2); }
  function quantity(value) { return value === null || value === undefined ? '—' : String(value); }
  function toast(message) {
    $('sa-status').textContent = message; $('sa-status').hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('sa-status').hidden = true; }, 2600);
  }
  function updateSaved() {
    $('sa-saved-count').textContent = saved.size;
    for (const button of document.querySelectorAll('[data-save-key]')) {
      const active = saved.has(button.dataset.saveKey);
      button.textContent = button.classList.contains('sa-drawer-save') ? active ? '★ 已关注 · 点击取消' : '☆ 加入我的关注' : active ? '★' : '☆';
      button.setAttribute('aria-pressed', String(active));
      button.setAttribute('aria-label', active ? '取消关注此商品' : '关注此商品');
    }
    for (const input of document.querySelectorAll('input[name="saved"]')) input.value = [...saved].join(',');
    for (const link of [$('sa-saved-link'), ...(data.filters.view === 'saved' ? [$('sa-export')] : [])]) {
      const url = new URL(link.href); url.searchParams.set('saved', [...saved].join(',')); url.searchParams.set('page', '1'); link.href = url.href;
    }
  }
  function save(key) {
    if (saved.has(key)) saved.delete(key);
    else {
      if (saved.size >= 200) { toast('当前标签页最多关注200项，请先移除部分商品。'); return; }
      saved.add(key);
    }
    try { sessionStorage.setItem(savedStorageKey, JSON.stringify([...saved])); } catch { toast('浏览器未允许保存关注，刷新后可能丢失。'); }
    updateSaved();
    if (data.filters.view === 'saved') {
      const url = new URL(location.href); url.searchParams.set('saved', [...saved].join(',')); url.searchParams.set('page', '1'); location.assign(url.href);
    }
  }
  function append(parent, tag, text, className = '') {
    const element = document.createElement(tag); element.textContent = text; if (className) element.className = className; parent.append(element); return element;
  }
  function openDialog(dialog, origin) { lastFocus = origin || document.activeElement; dialog.showModal(); }
  function detail(id, origin) {
    const row = byId.get(String(id)); if (!row) return;
    const container = $('sa-detail-content'); container.replaceChildren();
    const body = append(container, 'div', '', 'sa-drawer-content');
    append(body, 'span', row.category + ' · 标题初分', 'sa-type-tag'); append(body, 'h2', row.title || '标题未记录').id = 'sa-detail-title';
    const stats = append(body, 'div', '', 'sa-drawer-grid');
    for (const [label, value, note] of [['入口价格', money(row.price), row.price_label || '价格条件未记录'], ['展示数量', quantity(row.sales_count), row.sales_group_label + ' · ' + row.precision_label]]) {
      const stat = append(stats, 'div', '', 'sa-drawer-stat'); append(stat, 'span', label); append(stat, 'strong', value); append(stat, 'small', note);
    }
    const fields = append(body, 'dl', '');
    const adLabel = row.ad_status === 'identified' ? '已识别广告' : row.ad_status === 'not_identified' ? '未命中指定标识，不等于非广告' : '广告未确认或未记录';
    const entries = [['平台商品编号', row.platform_product_id || '身份未确认'], ['数据库商品身份', row.product_id || '未关联'], ['观察编号', '#' + row.id],
      ['采集时间', row.captured_at ? row.captured_at.replace('T', ' ') : '未记录'], ['来源关键词', data.filters.keyword], ['任务编号', row.task_id || '任务未记录'],
      ['批次编号', '#' + row.batch_id], ['排序与位置', (row.sort_label || '排序未记录') + ' · 第' + (row.page_number ?? '未知') + '页 · 位置' + (row.slot_index ?? '未知')],
      ['价格原文', row.price_text || '未记录'], ['数量原文', row.sales_text || '未提供'], ['店铺', row.shop_name || '未记录'], ['店铺分组依据', row.shop_basis],
      ['地区', row.location_text || '未记录'], ['广告识别', adLabel], ['范围内出现次数', row.occurrences + ' 个位置（不等于曝光量）']];
    for (const [label, value] of entries) { append(fields, 'dt', label); append(fields, 'dd', String(value)); }
    append(body, 'p', '入口价可能对应配件、最小规格或附带优惠条件；“+”是数量下界，近似值单独标记。此处所有字段来自同一条列表观察。', 'sa-drawer-note');
    const links = append(body, 'div', '', 'sa-drawer-links');
    if (row.platform_product_id) {
      const url = new URL('/collections/products/' + encodeURIComponent(row.platform_product_id), location.origin); url.searchParams.set('observation', String(row.id)); url.searchParams.set('return_to', location.pathname + location.search);
      const link = append(links, 'a', '查看详情资料 ↗', 'sa-button'); link.href = url.href;
    }
    const source = append(links, 'a', '查看来源批次 #' + row.batch_id, 'sa-button');
    const url = new URL('/collections/batches/' + row.batch_id, location.origin); url.searchParams.set('return_to', location.pathname + location.search); source.href = url.href;
    const saveButton = append(body, 'button', '', 'sa-button sa-primary sa-drawer-save'); saveButton.type = 'button'; saveButton.dataset.saveKey = row.key;
    updateSaved(); $('sa-tooltip').hidden = true; openDialog($('sa-detail-dialog'), origin);
  }
  function renderScatter() {
    const width = 560, height = 225, left = 61, right = 20, top = 18, bottom = 35;
    const rows = data.points;
    const maxPrice = Math.max(10, ...rows.map(row => Number(row.price)));
    const xMax = Math.ceil(maxPrice / (maxPrice > 100 ? Math.pow(10, Math.floor(Math.log10(maxPrice)) - 1) : 10)) * (maxPrice > 100 ? Math.pow(10, Math.floor(Math.log10(maxPrice)) - 1) : 10);
    const maxQuantity = Math.max(1, ...rows.map(row => Number(row.sales_count)));
    const yPower = Math.max(1, Math.ceil(Math.log10(maxQuantity + 1)));
    const x = value => left + Number(value) / xMax * (width - left - right);
    const y = value => height - bottom - Math.log10(Number(value) + 1) / Math.log10(Math.pow(10, yPower) + 1) * (height - top - bottom);
    const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': '同口径的入口价与展示数量散点图，保留零值'});
    const ticks = [0, ...Array.from({length: Math.min(yPower, 20)}, (_, index) => Math.pow(10, index + 1))];
    for (const value of ticks) {
      const yy = y(value); svg.append(svgElement('line', {x1:left,x2:width-right,y1:yy,y2:yy,stroke:'#eaf0e0','stroke-dasharray':'3 4'}));
      svg.append(svgElement('text', {x:left-9,y:yy+3,'text-anchor':'end'}, value >= 10000 ? Number(value / 10000).toLocaleString('zh-CN') + '万' : String(value)));
    }
    for (let index = 0; index <= 4; index++) {
      const value = xMax * index / 4; svg.append(svgElement('text', {x:x(value),y:height-14,'text-anchor':'middle'}, '¥' + Number(value.toFixed(2)).toLocaleString('zh-CN')));
    }
    svg.append(svgElement('text', {x:left,y:9}, '页面展示数量'));
    for (const row of rows) {
      const color = row.sales_precision === 'approximate' ? '#c5a05f' : '#759b69';
      const dot = svgElement('circle', {cx:x(row.price),cy:y(row.sales_count),r:4.4,fill:row.sales_precision === 'lower_bound'?'white':color,stroke:color,'stroke-width':1.4,
        tabindex:0,role:'button','data-analysis-point':row.id,'aria-label': `${row.title || '商品'}，${money(row.price)}，${row.sales_text || ''}，${row.precision_label}`});
      svg.append(dot);
    }
    if (!rows.length) svg.append(svgElement('text', {x:width/2,y:height/2,'text-anchor':'middle'}, '当前口径没有同时具备价格与数量的商品'));
    $('sa-scatter').replaceChildren(svg);
  }
  $('sa-keyword').addEventListener('change', () => { $('sa-snapshot').value = 'range'; const type = $('sa-scope-form').querySelector('input[name="category"]'); if (type) type.remove(); });
  for (const input of $('sa-scope-form').querySelectorAll('input[type="date"]')) input.addEventListener('change', () => { $('sa-snapshot').value = 'range'; });
  $('sa-exclude-ads').addEventListener('change', () => $('sa-scope-form').requestSubmit());
  $('sa-group-select').addEventListener('change', event => event.target.form.requestSubmit());
  $('sa-rules-open').addEventListener('click', event => openDialog($('sa-rules-dialog'), event.target));
  document.addEventListener('click', event => {
    const detailButton = event.target.closest('[data-analysis-detail],[data-analysis-point]');
    if (detailButton) detail(detailButton.dataset.analysisDetail || detailButton.dataset.analysisPoint, detailButton);
    const saveButton = event.target.closest('[data-save-key]'); if (saveButton) save(saveButton.dataset.saveKey);
    const close = event.target.closest('[data-sa-close]'); if (close) $(close.dataset.saClose).close();
  });
  $('sa-scatter').addEventListener('keydown', event => { if ((event.key === 'Enter' || event.key === ' ') && event.target.dataset.analysisPoint) { event.preventDefault(); detail(event.target.dataset.analysisPoint, event.target); } });
  $('sa-scatter').addEventListener('pointermove', event => {
    const dot = event.target.closest('[data-analysis-point]'); if (!dot) { $('sa-tooltip').hidden = true; return; }
    const row = byId.get(dot.dataset.analysisPoint); if (!row) return;
    $('sa-tooltip').textContent = (row.title || '标题未记录') + '\n' + money(row.price) + ' · ' + (row.sales_text || '未提供') + '\n' + row.sales_group_label + ' · ' + row.precision_label;
    $('sa-tooltip').style.left = Math.max(8, Math.min(event.clientX + 14, innerWidth - 250)) + 'px'; $('sa-tooltip').style.top = Math.max(8, Math.min(event.clientY + 15, innerHeight - 120)) + 'px'; $('sa-tooltip').hidden = false;
  });
  $('sa-scatter').addEventListener('pointerleave', () => { $('sa-tooltip').hidden = true; });
  for (const dialog of [$('sa-rules-dialog'), $('sa-detail-dialog')]) {
    dialog.addEventListener('close', () => { if (lastFocus && lastFocus.isConnected) lastFocus.focus(); });
    dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  }
  renderScatter(); updateSaved();
})();
