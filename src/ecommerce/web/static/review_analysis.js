/* 启动、进度与人工修订；GET轮询不触发付费调用。 */
(() => {
  const token = document.querySelector('meta[name="workbench-token"]')?.content;
  const modal = document.querySelector('#review-analysis-dialog');
  if (modal) {
    const content = modal.querySelector('[data-ra-modal-content]');
    let opener, loading, sequence = 0;
    const submitting = () => modal.querySelector('[data-ra-start]')?.dataset.raSubmitting === 'true';
    function clearContent() {
      content.dispatchEvent(new CustomEvent('ui:controls-remove', {bubbles:true}));
      content.replaceChildren();
    }
    function closeModal() { if (!submitting()) modal.close(); }
    modal.addEventListener('cancel', event => { if (submitting()) event.preventDefault(); });
    modal.addEventListener('click', event => {
      if (event.target.closest('[data-ra-close]')) closeModal();
      else if (event.target === modal) {
        const r = modal.getBoundingClientRect();
        if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeModal();
      }
    });
    modal.addEventListener('close', () => {
      sequence++; loading?.abort(); clearContent();
      opener?.focus({preventScroll:true});
    });
    document.addEventListener('click', async event => {
      const trigger = event.target.closest('[data-review-analysis="start"]');
      if (!trigger || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (modal.open) return;
      opener = trigger;
      clearContent();
      const status = document.createElement('p'); status.className = 'ui-modal-message'; status.setAttribute('role','status'); status.textContent = '正在读取本批评价资料…';
      content.append(status); content.setAttribute('aria-busy','true'); modal.showModal();
      const current = ++sequence;
      loading = new AbortController();
      try {
        const url = new URL(trigger.href, location.href); url.searchParams.set('dialog','1');
        const response = await fetch(url, {cache:'no-store', signal:loading.signal});
        if (response.headers.get('X-Review-Analysis-Fragment') !== '1') throw new Error();
        const text = await response.text();
        if (!modal.open || current !== sequence) return;
        // 仅接收同源、明确标记的服务器模板片段，模板中的资料均自动转义。
        content.innerHTML = text;
        content.dispatchEvent(new CustomEvent('ui:controls-added', {bubbles:true}));
        bindStart(content);
      } catch (error) {
        if (error.name !== 'AbortError' && modal.open && current === sequence) status.textContent = '暂时无法读取评价资料，请关闭后重试。';
      } finally { if (current === sequence) content.removeAttribute('aria-busy'); }
    });
  }
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
  function bind(selector, values, success, root = document) {
    root.querySelectorAll(selector).forEach(form => {
      if (form.dataset.raBound) return;
      form.dataset.raBound = 'true';
      form.addEventListener('invalid', event => { const details = event.target.closest('details'); if (details) details.open = true; }, true);
      form.addEventListener('submit', async event => {
      event.preventDefault();
      if (form.dataset.raSubmitting === 'true') return;
      const button = form.querySelector('button[type=submit]');
      const feedback = form.querySelector('.ra-feedback');
      const label = button.textContent;
      const closeButtons = form.closest('dialog')?.querySelectorAll('[data-ra-close]') || [];
      form.dataset.raSubmitting = 'true'; closeButtons.forEach(b => b.disabled = true);
      button.disabled = true;
      button.textContent = '正在提交…'; feedback.textContent = '正在提交，请稍候…';
      try { const result = await post(form.dataset.url, values(form)); success(result); }
      catch (error) { feedback.textContent = error.message; button.disabled = false; button.textContent = label; }
      finally { form.dataset.raSubmitting = 'false'; closeButtons.forEach(b => b.disabled = false); }
      });
    });
  }
  function bindStart(root = document) {
    root.querySelectorAll('[data-ra-start]').forEach(form => {
      if (form.dataset.raBudgetBound) return;
      form.dataset.raBudgetBound = 'true';
      const manual = form.elements.manual_budget, tokens = form.elements.max_tokens, calls = form.elements.max_calls;
      function budgetFeedback() {
        tokens.disabled = calls.disabled = !manual.checked;
        if (!manual.checked) { tokens.value = tokens.dataset.raDefault; calls.value = calls.dataset.raDefault; }
        const minimum = Number(tokens.dataset.raMinimum), current = Number(tokens.value);
        const message = manual.checked && Number.isFinite(current) && current < minimum
          ? `本次用量上限不足：至少需预留 ${minimum}，当前设置 ${current}，还差 ${minimum-current}；尚未发送评价。` : '';
        tokens.setCustomValidity(message);
        form.querySelector('[data-ra-budget-feedback]').textContent = message;
      }
      manual.addEventListener('change', budgetFeedback); tokens.addEventListener('input', budgetFeedback); budgetFeedback();
    });
    bind('[data-ra-start]', form => ({batch_id:Number(form.dataset.batch), input_hash:form.dataset.hash,
    plan_hash:form.dataset.planHash, model:form.elements.model.value,
    max_calls:form.elements.manual_budget.checked ? Number(form.elements.max_calls.value) : null,
    max_tokens:form.elements.manual_budget.checked ? Number(form.elements.max_tokens.value) : null,
    allow_paid:form.elements.allow_paid.checked, force:form.elements.force.checked}), result => location.assign(result.url), root);
  }
  bindStart();
  document.querySelectorAll('[data-ra-resume]').forEach(form => {
    const extra = form.elements.increase_budget;
    extra.addEventListener('change', () => {
      form.elements.max_tokens.disabled = form.elements.max_calls.disabled = !extra.checked;
    });
  });
  bind('[data-ra-resume]', form => ({allow_paid:form.elements.allow_paid.checked, acknowledge_unknown:form.elements.acknowledge_unknown.checked,
    plan_hash:form.dataset.planHash,
    max_tokens:form.elements.increase_budget.checked ? Number(form.elements.max_tokens.value) : null,
    max_calls:form.elements.increase_budget.checked ? Number(form.elements.max_calls.value) : null}), () => location.reload());
  document.querySelectorAll('[data-ra-recover]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    const feedback = document.querySelector('#ra-global-feedback');
    feedback.textContent = '正在本地校验已有返回，不调用AI…';
    try { await post(button.dataset.raRecover,{}); location.reload(); }
    catch(error) { feedback.textContent = error.message; button.disabled = false; }
  }));
  bind('[data-ra-correct]', form => ({expected_revision:Number(form.dataset.revision), kind:form.elements.kind.value,
    excluded:form.elements.excluded.checked, reason:form.elements.reason.value,
    points:[...form.querySelectorAll('[data-ra-point]')].filter(row => row.querySelector('[name=keep]').checked).map(row => ({
      topic:row.querySelector('[name=topic]').value, sentiment:row.querySelector('[name=sentiment]').value,
      segment:row.dataset.segment, quote:row.dataset.quote}))}), () => location.reload());
  bind('[data-ra-v4-review]', form => ({report_id:form.dataset.report,item_key:form.dataset.key,
    candidate_hash:form.dataset.candidate,source_hash:form.dataset.source,expected_revision:Number(form.dataset.revision),
    decision:form.elements.decision.value,reviewer:form.elements.reviewer.value,reason:form.elements.reason.value,
    review_context:{evidence_checked:form.elements.evidence_checked.checked,
      ...(form.elements.conditions_checked ? {conditions_checked:form.elements.conditions_checked.checked,stop_scope:form.elements.stop_scope.value} : {})}}), () => location.reload());
  bind('[data-ra-v4-correct]', form => ({expected_revision:Number(form.dataset.revision),excluded:form.elements.excluded.checked,
    reason:form.elements.reason.value,points:[...form.querySelectorAll('[data-ra-v4-point]')].filter(row=>row.querySelector('[name=keep]').checked).map(row=>({
      observation_id:row.dataset.id,topic:row.querySelector('[name=topic]').value,sentiment:row.querySelector('[name=sentiment]').value,
      fact_text:row.querySelector('[name=fact_text]').value}))}), () => location.reload());
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
