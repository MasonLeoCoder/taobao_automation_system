(() => {
  'use strict';
  const root = document.getElementById('listing');
  const node = key => document.getElementById('listing-ai-' + key);
  const panel = document.getElementById('listing-ai');
  if (!panel) return;
  const settingsPanel = node('settings');
  const fields = {title:'宝贝标题', guide:'导购标题'};
  const lengths = {title:[56, 60], guide:[28, 30]};
  const kinds = {search:'搜索词', trend:'趋势词', core:'核心词', modifier:'修饰词'};
  const input = key => document.getElementById('listing-' + key);
  const base = root.dataset.api.replace(/\/preflight$/, '');
  const token = document.querySelector('meta[name="workbench-token"]').content;
  const pageSize = 20;
  const maxTerms = 40;
  const paidEnabled = panel.dataset.paidEnabled === 'yes';
  let id, source, confirmed, result, applied = {}, selected = new Set(), locks;
  let version = 0, resultVersion = 0, busy = false, visible = pageSize, onlySelected = false, pollTimer;

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
      restoreRecommended();
    } catch (error) {
      if (request === version) node('source').textContent = '排行批次读取失败。' + error.message;
    }
  }

  function renderWords() {
    node('count').textContent = `已选 ${selected.size} / ${maxTerms}`;
    node('selected').setAttribute('aria-pressed', String(onlySelected));
    node('selected').textContent = onlySelected ? '查看全部' : '查看已选';
    const query = node('search').value.trim().toLowerCase(), kind = node('kind').value;
    const rows = (source?.words || []).filter(term => (!kind || term.type === kind) &&
      (!onlySelected || selected.has(term.id)) && term.keyword.toLowerCase().includes(query));
    node('words').replaceChildren(...rows.slice(0, visible).map(term => {
      const row = element('li');
      const pick = element('label'), checkbox = element('input');
      checkbox.type = 'checkbox';
      checkbox.checked = selected.has(term.id);
      checkbox.disabled = !checkbox.checked && selected.size >= maxTerms;
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(term.id);
        else {
          selected.delete(term.id);
          Object.values(locks).forEach(ids => ids.delete(term.id));
        }
        invalidate();
        renderWords();
        node('words').querySelector(`[data-pick="${term.id}"]`)?.focus();
      });
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
    }));
    if (!rows.length) node('words').append(element('li', '没有符合筛选条件的词条。'));
    node('more').hidden = rows.length <= visible;
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
      if (id && Object.keys(applied).length) {
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

  function showResult(record) {
    result = record;
    if (record?.sku_titles) {
      root.dispatchEvent(new CustomEvent('listing:sku-suggestions', {detail:{job:id, rows:record.sku_titles}}));
    }
    const box = node('results');
    box.hidden = !record;
    box.replaceChildren();
    if (!record) return;
    if (!record.groups) {
      box.append(element('p', record.error || 'AI 正在生成标题。刷新页面后可以继续查看结果。'));
      return;
    }
    const header = element('div');
    header.className = 'listing-ai-result-head';
    const heading = element('div');
    const headingTitle = element('h3', record.simulated ? '候选标题（模拟演示）' : '候选标题');
    headingTitle.tabIndex = -1;
    heading.append(headingTitle, element('p', `${record.groups.length} 组 · ${coverage(record.plan.types)}`));
    const settings = element('button', '调整生成设置');
    settings.type = 'button';
    settings.className = 'ui-button ui-secondary';
    header.append(heading);
    if (settingsPanel) {
      settings.addEventListener('click', () => {
        settingsPanel.open = true;
        settingsPanel.querySelector('summary').focus();
      });
      header.append(settings);
    }
    box.append(header, sourceDetails(record.plan.source, record.plan.types));
    if (record.state === 'invalid') box.append(element('p', '本次候选均未通过校验。'));
    if (record.groups.length < record.plan.groups) {
      box.append(element('p', `这次返回 ${record.groups.length} 组，少于请求的 ${record.plan.groups} 组。`));
    }
    const list = element('div');
    list.className = 'listing-ai-candidates';
    record.groups.forEach((group, index) => {
      const card = element('article');
      card.className = 'listing-ai-group';
      card.append(element('h4', '第 ' + (index + 1) + ' 组'));
      const grid = element('div');
      grid.className = 'listing-ai-candidate-grid';
      for (const [key, label] of Object.entries(fields)) {
        const title = group[key], cell = element('div'), line = element('p');
        cell.className = 'listing-ai-candidate';
        line.className = 'listing-ai-title-text';
        marked(line, title.segments);
        if (!title.value) line.textContent = '—';
        const [target, limit] = lengths[key];
        const {width, fill_value: fillValue} = title;
        const count = element('span', `${width} / ${limit}`);
        count.className = 'listing-ai-title-length';
        count.title = `分隔空格不计，建议 ${target}～${limit} 字符`;
        if (width > limit) count.classList.add('is-over');
        const fill = element('button', '填入' + label);
        fill.className = 'ui-button ui-secondary';
        fill.type = 'button';
        fill.disabled = Boolean(title.issue);
        fill.addEventListener('click', () => applyTitle(key, fillValue, record.plan));
        const bar = element('div');
        bar.className = 'listing-ai-candidate-bar';
        const labelAndCount = element('div');
        labelAndCount.append(element('strong', label), count);
        bar.append(labelAndCount, fill);
        cell.append(bar, line);
        if (title.used.length) {
          const ranks = element('ul');
          ranks.className = 'listing-ai-ranks';
          ranks.append(...title.used.map(item => element('li', rankLabel(item))));
          cell.append(ranks);
        } else {
          cell.append(element('small', '无排行词命中'));
        }
        if (title.issue) {
          const issue = element('p', title.issue);
          issue.className = 'listing-ai-issue';
          cell.append(issue);
        } else {
          if (width < target) {
            const note = element('small', `低于建议长度 ${target}～${limit} 字符`);
            note.className = 'listing-ai-advice';
            cell.append(note);
          }
          const used = title.used.map(item => item.keyword);
          for (const [index, word] of used.entries()) {
            const match = used.slice(index + 1).find(other => similarWords(word, other));
            if (!match) continue;
            const note = element('small', `用词相近 · ${word}／${match}`);
            note.className = 'listing-ai-advice';
            cell.append(note);
            break;
          }
        }
        grid.append(cell);
      }
      card.append(grid);
      list.append(card);
    });
    box.append(list);
  }

  async function recover(job) {
    const request = ++resultVersion;
    try {
      const record = await api('result');
      if (id !== job || request !== resultVersion) return;
      if (record) showResult(record);
      if (settingsPanel && !record?.groups && !confirmed) settingsPanel.open = true;
      if (record?.state === 'running') pollTimer = setTimeout(() => recover(job), 2000);
    } catch {
      if (id === job && request === resultVersion) message('暂时无法读取已保存的标题结果。可以刷新页面核对。');
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
      showResult(record);
      if (record.state === 'complete') {
        if (settingsPanel) settingsPanel.open = false;
        node('results').querySelector('h3')?.focus();
      }
      const count = record.sku_titles?.filter(row => row.title).length ?? 0;
      message(record.state === 'complete' ?
        `已保存 ${record.groups.length} 组候选，${count} 条合格 SKU 标题可在规格列核对。` : '请查看请求结果。');
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
  node('kind').addEventListener('change', () => { visible = pageSize; renderWords(); });
  node('search').addEventListener('input', () => { visible = pageSize; renderWords(); });
  node('selected').addEventListener('click', () => { onlySelected = !onlySelected; visible = pageSize; renderWords(); });
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
    clearTimeout(pollTimer);
    panel.hidden = !id;
    panel.open = false;
    if (settingsPanel) settingsPanel.open = false;
    node('period').value = '7d';
    node('batch').replaceChildren(new Option('最新有数据的批次', ''));
    node('facts').value = '';
    node('kind').value = node('search').value = '';
    onlySelected = false; visible = pageSize;
    message(); showResult(null); restoreRecommended();
    if (id) {
      try {
        const draft = JSON.parse(sessionStorage.getItem('listing-title-draft:' + id));
        if (draft) {
          applied = draft.applied;
          for (const key of Object.keys(fields)) input(key).value = draft.values[key];
        }
      } catch { /* 使用导入的标题 */ }
      void recover(id);
    }
    for (const key of Object.keys(fields)) updateEvidence(key);
  });
})();
