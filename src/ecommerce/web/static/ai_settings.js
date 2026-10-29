/* 固定文本连接检查；不接触密钥，不自动发起付费请求。 */
(() => {
  const form = document.querySelector('#ai-check');
  if (!form) return;
  const output = document.querySelector('#ai-result');
  const button = document.querySelector('#ai-start');
  const initiallyDisabled = button.disabled;
  let timer;
  function show(data) {
    output.textContent = data.message || data.error || '请求失败';
    if (data.usage) output.textContent += ' 用量：' + JSON.stringify(data.usage);
    button.disabled = initiallyDisabled || data.status === 'running';
    if (data.status === 'running') timer = setTimeout(poll, 1000);
  }
  async function poll() {
    try {
      const response = await fetch('/api/ai/check');
      if (!response.ok) throw new Error();
      show(await response.json());
    } catch (_) {
      output.textContent = '无法获取状态；可刷新页面继续查看，请勿重复启动测试。';
      button.disabled = true;
    }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    clearTimeout(timer);
    button.disabled = true;
    output.textContent = '正在提交…';
    try {
      const response = await fetch('/api/ai/check', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Collection-Token': form.dataset.token},
        body: JSON.stringify({provider: form.elements.provider.value, model: form.elements.model.value,
          allow_paid: form.elements.allow_paid.checked})
      });
      const data = await response.json();
      if (response.status === 409) { await poll(); return; }
      show(data);
    } catch (_) {
      output.textContent = '提交状态未知；请刷新查看，避免重复调用。';
    }
  });
  poll();
})();
