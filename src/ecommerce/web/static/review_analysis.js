/* 启动、进度与人工修订；GET轮询不触发付费调用。 */
(() => {
  const token = document.querySelector('meta[name="workbench-token"]')?.content;
  const summary = document.querySelector('[data-ra-batch-summary]');
  if (summary) {
    const links = document.querySelector('[data-ra-batch-links]');
    function resultLink(label, id) {
      const link = document.createElement('a');
      link.className = 'ui-button ui-secondary';
      link.textContent = label;
      link.href = '/analysis/reviews/tasks/' + encodeURIComponent(id);
      links.append(link);
    }
    async function loadBatchSummary() {
      try {
        const response = await fetch(summary.dataset.raBatchSummary);
        if (!response.ok) throw new Error();
        const data = await response.json();
        links.replaceChildren();
        const notes = [];
        if (data.active_id) { notes.push('本批正在分析'); resultLink('查看分析进度', data.active_id); }
        if (data.result_id) { notes.push('本批已有分析结果'); resultLink('查看本批报告', data.result_id); }
        if (['failed','partial','interrupted'].includes(data.latest_status)) notes.push(data.result_id ? '最新任务未完整完成，已有报告仍保留' : '最新任务未完整完成');
        summary.textContent = notes.join('；') || '本批尚无分析结果。';
        if (data.active_id) setTimeout(loadBatchSummary, 2500);
      } catch (_) { summary.textContent = '暂时无法读取本批分析状态，可通过“查看分析记录”查询。'; }
    }
    loadBatchSummary();
  }
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
