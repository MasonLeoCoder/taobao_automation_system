/* 账本弹窗：保留原表单提交回退，异步保存失败时保留所有输入，不自动重试。 */
(() => {
  'use strict';
  const dialog = document.getElementById('entry');
  const form = dialog?.querySelector('[data-ledger-entry-form]');
  const openButton = document.querySelector('[data-ledger-new]');
  if (!dialog || !form || !openButton || typeof dialog.showModal !== 'function') return;
  const error = dialog.querySelector('[data-ledger-error]');
  const closeButtons = dialog.querySelectorAll('[data-ledger-close]');
  let saving = false;

  function open() {
    if (!dialog.open) dialog.showModal();
    form.elements.occurred_at.focus({preventScroll:true});
  }

  function beijingNow() {
    const parts = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit',
      day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'}).formatToParts(new Date());
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:${values.second}`;
  }

  openButton.addEventListener('click', event => {
    // 编辑或错误页面通过普通链接重新取得空白表单和新的幂等编号。
    if (form.elements.revision.value !== '0' || error.textContent.trim()) return;
    event.preventDefault();
    form.reset();
    form.elements.occurred_at.value = beijingNow();
    open();
  });
  closeButtons.forEach(button => button.addEventListener('click', () => { if (!saving) dialog.close(); }));
  dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    const url = new URL(location.href);
    url.searchParams.delete('edit'); url.searchParams.delete('new'); url.hash = '';
    history.replaceState(null, '', url);
    openButton.focus({preventScroll:true});
  });
  form.addEventListener('invalid', event => {
    if (event.target.matches('select[hidden][required]')) {
      event.preventDefault();
      error.textContent = '请选择记账分类。';
      error.hidden = false;
      event.target.nextElementSibling?.focus();
    }
  }, true);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving) return;
    saving = true;
    error.hidden = true;
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
      error.textContent = failure instanceof TypeError ? '连接中断，保存结果尚未确认。请先刷新账本核对记录；本次填写仍保留。' : failure.message;
      error.hidden = false;
      error.scrollIntoView({block:'nearest'});
      saving = false;
      submit.disabled = false;
      closeButtons.forEach(button => { button.disabled = false; });
    }
  });
  if (dialog.querySelector('[data-auto-open="true"]')) {
    dialog.removeAttribute('open');
    open();
  }
})();
