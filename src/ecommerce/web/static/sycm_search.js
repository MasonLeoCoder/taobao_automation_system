(() => {
  const root = document.getElementById('sycm-rank');
  if (!root || !document.getElementById('sycm-filters')) return;
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const form = document.getElementById('sycm-start');
  const dialog = document.getElementById('sycm-dialog');
  const open = document.getElementById('sycm-new');
  const formError = document.getElementById('sycm-start-error');
  const box = document.getElementById('sycm-progress-box');
  const progress = document.getElementById('sycm-progress');
  const resume = document.getElementById('sycm-resume');
  const task = document.getElementById('sycm-task');
  const result = document.getElementById('sycm-result');
  const controls = root.querySelectorAll('#sycm-new,#sycm-start button[type="submit"],[data-sycm-retry]');
  let prompt, activeId, timer, starting = false;

  async function api(url, data) {
    const options = data === undefined ? {} : {method:'POST', headers:{
      'Content-Type':'application/json','X-Collection-Token':token},body:JSON.stringify(data)};
    const response = await fetch(url,options);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || '操作未完成，请刷新后重试。');
    }
    return response.json();
  }

  function show(message) {
    box.hidden = !message;
    progress.textContent = message || '';
  }

  function render(state) {
    clearTimeout(timer);
    if (!state || starting) return;
    const active = ['queued','running','waiting'].includes(state.phase);
    controls.forEach(button => { button.disabled = active; });
    const relevant = String(state.category_id) === root.dataset.category && state.period === root.dataset.period;
    const settled = !active && state.phase === 'succeeded' && state.id === root.dataset.batch;
    show(active || state.id === activeId || (relevant && !settled) ? state.message : '');
    prompt = state.prompt;
    resume.hidden = !prompt;
    task.hidden = !state.task_id;
    result.hidden = active || state.phase !== 'succeeded' || settled;
    if (state.task_id) task.href = root.dataset.tasks + '/' + encodeURIComponent(state.task_id);
    if (active) {
      activeId = state.id;
      timer = setTimeout(poll,1200);
    } else if (!result.hidden) {
      const url = new URL(root.dataset.index,location.origin);
      url.search = new URLSearchParams({category:state.category_id,period:state.period,batch:state.id});
      result.href = url;
    }
  }

  async function poll() {
    try { render(await api(root.dataset.status)); }
    catch (error) { show(error.message); timer = setTimeout(poll,2500); }
  }

  async function start(values) {
    if (starting) return;
    starting = true;
    clearTimeout(timer);
    controls.forEach(button => { button.disabled = true; });
    try {
      const state = await api(root.dataset.start,values);
      starting = false;
      activeId = state.id;
      if (dialog?.open) dialog.close();
      render(state);
    }
    catch (error) {
      starting = false;
      if (dialog?.open) showFormError(error.message);
      else show(error.message);
      controls.forEach(button => { button.disabled = false; });
      timer = setTimeout(poll,2500);
    }
  }

  function showFormError(message) {
    formError.textContent = message;
    formError.hidden = !message;
  }

  function describeSelection() {
    const values = new FormData(form);
    const count = values.getAll('word_types').length;
    const pages = Number(values.get('pages'));
    document.getElementById('sycm-estimate').textContent = Number.isInteger(pages) && pages > 0
      ? `已选 ${count} 种词，最多采集 ${count * pages} 页`
      : `已选 ${count} 种词，请填写每种词的页数`;
    const owner = form.elements.category_id.selectedOptions[0]?.textContent.trim() || '';
    const source = String(values.get('sycm_path') || '').split('>').map(part => part.trim()).join(' > ');
    const distinct = Boolean(source && source !== owner);
    document.getElementById('sycm-source-hint').textContent = distinct ? '已指定其他统计类目' : '采集来源与千牛类目一致';
    document.getElementById('sycm-scope-summary').hidden = !distinct;
    document.getElementById('sycm-owner').textContent = owner;
    document.getElementById('sycm-source').textContent = source;
  }

  open?.addEventListener('click',() => {
    showFormError('');
    describeSelection();
    dialog.showModal();
  });
  dialog?.querySelectorAll('[data-ra-close],[data-sycm-close]').forEach(button =>
    button.addEventListener('click',() => dialog.close()));
  form?.addEventListener('input',describeSelection);
  form?.addEventListener('change',describeSelection);
  form?.addEventListener('submit',event => {
    event.preventDefault();
    const values = new FormData(form);
    const word_types = values.getAll('word_types');
    if (!word_types.length) { showFormError('请至少勾选一种词类型。'); return; }
    showFormError('');
    start({category_id:Number(values.get('category_id')),period:values.get('period'),
      pages:Number(values.get('pages')),word_types,sycm_path:values.get('sycm_path')});
  });
  const filters = document.getElementById('sycm-filters');
  filters.addEventListener('change',() => {
    const batch = filters.elements.batch;
    if (!batch) return;
    const scope = filters.elements.category.value + ':' + filters.elements.period.value;
    batch.disabled = scope !== filters.elements.filter_scope.value;
    if (batch.disabled) batch.value = '';
  });
  root.querySelectorAll('[data-sycm-retry]').forEach(button =>
    button.addEventListener('click',() => start(JSON.parse(button.dataset.sycmRetry))));
  resume.addEventListener('click',async () => {
    resume.disabled = true;
    try { await api(root.dataset.resume,{prompt}); }
    catch (error) { show(error.message); }
    finally { resume.disabled = false; }
  });
  poll();
})();
