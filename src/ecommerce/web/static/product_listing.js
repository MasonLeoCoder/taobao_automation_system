(() => {
  'use strict';
  const root = document.querySelector('#listing');
  if (!root) return;
  const node = name => document.getElementById(`listing-${name}`);
  const base = root.dataset.api.replace(/\/preflight$/, '');
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const labels = Object.fromEntries([...node('mapping').querySelectorAll('[data-material]')].map(e => [e.dataset.material, e.dataset.label]));
  const aliases = {main:['1-1主图','1:1主图','1比1主图'], portrait:['3-4主图','3:4主图','3比4主图'], detail:['详情图'], sku:['sku图','sku图片']};
  const knownFolders = new Set(Object.values(aliases).flat());
  const folderAlias = path => path.split('/').at(-1).toLowerCase().replace(/\s/g,'');
  const singleFile = role => role === 'excel' || role === 'video';
  const isImage = file => /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(file.name);
  const groupFiles = path => dirs.get(path).filter(file => !/\.(xlsx|mp4)$/i.test(file.name));
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

  root.checkListingTitle = (key, raw, candidate = false) => {
    const value = candidate || key === 'sku' ? raw.replace(/\p{White_Space}/gu, '') :
      raw.replace(/^\p{White_Space}+|\p{White_Space}+$/gu, '');
    const width = [...value].reduce((sum, char) => sum + (char.codePointAt(0) < 128 ? 1 : 2), 0);
    const limit = key === 'title' ? 60 : 30;
    let issue = '';
    if (/[\x00-\x1f\x7f]/u.test(raw)) issue = '请使用单行文本';
    else if (width > limit) issue = `最多 ${limit} 字符，超出 ${width - limit} 字符`;
    else if (!width && (key === 'title' || candidate)) issue = '请输入标题';
    else if (key === 'guide' && width && width < 20) issue = `至少 20 字符，还差 ${20 - width} 字符`;
    return {value, width, limit, issue};
  };

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
    const titles = {login:'等待千牛扫码登录', native:'需要决定原生弹窗', review:'请核对填写差异', titles:'请修正千牛标题',
      receipt:'请核对提交结果', finish:'填写完成，等待检查', terminal:'任务已暂停或结束，请核对',
      video:'请核对商品视频', video_ratio:'视频比例需要处理', specifications:'请核对分层规格',
      main_image:'请核对当前主图', manual:'需要人工处理'};
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
      const words = {continue:'完成',confirm:'确认弹窗',cancel:'取消弹窗',approve:'确认差异并提交到仓库',
        recheck_titles:'重新核对标题',fill_only:'保留填写，不提交',skip_video:'跳过视频',
        finish:['specifications','main_image','video_ratio'].includes(prompt.kind) ? '结束本次任务' :
          task.outcome === 'uncertain' ? '结束，保留待核实状态' : '结束检查'};
      const button = element('button',words[action]);
      button.className = `ui-button ${['approve','continue','confirm','recheck_titles'].includes(action) ? 'ui-primary' : 'ui-secondary'}`;
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

  let skuOwner = null, skuDraft = {}, skuDraftContext = null;

  function saveSkuDraft() {
    try { if (skuOwner) sessionStorage.setItem('listing-sku-titles:' + skuOwner, JSON.stringify(skuDraft)); }
    catch { /* 当前页面仍可编辑 */ }
  }

  function checkTitles() {
    if (!node('titles')) return true;
    const errors = node('title-errors'), entries = [];
    function check(field, key, label) {
      const result = root.checkListingTitle(key, field.value);
      const {width, limit, issue} = result;
      const count = document.getElementById(field.dataset.count || field.getAttribute('aria-describedby'));
      const meter = `${width} / ${limit}`;
      count.textContent = meter + (issue ? ` · ${issue}` : '');
      count.classList.toggle('is-invalid', Boolean(issue));
      field.setAttribute('aria-invalid', String(Boolean(issue)));
      if (issue) {
        const link = element('button', `${label} · ${meter} · ${issue}`);
        link.type = 'button';
        link.addEventListener('click', () => field.focus());
        const row = element('li');
        row.append(link);
        entries.push(row);
      }
      return result;
    }
    check(node('title'), 'title', '宝贝标题');
    const guide = check(node('guide'), 'guide', '导购标题');
    for (const field of node('skus').querySelectorAll('[data-sku-key]')) {
      const result = check(field, 'sku', field.getAttribute('aria-label'));
      const fallback = node(field.dataset.skuKey + '-fallback');
      const comparison = field.closest('td').querySelector('[data-sku-comparison]');
      fallback.hidden = Boolean(result.value) || Boolean(comparison && !comparison.hidden);
      const fallbackTitle = skuDraftContext ? root.checkListingTitle('sku', guide.value).value : guide.value;
      fallback.textContent = guide.issue ? '请先修正正式导购标题' :
        fallbackTitle ? `将使用导购标题：${fallbackTitle}` : '将跳过填写 SKU 搜索标题';
    }
    errors.replaceChildren(...entries);
    errors.hidden = !id || node('titles').disabled || !entries.length;
    return !entries.length;
  }

  function startError(message = '') {
    node('start-error').textContent = message;
    node('start-error').hidden = !message;
  }

  function renderSkus(data, preview) {
    const rows = preview.skus || [];
    if (data.id && skuOwner !== data.id) {
      const previous = skuDraftContext;
      const context = preview.sku_mode ? {
        product: JSON.stringify([preview.category, preview.brand, preview.code]),
        keys: rows.map((row, index) => JSON.stringify([
          row['商家编码'], row['销售规格'], preview.sku_attributes?.[index],
        ])),
      } : null;
      const previousDraft = skuDraft;
      skuOwner = data.id;
      skuDraftContext = context;
      skuDraft = {};
      try { if (skuOwner) skuDraft = JSON.parse(sessionStorage.getItem('listing-sku-titles:' + skuOwner)) || {}; }
      catch { /* 保留空草稿 */ }
      if (!data.task && context && previous?.product === context.product) {
        const drafts = new Map(previous.keys.map((key, index) => [key, previousDraft[`sku-${index + 1}`]]));
        context.keys.forEach((key, index) => {
          const target = `sku-${index + 1}`, draft = drafts.get(key);
          if (!skuDraft[target] && draft) skuDraft[target] = {...draft, carried:true};
        });
        saveSkuDraft();
      }
    }
    node('skus').hidden = !rows.length;
    node('skus').querySelector('caption').textContent = preview.sku_mode
      ? '销售规格 · ' + preview.sku_mode : '销售规格';
    node('skus').querySelector('tbody').replaceChildren(...rows.map((row, index) => {
      const tr = element('tr'), name = row['销售规格'], spec = element('td');
      spec.append(element('strong', name));
      const combination = preview.sku_combinations?.[name];
      if (combination) {
        const values = element('small', `颜色 ${combination[0]} · 尺码 ${combination[1]}`);
        values.className = 'listing-sku-title-label';
        spec.append(values);
      }
      if (data.id) {
        const key = `sku-${index + 1}`;
        const field = element('input'), count = element('small'), fallback = element('small');
        field.type = 'text';
        field.className = 'ui-control';
        field.dataset.skuKey = key;
        field.value = (data.task ? data.sku_titles?.[key] : skuDraft[key]?.title) || '';
        field.disabled = Boolean(data.task);
        field.placeholder = '留空使用导购标题';
        field.setAttribute('aria-label', `${name}的 SKU 搜索标题`);
        count.id = `listing-${key}-count`;
        fallback.id = `listing-${key}-fallback`;
        fallback.className = 'listing-sku-fallback';
        field.dataset.count = count.id;
        field.setAttribute('aria-describedby', `${count.id} ${fallback.id}`);
        count.className = 'listing-title-count';
        spec.className = 'listing-sku-title';
        const label = element('small', '正式 SKU 搜索标题');
        label.className = 'listing-sku-title-label';
        spec.append(label, field, count, fallback);
        if (!data.task) {
          const draft = skuDraft[key];
          if (draft?.carried && draft.original) {
            const original = element('small', '上次导入的 AI 原稿 · ' + draft.original);
            original.className = 'listing-sku-title-label';
            spec.append(original);
          }
          const comparison = element('div');
          comparison.dataset.skuComparison = key;
          comparison.className = 'listing-sku-comparison';
          comparison.hidden = true;
          spec.append(comparison);
        }
      }
      tr.append(spec, ...['价格','数量','商家编码'].map(key => element('td',row[key])),
        element('td',preview.sku_images?.[name] || preview.sku_image_issues?.[name] || '未匹配'));
      return tr;
    }));
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
    const videoNote = node('video-preview');
    videoNote.hidden = !data.id || !preview.video;
    if (!videoNote.hidden) {
      const video = preview.video;
      const ratio = video.adapted
        ? `原视频 ${video.source_ratio} → 上传视频 ${video.upload_ratio} · 动态模糊背景`
        : `上传原视频 · ${video.upload_ratio}`;
      const link = element('a', '预览上传视频');
      link.href = `${base}/${data.id}/video`;
      link.target = '_blank';
      link.rel = 'noopener';
      videoNote.replaceChildren(document.createTextNode(`${ratio} · ${video.duration} 秒 · `), link);
    }
    node('issues').replaceChildren();
    for (const [label, values] of [['必须修正', errors], ['可跳过或需核对', warnings]]) {
      if (!values.length) continue;
      const list = element('ul'); list.append(...values.map(value => element('li',value)));
      node('issues').append(element('strong',label),list);
    }
    if (preview.video_error && fileMap.size) {
      const skip = element('button', '跳过视频并重新自检');
      skip.type = 'button';
      skip.className = 'ui-button ui-secondary';
      skip.addEventListener('click', () => {
        const choice = node('mapping').querySelector('[data-listing-role="video"]');
        choice.value = '-';
        choice.dispatchEvent(new Event('change', {bubbles:true}));
        node('import').requestSubmit();
      });
      node('issues').append(skip);
    }
    node('files').querySelector('dl').replaceChildren(...Object.entries(files).flatMap(([group,names]) => [element('dt',labels[group]),element('dd',names.join('、') || '未提供')]));
    renderSkus(data, preview);
    root.dispatchEvent(new CustomEvent('listing:review', {detail:task ? null : data.id}));
    const submit = node('submit');
    const canSubmit = submit.dataset.allowed === 'true' && preview.allow_submit !== false;
    if (!canSubmit || submit.disabled) submit.checked = canSubmit;
    submit.disabled = !canSubmit;
    node('submit-note').hidden = preview.allow_submit !== false;
    updateStartLabel();
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
    root.dispatchEvent(new CustomEvent('listing:review'));
    currentPrompt = seenPrompt = sentPrompt = null;
    node('manual-open').hidden = true; node('feedback-note').textContent = '';
    logOwner = oldest = null; latest = 0; logBusy = logAgain = logReady = hasOlder = false;
    node('log-rows').replaceChildren(); node('log-older').hidden = true;
    node('log-note').textContent = '等待任务日志…';
    startError();
  }

  function showRecognized() {
    const summary = node('recognized');
    summary.hidden = !fileMap.size;
    summary.replaceChildren(...[...node('mapping').querySelectorAll('select')].map(select => {
      const role = select.dataset.listingRole;
      const path = select.value;
      const value = !path ? '有多个候选，请选择' : path === '-' ? '未提供' :
        singleFile(role) ? fileMap.get(path).name : `${groupFiles(path).filter(isImage).length} 张 · ${path.split('/').at(-1)}`;
      const item = element('div');
      item.append(element('dt',labels[role]), element('dd',value));
      return item;
    }));
  }

  function chooseFolder() {
    resetReview();
    fileMap = new Map(); dirs = new Map();
    for (const file of node('folder').files) {
      const path = file.webkitRelativePath;
      if (!path || /^(~\$|\.DS_Store$|Thumbs\.db$|desktop\.ini$|填写说明\.txt$)/i.test(file.name)) continue;
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
      node('folder-description').textContent = selected ? `已读取 ${fileMap.size} 个文件` : '请选择解压后包含产品信息表和素材的整个文件夹';
      node('folder-change').hidden = !selected;
      folderName.closest('.listing-folder-card').classList.toggle('is-selected',selected);
    }
    node('preflight').disabled = !fileMap.size || (node('category') && !node('category').value);
    const mapping = node('mapping');
    mapping.dispatchEvent(new Event('ui:controls-remove',{bubbles:true}));
    const customFolders = [...dirs.keys()].filter(path => !knownFolders.has(folderAlias(path)) &&
      groupFiles(path).some(isImage));
    for (const box of mapping.querySelectorAll('[data-material]')) {
      const {material:role,label} = box.dataset;
      const previous = box.querySelector('select');
      const select = previous.cloneNode(false);
      select.classList.remove('ui-searchable-native');
      select.setAttribute('data-searchable-select','true'); select.setAttribute('aria-label',label);
      const exact = singleFile(role)
        ? [...fileMap.keys()].filter(path => path.toLowerCase().endsWith(role === 'excel' ? '.xlsx' : '.mp4'))
        : [...dirs.keys()].filter(path => aliases[role].includes(folderAlias(path)) && groupFiles(path).length);
      const options = !singleFile(role) && !exact.length ? customFolders : exact;
      select.add(new Option('请选择对应文件或子目录','')); select.add(new Option('未提供','-'));
      for (const value of options) select.add(new Option(value,value));
      select.value = exact.length === 1 ? exact[0] : exact.length > 1 ? '' : '-';
      box.hidden = exact.length === 1 || !options.length;
      previous.replaceWith(select);
    }
    mapping.hidden = !fileMap.size || !mapping.querySelector('[data-material]:not([hidden])');
    mapping.dispatchEvent(new Event('ui:controls-added',{bubbles:true}));
    showRecognized();
    const ambiguous = [...mapping.querySelectorAll('select')].some(select => !select.value);
    node('import-note').textContent = !fileMap.size ? '未读到文件，请重新选择商品文件夹。' :
      ambiguous ? '存在多个候选项，请先选择对应的文件或目录。' :
        mapping.hidden ? '已识别商品资料。' : '目录名不同时，可以手动指定分组。';
    if (!node('preflight').disabled && !ambiguous) node('import').requestSubmit();
  }

  node('folder').addEventListener('change',chooseFolder);
  function updateTemplateLink() {
    const selected = node('category').selectedOptions[0];
    const link = node('template-download');
    const note = node('template-note');
    const url = selected?.dataset.templateUrl;
    node('template-label').textContent = url ? `下载${selected.dataset.categoryName}资料包` : '下载完整商品资料包';
    note.hidden = Boolean(url);
    if (url) {
      link.href = url;
      link.removeAttribute('aria-disabled');
      link.removeAttribute('aria-describedby');
    } else {
      link.removeAttribute('href');
      link.setAttribute('aria-disabled', 'true');
      link.setAttribute('aria-describedby', note.id);
      note.textContent = selected?.value ? '该类目的资料包待适配' : '先选择千牛类目';
    }
  }
  if (node('category')) updateTemplateLink();
  node('category')?.addEventListener('change',() => {
    const selected = node('category').selectedOptions[0];
    updateTemplateLink();
    node('keyword').value = selected?.dataset.searchKeyword || '';
    node('preflight').disabled = !fileMap.size || !node('category').value;
    node('import-note').textContent = '类目已更换，请确认素材并重新自检。';
  });
  node('manual-close').addEventListener('click',() => node('manual-dialog').close());
  node('manual-open').addEventListener('click',() => { if (currentPrompt && !node('manual-dialog').open) node('manual-dialog').showModal(); });
  node('log-older').addEventListener('click',() => loadLogs(true));
  node('import').addEventListener('change',event => {
    if (event.target.matches('select[data-listing-role]')) showRecognized();
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
      if (singleFile(role)) form.append(role,fileMap.get(selection),fileMap.get(selection).name);
      else for (const file of groupFiles(selection)) form.append(role,file,file.name);
    }
    busy = true; resetReview(); node('import-fields').disabled = true;
    node('import-note').textContent = form.has('video')
      ? '正在导入资料并检查视频。比例不符时会在本地适配，请等待…'
      : '正在导入资料并自检，尚未打开千牛…';
    try {
      const data = await api(root.dataset.api,form);
      remember(data.id); busy = false;
      render(data);
      node('import-note').textContent = data.id ? '自检通过，请核对资料后开始。' : '自检未通过，请检查下方问题后重试。';
    } catch (error) { node('import-note').textContent = error.message; }
    finally { busy = false; node('import-fields').disabled = false; }
  });
  function updateStartLabel() {
    node('start').textContent = node('submit').checked ? '开始填写并提交到仓库' : '仅填写';
  }
  node('submit').addEventListener('change',updateStartLabel);
  node('titles')?.addEventListener('input',() => {
    startError();
    node('start').disabled = !checkTitles() || !id || busy;
  });
  node('skus').addEventListener('input', event => {
    const field = event.target;
    if (!field.dataset.skuKey) return;
    skuDraft[field.dataset.skuKey] = {...skuDraft[field.dataset.skuKey], title:field.value, manual:true};
    saveSkuDraft();
    startError();
    node('start').disabled = !checkTitles() || !id || busy;
  });
  root.addEventListener('listing:sku-result', ({detail}) => {
    const {job, attempt, rows, autoFill, previousEdits} = detail;
    if (!job || job !== id || job !== skuOwner || busy || node('titles').disabled) return;
    if (autoFill) {
      const byKey = new Map(rows.map(row => [row.sku_key, row.raw_title ?? row.title ?? '']));
      for (const field of node('skus').querySelectorAll('[data-sku-key]')) {
        const key = field.dataset.skuKey, draft = skuDraft[key];
        if (draft && (draft.manual !== false || draft.attempt === attempt)) {
          if (byKey.has(key)) skuDraft[key] = {...draft, original:byKey.get(key)};
          continue;
        }
        const manual = typeof previousEdits?.[key] === 'string';
        const raw = manual ? previousEdits[key] : byKey.get(key) ?? '';
        const title = root.checkListingTitle('sku', raw).value;
        field.value = title;
        skuDraft[key] = {title, attempt, manual, original:byKey.get(key)};
      }
      saveSkuDraft();
    }
    node('start').disabled = !checkTitles();
  });
  node('start').addEventListener('click',async () => {
    if (!id || busy || !checkTitles()) return;
    startError();
    const body = {submit:node('submit').checked};
    if (node('titles')) {
      body.titles = {title:node('title').value, guide:node('guide').value};
      node('titles').disabled = true;
    }
    const skuFields = [...node('skus').querySelectorAll('[data-sku-key]')];
    if (skuFields.length) {
      body.sku_titles = Object.fromEntries(skuFields.map(field => [field.dataset.skuKey, field.value]));
      skuFields.forEach(field => { field.disabled = true; });
    }
    busy = true; node('start').disabled = true; node('import-fields').disabled = true;
    try {
      render(await api(`${base}/${id}/start`,body)); poll();
    } catch (error) {
      startError(error.message); node('import-fields').disabled = false;
      if (node('titles')) node('titles').disabled = false;
      skuFields.forEach(field => { field.disabled = false; });
      node('start').disabled = !checkTitles();
    } finally { busy = false; }
  });
  const saved = new URL(location.href).searchParams.get('job');
  if (saved && /^[0-9a-f]{32}$/.test(saved)) { id = saved; poll(); }
})();
