/* 解析不下载；明确选择范围后准备，文件实际关闭成功后才记录已保存。 */
(() => {
  if (!document.querySelector('[data-bilibili]')) return;
  const el = id => document.getElementById('bv-' + id);
  const token = document.querySelector('meta[name="workbench-token"]')?.content || '';
  const store = {get: key => {try {return localStorage.getItem(key);} catch (_) {return null;}}, set: (key, value) => {try {localStorage.setItem(key, value);} catch (_) {}}};
  let row = null, generation = 0, requesting = false, saving = false, playable = false, aborter = null, assetExplicit = false;
  const activePhases = ['queued', 'resolving', 'downloading', 'merging'];
  const busy = () => !!row && activePhases.includes(row.phase);
  const locked = () => requesting || saving || busy();
  const size = n => !Number.isFinite(n) ? '大小未知' : n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + ' GiB' : n >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(1) + ' MiB' : Math.ceil(n / 1024) + ' KiB';
  const duration = n => !Number.isFinite(n) ? '—' : (n >= 3600 ? Math.floor(n / 3600) + ':' : '') + String(Math.floor(n / 60) % 60).padStart(2, '0') + ':' + String(Math.floor(n) % 60).padStart(2, '0');
  const date = n => n ? new Date(typeof n === 'number' ? n * 1000 : n).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '—';
  const count = n => Number.isFinite(n) ? new Intl.NumberFormat('zh-CN', {notation: 'compact', maximumFractionDigits: 1}).format(n) : '—';
  const say = (message, error = false) => {el('status').textContent = message; el('status').classList.toggle('is-error', error);};
  const catalog = () => row?.catalogs?.[el('episode').value];
  const videoSpec = () => catalog()?.videos.find(v => v.id === el('format').value);
  const audioSpec = () => catalog()?.audios.find(a => a.id === el('audio-format').value);
  const selectedAsset = () => row?.resources?.[el('asset').value];
  const url = (key, record = row) => '/api/tools/bilibili/' + record.id + '/assets/' + encodeURIComponent(key);
  async function api(path, body) {
    const response = await fetch('/api/tools/bilibili' + path, {method: body ? 'POST' : 'GET', headers: body ? {'Content-Type': 'application/json', 'X-Collection-Token': token} : {}, body: body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败，请重试');
    return data;
  }
  function options(element, values, preferred) {
    const signature = JSON.stringify(values);
    if (element.dataset.signature === signature) return;
    const old = preferred || element.value;
    element.replaceChildren(...values.map(([value, label]) => {const option = document.createElement('option'); option.value = value; option.textContent = label; return option;}));
    if (values.some(v => v[0] === old)) element.value = old;
    element.dataset.signature = signature;
  }
  function episodes() {
    const meta = row?.metadata;
    const result = new Map();
    for (const part of [...(meta?.collection?.items || []), ...(meta?.parts || [])]) result.set(part.id, part);
    return result;
  }
  function range() {
    if (el('scope').value === 'parts') return row?.metadata?.parts || [];
    if (el('scope').value === 'collection') return row?.metadata?.collection?.items || [];
    const episode = episodes().get(el('episode').value);
    return episode ? [episode] : [];
  }
  function codec(value) {
    return value.startsWith('avc') ? 'H.264' : value.startsWith('hev') || value.startsWith('hvc') ? 'H.265' : value.startsWith('av01') ? 'AV1' : value.startsWith('mp4a') ? 'AAC' : value;
  }
  function formatLabel(v) {
    return (v.width && v.height ? v.width + '×' + v.height : '原始规格') + ' · ' + codec(v.codec) + (v.bitrate ? ' · ' + Math.round(v.bitrate / 1000) + ' kbps' : '') + (v.combined ? ' · 含音频' : '');
  }
  function specNote() {
    const v = videoSpec(), a = audioSpec();
    const selected = range(), seconds = selected.reduce((sum, p) => sum + p.duration, 0);
    const bitrate = (v?.bitrate || 0) + (v?.combined ? 0 : a?.bitrate || 0);
    el('range-note').textContent = `${selected.length} 集 · 总时长 ${duration(seconds)} · ` + (bitrate ? '按当前码率估算约 ' + size(bitrate * seconds / 8) : '大小待实际资源返回后核对') + (selected.length > 1 ? '；各集实际规格和大小会逐一核对。' : '');
    el('spec').textContent = v ? formatLabel(v) + (v.fps ? ' · 帧率 ' + v.fps : '') + '；' + (a ? '音频 ' + codec(a.codec) + ' · ' + Math.round(a.bitrate / 1000) + ' kbps' : '音频包含在视频中') : '';
  }
  function batchAssets() {
    if (!row?.plan?.length || row.plan.length < 2 || row.plan.some(p => p.state !== 'prepared')) return [];
    return row.plan.map(p => [p.resource_key, row.resources[p.resource_key]]).filter(([, a]) => a?.kind === 'video' && a.ready);
  }
  function controls() {
    const lock = locked(), asset = selectedAsset();
    for (const id of ['start', 'login', 'episode', 'scope', 'format', 'audio-format', 'cover-format', 'settings', 'cleanup', 'asset']) el(id).disabled = lock;
    for (const id of ['prepare', 'audio', 'cover-prepare']) el(id).disabled = lock || !row?.metadata_ready || !catalog();
    el('pause').disabled = saving || !busy();
    el('cancel').disabled = saving || !row || ![...activePhases, 'paused', 'failed', 'partial', 'interrupted'].includes(row.phase);
    el('resume').disabled = lock || !row?.operation || !['paused', 'failed', 'partial', 'cancelled', 'interrupted', 'waiting'].includes(row.phase);
    el('reviewed').disabled = saving || !asset?.ready || asset.kind === 'video' && !playable;
    el('save').disabled = lock || !asset?.ready || asset.kind === 'video' && (!playable || !el('reviewed').checked);
    const batch = batchAssets();
    el('save-all').hidden = row?.operation?.scope === 'single' || !row?.plan?.length || row.plan.length < 2;
    el('save-all').disabled = lock || batch.length !== row?.plan?.length || !el('reviewed').checked || !playable;
    el('stop-save').hidden = !saving;
  }
  function resetPreview() {
    playable = false; el('reviewed').checked = false;
    el('video').pause(); el('audio-player').pause();
  }
  function preview() {
    const key = el('asset').value, asset = selectedAsset();
    for (const id of ['video', 'audio-player', 'image']) el(id).hidden = true;
    if (!asset?.ready) {el('output').hidden = true; resetPreview(); return;}
    if (!assetExplicit && (asset.episode_id !== el('episode').value || asset.kind === 'video' && (asset.video_id !== el('format').value || asset.audio_id !== el('audio-format').value))) {
      el('output').hidden = true; resetPreview(); return;
    }
    el('output').hidden = false;
    const id = asset.kind === 'video' ? 'video' : asset.kind === 'audio' ? 'audio-player' : 'image';
    const player = el(id), source = url(key);
    player.hidden = false;
    if (player.getAttribute('src') !== source) {resetPreview(); player.src = source;}
    else if (id === 'video') playable = !player.error && player.readyState >= 2 && player.videoWidth > 0;
    el('preview-note').textContent = asset.filename + ' · ' + size(asset.bytes) + (asset.kind === 'video' ? '；请核对画面和声音。' : '');
    el('reviewed').closest('label').hidden = asset.kind !== 'video';
    saved(); controls();
  }
  function saved() {
    let values = [];
    try {values = JSON.parse(store.get('bilibili.saves.' + row?.id) || '[]'); if (!Array.isArray(values)) values = [];} catch (_) {}
    el('saved').textContent = values.length ? '本浏览器曾完成保存：' + values.map(v => v.filename + '（' + date(v.time) + '）').join('；') : '';
  }
  function remember(record, key, name) {
    let values = [];
    try {const value = JSON.parse(store.get('bilibili.saves.' + record.id) || '[]'); if (Array.isArray(value)) values = value.filter(v => v.key !== key);} catch (_) {}
    values.push({key, filename: name, time: new Date().toISOString()});
    store.set('bilibili.saves.' + record.id, JSON.stringify(values));
  }
  function display(value) {
    row = value;
    el('result').hidden = !row.metadata_ready;
    if (row.metadata_ready) {
      const meta = row.metadata;
      const selectedInfo = row.catalogs?.[row.selected]?.episode;
      const sameSubmission = !selectedInfo || selectedInfo.bvid === meta.bvid;
      const stat = selectedInfo?.stats || (sameSubmission ? meta.stats : {}) || {};
      el('title').textContent = selectedInfo?.submission_title || meta.title; el('author').textContent = selectedInfo?.author || (sameSubmission ? meta.author : '未提供');
      el('stats').textContent = `播放 ${count(stat.view)} · 赞 ${count(stat.like)} · 评论 ${count(stat.reply)} · 收藏 ${count(stat.favorite)} · 分享 ${count(stat.share)}（解析时快照）`;
      el('times').textContent = '发布：' + date(selectedInfo?.published || (sameSubmission ? meta.published : null)) + ' · 解析：' + date(selectedInfo?.parsed_at || meta.parsed_at);
      options(el('episode'), [...episodes().values()].map((p, i) => [p.id, (meta.collection.items.length > 1 ? String(i + 1).padStart(2, '0') + ' · ' : '') + 'P' + p.page + ' · ' + p.title]), row.selected);
      const scopes = [['single', '只下载当前一集']];
      if (meta.parts.length > 1) scopes.push(['parts', '下载当前投稿全部 ' + meta.parts.length + ' P']);
      if (meta.collection.items.length > 1) scopes.push(['collection', '下载合集全部 ' + meta.collection.items.length + ' 集']);
      options(el('scope'), scopes);
      const part = episodes().get(el('episode').value), cat = catalog();
      el('episode-note').textContent = part ? '当前集：' + duration(part.duration) + ' · ' + part.bvid + ' · P' + part.page + (meta.collection.title ? ' · 合集：' + meta.collection.title : '') : '';
      options(el('format'), (cat?.videos || []).map(format => [format.id, formatLabel(format)]));
      options(el('audio-format'), cat?.audios.length ? cat.audios.map(a => [a.id, codec(a.codec) + ' · ' + Math.round(a.bitrate / 1000) + ' kbps']) : [['', '音频包含在视频中']]);
      el('permission').textContent = cat?.permission_note || '请先获取当前集的实际规格。';
      for (const kind of ['cover', 'avatar']) {
        const ready = sameSubmission && row.resources[kind]?.ready;
        el(kind).hidden = !ready;
        if (ready && el(kind).getAttribute('src') !== url(kind)) el(kind).src = url(kind);
      }
      el('cover-empty').hidden = !!(sameSubmission && row.resources.cover?.ready);
      specNote();
    }
    el('work').hidden = !row.plan?.length && !busy();
    const progress = el('progress');
    if (row.phase === 'merging' && row.duration && row.processed_seconds) progress.value = Math.min(99, row.processed_seconds / row.duration * 100);
    else if (row.total) progress.value = Math.min(100, row.loaded / row.total * 100);
    else if (row.phase === 'prepared') progress.value = 100;
    else progress.removeAttribute('value');
    el('progress-note').textContent = (row.stage ? row.stage + ' · ' : '') + (row.phase === 'merging' ? '合并阶段 · ' + duration(row.processed_seconds || 0) : size(row.loaded || 0) + ' / ' + (row.total ? size(row.total) : '总大小未知') + (row.speed ? ' · ' + size(row.speed) + '/s' : '') + (Number.isFinite(row.eta) ? ' · 预计剩余 ' + duration(row.eta) : ''));
    if (row.phase === 'prepared') el('progress-note').textContent = '本次已完成 ' + (row.plan || []).filter(p => p.state === 'prepared').length + ' / ' + (row.plan || []).length + ' 集的素材准备。';
    el('plan').replaceChildren(...(row.plan || []).map((p, i) => {const item = document.createElement('div'), title = document.createElement('strong'), note = document.createElement('p'); title.textContent = String(i + 1).padStart(2, '0') + ' · ' + p.title; note.className = 'ui-form-note'; note.textContent = p.message; item.append(title, note); return item;}));
    const assets = Object.entries(row.resources || {}).filter(([key, a]) => a.ready && !['avatar', 'cover'].includes(key));
    const previous = el('asset').value;
    const recent = row.plan?.find(p => p.episode_id === row.selected && p.state === 'prepared')?.resource_key;
    options(el('asset'), assets.map(([key, a]) => [key, (a.kind === 'video' ? '视频 · ' : a.kind === 'audio' ? '声音 · ' : '封面 · ') + a.filename]), recent);
    if (previous !== el('asset').value) resetPreview();
    preview();
    if (!saving) say(row.message, ['failed', 'partial'].includes(row.phase));
    controls();
    return busy();
  }
  async function poll(id, version) {
    if (version !== generation) return;
    try {
      const value = await api('/' + id);
      if (version !== generation) return;
      if (display(value)) setTimeout(() => poll(id, version), 1000); else history();
    } catch (error) {say(error.message + '；刷新后可核对原任务。', true);}
  }
  async function load(id) {
    if (saving) return;
    const version = ++generation;
    resetPreview(); assetExplicit = false; row = null; el('result').hidden = true; el('output').hidden = true;
    for (const id of ['episode', 'scope', 'format', 'audio-format', 'asset']) {el(id).dataset.signature = ''; el(id).replaceChildren();}
    store.set('bilibili.active', id);
    const value = await api('/' + id);
    if (version !== generation) return;
    el('input').value = value.input;
    if (display(value)) setTimeout(() => poll(id, version), 800);
  }
  async function action(body) {
    if (requesting || saving || !row) return;
    requesting = true; controls();
    try {
      const id = row.id;
      await api('/' + id, body);
      const version = ++generation;
      if (display(await api('/' + id))) setTimeout(() => poll(id, version), 600); else history();
    } catch (error) {say(error.message, true);} finally {requesting = false; controls();}
  }
  async function start(interactive) {
    if (locked()) return;
    requesting = true; controls(); say('正在提交解析任务…');
    try {const value = await api('', {input: el('input').value, interactive}); await load(value.id); history();}
    catch (error) {say(error.message, true);} finally {requesting = false; controls();}
  }
  function prepare(kind) {
    if (locked() || !catalog()) return;
    resetPreview(); assetExplicit = false;
    action({action: 'prepare', kind, episode_id: el('episode').value, video_id: el('format').value, audio_id: el('audio-format').value,
      scope: kind === 'video' ? el('scope').value : 'single', output_format: kind === 'cover' ? el('cover-format').value : 'original'});
  }
  async function history() {
    try {
      const data = await api('');
      el('disk').textContent = '缓存占用 ' + size(data.cache_bytes) + ' · 临时磁盘剩余 ' + size(data.free_bytes) + ' · ' + data.temp_dir + (data.warning ? '；缓存超过提醒容量，可清理或更换目录，仍可继续下载。' : '');
      if (!el('temp-dir').dataset.edited) {
        el('temp-dir').value = data.options.temp_dir || ''; el('cache-warn').value = data.options.cache_warn_gib; el('idle').value = data.options.idle_hours;
      }
      el('history').replaceChildren();
      for (const record of data.items) {
        const item = document.createElement('div'), title = document.createElement('strong'), note = document.createElement('p'), actions = document.createElement('div');
        title.textContent = record.metadata?.title || '待解析视频'; note.className = 'ui-form-note'; note.textContent = date(record.created) + ' · ' + record.message; actions.className = 'module-actions';
        const open = document.createElement('button'); open.className = 'ui-button ui-secondary'; open.type = 'button'; open.textContent = '查看记录'; open.disabled = saving || busy(); open.onclick = () => load(record.id).catch(error => say(error.message, true)); actions.append(open);
        if (record.task_id) {const link = document.createElement('a'); link.className = 'ui-button ui-secondary'; link.href = '/tasks/' + encodeURIComponent(record.task_id); link.textContent = '任务与日志'; actions.append(link);}
        const remove = document.createElement('button'); remove.className = 'ui-button ui-secondary'; remove.type = 'button'; remove.textContent = '删除缓存'; remove.disabled = saving || activePhases.includes(record.phase);
        remove.onclick = async () => {try {await api('/' + record.id, {action: 'delete_cache'}); if (row?.id === record.id) display(await api('/' + record.id)); history();} catch (error) {say(error.message, true);}};
        actions.append(remove); item.append(title, note, actions);
        const audit = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = '查看本记录历次任务'; audit.append(summary);
        for (const attempt of record.attempts || []) {const p = document.createElement('p'), a = document.createElement('a'); a.href = '/tasks/' + encodeURIComponent(attempt.task_id); a.textContent = date(attempt.created) + ' · ' + attempt.action + (attempt.episode_id ? ' · ' + attempt.episode_id : ''); p.append(a); audit.append(p);}
        item.append(audit); el('history').append(item);
        const resources = document.createElement('details'), resourceTitle = document.createElement('summary');
        resourceTitle.textContent = '素材与保存摘要'; resources.append(resourceTitle);
        for (const [key, asset] of Object.entries(record.resources || {})) {
          if (['cover', 'avatar'].includes(key)) continue;
          const p = document.createElement('p'); p.className = 'ui-form-note';
          p.textContent = asset.filename + ' · ' + size(asset.bytes) + ' · ' + (asset.ready ? '缓存可用' : '需重新准备'); resources.append(p);
        }
        let saves = [];
        try {const value = JSON.parse(store.get('bilibili.saves.' + record.id) || '[]'); if (Array.isArray(value)) saves = value;} catch (_) {}
        for (const save of saves) {const p = document.createElement('p'); p.className = 'ui-form-note'; p.textContent = '本浏览器曾保存：' + save.filename + '（' + date(save.time) + '）'; resources.append(p);}
        item.append(resources);
      }
      if (!data.items.length) el('history').textContent = '暂无解析记录。';
    } catch (error) {say(error.message, true);}
  }
  async function writeAsset(handle, record, key) {
    const asset = record.resources[key];
    let writable = null, reader = null;
    try {
      writable = await handle.createWritable();
      const response = await fetch(url(key, record), {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Collection-Token': token}, body: JSON.stringify(asset.kind === 'video' ? {reviewed: true} : {save: true}), signal: aborter.signal});
      if (!response.ok) {const data = await response.json(); throw new Error(data.error || '保存失败');}
      reader = response.body.getReader(); let received = 0;
      while (true) {
        if (aborter.signal.aborted) throw new DOMException('已停止', 'AbortError');
        const {done, value} = await reader.read(); if (done) break;
        await writable.write(value); received += value.length;
        say('正在保存 ' + handle.name + ' · ' + size(received) + ' / ' + size(asset.bytes));
      }
      if (received !== asset.bytes) throw new Error('文件接收不完整，请重新保存');
      if (aborter.signal.aborted) throw new DOMException('已停止', 'AbortError');
      await writable.close(); writable = null;
      remember(record, key, handle.name);
    } finally {
      if (reader) try {await reader.cancel();} catch (_) {}
      if (writable) try {await writable.abort();} catch (_) {}
    }
  }
  async function newFile(directory, filename) {
    const dot = filename.lastIndexOf('.'), stem = filename.slice(0, dot), ext = filename.slice(dot);
    for (let i = 0; i < 10000; i++) {
      if (aborter.signal.aborted) throw new DOMException('已停止', 'AbortError');
      const name = i ? stem + ' (' + i + ')' + ext : filename;
      try {await directory.getFileHandle(name);}
      catch (error) {if (error.name === 'NotFoundError') return directory.getFileHandle(name, {create: true}); throw error;}
    }
    throw new Error('同名文件过多，请选择其他文件夹');
  }
  async function save(all = false) {
    if (locked()) return;
    const asset = selectedAsset(), batch = all ? batchAssets() : [[el('asset').value, asset]], record = row;
    if (!asset?.ready || asset.kind === 'video' && (!playable || !el('reviewed').checked) || all && batch.length !== row.plan.length) return;
    if (all ? !window.showDirectoryPicker : !window.showSaveFilePicker) {say('请用桌面版Chrome或Edge打开本机页面，以便自行选择保存位置。', true); return;}
    saving = true; aborter = new AbortController(); controls();
    try {
      // 原生选择器必须直接由本次点击触发，之前不能先请求后端。
      const handle = all ? await window.showDirectoryPicker({id: 'bilibili-videos', mode: 'readwrite'}) : await window.showSaveFilePicker({id: 'bilibili-' + asset.kind, suggestedName: asset.filename, types: [{description: '媒体素材', accept: {[asset.mime]: ['.' + asset.filename.split('.').pop()]}}]});
      let complete = 0;
      for (const [key, resource] of batch) {
        if (aborter.signal.aborted) throw new DOMException('已停止', 'AbortError');
        const file = all ? await newFile(handle, resource.filename) : handle;
        await writeAsset(file, record, key); complete++; saved();
        say('已保存 ' + complete + ' / ' + batch.length + ' 个文件');
      }
      say(all ? '本次全部 ' + complete + ' 集已保存。' : '已保存：' + handle.name);
    } catch (error) {aborter.abort(); say(error.name === 'AbortError' ? '已停止保存；已完成的文件保留，可复用缓存继续保存。' : error.message + '；未完成的文件不记为保存成功。', error.name !== 'AbortError');}
    finally {saving = false; aborter = null; controls(); saved(); history();}
  }
  el('start').onclick = () => start(false); el('login').onclick = () => start(true);
  el('episode').onchange = () => {resetPreview(); assetExplicit = false; action({action: 'select', episode_id: el('episode').value});};
  for (const id of ['format', 'audio-format']) el(id).onchange = () => {
    resetPreview(); assetExplicit = false; specNote();
    const ready = Object.entries(row.resources).find(([, a]) => a.ready && a.kind === 'video' && a.episode_id === el('episode').value && a.video_id === el('format').value && a.audio_id === el('audio-format').value);
    if (ready) {el('asset').value = ready[0]; preview();}
    else if (selectedAsset()?.kind === 'video') {el('output').hidden = true; say('所选版本尚未准备，请确认范围并准备视频。');}
    else preview();
    controls();
  };
  el('scope').onchange = () => {resetPreview(); specNote(); preview(); controls();};
  el('asset').onchange = () => {resetPreview(); assetExplicit = true; preview();}; el('reviewed').onchange = controls;
  el('prepare').onclick = () => prepare('video'); el('audio').onclick = () => prepare('audio'); el('cover-prepare').onclick = () => prepare('cover');
  for (const id of ['pause', 'resume', 'cancel']) el(id).onclick = () => action({action: id});
  el('save').onclick = () => save(false); el('save-all').onclick = () => save(true); el('stop-save').onclick = () => aborter?.abort();
  el('copy').onclick = async () => {try {await navigator.clipboard.writeText(episodes().get(el('episode').value).url); say('已复制当前集链接。');} catch (_) {say('复制失败，可从分集信息中核对BV号。', true);}};
  for (const id of ['temp-dir', 'cache-warn', 'idle']) el(id).oninput = () => {el('temp-dir').dataset.edited = 'true';};
  el('settings').onclick = async () => {try {await api('/settings', {temp_dir: el('temp-dir').value, cache_warn_gib: Number(el('cache-warn').value), idle_hours: Number(el('idle').value)}); delete el('temp-dir').dataset.edited; await history(); say('设置已生效，新解析记录使用新临时目录。');} catch (error) {say(error.message, true);}};
  el('cleanup').onclick = async () => {try {const data = await api('/cleanup', {action: 'cleanup'}); await history(); if (row) display(await api('/' + row.id)); say('已清理 ' + data.cleaned + ' 条过期缓存。');} catch (error) {say(error.message, true);}};
  el('video').onloadedmetadata = () => {el('video').currentTime = Math.min(.1, el('video').duration / 2);};
  el('video').onloadeddata = () => {playable = !el('video').error && el('video').videoWidth > 0 && selectedAsset()?.ready && el('video').getAttribute('src') === url(el('asset').value); controls();};
  el('video').onerror = () => {playable = false; el('reviewed').checked = false; controls(); if (selectedAsset()?.kind === 'video') say('当前浏览器无法预览这个编码，请选择H.264兼容版本后重新准备。', true);};
  el('video').onplay = () => el('audio-player').pause(); el('audio-player').onplay = () => el('video').pause();
  for (const id of ['cover', 'avatar', 'image']) el(id).onerror = () => {el(id).hidden = true;};
  controls(); history(); const previous = store.get('bilibili.active'); if (previous) load(previous).catch(error => say(error.message, true));
})();
