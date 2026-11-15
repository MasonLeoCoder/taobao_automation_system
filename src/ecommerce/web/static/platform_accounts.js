(() => {
  const root = document.getElementById('platform-accounts');
  if (!root) return;
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const message = document.getElementById('account-operation-message');
  const proceed = document.getElementById('account-continue');
  const cancel = document.getElementById('account-cancel');
  const refresh = document.getElementById('account-refresh');
  const actions = [...root.querySelectorAll('[data-account-action]')];
  const confirmation = document.getElementById('account-confirm');
  const buyer = root.dataset.platform === 'taobao';
  const impacts = {
    disable: buyer ? '停用后，该账号不能检查登录或重新登录。账号登记和本机登录资料仍保留。'
      : '停用后，该账号不能打开千牛、检查登录或重新登录。账号登记和已保存的登录资料仍可保留。',
    enable: '启用后可以继续使用该账号。如果登录资料已清除或失效，需要重新扫码。',
    clear: buyer ? '清理此账号在当前环境保存的本机登录资料。下次使用时需要重新扫码，账号登记仍保留。'
      : '清理此账号在当前环境保存的本机登录资料。下次使用时需要重新扫码，账号登记和店铺关联会保留。',
    retry_cleanup: '重试清理此账号已经撤下的本机登录资料。当前启用的会话可以继续使用。',
  };
  let state;
  let confirmationPayload;
  let submitted;
  let busy = false;
  let poll;

  const active = phase => ['running', 'scan', 'waiting', 'open', 'stopping'].includes(phase);
  async function request(url, payload) {
    const response = await fetch(url, payload ? {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Collection-Token': token},
      body: JSON.stringify(payload),
    } : undefined);
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.error || '账号管理请求失败，请刷新后重试');
    return data;
  }

  function show() {
    actions.forEach(button => { button.disabled = busy || active(state?.phase); });
    proceed.hidden = state?.phase !== 'waiting';
    cancel.hidden = !['scan', 'waiting', 'open'].includes(state?.phase);
    cancel.textContent = state?.phase === 'open' ? '关闭千牛窗口' : '取消';
    proceed.disabled = cancel.disabled = busy;
    refresh.hidden = !['succeeded', 'partial', 'failed', 'cancelled'].includes(state?.phase);
    message.textContent = state?.message || '';
    message.dataset.phase = state?.phase || '';
  }

  async function update(url, payload) {
    clearTimeout(poll);
    busy = true;
    show();
    let retry = false;
    try {
      state = await request(url, payload);
      if (url === root.dataset.start) submitted = state.id;
      if (state?.id === submitted && submitted &&
          (['succeeded', 'partial'].includes(state.phase) || (state.phase === 'failed' && state.refresh_accounts))) {
        location.reload();
        return;
      }
    } catch (error) {
      const previousState = state;
      retry = !payload || active(previousState?.phase) || error instanceof TypeError;
      state = {...previousState, phase: active(previousState?.phase) ? previousState.phase : 'failed', message: error.message};
    } finally {
      busy = false;
      show();
      if (active(state?.phase) || retry) {
        poll = setTimeout(() => update(root.dataset.status), 1000);
      }
    }
  }

  confirmation.addEventListener('close', () => {
    if (confirmation.returnValue === 'confirm' && confirmationPayload && !busy && !active(state?.phase)) {
      update(root.dataset.start, confirmationPayload);
    }
    confirmationPayload = null;
  });

  root.addEventListener('click', event => {
    const button = event.target.closest('[data-account-action]');
    if (!button || busy || active(state?.phase)) return;
    const row = button.closest('[data-account-id]');
    const payload = {action: button.dataset.accountAction};
    if (row) {
      Object.assign(payload, {account_id: row.dataset.accountId, revision: Number(row.dataset.revision)});
      if (!buyer) payload.shop_id = row.dataset.shopId;
    }
    if (Object.hasOwn(impacts, payload.action)) {
      confirmationPayload = payload;
      const action = button.textContent.trim();
      document.getElementById('account-confirm-title').textContent = action;
      document.getElementById('account-confirm-login').textContent = row.querySelector('[data-login-name]').textContent;
      if (!buyer) document.getElementById('account-confirm-shop').textContent =
        `${row.querySelector('[data-shop-name]').textContent} · ${row.dataset.shopId}`;
      document.getElementById('account-confirm-impact').textContent = impacts[payload.action];
      document.getElementById('account-confirm-submit').textContent = action;
      button.closest('details').open = false;
      confirmation.returnValue = '';
      confirmation.showModal();
      return;
    }
    update(root.dataset.start, payload);
  });
  proceed.addEventListener('click', () => update(root.dataset.respond,
    {id: state.id, action: 'continue', prompt: state.prompt}));
  cancel.addEventListener('click', () => update(root.dataset.respond,
    {id: state.id, action: 'cancel', prompt: state.prompt}));
  update(root.dataset.status);
})();
