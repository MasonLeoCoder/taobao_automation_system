(() => {
  const root = document.getElementById('publish-categories');
  if (!root) return;

  const treeSearch = document.getElementById('category-tree-search');
  if (treeSearch) {
    const query = document.getElementById('category-tree-query');
    const clear = document.getElementById('category-tree-clear');
    const tree = document.getElementById('category-tree-branches');
    const matches = document.getElementById('category-tree-matches');
    const status = document.getElementById('category-tree-search-status');
    const normalize = text => text.normalize('NFKC').toLowerCase().trim().replace(/\s*[>›]\s*/gu, ' > ');
    const leafLinks = document.getElementById('category-tree-leaves')?.content.querySelectorAll('[data-category-path]') ?? [];
    const categories = [...tree.querySelectorAll('[data-category-path]'), ...leafLinks].map(link => {
      const path = link.dataset.categoryPath;
      const parts = path.split(' > ');
      const name = parts.pop();
      return {link, name, parents:parts.join(' › '), nameKey:normalize(name), pathKey:normalize(path)};
    });

    function filterTree() {
      const keyword = normalize(query.value);
      clear.hidden = !query.value;
      tree.hidden = Boolean(keyword);
      status.hidden = !keyword;
      matches.replaceChildren();
      if (!keyword) {
        matches.hidden = true;
        status.textContent = '';
        return;
      }
      const byName = [], byPath = [];
      for (const category of categories) {
        if (category.nameKey.includes(keyword)) byName.push(category);
        else if (category.pathKey.includes(keyword)) byPath.push(category);
      }
      const results = [...byName, ...byPath];
      const fragment = document.createDocumentFragment();
      for (const {link, name, parents} of results) {
        const item = document.createElement('li');
        const result = link.cloneNode(false);
        result.removeAttribute('data-category-path');
        result.classList.add('category-tree-match');
        const title = document.createElement('strong');
        title.textContent = name;
        result.append(title);
        if (parents) {
          const path = document.createElement('small');
          path.textContent = parents;
          result.append(path);
        }
        item.append(result);
        fragment.append(item);
      }
      matches.append(fragment);
      matches.hidden = !results.length;
      matches.scrollTop = 0;
      status.textContent = results.length ? `找到 ${results.length} 个类目` : '未找到匹配的类目';
    }

    function clearTreeSearch() {
      query.value = '';
      filterTree();
      query.focus();
    }
    query.addEventListener('input', filterTree);
    query.addEventListener('keydown', event => {
      if (event.key === 'Escape' && query.value && !event.isComposing) {
        event.preventDefault();
        clearTreeSearch();
      }
    });
    clear.addEventListener('click', clearTreeSearch);
    filterTree();
    treeSearch.hidden = false;
  }

  const token = document.querySelector('meta[name="workbench-token"]').content;
  const progress = document.getElementById('category-progress');
  const notice = document.getElementById('category-live');
  const resume = document.getElementById('category-resume');
  const controls = root.querySelectorAll('#category-full,[data-retry],[data-retry-scopes]');
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
