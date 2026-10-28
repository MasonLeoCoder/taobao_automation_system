/* 可搜索的本地选择控件：原生select继续负责表单值，检索文本仅过滤候选项。 */
(() => {
  'use strict';
  let sequence = 0;
  const normalize = value => value.normalize('NFKC').toLocaleLowerCase().trim();

  function element(tag, className = '', text = '') {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  }
  function icon(name) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 20 20');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', {search:'M13.5 13.5 18 18M15 8.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0', chevron:'m5 8 5 5 5-5', check:'m4 10 4 4 8-8', close:'m5 5 10 10M15 5 5 15'}[name]);
    svg.append(path);
    return svg;
  }

  function enhance(select) {
    const id = 'searchable-select-' + (++sequence);
    const choices = Array.from(select.options).filter(option => option.value && !option.disabled)
      .map(option => ({value:option.value, label:option.textContent.trim()}));
    const trigger = element('button', 'ss-trigger');
    trigger.type = 'button';
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', id);
    const triggerIcon = element('span', 'ss-trigger-icon'); triggerIcon.append(icon('search'));
    const valueText = element('span', 'ss-value');
    const chevron = element('span', 'ss-chevron'); chevron.append(icon('chevron'));
    trigger.append(triggerIcon, valueText, chevron);

    const popup = element('div', 'ss-popover'); popup.id = id;
    popup.setAttribute('role', 'dialog'); popup.setAttribute('aria-label', '选择搜索关键词');
    const nativePopover = typeof popup.showPopover === 'function';
    if (nativePopover) popup.setAttribute('popover', 'auto'); else popup.hidden = true;
    const heading = element('div', 'ss-heading');
    heading.append(element('strong', '', '选择搜索词'), element('span', 'ss-heading-hint', '已采集的关键词'));
    const searchBox = element('div', 'ss-search');
    searchBox.append(icon('search'));
    const input = element('input', 'ss-input'); input.type = 'search'; input.placeholder = '输入文字，查找搜索词'; input.autocomplete = 'off'; input.spellcheck = false;
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-label', '查找搜索词'); input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-expanded', 'false'); input.setAttribute('aria-controls', id + '-list');
    const clear = element('button', 'ss-clear'); clear.type = 'button'; clear.setAttribute('aria-label', '清空搜索'); clear.append(icon('close')); clear.hidden = true;
    searchBox.append(input, clear);
    const resultStatus = element('div', 'ss-result-status'); resultStatus.setAttribute('role', 'status'); resultStatus.setAttribute('aria-live', 'polite');
    const list = element('div', 'ss-list'); list.id = id + '-list'; list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', '匹配的搜索词');
    const empty = element('div', 'ss-empty'); empty.hidden = true; empty.append(icon('search'), element('strong', '', '没有匹配的搜索词'), element('span', '', '试试更短的文字，或清空后重新选择。'));
    const footer = element('div', 'ss-footer');
    footer.append(element('span', '', '仅选择已采集的搜索词'), element('span', 'ss-keyboard-hint', '↑↓ 选择  ·  Enter 确认'));
    popup.append(heading, searchBox, resultStatus, list, empty, footer);
    document.body.append(popup);
    select.after(trigger); select.hidden = true;
    let opened = false, active = -1, matches = [], composing = false;

    function updateValue() {
      const option = select.selectedOptions[0];
      valueText.textContent = select.value && option ? option.textContent.trim() : select.dataset.placeholder || '请选择搜索词';
      trigger.title = valueText.textContent;
      trigger.setAttribute('aria-label', '搜索关键词，当前：' + valueText.textContent);
      trigger.disabled = select.disabled;
      trigger.classList.toggle('ss-placeholder', !select.value);
    }
    function position() {
      if (!opened) return;
      const bounds = trigger.getBoundingClientRect(), viewport = window.visualViewport;
      const width = viewport ? viewport.width : innerWidth, height = viewport ? viewport.height : innerHeight;
      const leftEdge = viewport ? viewport.offsetLeft : 0, topEdge = viewport ? viewport.offsetTop : 0;
      const panelWidth = Math.min(Math.max(bounds.width, 340), width - 24);
      const below = topEdge + height - bounds.bottom - 12, above = bounds.top - topEdge - 12;
      const upwards = below < 220 && above > below;
      popup.style.width = panelWidth + 'px';
      popup.style.maxHeight = Math.max(80, Math.min(420, upwards ? above : below)) + 'px';
      popup.style.left = Math.max(leftEdge + 12, Math.min(bounds.left, leftEdge + width - panelWidth - 12)) + 'px';
      popup.style.top = Math.max(topEdge + 8, upwards ? bounds.top - popup.getBoundingClientRect().height - 8 : bounds.bottom + 8) + 'px';
      popup.classList.toggle('ss-above', upwards);
    }
    function setActive(index, scroll = false) {
      active = matches.length ? (index + matches.length) % matches.length : -1;
      for (let i = 0; i < list.children.length; i++) list.children[i].classList.toggle('ss-active', i === active);
      if (active >= 0) {
        input.setAttribute('aria-activedescendant', list.children[active].id);
        if (scroll) list.children[active].scrollIntoView({block:'nearest'});
      } else input.removeAttribute('aria-activedescendant');
    }
    function highlight(label) {
      const span = element('span', 'ss-option-text');
      const query = input.value.trim(), index = query ? label.toLocaleLowerCase().indexOf(query.toLocaleLowerCase()) : -1;
      if (index < 0) span.textContent = label;
      else span.append(document.createTextNode(label.slice(0, index)), element('mark', '', label.slice(index, index + query.length)), document.createTextNode(label.slice(index + query.length)));
      return span;
    }
    function render() {
      const query = normalize(input.value);
      matches = choices.filter(choice => normalize(choice.label).includes(query));
      const fragment = document.createDocumentFragment();
      matches.forEach((choice, index) => {
        const row = element('div', 'ss-option'); row.id = id + '-option-' + index; row.dataset.index = index;
        row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(choice.value === select.value));
        const glyph = element('span', 'ss-option-glyph', '#'); glyph.setAttribute('aria-hidden', 'true');
        const check = element('span', 'ss-option-check'); check.append(icon('check'));
        row.append(glyph, highlight(choice.label), check); fragment.append(row);
      });
      list.replaceChildren(fragment); list.scrollTop = 0;
      empty.hidden = matches.length > 0;
      if (!choices.length) { empty.querySelector('strong').textContent = '当前日期范围内暂无搜索词'; empty.querySelector('span').textContent = '可调整日期范围后再选择。'; }
      resultStatus.textContent = query ? `匹配 ${matches.length} / ${choices.length} 个搜索词` : `共 ${choices.length} 个搜索词`;
      clear.hidden = !input.value;
      const selectedIndex = matches.findIndex(choice => choice.value === select.value);
      setActive(selectedIndex >= 0 ? selectedIndex : 0);
      position();
    }
    function closed(restoreFocus = false) {
      opened = false; trigger.setAttribute('aria-expanded', 'false'); input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant');
      if (restoreFocus) trigger.focus({preventScroll:true});
    }
    function close(restoreFocus = false) {
      if (!opened) return;
      if (nativePopover && popup.matches(':popover-open')) popup.hidePopover(); else popup.hidden = true;
      closed(restoreFocus);
    }
    function open(query = '') {
      if (select.disabled) return;
      input.value = query; opened = true;
      if (nativePopover) popup.showPopover(); else popup.hidden = false;
      trigger.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-expanded', 'true');
      render(); input.focus({preventScroll:true}); position();
    }
    function choose(index) {
      const choice = matches[index]; if (!choice) return;
      const changed = select.value !== choice.value;
      select.value = choice.value; updateValue(); close(true);
      if (changed) select.dispatchEvent(new Event('change', {bubbles:true}));
    }
    trigger.addEventListener('click', () => opened ? close(true) : open());
    trigger.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(); }
      else if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); open(event.key); }
    });
    input.addEventListener('input', () => { if (!composing) render(); });
    input.addEventListener('compositionstart', () => { composing = true; });
    input.addEventListener('compositionend', () => { composing = false; render(); });
    input.addEventListener('keydown', event => {
      if (composing || event.isComposing) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setActive(active + (event.key === 'ArrowDown' ? 1 : -1), true); }
      else if (event.key === 'Enter') { event.preventDefault(); choose(active); }
      else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
    });
    clear.addEventListener('pointerdown', event => event.preventDefault());
    clear.addEventListener('click', () => { input.value = ''; input.focus({preventScroll:true}); render(); });
    list.addEventListener('pointerdown', event => { if (event.target.closest('[role="option"]')) event.preventDefault(); });
    list.addEventListener('pointermove', event => { const row = event.target.closest('[role="option"]'); if (row) setActive(Number(row.dataset.index)); });
    list.addEventListener('click', event => { const row = event.target.closest('[role="option"]'); if (row) choose(Number(row.dataset.index)); });
    popup.addEventListener('toggle', event => { if (event.newState === 'closed' && opened) closed(); });
    document.addEventListener('focusin', event => { if (opened && !popup.contains(event.target) && !trigger.contains(event.target)) close(); });
    document.addEventListener('pointerdown', event => { if (opened && !popup.contains(event.target) && !trigger.contains(event.target)) close(); });
    document.addEventListener('keydown', event => { if (opened && event.key === 'Escape' && !composing) { event.preventDefault(); close(true); } });
    window.addEventListener('resize', position);
    window.addEventListener('scroll', event => { if (!popup.contains(event.target)) position(); }, true);
    if (window.visualViewport) { visualViewport.addEventListener('resize', position); visualViewport.addEventListener('scroll', position); }
    select.addEventListener('change', updateValue);
    select.form?.addEventListener('reset', () => queueMicrotask(() => { close(); updateValue(); }));
    updateValue();
  }
  document.querySelectorAll('select[data-searchable-select]').forEach(enhance);
})();
