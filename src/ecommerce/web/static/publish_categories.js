(() => {
  const root = document.getElementById('publish-categories');
  if (!root) return;
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const progress = document.getElementById('category-progress');
  const notice = document.getElementById('category-live');
  const resume = document.getElementById('category-resume');
  const controls = root.querySelectorAll('#category-pair button,#category-full,[data-retry],[data-retry-scopes]');
  let activeId = null;
  let prompt = null;
  let timer;

  async function api(url, data) {
    const options = data === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json','X-Collection-Token':token}, body:JSON.stringify(data)
    };
    const response = await fetch(url,options);
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.error || '操作未完成，请刷新后重试。');
    }
    return response.json();
  }

  function render(state) {
    clearTimeout(timer);
    if (!state) return;
    const active = ['queued','running','waiting'].includes(state.phase);
    controls.forEach(control => { control.disabled = active; });
    notice.hidden = !state.message;
    progress.textContent = state.message || '';
    prompt = state.prompt;
    resume.hidden = !prompt;
    if (active) {
      activeId = state.id;
      timer = setTimeout(poll,1000);
    } else if (state.id === activeId) {
      location.reload();
    }
  }

  async function poll() {
    try { render(await api(root.dataset.status)); }
    catch (error) { notice.hidden = false; progress.textContent = error.message; timer = setTimeout(poll,2500); }
  }

  async function start(scopes) {
    clearTimeout(timer);
    controls.forEach(control => { control.disabled = true; });
    try { render(await api(root.dataset.scan,{scopes:[...new Set(scopes)]})); }
    catch (error) {
      notice.hidden = false;
      progress.textContent = error.message;
      controls.forEach(control => { control.disabled = false; });
    }
  }

  document.getElementById('category-pair')?.addEventListener('submit',event => {
    event.preventDefault();
    start([event.currentTarget.dataset.scope,document.getElementById('category-second').value]);
  });
  const confirmation = document.getElementById('category-confirm');
  document.getElementById('category-full')?.addEventListener('click',() => {
    confirmation.returnValue = '';
    confirmation.showModal();
  });
  confirmation?.addEventListener('close',() => {
    if (confirmation.returnValue === 'confirm') start([]);
  });
  root.querySelectorAll('[data-retry],[data-retry-scopes]').forEach(button => {
    button.addEventListener('click',() => start(button.dataset.retry ?
      [button.dataset.retry] : JSON.parse(button.dataset.retryScopes)));
  });
  resume.addEventListener('click',async () => {
    resume.disabled = true;
    try { await api(root.dataset.resume,{prompt}); }
    catch (error) { progress.textContent = error.message; }
    finally { resume.disabled = false; }
  });
  poll();
})();
