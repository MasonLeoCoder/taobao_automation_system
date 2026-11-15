(() => {
  const root = document.getElementById('publish-categories');
  if (!root) return;

  const stateKey = 'publishCategoryTree';
  const handoffKey = 'publish-category-tree-handoff';
  const currentUrl = new URL(location.href);
  const selection = url => `${url.searchParams.get('branch') || ''}${url.hash}`;
  let state = history.state?.[stateKey] || {};
  let handoff;
  try {
    const pending = JSON.parse(sessionStorage.getItem(handoffKey));
    sessionStorage.removeItem(handoffKey);
    if (pending?.url === currentUrl.href && Date.now() - pending.time < 30000) {
      handoff = pending;
      state = pending.state;
    }
  } catch { /* 浏览器限制存储时仍可浏览目录 */ }

  let treeState = () => state;
  function rememberTree() {
    const saved = treeState();
    try { history.replaceState({...history.state, [stateKey]:saved}, ''); }
    catch { /* 浏览器历史不可写时仍可正常跳转 */ }
    return saved;
  }

  function keepTreeFor(url) {
    if (url.origin !== currentUrl.origin || url.pathname !== currentUrl.pathname || url.href === currentUrl.href) return;
    const saved = rememberTree();
    try {
      sessionStorage.setItem(handoffKey, JSON.stringify({url:url.href, from:selection(currentUrl), time:Date.now(), state:saved}));
    } catch { /* 页面打开后仍会定位当前类目 */ }
  }

  root.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[href]');
    if (link && !link.target) keepTreeFor(new URL(link.href));
  });
  root.querySelector('.category-filter')?.addEventListener('submit', event => {
    const url = new URL(event.currentTarget.action);
    url.search = new URLSearchParams(new FormData(event.currentTarget));
    keepTreeFor(url);
  });
  window.addEventListener('pagehide', rememberTree);

  const treeSearch = document.getElementById('category-tree-search');
  if (treeSearch) {
    const query = document.getElementById('category-tree-query');
    const clear = document.getElementById('category-tree-clear');
    const tree = document.getElementById('category-tree-branches');
    const matches = document.getElementById('category-tree-matches');
    const status = document.getElementById('category-tree-search-status');
    const currentBranch = tree.querySelector('[aria-current="location"]');
    const nodes = [...tree.querySelectorAll('.category-node')].map(node => [node, node.querySelector('a').dataset.categoryPath]);
    const leafLinks = document.getElementById('category-tree-leaves')?.content.querySelectorAll('[data-category-path]') ?? [];
    const currentLeaf = currentUrl.hash && [...leafLinks].find(link => link.getAttribute('href').endsWith(currentUrl.hash));
    const currentLink = currentLeaf || currentBranch;
    let treePosition = 0;
    const normalize = text => text.normalize('NFKC').toLowerCase().trim().replace(/\s*[>›]\s*/gu, ' > ');
    const categories = [...tree.querySelectorAll('[data-category-path]'), ...leafLinks].map(link => {
      const path = link.dataset.categoryPath;
      const parts = path.split(' > ');
      const name = parts.pop();
      return {link, name, parents:parts.join(' › '), nameKey:normalize(name), pathKey:normalize(path)};
    });

    function filterTree() {
      const keyword = normalize(query.value);
      clear.hidden = !query.value;
      if (!tree.hidden) treePosition = tree.scrollTop;
      tree.hidden = Boolean(keyword);
      status.hidden = !keyword;
      matches.replaceChildren();
      if (!keyword) {
        matches.hidden = true;
        status.textContent = '';
        tree.scrollTop = treePosition;
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
        if (link === currentLink) result.setAttribute('aria-current', 'location');
        else result.removeAttribute('aria-current');
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

    function reveal(container, item) {
      if (!item || container.hidden) return;
      const bounds = container.getBoundingClientRect(), target = item.getBoundingClientRect();
      const top = bounds.top + container.clientTop;
      if (target.top < top || target.bottom > top + container.clientHeight) {
        container.scrollTop += target.top - top - (container.clientHeight - target.height) / 2;
      }
    }

    function openCurrentBranch() {
      for (let node = currentBranch?.closest('.category-node'); node; node = node.parentElement.closest('.category-node')) {
        node.open = true;
      }
    }

    function clearTreeSearch() {
      query.value = '';
      filterTree();
      openCurrentBranch();
      if (currentBranch?.dataset.categoryPath) reveal(tree, currentBranch);
      query.focus({preventScroll:true});
    }

    function searchKeys(event) {
      if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.key === 'Escape' && query.value) {
        event.preventDefault();
        clearTreeSearch();
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        const links = [...matches.querySelectorAll('a')];
        if (!links.length) return;
        const index = links.indexOf(event.target);
        const next = event.key === 'ArrowDown' ? Math.min(index + 1, links.length - 1) :
          index < 0 ? links.length - 1 : Math.max(index - 1, 0);
        event.preventDefault();
        links[next].focus({preventScroll:true});
        reveal(matches, links[next]);
      }
    }

    query.addEventListener('input', filterTree);
    query.addEventListener('keydown', searchKeys);
    matches.addEventListener('keydown', searchKeys);
    clear.addEventListener('click', clearTreeSearch);
    if (Array.isArray(state.open)) {
      const open = new Set(state.open);
      for (const [node, path] of nodes) node.open = open.has(path);
    }
    openCurrentBranch();
    query.value = typeof state.query === 'string' ? state.query : '';
    filterTree();
    treeSearch.hidden = false;
    treePosition = state.treePosition || 0;
    tree.scrollTop = treePosition;
    matches.scrollTop = state.matchPosition || 0;
    const currentMatch = matches.querySelector('[aria-current="location"]');
    if (handoff && handoff.from !== selection(currentUrl) && !currentMatch) {
      query.value = '';
      filterTree();
    }
    const back = performance.getEntriesByType('navigation')[0]?.type === 'back_forward';
    if (!back || handoff) {
      if (tree.hidden) reveal(matches, currentMatch);
      else if (currentBranch?.dataset.categoryPath) reveal(tree, currentBranch);
    }
    treeState = () => ({query:query.value, treePosition:tree.hidden ? treePosition : tree.scrollTop,
      matchPosition:matches.scrollTop, open:nodes.filter(([node]) => node.open).map(([, path]) => path)});
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
    const source = state.target?.login_name ? `${state.target.shop_name} · ${state.target.login_name} · ` : '';
    progress.textContent = state.message ? source + state.message : '';
    prompt = state.prompt;
    resume.hidden = !prompt;
    if (active) {
      activeId = state.id;
      timer = setTimeout(poll,1000);
    } else if (state.id === activeId) {
      rememberTree();
      location.reload();
    }
  }

  async function poll() {
    try { render(await api(root.dataset.status)); }
    catch (error) { notice.hidden = false; progress.textContent = error.message; timer = setTimeout(poll,2500); }
  }

  async function start(scopes, target = {}) {
    clearTimeout(timer);
    controls.forEach(control => { control.disabled = true; });
    try {
      render(await api(root.dataset.scan,{scopes:[...new Set(scopes)], ...target}));
      return true;
    }
    catch (error) {
      if (scanError && confirmation.open) {
        scanError.textContent = error.message;
        scanError.hidden = false;
      } else {
        notice.hidden = false;
        progress.textContent = error.message;
      }
      controls.forEach(control => { control.disabled = false; });
      return false;
    }
  }

  const confirmation = document.getElementById('category-confirm');
  const form = document.getElementById('category-scan-form');
  const scanSubmit = document.getElementById('category-scan-submit');
  const scanError = document.getElementById('category-scan-error');
  const managed = Boolean(form?.elements.shop_id);
  let scopes = [], submitting = false;
  function confirmScan(paths) {
    if (managed) {
      scopes = paths;
      form.elements.shop_id.value = '';
      form.elements.shop_id.dispatchEvent(new Event('change',{bubbles:true}));
      document.getElementById('category-confirm-title').textContent = paths.length ? '重采此分支' : '刷新一级类目';
      document.getElementById('category-confirm-scope').textContent = paths.length ? paths.join('、') : '读取发品页可见的一级类目，结果用于公共类目目录。';
      scanSubmit.textContent = paths.length ? '开始采集分支' : '开始刷新一级类目';
      scanError.hidden = true;
    }
    confirmation.returnValue = '';
    confirmation.showModal();
  }
  document.getElementById('category-full')?.addEventListener('click',() => confirmScan([]));
  confirmation?.addEventListener('close',() => {
    if (!managed && confirmation.returnValue === 'confirm') start([]);
  });
  function updateSubmit() {
    scanSubmit.disabled = submitting || !form.elements.shop_id.value || !form.elements.account_id.value;
  }
  if (managed) {
    form.addEventListener('change',event => {
      if (event.target.name === 'shop_id') window.updateManagedAccounts(form, 'category');
      if (['shop_id','account_id'].includes(event.target.name)) updateSubmit();
    });
    form.addEventListener('submit',async event => {
      if (event.submitter?.value === 'cancel') return;
      event.preventDefault();
      if (scanSubmit.disabled) return;
      submitting = true;
      updateSubmit();
      scanError.hidden = true;
      const started = await start(scopes, {shop_id:form.elements.shop_id.value,account_id:form.elements.account_id.value});
      submitting = false;
      updateSubmit();
      if (started && confirmation.open) confirmation.close();
    });
  }
  root.querySelectorAll('[data-retry],[data-retry-scopes]').forEach(button => {
    button.addEventListener('click',() => {
      const paths = button.dataset.retry ? [button.dataset.retry] : JSON.parse(button.dataset.retryScopes);
      if (managed) confirmScan(paths);
      else start(paths);
    });
  });
  resume.addEventListener('click',async () => {
    resume.disabled = true;
    try { await api(root.dataset.resume,{prompt}); }
    catch (error) { progress.textContent = error.message; }
    finally { resume.disabled = false; }
  });
  poll();
})();
