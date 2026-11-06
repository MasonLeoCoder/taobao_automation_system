(() => {
  'use strict';
  const root = document.querySelector('#listing');
  if (!root) return;
  const node = name => document.getElementById(`listing-${name}`);
  const base = root.dataset.api.replace(/\/preflight$/, '');
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const labels = Object.fromEntries([...node('mapping').querySelectorAll('[data-material]')].map(e => [e.dataset.material, e.dataset.label]));
  const aliases = {main:['1-1主图','1:1主图','1比1主图'], portrait:['3-4主图','3:4主图','3比4主图'], detail:['详情图'], sku:['sku图','sku图片']};
  const active = ['queued','running','waiting'];
  const phases = {queued:'等待启动', running:'执行中', waiting:'等待人工处理', succeeded:'已完成', failed:'执行失败', cancelled:'已结束', interrupted:'执行中断', needs_attention:'需要人工核对'};
  let fileMap = new Map(), dirs = new Map(), id = null, timer, busy = false, sentPrompt, seenPrompt, currentPrompt;
  let logOwner = null, logBusy = false, logAgain = false, logReady = false, oldest = null, latest = 0, hasOlder = false;

  async function api(url, body) {
    const options = body === undefined ? {} : {method:'POST', headers:{'X-Collection-Token':token}};
    if (body instanceof FormData) options.body = body;
    else if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    const response = await fetch(url, options);
    let data;
    try { data = await response.json(); } catch { throw new Error('无法读取服务器结果，请重试。'); }
    if (!response.ok && !Array.isArray(data.errors)) throw new Error(data.error || '操作失败，请重试。');
    return data;
  }

  function remember(value) {
    id = value;
    const url = new URL(location.href);
    if (value) url.searchParams.set('job', value); else url.searchParams.delete('job');
    history.replaceState(null, '', url);
  }

  function element(tag, text) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function renderPrompt(task) {
    const terminal = ['needs_attention','failed','interrupted'].includes(task?.phase);
    const prompt = task?.prompt || (terminal ? {id:`terminal-${id}`, kind:'terminal', actions:[], evidence:task.evidence} : null);
    const dialog = node('manual-dialog');
    currentPrompt = prompt;
    node('manual-open').hidden = !prompt;
    if (!prompt) {
      if (dialog.open) dialog.close();
      return;
    }
    if (seenPrompt === prompt.id) return;
    seenPrompt = prompt.id;
    const titles = {login:'等待千牛扫码登录', native:'需要决定原生弹窗', review:'请核对填写差异',
      receipt:'请核对提交结果', finish:'填写完成，等待检查', terminal:'任务已暂停或结束，请核对', manual:'需要人工处理'};
    node('manual-dialog-title').textContent = titles[prompt.kind] || titles.manual;
    node('manual-message').textContent = task.message + (terminal && !task.prompt ? '\n当前任务已结束，不能从该步骤自动继续。' : '');
    const diff = node('manual-differences');
    diff.replaceChildren(...(prompt.differences || []).map(value => element('li',value)));
    diff.hidden = !diff.childElementCount;
    node('manual-evidence').hidden = !prompt.evidence;
    if (prompt.evidence) node('manual-evidence').href = `${base}/${id}/evidence/${prompt.evidence}`;
    node('manual-note').textContent = '';
    const buttons = node('manual-actions');
    buttons.replaceChildren();
    for (const action of prompt.actions) {
      const words = {continue:'完成',confirm:'确认弹窗',cancel:'取消弹窗',approve:'确认差异并提交到仓库',fill_only:'保留填写，不提交',finish:task.outcome === 'uncertain' ? '结束，保留待核实状态' : '结束检查'};
      const button = element('button',words[action]);
      button.className = `ui-button ${['approve','continue','confirm'].includes(action) ? 'ui-primary' : 'ui-secondary'}`;
      button.type = 'button'; button.disabled = sentPrompt === prompt.id;
      button.addEventListener('click',() => sendFeedback(prompt.id, action)); buttons.append(button);
    }
    if (!dialog.open) dialog.showModal();
  }

  function logRow(event) {
    const row = element('li');
    const level = {INFO:'信息', WARNING:'警告', ERROR:'错误', CRITICAL:'严重错误', DEBUG:'调试'}[event.level] || '信息';
    const time = element('time',event.time ? event.time.replace('T',' ').slice(0,19) : '—');
    if (event.time) time.dateTime = event.time;
    const message = element('p',`${level}${event.step ? ' · ' + event.step : ''}：${event.message}`);
    message.className = 'log-message'; row.append(time,message);
    row.className = `log-row-${['WARNING','ERROR','CRITICAL'].includes(event.level) ? event.level.toLowerCase() : 'info'}`;
    return row;
  }

  async function loadLogs(older=false) {
    if (!id || node('task').hidden || (older && (!hasOlder || oldest === null))) return;
    if (logBusy) {
      if (!older) logAgain = true;
      return;
    }
    const requested = id;
    logOwner = requested; logBusy = true;
    node('log-older').disabled = true;
    const scroller = node('log-scroll'), rows = node('log-rows');
    const stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 28;
    try {
      let more;
      do {
        const query = older ? `?before=${oldest}` : logReady ? `?after=${latest}` : '';
        const data = await api(`${base}/${requested}/logs${query}`);
        if (id !== requested || logOwner !== requested) return;
        const wasReady = logReady;
        if (data.events.length) {
          const records = data.events.map(logRow);
          if (older) rows.prepend(...records); else rows.append(...records);
          if (oldest === null || older) oldest = data.previous_cursor;
          if (!older) latest = data.next_cursor;
        }
        if (!wasReady || older) hasOlder = data.has_previous;
        logReady = true;
        more = !older && data.has_more && data.events.length > 0;
      } while (more);
      node('log-note').textContent = rows.childElementCount ? '' : '等待任务日志…';
      if (older) scroller.scrollTop = 0;
      else if (stick) scroller.scrollTop = scroller.scrollHeight;
    } catch (error) {
      if (id === requested) node('log-note').textContent = `暂时无法读取日志：${error.message}`;
    } finally {
      if (logOwner === requested) {
        logBusy = false;
        node('log-older').disabled = false;
        node('log-older').hidden = !hasOlder;
        if (logAgain) {
          logAgain = false;
          void loadLogs();
        }
      }
    }
  }

  function checkTitles() {
    if (!node('titles')) return true;
    let valid = true;
    for (const [key,limit] of [['title',60],['guide',30]]) {
      const input = node(key);
      const used = [...input.value.trim()].reduce((width,char) => width + (char.codePointAt(0) < 128 ? 1 : 2),0);
      const issue = used > limit ? '超过上限' : key === 'title' && !used ? '必填' :
        key === 'guide' && used && used < 20 ? '至少 20 个字符' : '';
      const count = node(key+'-count');
      count.textContent = `${used} / ${limit}${issue ? ' · '+issue : ''}`;
      count.classList.toggle('is-invalid',Boolean(issue));
      input.setAttribute('aria-invalid',String(Boolean(issue)));
      valid = valid && !issue;
    }
    return valid;
  }

  function render(data) {
    const task = data.task;
    const login = task?.prompt?.kind === 'login';
    renderPrompt(task);
    node('review').hidden = false;
    const {preview = {}, files = {}, errors = [], warnings = []} = data;
    const titles = node('titles');
    if (titles) {
      titles.hidden = !data.id && !preview.title;
      titles.disabled = !data.id || !!task;
      node('title').value = preview.title || '';
      node('guide').value = preview.guide || '';
    }
    const summary = [['类目',preview.category],['品牌',preview.brand],['商家编码',preview.code],['一口价',preview.price],['店铺分类',preview.shop_categories?.join('、')]];
    if (!titles) summary.unshift(['宝贝标题',preview.title],['导购标题',preview.guide]);
    node('summary').replaceChildren(...summary.filter(([, value]) => value).map(([label,value]) => {
      const box = element('div'); box.className = 'listing-summary-entry'; box.append(element('strong',label),element('span',value)); return box;
    }));
    node('issues').replaceChildren();
    for (const [label, values] of [['必须修正', errors], ['可跳过或需核对', warnings]]) {
      if (!values.length) continue;
      const list = element('ul'); list.append(...values.map(value => element('li',value)));
      node('issues').append(element('strong',label),list);
    }
    node('files').querySelector('dl').replaceChildren(...Object.entries(files).flatMap(([group,names]) => [element('dt',labels[group]),element('dd',names.join('、') || '未提供')]));
    const rows = preview.skus || [];
    node('skus').hidden = !rows.length;
    node('skus').querySelector('tbody').replaceChildren(...rows.map(row => {
      const tr = element('tr');
      tr.append(...['销售规格','价格','数量','商家编码'].map(key => element('td',row[key])),element('td',preview.sku_images?.[row['销售规格']] || '未匹配')); return tr;
    }));
    const validTitles = checkTitles();
    node('start').disabled = !data.id || !!task || busy || !validTitles;
    node('action').hidden = !!task;
    node('task').hidden = !task;
    if (!task) return;
    node('import-fields').disabled = active.includes(task.phase);
    node('phase').textContent = login ? '等待扫码登录' : task.outcome === 'warehouse' ? '已入仓' : task.outcome === 'uncertain' ? '提交结果待核实' : task.outcome === 'filled' ? '已填写，未提交' : phases[task.phase];
    node('message').textContent = task.message;
    node('item').hidden = !task.item_id;
    node('item').textContent = task.item_id ? `平台商品编号：${task.item_id}` : '';
    node('log').hidden = !task.task_id;
    if (task.task_id) node('log').href = `${root.dataset.tasks}/${encodeURIComponent(task.task_id)}`;
    node('evidence').hidden = !task.evidence;
    if (task.evidence) node('evidence').href = `${base}/${id}/evidence/${task.evidence}`;
    const prompt = task.prompt;
    node('differences').replaceChildren(...(prompt?.differences?.length ? prompt.differences : task.notes || []).map(value => element('li',value)));
    node('differences').hidden = !node('differences').childElementCount;
  }

  async function poll() {
    const requested = id;
    if (!requested) return;
    try {
      const data = await api(`${base}/${requested}`);
      if (id !== requested) return;
      render(data);
      void loadLogs();
      if (id === requested && active.includes(data.task?.phase)) timer = setTimeout(poll, 1500);
    } catch (error) {
      if (id !== requested) return;
      node('feedback-note').textContent = error.message;
      if (node('task').hidden) node('import-note').textContent = error.message;
      else timer = setTimeout(poll, 2500);
    }
  }

  async function sendFeedback(prompt, action) {
    if (!id || sentPrompt === prompt) return;
    sentPrompt = prompt;
    for (const button of node('manual-actions').children) button.disabled = true;
    try {
      await api(`${base}/${id}/feedback`, {prompt_id:prompt, action});
      if (currentPrompt?.id === prompt) node('manual-note').textContent = '已收到反馈，正在复查当前步骤。';
    } catch (error) {
      sentPrompt = null;
      if (currentPrompt?.id === prompt) {
        node('manual-note').textContent = error.message;
        for (const button of node('manual-actions').children) button.disabled = false;
      }
    }
  }

  function resetReview() {
    if (node('manual-dialog').open) node('manual-dialog').close();
    clearTimeout(timer); remember(null); node('review').hidden = true; node('task').hidden = true;
    currentPrompt = seenPrompt = sentPrompt = null;
    node('manual-open').hidden = true; node('feedback-note').textContent = '';
    logOwner = oldest = null; latest = 0; logBusy = logAgain = logReady = hasOlder = false;
    node('log-rows').replaceChildren(); node('log-older').hidden = true;
    node('log-note').textContent = '等待任务日志…';
  }

  function chooseFolder() {
    resetReview();
    fileMap = new Map(); dirs = new Map();
    for (const file of node('folder').files) {
      const path = file.webkitRelativePath;
      if (!path || /^(~\$|\.DS_Store$|Thumbs\.db$|desktop\.ini$)/i.test(file.name)) continue;
      fileMap.set(path,file);
      const dir = path.slice(0,path.lastIndexOf('/'));
      if (!dirs.has(dir)) dirs.set(dir,[]);
      dirs.get(dir).push(file);
    }
    const folderName = node('folder-name');
    if (folderName) {
      const firstPath = fileMap.keys().next().value;
      const selected = Boolean(firstPath);
      folderName.textContent = selected ? firstPath.split('/')[0] : '选择商品文件夹';
      folderName.title = selected ? folderName.textContent : '';
      node('folder-description').textContent = selected ? `已读取 ${fileMap.size} 个文件` : '请选择包含产品信息表和素材的目录';
      node('folder-change').hidden = !selected;
      folderName.closest('.listing-folder-card').classList.toggle('is-selected',selected);
    }
    node('preflight').disabled = !fileMap.size || (node('category') && !node('category').value);
    node('mapping').hidden = !fileMap.size;
    const mapping = node('mapping');
    mapping.dispatchEvent(new Event('ui:controls-remove',{bubbles:true}));
    for (const box of mapping.querySelectorAll('[data-material]')) {
      const {material:role,label} = box.dataset;
      const previous = box.querySelector('select');
      const select = previous.cloneNode(false);
      select.classList.remove('ui-searchable-native');
      select.setAttribute('data-searchable-select','true'); select.setAttribute('aria-label',label);
      const isFile = role === 'excel' || role === 'video';
      const options = isFile ? [...fileMap.keys()].filter(path => path.toLowerCase().endsWith(role === 'excel' ? '.xlsx' : '.mp4')) : [...dirs.keys()];
      select.add(new Option('请选择对应文件或子目录','')); select.add(new Option('未提供','-'));
      for (const value of options) select.add(new Option(value,value));
      const exact = isFile ? options : options.filter(dir => aliases[role].includes(dir.split('/').at(-1).toLowerCase().replace(/\s/g,'')));
      select.value = exact.length === 1 ? exact[0] : exact.length > 1 ? '' : '-';
      previous.replaceWith(select);
    }
    mapping.dispatchEvent(new Event('ui:controls-added',{bubbles:true}));
    node('import-note').textContent = fileMap.size ? '已识别文件。目录名不同时，可以手动指定分组。' : '未读到文件，请重新选择商品文件夹。';
    if (!node('preflight').disabled && [...mapping.querySelectorAll('select')].every(select => select.value !== '')) node('import').requestSubmit();
  }

  node('folder').addEventListener('change',chooseFolder);
  node('category')?.addEventListener('change',() => {
    const selected = node('category').selectedOptions[0];
    node('keyword').value = selected?.dataset.categoryName || '';
    node('preflight').disabled = !fileMap.size || !node('category').value;
    node('import-note').textContent = '类目已更换，请确认素材并重新自检。';
  });
  node('manual-close').addEventListener('click',() => node('manual-dialog').close());
  node('manual-open').addEventListener('click',() => { if (currentPrompt && !node('manual-dialog').open) node('manual-dialog').showModal(); });
  node('log-older').addEventListener('click',() => loadLogs(true));
  node('import').addEventListener('change',event => {
    if (event.target !== node('folder') && id && !busy) resetReview();
  });
  node('import').addEventListener('submit',async event => {
    event.preventDefault();
    if (busy || !fileMap.size) return;
    const form = new FormData(node('import'));
    const selections = [...node('mapping').querySelectorAll('select')];
    if (selections.some(select => !select.value)) {
      node('import-note').textContent = '存在多个候选项，请先选择对应的文件或目录。'; return;
    }
    for (const select of selections) {
      const role = select.dataset.listingRole, selection = select.value;
      if (selection === '-') continue;
      if (role === 'excel' || role === 'video') form.append(role,fileMap.get(selection),fileMap.get(selection).name);
      else for (const file of dirs.get(selection).filter(file => !/\.(xlsx|mp4)$/i.test(file.name))) form.append(role,file,file.name);
    }
    busy = true; resetReview(); node('import-fields').disabled = true;
    node('import-note').textContent = '正在导入资料并自检，尚未打开千牛…';
    try {
      const data = await api(root.dataset.api,form);
      remember(data.id); busy = false;
      render(data);
      node('import-note').textContent = data.id ? '自检通过，请核对资料后开始。' : '自检未通过，请检查下方问题后重试。';
    } catch (error) { node('import-note').textContent = error.message; }
    finally { busy = false; node('import-fields').disabled = false; }
  });
  node('submit').addEventListener('change',() => {
    node('start').textContent = node('submit').checked ? '开始填写并提交到仓库' : '仅填写';
  });
  node('titles')?.addEventListener('input',() => {
    node('start').disabled = !checkTitles() || !id || busy;
  });
  node('start').addEventListener('click',async () => {
    if (!id || busy || !checkTitles()) return;
    const body = {submit:node('submit').checked};
    if (node('titles')) {
      body.titles = {title:node('title').value, guide:node('guide').value};
      node('titles').disabled = true;
    }
    busy = true; node('start').disabled = true; node('import-fields').disabled = true;
    try {
      render(await api(`${base}/${id}/start`,body)); poll();
    } catch (error) {
      node('import-note').textContent = error.message; node('import-fields').disabled = false;
      if (node('titles')) node('titles').disabled = false;
      node('start').disabled = !checkTitles();
    } finally { busy = false; }
  });
  const saved = new URL(location.href).searchParams.get('job');
  if (saved && /^[0-9a-f]{32}$/.test(saved)) { id = saved; poll(); }
})();
