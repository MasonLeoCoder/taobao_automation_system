/* 网页采集只派发短请求；浏览器工作由独立任务完成。 */
(() => {
  const client = document.getElementById('collection-client');
  if (!client) return;
  const product = document.querySelector('[data-collection-product]')?.dataset.collectionProduct;
  const workspace = document.querySelector('[data-collection-workspace]');
  const bar = document.getElementById('collection-task-bar');
  const dialog = document.getElementById('collection-attention');
  const labels = {basic_info:'基本信息',video:'视频',main_images:'主图',skus:'SKU',parameters:'商品参数',detail_images:'详情图片',review_summary:'评价概览',reviews:'评价明细',questions:'问大家'};
  const states = {queued:'启动中',running:'采集中',waiting:'等待采集',not_run:'本次未执行',skipped:'已跳过',needs_attention:'需要人工处理',storage_pending:'等待入库',succeeded:'采集完成',failed:'采集失败',cancelled:'已停止',interrupted:'进程已退出',unverified:'进程待核实',verification_pending:'结果待核对'};
  const tracked = new Map();
  let plan = null;
  const catalogPlans = new Map();
  const refreshedCatalogJobs = new Set();
  const submittedPrompts = new Set();
  let promptJob = null;
  let busy = false;
  let submitting = false;
  let stateKnown = false;
  const endpoint = job => job.status_url || `/api/collection-jobs/${job.id}`;
  const targetLabel = job => job.mode === 'keyword' ? `关键词“${job.options.keyword}”` : `商品 ${job.product_id}`;
  const stateLabel = job => job.halted ? '采集已中断（保留现场）' : job.skipped ? '已跳过（未找到入口）' : job.state === 'succeeded' && job.skipped_count ? '已结束（含跳过项目）' : states[job.state] || job.state;

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
    status.textContent = `${stateLabel(job)}：${job.message}`;
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
    if (!['remaining','detail'].includes(job.mode)) return [job];
    return (job.modules || []).map(module => {
      const item = (job.results || []).find(result => result.module === module) || {};
      const started = (job.started_modules || []).includes(module);
      const current = job.current_module === module;
      const state = item.skipped ? 'skipped' : item.valid ? 'succeeded' : current && !job.done ? job.state : job.done ? (started ? job.state : 'not_run') : 'waiting';
      return {...job,module,state,halted:Boolean(job.halted && current && !item.valid),skipped:Boolean(item.skipped),skipped_count:item.skipped ? 1 : 0,batch_id:item.batch_id,valid:Boolean(item.valid),result_url:item.result_url,
        result_label:item.label || '查看资料',
        message:item.skipped ? item.message || '未找到问大家入口，本次已跳过；下次任务会重新寻找' : item.valid ? '本模块已保存并确认入库' : job.halted && current ? job.message : job.done ? (item.batch_id ? '本次仅保留采集尝试，可重新采集' : '本次未保存有效结果，可重新采集') : current ? '正在采集本模块' : '等待当前任务依次采集'};
    });
  }

  function jobLabel(job) {
    if (job.mode === 'keyword') {
      const progress = job.progress || {};
      return `列表采集 · 已保存 ${progress.completed_page_count || 0}/${job.options.pages} 页 · ${progress.collected_count || 0} 条商品记录${!job.done && progress.current_page ? ` · 当前第 ${progress.current_page} 页` : ''}${job.options.with_details ? ` · 详情完成 ${progress.detail_completed_count || 0} 个商品` : ''}${progress.current_product ? ` · 当前商品 ${progress.current_product}` : ''}${progress.current_module ? ` · ${labels[progress.current_module]}` : ''}`;
    }
    if (job.mode === 'detail') return `详情采集 · 已完成 ${job.completed_count}/${job.total_count} 项${job.skipped_count ? ` · 本次跳过 ${job.skipped_count} 项` : ''}${job.current_module ? ` · 当前：${labels[job.current_module]}` : ''}`;
    if (job.mode !== 'remaining') return labels[job.module] || job.module;
    return `采集所有 · 已完成 ${job.completed_count}/${job.total_count} 项 · 跳过 ${(job.skipped_modules || []).length} 项${job.skipped_count ? ` · 本次入口缺失跳过 ${job.skipped_count} 项` : ''}${job.current_module ? ` · 当前：${labels[job.current_module]}` : ''}`;
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
    document.getElementById('collection-attention-product').textContent = `${targetLabel(job)} · ${jobLabel(job)}`;
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
    const submit = document.getElementById('collection-submit');
    if (submit) {
      submit.disabled = submitting || Boolean(current) || !stateKnown || submit.closest('fieldset').disabled;
      submit.textContent = submitting ? '正在提交…' : current ? '已有任务进行中' : '开始采集';
    }
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
      const key = `${card.product_id}:${card.module}`;
      const previous = latest.get(key);
      if (!previous || `${card.created_at || ''}${card.id}` >= `${previous.created_at || ''}${previous.id}`) latest.set(key,card);
    }));
    latest.forEach(renderCard);
    bar.replaceChildren(); bar.hidden = !current;
    if (current) {
      const message = document.createElement('span');
      message.textContent = `${targetLabel(current)} · ${jobLabel(current)} · ${stateLabel(current)}。${current.message}`;
      bar.append(message);
      if (current.product_url) bar.append(link('查看商品',current.product_url));
      bar.append(link('查看日志',current.logs_url));
      const stop = document.createElement('button'); stop.type = 'button'; stop.className = 'button action-button action-danger'; stop.textContent = '停止采集';
      stop.addEventListener('click',async () => {
        stop.disabled = true;
        try { await sendControl(current,'stop'); }
        catch (error) { message.textContent = error.message; stop.disabled = false; }
      });
      bar.append(stop); showPrompt(current);
    } else if (dialog.open) dialog.close();
    renderHistory(jobs);
  }

  function renderHistory(jobs) {
    const container = document.getElementById('collection-job-list');
    if (!container || !stateKnown) return;
    container.replaceChildren();
    document.getElementById('collection-history-status').textContent = jobs.length ? '最近 20 个网页采集任务；每次采集的结果与日志独立保留。' : '暂无网页采集任务。配置上方参数后即可开始。';
    jobs.sort((a,b) => `${b.created_at}${b.id}`.localeCompare(`${a.created_at}${a.id}`)).slice(0,20).forEach(job => {
      const article = document.createElement('article'); article.className = 'collection-job'; article.dataset.jobId = job.id;
      const title = document.createElement('h3'); title.textContent = `${targetLabel(job)} · ${stateLabel(job)}`;
      const progress = document.createElement('p'); progress.textContent = jobLabel(job);
      const message = document.createElement(job.message.length > 180 ? 'details' : 'p');
      if (job.message.length > 180) {
        const summary = document.createElement('summary'); summary.textContent = '查看任务说明';
        const text = document.createElement('p'); text.textContent = job.message; message.append(summary,text);
      } else message.textContent = job.message;
      const time = document.createElement('small'); time.textContent = new Date(job.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
      const actions = document.createElement('div'); actions.className = 'module-actions';
      actions.append(link('查看日志',job.logs_url));
      if (job.result_url) actions.append(link(job.done && job.valid ? '查看采集结果' : '查看已有资料',job.result_url,'action-primary'));
      if (!job.done) {
        const stop = document.createElement('button'); stop.type = 'button'; stop.className = 'button action-button action-danger'; stop.textContent = '停止采集';
        stop.addEventListener('click',async () => {
          stop.disabled = true;
          try { await sendControl(job,'stop'); } catch (error) { message.textContent = error.message; stop.disabled = false; }
        }); actions.append(stop);
      }
      article.append(title,time,progress);
      if (job.options) {
        const configuration = document.createElement('p'); configuration.className = 'collection-job-configuration';
        configuration.textContent = job.mode === 'keyword'
          ? `本次设置：${job.options.pages} 页 · 每页${job.options.limit === null ? '不限数量' : `最多 ${job.options.limit} 个商品`} · ${job.options.with_details ? '连带九个详情模块' : '仅采集列表'}`
          : `本次设置：${job.modules.map(module => labels[module]).join('、')}${job.modules.includes('reviews') ? ` · 评价${job.options.all_reviews ? '不限数量' : '最多 100 条'}` : ''}${job.modules.includes('questions') ? ` · 问大家${job.options.all_questions ? '不限数量' : '最多 50 题'}` : ''}`;
        article.append(configuration);
      }
      article.append(message,actions); container.append(article);
    });
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const active = await api('/api/collection-jobs/active');
      active.jobs.forEach(job => tracked.set(job.id,job));
      if (workspace) {
        const history = await api('/api/collection-jobs');
        history.jobs.forEach(job => tracked.set(job.id,job));
      }
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
      stateKnown = true;
      bar.removeAttribute('data-error'); render();
    } catch (error) {
      stateKnown = false;
      const submit = document.getElementById('collection-submit');
      if (submit) submit.disabled = true;
      const historyStatus = document.getElementById('collection-history-status');
      if (historyStatus) historyStatus.textContent = `任务状态暂时无法读取：${error.message}`;
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
  function setupForm() {
    const form = document.getElementById('collection-start-form');
    if (!form) return;
    const get = id => document.getElementById(`collection-${id}`);
    let mode = 'keyword';
    const modules = [...form.querySelectorAll('[name="modules"]')];
    function update() {
      ['keyword','detail'].forEach(name => {
        const selected = mode === name;
        const panel = document.getElementById(`${name}-panel`); panel.hidden = !selected;
        panel.querySelectorAll('input,select').forEach(input => input.disabled = !selected);
        const tab = document.getElementById(`${name}-tab`); tab.setAttribute('aria-selected',String(selected)); tab.tabIndex = selected ? 0 : -1;
      });
      get('limit').disabled = mode !== 'keyword' || get('unlimited').checked;
      get('limit').required = !get('limit').disabled;
      get('detail-order').hidden = !get('with-details').checked;
      get('review-range').disabled = mode !== 'detail' || !modules.find(node => node.value === 'reviews').checked;
      get('question-range').disabled = mode !== 'detail' || !modules.find(node => node.value === 'questions').checked;
      const selected = modules.filter(node => node.checked);
      get('select-all').textContent = selected.length === modules.length ? '取消全选' : '全选';
      get('request-summary').textContent = mode === 'keyword'
        ? `关键词：${get('keyword').value.trim() || '待填写'} · 从第 1 页开始采集 ${get('pages').value || '待填写'} 页 · ${get('unlimited').checked ? '每页不限数量' : `每页最多 ${get('limit').value || '待填写'} 个商品`} · ${get('with-details').checked ? '连带采集九个详情模块' : '仅采集列表'}`
        : `商品：${get('input').value.trim() || '待填写'} · ${selected.length ? selected.map(node => labels[node.value]).join('、') : '尚未选择模块'}${!get('review-range').disabled ? ` · 评价${get('review-range').value === 'all' ? '不限数量' : '最多 100 条'}` : ''}${!get('question-range').disabled ? ` · 问大家${get('question-range').value === 'all' ? '不限数量' : '最多 50 题'}` : ''}`;
    }
    document.querySelectorAll('[data-mode-tab]').forEach(tab => {
      tab.addEventListener('click',() => { mode = tab.dataset.modeTab; update(); });
      tab.addEventListener('keydown',event => {
        if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
        event.preventDefault(); mode = event.key === 'Home' ? 'keyword' : event.key === 'End' ? 'detail' : mode === 'keyword' ? 'detail' : 'keyword';
        update(); document.getElementById(`${mode}-tab`).focus();
      });
    });
    get('select-all').addEventListener('click',() => { const selected = modules.every(node => node.checked); modules.forEach(node => node.checked = !selected); update(); });
    form.addEventListener('input',update); form.addEventListener('change',update);
    const parameters = new URLSearchParams(location.search);
    if (parameters.get('mode') === 'detail' || parameters.has('input')) {
      mode = 'detail'; get('input').value = parameters.get('input') || '';
      const selected = parameters.get('modules');
      if (selected && modules.some(node => node.value === selected)) modules.forEach(node => node.checked = node.value === selected);
    }
    form.addEventListener('submit',async event => {
      event.preventDefault();
      if (submitting || !stateKnown || [...tracked.values()].some(job => !job.done)) return;
      const selected = modules.filter(node => node.checked).map(node => node.value);
      if (mode === 'detail' && !selected.length) { get('submit-status').textContent = '请至少选择一个采集模块。'; return; }
      const body = mode === 'keyword' ? {mode,keyword:get('keyword').value,pages:Number(get('pages').value),limit:get('unlimited').checked ? null : Number(get('limit').value),with_details:get('with-details').checked}
        : {mode,input:get('input').value,modules:selected,all_reviews:!get('review-range').disabled && get('review-range').value === 'all',all_questions:!get('question-range').disabled && get('question-range').value === 'all'};
      submitting = true; render(); get('submit-status').textContent = '正在提交采集任务…';
      try {
        const job = await api('/api/collection-jobs',body); tracked.set(job.id,job);
        get('submit-status').textContent = '任务已提交，后台开始运行。下方可查看进度和结果。';
      } catch (error) { get('submit-status').textContent = error.message; }
      finally { submitting = false; render(); await refresh(); }
    });
    update(); render();
  }
  setupForm();
  // Escape 不隐式放行或停止采集，须明确选择当前提示的操作。
  dialog.addEventListener('cancel',event => event.preventDefault());
  refresh(); setInterval(refresh,2000);
})();
