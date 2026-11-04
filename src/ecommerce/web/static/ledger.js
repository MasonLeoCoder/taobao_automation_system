/* 账本弹窗：保留原表单提交回退，异步保存失败时保留所有输入，不自动重试。 */
(() => {
  'use strict';
  const monthForm = document.querySelector('[data-ledger-month-form]');
  monthForm?.elements.month.addEventListener('change', () => {
    if (monthForm.checkValidity()) monthForm.requestSubmit();
  });
  if (document.querySelector('[data-ledger-notice]')) {
    const url = new URL(location.href);
    url.searchParams.delete('receipt');
    history.replaceState(null, '', url);
  }
  const notes = Array.from(document.querySelectorAll('[data-ledger-note]'));
  function measureNote(note) {
    if (note.open) return;
    const preview = note.querySelector('.ledger-note-preview');
    const expand = note.querySelector('.ledger-note-expand');
    expand.hidden = true;
    const fullText = note.querySelector('.ledger-note-full').textContent;
    const clipped = preview.scrollWidth > preview.clientWidth || fullText.length > preview.textContent.length || fullText.includes('\n');
    expand.hidden = !clipped;
    note.dataset.expandable = String(clipped);
    note.querySelector('summary').tabIndex = clipped ? 0 : -1;
  }
  notes.forEach(note => {
    note.querySelector('summary').addEventListener('click', event => {
      if (!note.open && note.dataset.expandable === 'false') event.preventDefault();
    });
    note.addEventListener('toggle', () => measureNote(note));
    measureNote(note);
  });
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(() => notes.forEach(measureNote));
    const table = document.querySelector('.ledger-table-wrap');
    if (table) observer.observe(table);
  } else window.addEventListener('resize', () => notes.forEach(measureNote));

  function beijingNow() {
    const parts = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit',
      day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'}).formatToParts(new Date());
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:${values.second}`;
  }

  function setupDialog(id, formSelector, triggerSelector, focusField, queryKeys) {
    const dialog = document.getElementById(id);
    const form = dialog?.querySelector(formSelector);
    const openButton = document.querySelector(triggerSelector);
    if (!dialog || !form || !openButton || typeof dialog.showModal !== 'function') return;
    const error = dialog.querySelector('[data-ledger-error]');
    const closeButtons = dialog.querySelectorAll('[data-ledger-close]');
    let saving = false;
    function open() {
      if (!dialog.open) dialog.showModal();
      form.elements[focusField].focus({preventScroll:true});
    }
    openButton.addEventListener('click', event => {
      if ((id === 'entry' && form.elements.revision.value !== '0') || error.textContent.trim()) return;
      event.preventDefault();
      form.reset();
      if (id === 'entry') form.elements.occurred_at.value = beijingNow();
      open();
    });
    closeButtons.forEach(button => button.addEventListener('click', () => { if (!saving) dialog.close(); }));
    dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
    dialog.addEventListener('close', () => {
      const url = new URL(location.href);
      queryKeys.forEach(key => url.searchParams.delete(key)); url.hash = '';
      history.replaceState(null, '', url);
      openButton.focus({preventScroll:true});
    });
    form.addEventListener('invalid', event => {
      if (event.target.matches('select[hidden][required]')) {
        event.preventDefault();
        error.textContent = '请选择记账分类。'; error.hidden = false;
        event.target.nextElementSibling?.focus();
      }
    }, true);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (saving) return;
      saving = true; error.hidden = true;
      const submit = form.querySelector('[type="submit"]');
      const body = new FormData(form);
      submit.disabled = true;
      closeButtons.forEach(button => { button.disabled = true; });
      try {
        const response = await fetch(form.getAttribute('action'), {method:'POST', body, headers:{Accept:'application/json'}});
        const data = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : null;
        if (!response.ok || !data?.redirect) throw new Error(data?.error || '保存未完成，请核对页面状态后重试。');
        location.assign(data.redirect);
      } catch (failure) {
        error.textContent = failure instanceof TypeError ? '连接中断，保存结果尚未确认。请先刷新页面核对；本次填写仍保留。' : failure.message;
        error.hidden = false; error.scrollIntoView({block:'nearest'});
        saving = false; submit.disabled = false;
        closeButtons.forEach(button => { button.disabled = false; });
      }
    });
    if (dialog.querySelector('[data-auto-open="true"]')) {
      dialog.removeAttribute('open'); open();
    }
  }
  setupDialog('entry', '[data-ledger-entry-form]', '[data-ledger-new]', 'occurred_at', ['edit','new']);
  setupDialog('limit-editor', '[data-ledger-limit-form]', '[data-ledger-limit-open]', 'amount', ['edit_limit']);
})();
