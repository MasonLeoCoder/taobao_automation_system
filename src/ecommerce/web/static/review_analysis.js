/* 启动、进度与人工修订；GET轮询不触发付费调用。 */
(() => {
  const token = document.querySelector('meta[name="workbench-token"]')?.content;
  if (/^#review-\d+$/.test(location.hash)) {
    const target = document.querySelector(location.hash);
    if (target instanceof HTMLDetailsElement) target.open = true;
  }
  async function post(url, data) {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Collection-Token': token}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '操作失败，请刷新查看状态。');
    return result;
  }
  function bind(selector, values, success) {
    document.querySelectorAll(selector).forEach(form => form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button[type=submit]');
      const feedback = form.querySelector('.ra-feedback');
      button.disabled = true;
      feedback.textContent = '正在保存…';
      try { const result = await post(form.dataset.url, values(form)); success(result); }
      catch (error) { feedback.textContent = error.message; button.disabled = false; }
    }));
  }
  bind('[data-ra-start]', form => ({batch_id:Number(form.dataset.batch), input_hash:form.dataset.hash,
    model:form.elements.model.value, max_calls:Number(form.elements.max_calls.value), max_tokens:Number(form.elements.max_tokens.value),
    allow_paid:form.elements.allow_paid.checked, force:form.elements.force.checked}), result => location.assign(result.url));
  bind('[data-ra-resume]', form => ({allow_paid:form.elements.allow_paid.checked, acknowledge_unknown:form.elements.acknowledge_unknown.checked}), () => location.reload());
  bind('[data-ra-correct]', form => ({expected_revision:Number(form.dataset.revision), kind:form.elements.kind.value,
    excluded:form.elements.excluded.checked, reason:form.elements.reason.value,
    points:[...form.querySelectorAll('[data-ra-point]')].filter(row => row.querySelector('[name=keep]').checked).map(row => ({
      topic:row.querySelector('[name=topic]').value, sentiment:row.querySelector('[name=sentiment]').value,
      segment:row.dataset.segment, quote:row.dataset.quote}))}), () => location.reload());
  document.querySelectorAll('[data-ra-recount]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try { await post(button.dataset.raRecount, {}); location.reload(); }
    catch(error) { document.querySelector('#ra-global-feedback').textContent = error.message; button.disabled = false; }
  }));
  const task = document.querySelector('[data-ra-task]');
  if (task?.dataset.raActive === 'true') {
    async function poll() {
      try {
        const response = await fetch('/api/analysis/reviews/tasks/' + task.dataset.raTask);
        if (!response.ok) throw new Error();
        const data = await response.json();
        document.querySelector('#ra-progress').textContent = data.message + ' · 已保存 ' + data.processed_count + '/' + data.source_count + ' 条';
        if (!['queued','running'].includes(data.status) || data.appears_interrupted) location.reload();
        else setTimeout(poll, 2000);
      } catch (_) { document.querySelector('#ra-progress').textContent = '暂时无法读取进度，请刷新查看；不会自动重新启动分析。'; }
    }
    setTimeout(poll, 1500);
  }
})();
