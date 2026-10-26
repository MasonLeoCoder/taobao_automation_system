/* 网页采集只派发短请求；浏览器工作由独立任务完成。 */
(() => {
  const client = document.getElementById('collection-client');
  if (!client) return;
  const product = document.querySelector('[data-collection-product]')?.dataset.collectionProduct;
  const bar = document.getElementById('collection-task-bar');
  const dialog = document.getElementById('collection-attention');
  const labels = {basic_info:'基本信息',video:'视频',main_images:'主图',skus:'SKU',parameters:'商品参数',detail_images:'详情图片',review_summary:'评价概览',reviews:'评价明细',questions:'问大家'};
  const states = {queued:'启动中',running:'采集中',waiting:'等待采集',not_run:'本次未执行',needs_attention:'需要人工处理',storage_pending:'等待入库',succeeded:'采集完成',failed:'采集失败',cancelled:'已停止',interrupted:'进程已退出',unverified:'进程待核实',verification_pending:'结果待核对'};
  const tracked = new Map();
  let plan = null;
  const catalogPlans = new Map();
  const refreshedCatalogJobs = new Set();
  const submittedPrompts = new Set();
  let promptJob = null;
  let busy = false;
  let submitting = false;
  const endpoint = job => `/api/collections/products/${encodeURIComponent(job.product_id)}/jobs/${job.id}`;

  async function api(url, body) {
    const response = await fetch(url, body === undefined ? {cache:'no-store'} : {
      method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':client.dataset.token},body:JSON.stringify(body)
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error || (response.status === 403 ? '操作令牌已失效，请刷新页面' : `请求失败（${response.status}），请稍后重试`));
    return value;
  }

  function link(label, url, color = 'action-history') {
    const node = document.createElement('a');
    node.className = `button action-button ${color}`; node.textContent = label; node.href = url;
    return node;
  }

  function renderCard(job) {
    if (job.product_id !== product) return;
    const card = document.querySelector(`[data-collection-module="${job.module}"]`);
    if (!card) return;
    // 旧网页任务不能覆盖页面已展示的较新归类结果。
    if (job.done && job.batch_id && Number(card.dataset.latestBatch) > Number(job.batch_id)) return;
    const status = card.querySelector('.module-job-status');
    status.textContent = `${states[job.state] || job.state}：${job.message}`;
    const actions = card.querySelector('.module-actions');
    let result = actions.querySelector('[data-job-result]');
    if (job.done && job.result_url) {
      if (!result) result = [...actions.querySelectorAll('a')].find(node => new URL(node.href).searchParams.get('batch') === String(job.batch_id));
      if (!result) { result = link('', job.result_url); result.dataset.jobResult = 'true'; actions.append(result); }
      result.dataset.jobResult = 'true';
      result.href = job.result_url;
      result.textContent = job.valid ? `${job.result_label} →` : '查看本次采集尝试';
      if (job.valid) actions.querySelector('.module-collect')?.remove();
      if (job.valid) document.querySelectorAll(`.module-collect[data-product][data-module="${job.module}"]`).forEach(button => {
        button.replaceWith(link(`${job.result_label} →`,job.result_url,'action-primary'));
      });
    }
    if (job.done && !job.valid) {
      const button = actions.querySelector('.module-collect');
      if (button) button.textContent = '重新采集';
    }
    let logs = actions.querySelector('[data-job-logs]');
    if (!logs) { logs = link('查看任务日志',job.logs_url); logs.dataset.jobLogs = 'true'; actions.append(logs); }
  }

  function cardsFor(job) {
    if (job.mode !== 'remaining') return [job];
    return (job.modules || []).map(module => {
      const item = (job.results || []).find(result => result.module === module) || {};
      const started = (job.started_modules || []).includes(module);
      const current = job.current_module === module;
      const state = item.valid ? 'succeeded' : current && !job.done ? job.state : job.done ? (started ? job.state : 'not_run') : 'waiting';
      return {...job,module,state,batch_id:item.batch_id,valid:Boolean(item.valid),result_url:item.result_url,
        result_label:item.label || '查看资料',
        message:item.valid ? '本模块已保存并确认入库' : job.done ? (item.batch_id ? '本次仅保留采集尝试，可重新采集' : '本次未保存有效结果，可重新采集') : current ? '正在采集本模块' : '等待当前任务依次采集'};
    });
  }

  function jobLabel(job) {
    if (job.mode !== 'remaining') return labels[job.module] || job.module;
    return `采集所有 · 已完成 ${job.completed_count}/${job.total_count} 项 · 跳过 ${(job.skipped_modules || []).length} 项${job.current_module ? ` · 当前：${labels[job.current_module]}` : ''}`;
  }

  async function sendControl(job, action, promptId) {
    const result = await api(`${endpoint(job)}/control`,{action,prompt_id:promptId || null});
    if (promptId) submittedPrompts.add(promptId);
    document.getElementById('collection-attention-feedback').textContent = result.message;
    await refresh();
  }

  function showPrompt(job) {
    const prompt = job.prompt;
    if (!prompt || job.done || submittedPrompts.has(prompt.prompt_id)) {
      if (promptJob?.id === job.id && dialog.open) dialog.close();
      return;
    }
    if (dialog.open && promptJob?.prompt?.prompt_id === prompt.prompt_id && promptJob.storage_pending === job.storage_pending) return;
    promptJob = job;
    document.getElementById('collection-attention-product').textContent = `商品 ${job.product_id} · ${jobLabel(job)}`;
    document.getElementById('collection-attention-step').textContent = `当前步骤：${prompt.step || '人工处理'}`;
    document.getElementById('collection-attention-message').textContent = prompt.message || job.message;
    document.getElementById('collection-attention-feedback').textContent = '';
    const actions = document.getElementById('collection-attention-actions'); actions.replaceChildren();
    const choices = {'继续':'continue','确认':'confirm','取消':'cancel'};
    [...(job.storage_pending ? [] : prompt.allowed || []),'停止'].forEach(label => {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
      button.className = `button action-button ${label === '停止' ? 'action-danger' : 'action-primary'}`;
      button.addEventListener('click', async () => {
        actions.querySelectorAll('button').forEach(node => node.disabled = true);
        try { await sendControl(job, choices[label] || 'stop',prompt.prompt_id); dialog.close(); }
        catch (error) {
          document.getElementById('collection-attention-feedback').textContent = error.message;
          actions.querySelectorAll('button').forEach(node => node.disabled = false);
        }
      });
      actions.append(button);
    });
    if (!dialog.open) dialog.showModal();
  }

  function render() {
    const jobs = [...tracked.values()];
    const current = jobs.find(job => !job.done);
    document.querySelectorAll('.module-collect').forEach(button => button.disabled = submitting || Boolean(current));
    document.querySelectorAll('.collect-remaining').forEach(button => {
      const id = button.dataset.product || product;
      const currentPlan = id === product ? plan : catalogPlans.get(id);
      const count = currentPlan ? currentPlan.modules.length : Number(button.dataset.remainingCount);
      button.dataset.remainingCount = String(count);
      button.disabled = submitting || Boolean(current) || count === 0;
      button.textContent = current?.mode === 'remaining' && current.product_id === id ? '正在采集所有…' : count ? (button.dataset.product ? `补采未完成模块（${count}）` : `采集所有（剩余 ${count} 项）`) : '已全部采集';
    });
    // 一项模块只展示最新任务，批量结果与旧单模块任务不会互相覆盖。
    const latest = new Map();
    jobs.forEach(job => cardsFor(job).forEach(card => {
      const previous = latest.get(card.module);
      if (!previous || `${card.created_at || ''}${card.id}` >= `${previous.created_at || ''}${previous.id}`) latest.set(card.module,card);
    }));
    latest.forEach(renderCard);
    bar.replaceChildren(); bar.hidden = !current;
    if (current) {
      const message = document.createElement('span');
      message.textContent = `商品 ${current.product_id} · ${jobLabel(current)} · ${states[current.state] || current.state}。${current.message}`;
      bar.append(message,link('查看商品',current.product_url),link('查看日志',current.logs_url));
      const stop = document.createElement('button'); stop.type = 'button'; stop.className = 'button action-button action-danger'; stop.textContent = '停止采集';
      stop.addEventListener('click',async () => {
        stop.disabled = true;
        try { await sendControl(current,'stop'); }
        catch (error) { message.textContent = error.message; stop.disabled = false; }
      });
      bar.append(stop); showPrompt(current);
    } else if (dialog.open) dialog.close();
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const active = await api('/api/collection-jobs/active');
      active.jobs.forEach(job => tracked.set(job.id,job));
      const pending = [...tracked.values()].filter(job => !job.done && !active.jobs.some(other => other.id === job.id));
      for (const job of pending) tracked.set(job.id,await api(endpoint(job)));
      if (product) {
        const recent = await api(`/api/collections/products/${product}/jobs`);
        plan = recent.plan || plan;
        recent.jobs.forEach(job => tracked.set(job.id,job));
      }
      for (const job of tracked.values()) {
        if (job.done && !refreshedCatalogJobs.has(job.id) && document.querySelector(`.collect-remaining[data-product="${job.product_id}"]`)) {
          const recent = await api(`/api/collections/products/${job.product_id}/jobs`);
          catalogPlans.set(job.product_id,recent.plan);
          refreshedCatalogJobs.add(job.id);
        }
      }
      bar.removeAttribute('data-error'); render();
    } catch (error) {
      bar.hidden = false; bar.textContent = `采集状态暂时无法读取：${error.message}。不会自动重新采集，请恢复连接后查看任务状态。`; bar.dataset.error = 'true';
    } finally { busy = false; }
  }

  document.querySelectorAll('.module-collect').forEach(button => button.addEventListener('click',async () => {
    const id = button.dataset.product || product;
    button.disabled = true;
    const status = button.closest('.card')?.querySelector('.module-job-status');
    try {
      const job = await api(`/api/collections/products/${id}/jobs`,{module:button.dataset.module});
      tracked.set(job.id,job); render();
    } catch (error) {
      if (status) status.textContent = error.message;
      else { bar.hidden = false; bar.textContent = error.message; }
      button.disabled = false;
    }
  }));
  document.querySelectorAll('.collect-remaining').forEach(button => button.addEventListener('click',async () => {
    submitting = true; render();
    const note = document.getElementById('collection-plan-status');
    try {
      const id = button.dataset.product || product;
      const job = await api(`/api/collections/products/${id}/jobs`,{mode:'remaining'});
      tracked.set(job.id,job);
      if (note) note.textContent = `本次计划采集 ${job.total_count} 项，跳过已完成的 ${job.skipped_modules.length} 项。任务在后台运行。`;
    } catch (error) {
      if (note) note.textContent = error.message;
    } finally {
      submitting = false; render(); await refresh();
    }
  }));
  // Escape 不隐式放行或停止采集，须明确选择当前提示的操作。
  dialog.addEventListener('cancel',event => event.preventDefault());
  refresh(); setInterval(refresh,2000);
})();
