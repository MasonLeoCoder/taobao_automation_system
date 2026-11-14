(() => {
  const root = document.getElementById('platform-accounts');
  if (!root) return;
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const message = document.getElementById('account-operation-message');
  const proceed = document.getElementById('account-continue');
  const cancel = document.getElementById('account-cancel');
  const refresh = document.getElementById('account-refresh');
  const actions = [...root.querySelectorAll('[data-account-action]')];
  let state;
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
    refresh.hidden = state?.phase !== 'succeeded';
    message.textContent = state?.message || '';
  }

  async function update(url, payload) {
    clearTimeout(poll);
    busy = true;
    show();
    let retry = false;
    try {
      state = await request(url, payload);
    } catch (error) {
      state = {...state, message: error.message};
      retry = !payload || active(state?.phase) || error instanceof TypeError;
    } finally {
      busy = false;
      show();
      if (active(state?.phase) || retry) {
        poll = setTimeout(() => update(root.dataset.status), 1000);
      }
    }
  }

  root.addEventListener('click', event => {
    const button = event.target.closest('[data-account-action]');
    if (!button || busy || active(state?.phase)) return;
    const row = button.closest('[data-account-id]');
    const payload = {action: button.dataset.accountAction};
    if (row) Object.assign(payload, {account_id: row.dataset.accountId, shop_id: row.dataset.shopId});
    update(root.dataset.start, payload);
  });
  proceed.addEventListener('click', () => update(root.dataset.respond,
    {id: state.id, action: 'continue', prompt: state.prompt}));
  cancel.addEventListener('click', () => update(root.dataset.respond,
    {id: state.id, action: 'cancel', prompt: state.prompt}));
  update(root.dataset.status);
})();
