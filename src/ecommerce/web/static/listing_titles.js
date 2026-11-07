(() => {
  'use strict';
  const root = document.getElementById('listing');
  const node = key => document.getElementById('listing-ai-' + key);
  const panel = document.getElementById('listing-ai');
  if (!panel) return;
  const settingsPanel = node('settings');
  const fields = {title:'宝贝标题', guide:'导购标题'};
  const targets = {title:56, guide:28, sku:0};
  const kinds = {search:'搜索词', trend:'趋势词', core:'核心词', modifier:'修饰词'};
  const input = key => document.getElementById('listing-' + key);
  const base = root.dataset.api.replace(/\/preflight$/, '');
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const pageSize = 20;
  const maxTerms = 40;
  const paidEnabled = panel.dataset.paidEnabled === 'yes';
  let id, source, confirmed, result, applied = {}, selected = new Set(), locks;
  let version = 0, resultVersion = 0, busy = false, visible = pageSize, onlySelected = false, pollTimer;
  let attempts = [], latestId, drafts = {};

  function element(tag, value) {
    const el = document.createElement(tag);
    if (value !== undefined) el.textContent = value;
    return el;
  }

  async function api(path, body) {
    const options = body === undefined ? {} : {method:'POST', body:JSON.stringify(body),
      headers:{'Content-Type':'application/json', 'X-Collection-Token':token}};
    const response = await fetch(`${base}/${id}/titles/${path}`, options);
    const data = await response.json().catch(() => { throw new Error('暂时无法读取标题服务的结果。'); });
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
  }

  function message(text = '') {
    node('message').textContent = text;
    node('message').hidden = !text;
  }

  function buttons() {
    node('preview').disabled = busy || !source?.batch || !selected.size || (source.mismatch && !node('mismatch').checked);
    node('generate').disabled = !paidEnabled || busy || !confirmed || !node('paid').checked || confirmed.result?.state === 'complete';
    node('period').disabled = node('batch').disabled = busy;
  }

  function invalidate() {
    confirmed = null;
    node('plan').hidden = true;
    node('paid').checked = false;
    buttons();
  }

  function time(value) { return value?.replace('T', ' ').slice(0, 16) || '未知'; }

  function scope(batch) {
    const dates = batch.period_start === batch.period_end ? batch.period_start :
      [batch.period_start, batch.period_end].filter(Boolean).join(' 至 ');
    return `统计类目 ${batch.sycm_category_path} · 统计日期 ${dates || '未记录'} · ${time(batch.started_at)} 采集`;
  }

  function coverage(types) {
    const present = [], missing = [];
    for (const [key, label] of Object.entries(kinds)) {
      (types[key]?.row_count > 0 ? present : missing).push(label);
    }
    return `${present.length ? '本批含' + present.join('、') : '本批暂无可用词'}${missing.length ? ' · 缺' + missing.join('、') : ''}`;
  }

  function sourceDetails(batch, types, expanded = false) {
    const details = element('details');
    details.className = 'listing-ai-source-details';
    details.open = expanded;
    const dates = [batch.period_start, batch.period_end].filter(Boolean);
    const values = [
      ['统计类目', batch.sycm_category_path],
      ['统计日期', [...new Set(dates)].join(' 至 ') || '未记录'],
      ['采集时间', time(batch.started_at)],
      ['采集页数', Object.entries(kinds).map(([key, label]) => `${label} ${types[key]?.collected_pages ?? 0} 页`).join(' · ')],
      ['批次编号', batch.id],
    ];
    const list = element('dl');
    for (const [label, value] of values) list.append(element('dt', label), element('dd', value));
    details.append(element('summary', '查看采集来源'), list);
    return details;
  }

  function restoreRecommended() {
    selected = new Set(source?.suggested || []);
    locks = {title:new Set(), guide:new Set()};
    invalidate();
    renderWords();
  }

  function resetWordPage() {
    visible = pageSize;
    node('words').scrollTop = node('picked-words').scrollTop = 0;
  }

  async function loadSource() {
    const request = ++version;
    const period = node('period').value, batch = node('batch').value;
    source = null;
    node('terms').hidden = true;
    node('mismatch-label').hidden = true;
    node('mismatch').checked = false;
    invalidate();
    node('source').textContent = '正在读取排行批次…';
    try {
      const data = await api('source?' + new URLSearchParams({period, batch}));
      if (request !== version) return;
      source = data;
      node('batch').replaceChildren(new Option('最新有数据的批次', ''),
        ...data.history.map(item => new Option(`${time(item.started_at)} · 共 ${item.captured_pages} 页`, item.id)));
      node('batch').value = batch;
      if (data.batch) {
        const label = coverage(data.types) + (data.batch.status === 'partial' ? ' · 部分完成' : '');
        node('source').replaceChildren(element('p', label), sourceDetails(data.batch, data.types, data.mismatch));
      } else {
        node('source').textContent = '该类目在所选周期没有可用的搜索排行批次。可切换周期，或先在「搜索排行」采集。';
      }
      node('mismatch-label').hidden = !data.mismatch;
      node('terms').hidden = !data.words.length;
      resetWordPage();
      restoreRecommended();
    } catch (error) {
      if (request === version) node('source').textContent = '排行批次读取失败。' + error.message;
    }
  }

  function changeWord(term, checkbox) {
    const list = checkbox.closest('ul');
    const picks = [...list.querySelectorAll('[data-pick]')];
    const index = picks.indexOf(checkbox), neighbor = picks[index + 1] || picks[index - 1];
    const listTop = list.getBoundingClientRect().top;
    const neighborOffset = neighbor ? neighbor.getBoundingClientRect().top - listTop : 0;
    if (checkbox.checked) selected.add(term.id);
    else {
      selected.delete(term.id);
      Object.values(locks).forEach(ids => ids.delete(term.id));
    }
    invalidate();
    renderWords();
    const next = neighbor && list.querySelector(`[data-pick="${neighbor.dataset.pick}"]`);
    const top = list.getBoundingClientRect().top;
    if (next) list.scrollTop += next.getBoundingClientRect().top - top - neighborOffset;
    if (top !== listTop) window.scrollBy({top:top - listTop, behavior:'instant'});
    (next && !next.disabled ? next : list).focus({preventScroll:true});
  }

  function wordRow(term) {
    const row = element('li');
    const pick = element('label'), checkbox = element('input');
    checkbox.type = 'checkbox';
    checkbox.checked = selected.has(term.id);
    checkbox.disabled = !checkbox.checked && selected.size >= maxTerms;
    checkbox.addEventListener('change', () => changeWord(term, checkbox));
    checkbox.dataset.pick = term.id;
    pick.append(checkbox, element('strong', term.keyword));
    const detail = element('small', `${term.type_label} 第 ${term.rank} 名${term.popularity_raw ? ' · 人气 ' + term.popularity_raw : ''}`);
    row.append(pick, detail);
    if (checkbox.checked) {
      const required = element('div');
      for (const [key, label] of Object.entries(fields)) {
        const choice = element('label'), box = element('input');
        box.type = 'checkbox';
        box.checked = locks[key].has(term.id);
        box.addEventListener('change', () => {
          if (box.checked) locks[key].add(term.id);
          else locks[key].delete(term.id);
          invalidate();
        });
        choice.append(box, document.createTextNode(label + '必用'));
        required.append(choice);
      }
      row.append(required);
    }
    return row;
  }

  function renderWordList(list, groups, limit) {
    const items = [];
    for (const [kind, terms] of groups) {
      if (!terms.length) continue;
      if (groups.length > 1) {
        const heading = element('li', `${kinds[kind]} · ${terms.length} 条`);
        heading.className = 'listing-ai-word-kind';
        items.push(heading);
      }
      items.push(...terms.slice(0, limit).map(wordRow));
    }
    const previous = list.scrollTop;
    list.replaceChildren(...(items.length ? items : [element('li', '没有符合筛选条件的词条。')]));
    list.scrollTop = previous;
  }

  function renderWords() {
    node('count').textContent = `已选 ${selected.size} / ${maxTerms}`;
    node('term-limit').hidden = selected.size < maxTerms;
    node('selected').setAttribute('aria-pressed', String(onlySelected));
    node('selected').textContent = onlySelected ? '查看全部' : '查看已选';
    node('unpicked').hidden = onlySelected;
    const query = node('search').value.trim().toLowerCase(), kind = node('kind').value;
    const rows = (source?.words || []).filter(term => (!kind || term.type === kind) && term.keyword.toLowerCase().includes(query))
      .sort((a, b) => a.rank - b.rank || a.page_no - b.page_no);
    const types = kind ? [kind] : Object.keys(kinds);
    const picked = types.map(type => [type, rows.filter(term => term.type === type && selected.has(term.id))]);
    const unpicked = types.map(type => [type, rows.filter(term => term.type === type && !selected.has(term.id))]);
    const count = groups => groups.reduce((total, [, terms]) => total + terms.length, 0);
    node('picked-count').textContent = `已勾选 · ${count(picked)} 条`;
    node('unpicked-count').textContent = `未勾选 · ${count(unpicked)} 条`;
    const limit = visible / types.length;
    renderWordList(node('picked-words'), picked, Infinity);
    renderWordList(node('words'), unpicked, limit);
    node('more').hidden = unpicked.every(([, terms]) => terms.length <= limit);
  }

  function evidence(value, terms) {
    const hits = [];
    for (const term of terms) {
      const word = term.keyword;
      for (let offset = value.indexOf(word); offset >= 0; offset = value.indexOf(word, offset + 1)) {
        hits.push({start:offset, end:offset + word.length, word});
      }
    }
    hits.sort((a, b) => a.start - b.start || b.end - a.end);
    const segments = [], used = new Map();
    let cursor = 0;
    for (const hit of hits) {
      if (hit.start < cursor) continue;
      if (hit.start > cursor) segments.push({text:value.slice(cursor, hit.start)});
      segments.push({text:hit.word, ranked:true});
      const prior = used.get(hit.word);
      used.set(hit.word, {keyword:hit.word, count:(prior?.count || 0) + 1,
        sources:prior?.sources || terms.filter(term => term.keyword === hit.word)});
      cursor = hit.end;
    }
    if (cursor < value.length) segments.push({text:value.slice(cursor)});
    return {segments, used:[...used.values()]};
  }

  function rankLabel(item) {
    const text = item.sources.map(term => term.type_label + '第 ' + term.rank + ' 名').join(' / ');
    return `${item.keyword} · ${text}${item.count > 1 ? ' · 重复 ' + item.count + ' 次' : ''}`;
  }

  function similarWords(first, second) {
    let left = [...first], right = [...second];
    if (left.length > right.length) [left, right] = [right, left];
    if (left.length < 4 || right.length - left.length > 1) return false;
    const difference = left.findIndex((char, index) => char !== right[index]);
    if (difference < 0) return right.length > left.length;
    const skip = left.length === right.length ? 1 : 0;
    return left.slice(difference + skip).join('') === right.slice(difference + 1).join('');
  }

  function marked(container, segments) {
    container.replaceChildren(...segments.map(part => part.ranked ? element('mark', part.text) : document.createTextNode(part.text)));
  }

  function updateEvidence(key) {
    const label = input(key + '-evidence'), origin = applied[key];
    label.hidden = !origin;
    label.replaceChildren();
    if (!origin) return;
    const match = evidence(input(key).value.trim(), origin.words);
    const line = element('span');
    marked(line, match.segments);
    label.append(line, element('small', match.used.map(rankLabel).join('；') || '当前标题未逐字使用本次已选排行词'));
    label.title = scope(origin.source);
  }

  function saveDraft() {
    try {
      if (id) {
        const values = Object.fromEntries(Object.keys(fields).map(key => [key, input(key).value]));
        sessionStorage.setItem('listing-title-draft:' + id, JSON.stringify({values, applied}));
      }
    } catch { /* 当前页面仍可编辑和填入 */ }
  }

  function applyTitle(key, value, plan) {
    if (!id || input('titles').disabled) return;
    input(key).value = value;
    applied[key] = {words:plan.input.words, source:plan.source};
    input(key).dispatchEvent(new Event('input', {bubbles:true}));
    message(`已覆盖${fields[key]}，可在上方继续编辑。`);
    input(key).focus();
  }

  function candidateDraft(record, key, original) {
    return drafts[record.id]?.[key] ?? original;
  }

  function saveCandidateDrafts() {
    try { sessionStorage.setItem('listing-candidate-drafts:' + id, JSON.stringify(drafts)); }
    catch { /* 当前页面仍可编辑 */ }
  }

  function checkCandidate(key, raw, plan) {
    const {value, width, limit, issue} = root.checkListingTitle(key, raw, true);
    const match = evidence(value, plan.input.words);
    const words = new Set(match.used.map(item => item.keyword));
    let problem = issue;
    if (!problem && match.used.some(item => item.count > 1)) problem = '同一标题重复使用排行词';
    if (!problem && !words.size) problem = '请使用至少一个本次已选排行词';
    if (!problem && (plan.input.locked?.[key] || []).some(word => !words.has(word))) problem = '缺少指定的必用词';
    return {value, width, limit, issue:problem, ...match};
  }

  function candidateEditor(container, record, key, draftKey, label, original, apply, refreshers, index) {
    const bar = element('div'), caption = element('div'), count = element('span');
    const edit = element('textarea'), fill = element('button', '填入' + label);
    const line = element('p'), ranks = element('ul'), note = element('small');
    bar.className = 'listing-ai-candidate-bar';
    count.className = 'listing-ai-title-length';
    caption.append(element('strong', label), count);
    fill.type = 'button'; fill.className = 'ui-button ui-secondary';
    bar.append(caption, fill);
    edit.className = 'ui-control'; edit.rows = 2;
    edit.setAttribute('aria-label', key === 'sku' ? `${container.closest('td').querySelector('strong').textContent}的 SKU 搜索标题候选` : `第 ${index + 1} 组${label}候选`);
    edit.value = candidateDraft(record, draftKey, original);
    line.className = 'listing-ai-title-text';
    ranks.className = 'listing-ai-ranks';
    note.id = `listing-candidate-${draftKey}-note`;
    edit.setAttribute('aria-describedby', note.id);
    container.append(bar, edit, line, ranks, note);

    const target = targets[key];
    const signature = value => value.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
    function check() {
      const data = checkCandidate(key, edit.value, record.plan);
      if (data.issue || !index) return data;
      const identity = signature(data.value);
      const repeated = record.groups.slice(0, index).some((group, prior) => {
        const other = checkCandidate(key, candidateDraft(record, `${prior}-${key}`, group[key].value), record.plan);
        return !other.issue && signature(other.value) === identity;
      });
      return repeated ? {...data, issue:'与上方候选标题重复'} : data;
    }
    function refresh() {
      const {width, limit, issue, segments, used} = check();
      count.textContent = `${width} / ${limit}`;
      count.classList.toggle('is-over', width > limit);
      edit.setAttribute('aria-invalid', String(Boolean(issue)));
      fill.disabled = Boolean(issue);
      marked(line, segments);
      ranks.replaceChildren(...used.map(item => element('li', rankLabel(item))));
      ranks.hidden = !used.length;
      const words = used.map(item => item.keyword);
      const similar = words.map((word, i) => {
        const other = words.slice(i + 1).find(value => similarWords(word, value));
        return other && `${word}／${other}`;
      }).find(Boolean);
      note.textContent = issue || (width < target ? `低于建议长度 ${target}～${limit} 字符` :
        similar ? `用词相近 · ${similar}` : '');
      note.className = issue ? 'listing-ai-issue' : 'listing-ai-advice';
      note.hidden = !note.textContent;
    }
    edit.addEventListener('input', () => {
      (drafts[record.id] ||= {})[draftKey] = edit.value;
      saveCandidateDrafts();
      if (refreshers) refreshers.forEach(update => update());
      else refresh();
    });
    fill.addEventListener('click', () => {
      const checked = check();
      if (!checked.issue) apply(checked.value);
    });
    refreshers?.push(refresh);
    refresh();
  }

  function showSkuCandidates(record) {
    const rows = new Map((record?.sku_titles || []).map(row => [row.sku_key, row]));
    for (const container of root.querySelectorAll('[data-sku-candidate]')) {
      container.replaceChildren();
      const key = container.dataset.skuCandidate, row = rows.get(key);
      container.hidden = !row;
      if (!row) continue;
      candidateEditor(container, record, 'sku', key, 'SKU 搜索标题', row.raw_title ?? row.title, value => {
        const field = container.parentElement.querySelector('[data-sku-key]');
        if (field.disabled) return;
        field.value = value;
        field.dispatchEvent(new Event('input', {bubbles:true}));
        field.focus();
      });
    }
  }

  function showResult(record) {
    result = record;
    showSkuCandidates(record);
    const box = node('results');
    box.hidden = !record;
    box.replaceChildren();
    if (!record) return;
    if (record.attempts) attempts = record.attempts;
    else if (record.groups && !attempts.includes(record.id)) attempts = [...attempts, record.id];
    latestId = record.latest_id || latestId || record.id;
    try {
      if (record.groups) sessionStorage.setItem('listing-candidate-view:' + id, record.id);
      else sessionStorage.removeItem('listing-candidate-view:' + id);
    }
    catch { /* 当前页面仍可查看候选 */ }
    const header = element('div');
    header.className = 'listing-ai-result-head';
    const heading = element('div');
    const headingTitle = element('h3', record.simulated ? '候选标题（模拟演示）' : '候选标题');
    headingTitle.tabIndex = -1;
    heading.append(headingTitle);
    if (record.groups) heading.append(element('p', `${record.groups.length} 组 · ${coverage(record.plan.types)}`));
    const settings = element('button', '调整生成设置');
    settings.type = 'button';
    settings.className = 'ui-button ui-secondary';
    header.append(heading);
    if (attempts.length > (record.groups ? 1 : 0)) {
      const choices = element('select');
      choices.className = 'ui-control';
      choices.setAttribute('aria-label', '查看已保存的候选');
      if (!record.groups) choices.add(new Option('查看已保存的候选', ''));
      for (const attempt of [latestId, ...attempts.filter(value => value !== latestId).reverse()]) {
        if (!attempts.includes(attempt)) continue;
        const position = attempts.indexOf(attempt) + 1;
        const edited = Object.keys(drafts[attempt] || {}).length ? ' · 有手改草稿' : '';
        choices.add(new Option(`候选 ${position}${attempt === latestId ? ' · 最新' : ''}${edited}`, attempt));
      }
      choices.value = record.groups ? record.id : '';
      choices.addEventListener('change', () => { if (choices.value) void recover(id, choices.value); });
      header.append(choices);
    }
    if (settingsPanel) {
      settings.addEventListener('click', () => {
        settingsPanel.open = true;
        settingsPanel.querySelector('summary').focus();
      });
      header.append(settings);
    }
    box.append(header);
    if (!record.groups) {
      box.append(element('p', record.error || 'AI 正在生成标题。刷新页面后可以继续查看结果。'));
      return;
    }
    box.append(sourceDetails(record.plan.source, record.plan.types));
    if (record.state === 'invalid') box.append(element('p', 'AI 原稿未通过校验。可以修改候选，校验通过后再填入。'));
    if (record.groups.length < record.plan.groups) {
      box.append(element('p', `这次返回 ${record.groups.length} 组，少于请求的 ${record.plan.groups} 组。`));
    }
    const list = element('div');
    list.className = 'listing-ai-candidates';
    const refreshers = [];
    record.groups.forEach((group, index) => {
      const card = element('article');
      card.className = 'listing-ai-group';
      card.append(element('h4', '第 ' + (index + 1) + ' 组'));
      const grid = element('div');
      grid.className = 'listing-ai-candidate-grid';
      for (const [key, label] of Object.entries(fields)) {
        const cell = element('div');
        cell.className = 'listing-ai-candidate';
        candidateEditor(cell, record, key, `${index}-${key}`, label, group[key].value,
          value => applyTitle(key, value, record.plan), refreshers, index);
        grid.append(cell);
      }
      card.append(grid);
      list.append(card);
    });
    box.append(list);
  }

  async function recover(job, attempt) {
    const request = ++resultVersion;
    try {
      const record = await api(attempt ? `result?attempt=${encodeURIComponent(attempt)}` : 'result');
      if (id !== job || request !== resultVersion) return;
      if (record) showResult(record);
      if (settingsPanel && !record?.groups && !confirmed) settingsPanel.open = true;
      if (record?.state === 'running') pollTimer = setTimeout(() => recover(job), 2000);
    } catch {
      if (id !== job || request !== resultVersion) return;
      if (attempt && !result) void recover(job);
      else message('暂时无法读取已保存的标题结果。可以刷新页面核对。');
    }
  }

  function requestInput() {
    return {period:node('period').value, batch:source.batch.id, selected:[...selected],
      locked:Object.fromEntries(Object.entries(locks).map(([key, ids]) => [key, [...ids]])),
      titles:Object.fromEntries(Object.keys(fields).map(key => [key, input(key).value])),
      facts:node('facts').value, allow_mismatch:node('mismatch').checked};
  }

  node('preview').addEventListener('click', async () => {
    if (node('preview').disabled) return;
    const job = id, data = requestInput();
    busy = true; invalidate(); message();
    try {
      const prepared = await api('preview', data);
      if (id !== job || !source?.batch || JSON.stringify(data) !== JSON.stringify(requestInput())) return;
      confirmed = {...prepared, data};
      const {plan, result:record} = prepared;
      node('budget').textContent = `${plan.model} · 1 次 · ${plan.groups} 组候选 · ${plan.input.sku_specs?.length ?? 0} 个 SKU · 预计费用上限 USD ${plan.cost.amount.toFixed(6)}（本机配置估算）`;
      node('input').textContent = JSON.stringify(plan.input, null, 2);
      node('plan').hidden = false;
      node('generate').textContent = record ? '确认并重新生成' : '确认并生成 5 组';
      if (record) showResult(record);
      node('generate').hidden = !paidEnabled || record?.state === 'complete' || record?.state === 'running';
      node('paid').parentElement.hidden = node('generate').hidden;
      if (record?.state === 'running') void recover(job);
      if (record?.state === 'complete') message('同一份资料已有结果，已打开保存的候选。');
    } catch (error) {
      if (id === job) message(error.message);
    } finally {
      if (id === job) { busy = false; buttons(); }
    }
  });

  node('generate').addEventListener('click', async () => {
    if (node('generate').disabled) return;
    const job = id, {data, plan, result:previous} = confirmed;
    resultVersion++;
    clearTimeout(pollTimer);
    busy = true; invalidate();
    message('已发起请求，正在生成候选标题…');
    try {
      const record = await api('generate', {input:data, key:plan.key, allow_paid:true, retry:previous?.id ?? null});
      if (job !== id) return;
      showResult({...record, latest_id:record.id});
      if (record.state === 'complete') {
        if (settingsPanel) settingsPanel.open = false;
        node('results').querySelector('h3')?.focus();
      }
      message(record.groups ? `已保存 ${record.groups.length} 组候选，SKU 候选可在规格列编辑。` : '请查看请求结果。');
    } catch (error) {
      if (job !== id) return;
      message(error.message + ' 请先核对已保存的结果。');
      void recover(job);
    } finally {
      if (job === id) { busy = false; buttons(); }
    }
  });

  panel.addEventListener('toggle', () => {
    if (!panel.open || !id) return;
    if (!source) void loadSource();
    if (!result && !busy) void recover(id);
  });
  node('period').addEventListener('change', () => { node('batch').value = ''; void loadSource(); });
  node('batch').addEventListener('change', loadSource);
  node('mismatch').addEventListener('change', invalidate);
  node('facts').addEventListener('input', invalidate);
  node('paid').addEventListener('change', buttons);
  for (const [key, event] of [['kind','change'], ['search','input']]) {
    node(key).addEventListener(event, () => { resetWordPage(); renderWords(); });
  }
  node('selected').addEventListener('click', () => {
    onlySelected = !onlySelected;
    if (onlySelected) node('picked').open = true;
    renderWords();
  });
  node('recommend').addEventListener('click', restoreRecommended);
  node('more').addEventListener('click', () => { visible += pageSize; renderWords(); });
  for (const key of Object.keys(fields)) input(key).addEventListener('input', () => {
    updateEvidence(key);
    saveDraft();
    invalidate();
  });

  root.addEventListener('listing:review', event => {
    const job = event.detail || null;
    if (id === job) return;
    id = job; source = null; applied = {}; busy = false; version++; resultVersion++;
    attempts = []; latestId = null; drafts = {};
    clearTimeout(pollTimer);
    panel.hidden = !id;
    panel.open = false;
    if (settingsPanel) settingsPanel.open = false;
    node('period').value = '7d';
    node('batch').replaceChildren(new Option('最新有数据的批次', ''));
    node('facts').value = '';
    node('kind').value = node('search').value = '';
    onlySelected = false;
    node('picked').open = true;
    resetWordPage();
    message(); showResult(null); restoreRecommended();
    if (id) {
      let attempt;
      try {
        const draft = JSON.parse(sessionStorage.getItem('listing-title-draft:' + id));
        if (draft) {
          applied = draft.applied;
          for (const key of Object.keys(fields)) input(key).value = draft.values[key];
        }
        drafts = JSON.parse(sessionStorage.getItem('listing-candidate-drafts:' + id)) || {};
        attempt = sessionStorage.getItem('listing-candidate-view:' + id);
      } catch { /* 使用导入的标题 */ }
      void recover(id, attempt);
    }
    for (const key of Object.keys(fields)) updateEvidence(key);
  });
})();
