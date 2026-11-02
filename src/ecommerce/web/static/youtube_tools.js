/* 当前单视频：规格变化清除确认，实际文件写完后才记已保存。 */
(() => {
  if (!document.querySelector('[data-youtube]')) return;
  const el = id => document.getElementById('yt-' + id);
  const token = document.querySelector('meta[name="workbench-token"]')?.content || '';
  const storage = {get: key => {try {return localStorage.getItem(key);} catch (_) {return null;}}, set: (key, value) => {try {localStorage.setItem(key, value);} catch (_) {}}};
  let row = null, generation = 0, requesting = false, saving = false, playable = false, controller = null, explicit = false, wanted = null;
  const active = ['queued', 'resolving', 'downloading', 'merging', 'waiting'];
  const busy = () => !!row && active.includes(row.phase);
  const locked = () => requesting || saving || busy();
  const video = () => row?.catalog?.videos.find(v => v.id === el('format').value);
  const audio = () => row?.catalog?.audios.find(a => a.id === el('audio-format').value);
  const audioId = () => video()?.combined ? '' : el('audio-format').value;
  const asset = () => row?.resources?.[el('asset').value];
  const url = (key, record = row) => '/api/tools/youtube/' + record.id + '/assets/' + encodeURIComponent(key);
  const size = n => !Number.isFinite(n) ? '大小未知' : n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + ' GiB' : n >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(1) + ' MiB' : Math.ceil(n / 1024) + ' KiB';
  const duration = n => !Number.isFinite(n) ? '—' : (n >= 3600 ? Math.floor(n / 3600) + ':' : '') + String(Math.floor(n / 60) % 60).padStart(2, '0') + ':' + String(Math.floor(n) % 60).padStart(2, '0');
  const date = n => n ? new Date(typeof n === 'number' ? n * 1000 : n).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '—';
  const count = n => Number.isFinite(n) ? new Intl.NumberFormat('zh-CN', {notation: 'compact', maximumFractionDigits: 1}).format(n) : '—';
  const codec = c => c.startsWith('avc') ? 'H.264' : c.startsWith('av01') ? 'AV1' : c.startsWith('vp9') || c.startsWith('vp09') ? 'VP9' : c.startsWith('mp4a') ? 'AAC' : c;
  const say = (text, error = false) => {el('status').textContent = text; el('status').classList.toggle('is-error', error);};
  async function api(path, body) {
    const response = await fetch('/api/tools/youtube' + path, {method: body ? 'POST' : 'GET', headers: body ? {'Content-Type': 'application/json', 'X-Collection-Token': token} : {}, body: body ? JSON.stringify(body) : undefined});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || '请求失败，请重试'); return data;
  }
  function options(element, values, preferred) {
    const signature = JSON.stringify(values);
    if (element.dataset.signature === signature) return;
    const old = preferred || element.value;
    element.replaceChildren(...values.map(([value, label]) => {const o = document.createElement('option'); o.value = value; o.textContent = label; return o;}));
    if (values.some(v => v[0] === old)) element.value = old;
    element.dataset.signature = signature;
  }
  function audioOptions() {
    const audios = row?.catalog?.audios || [];
    const languages = [...new Set(audios.map(a => a.language || 'unknown'))];
    const operationAudio = audios.find(a => a.id === (row?.selection?.audio_id || row?.operation?.audio_id));
    const preferred = operationAudio?.language || audios.find(a => a.original)?.language || languages[0];
    options(el('language'), languages.map(language => [language, (language === 'unknown' ? '语言未提供' : language) + (audios.some(a => (a.language || 'unknown') === language && a.original) ? ' · 原声' : '')]), el('language').value || preferred);
    const filtered = audios.filter(a => (a.language || 'unknown') === el('language').value);
    options(el('audio-format'), filtered.map(a => [a.id, codec(a.codec) + ' · ' + Math.round(a.bitrate / 1000) + ' kbps' + (a.note.includes('DRC') ? ' · 动态范围压缩' : '')]), el('audio-format').value || operationAudio?.id);
  }
  function details() {
    const v = video(), a = audio();
    const bytes = v && (v.bytes || v.estimated_bytes), audioBytes = v?.combined ? 0 : a && (a.bytes || a.estimated_bytes);
    el('size').textContent = v ? '当前完整视频 · ' + duration(row.metadata.duration) + ' · ' + (bytes && (v.combined || audioBytes) ? '预计 ' + size(bytes + audioBytes) : '大小待实际下载核对') + ' · 不自动降低画质' : '';
    el('spec').textContent = v?.combined ? '这个版本自带音频，语言：' + (v.language || '未提供') + '；下方独立音轨不会用于这个版本。' : a ? '所选音轨：' + (a.original ? '原声 · ' : '') + (a.language || '语言未提供') + ' · ' + codec(a.codec) + (a.channels ? ' · ' + a.channels + '声道' : '') : '当前没有独立音轨可选。';
  }
  function controls() {
    const lock = locked(), item = asset();
    for (const id of ['start', 'login', 'format', 'cover-format', 'asset', 'settings', 'cleanup']) el(id).disabled = lock;
    for (const id of ['language', 'audio-format']) el(id).disabled = lock || !!video()?.combined;
    for (const id of ['prepare', 'audio', 'cover-prepare']) el(id).disabled = lock || !row?.metadata_ready;
    el('pause').disabled = saving || !busy() || row?.phase === 'waiting';
    el('resume').disabled = lock || !row?.operation || !['paused', 'failed', 'cancelled', 'interrupted', 'waiting'].includes(row.phase);
    el('cancel').disabled = saving || !row || row.phase === 'waiting' || ![...active, 'paused', 'failed', 'interrupted'].includes(row.phase);
    el('reviewed').disabled = saving || !item?.ready || item.kind === 'video' && !playable;
    el('save').disabled = lock || !item?.ready || item.kind === 'video' && (!playable || !el('reviewed').checked);
    el('stop-save').hidden = !saving;
  }
  function reset() {playable = false; el('reviewed').checked = false; el('video').pause(); el('audio-player').pause();}
  function preview() {
    const item = asset(), key = el('asset').value;
    for (const id of ['video', 'audio-player', 'image']) el(id).hidden = true;
    const matches = item?.kind !== 'video' || item.video_id === el('format').value && item.audio_id === audioId();
    if (!item?.ready || !explicit && !matches) {el('output').hidden = true; reset(); controls(); return;}
    el('output').hidden = false;
    const player = el(item.kind === 'video' ? 'video' : item.kind === 'audio' ? 'audio-player' : 'image');
    player.hidden = false;
    if (player.getAttribute('src') !== url(key)) {reset(); player.src = url(key);}
    else if (item.kind === 'video') playable = !player.error && player.readyState >= 2 && player.videoWidth > 0;
    el('preview-note').textContent = item.filename + ' · ' + size(item.bytes);
    el('reviewed').closest('label').hidden = item.kind !== 'video';
    saved(); controls();
  }
  function saveRecords(record = row) {
    try {const values = JSON.parse(storage.get('youtube.saves.' + record?.id) || '[]'); return Array.isArray(values) ? values : [];} catch (_) {return [];}
  }
  function saved() {const values = saveRecords(); el('saved').textContent = values.length ? '本浏览器曾完成保存：' + values.map(v => v.filename + '（' + date(v.time) + '）').join('；') : '';}
  function display(value) {
    row = value;
    el('result').hidden = !row.metadata_ready;
    if (row.metadata_ready) {
      const meta = row.metadata, stat = meta.stats || {};
      el('title').textContent = meta.title; el('author').textContent = meta.author;
      el('meta').textContent = `时长 ${duration(meta.duration)} · 播放 ${count(stat.view_count)} · 赞 ${count(stat.like_count)} · 评论 ${count(stat.comment_count)}（解析时快照）`;
      el('times').textContent = '发布：' + (meta.published ? date(meta.published) : meta.upload_date || '—') + ' · 解析：' + date(meta.parsed_at);
      options(el('format'), row.catalog.videos.map(v => [v.id, v.height + 'P · ' + (v.fps || '—') + '帧 · ' + codec(v.codec) + (v.combined ? ' · 含音频 ' + (v.language || '未知语言') : '')]), el('format').value || row.selection?.video_id || row.operation?.video_id);
      audioOptions(); details();
      const hasCover = !!row.resources.cover?.ready; el('cover').hidden = !hasCover; el('cover-empty').hidden = hasCover;
      if (hasCover && el('cover').getAttribute('src') !== url('cover')) el('cover').src = url('cover');
      const ready = Object.entries(row.resources).filter(([key, a]) => key !== 'cover' && a.ready);
      options(el('asset'), ready.map(([key, a]) => [key, (a.kind === 'video' ? '视频 · ' : a.kind === 'audio' ? '声音 · ' : '封面 · ') + a.filename]), wanted || row.operation?.key);
      if (wanted && row.resources[wanted]?.ready) {el('asset').value = wanted; wanted = null;}
      preview();
    } else {el('output').hidden = true; reset();}
    el('work').hidden = !row.operation && !busy();
    const p = el('progress');
    if (row.phase === 'prepared') p.value = 100;
    else if (row.phase === 'merging' && row.metadata?.duration && row.processed_seconds) p.value = Math.min(99, row.processed_seconds / row.metadata.duration * 100);
    else if (row.total) p.value = row.loaded / row.total * 100;
    else p.removeAttribute('value');
    el('progress-note').textContent = row.phase === 'prepared' ? '所选素材已准备，可以预览或保存。' : row.phase === 'merging' ? (row.stage || '合并') + ' · 已处理 ' + duration(row.processed_seconds || 0) : (row.stage ? row.stage + ' · ' : '') + size(row.loaded || 0) + ' / ' + (row.total ? size(row.total) : '总大小未知') + (row.speed ? ' · ' + size(row.speed) + '/s' : '') + (Number.isFinite(row.eta) ? ' · 预计剩余 ' + duration(row.eta) : '');
    if (!saving) say(row.message, row.phase === 'failed');
    controls(); return busy();
  }
  async function poll(id, version) {
    if (version !== generation) return;
    try {const value = await api('/' + id); if (version !== generation) return; if (display(value)) setTimeout(() => poll(id, version), 1000); else history();}
    catch (error) {say(error.message + '；刷新后可核对原任务。', true);}
  }
  async function load(id) {
    const version = ++generation; reset(); row = null; explicit = false; wanted = null;
    el('result').hidden = true; el('output').hidden = true;
    for (const id of ['format', 'language', 'audio-format', 'asset']) {el(id).dataset.signature = ''; el(id).replaceChildren();}
    storage.set('youtube.active', id);
    const value = await api('/' + id); if (version !== generation) return;
    el('input').value = value.input;
    if (display(value)) setTimeout(() => poll(id, version), 800);
  }
  async function action(body) {
    if (requesting || saving || !row) return;
    requesting = true; controls();
    try {const id = row.id, result = await api('/' + id, body); if (result.key) wanted = result.key; const version = ++generation; if (display(await api('/' + id))) setTimeout(() => poll(id, version), 600); else history();}
    catch (error) {say(error.message, true);} finally {requesting = false; controls();}
  }
  async function start(interactive) {
    if (locked()) return; requesting = true; controls(); say('正在提交当前视频的解析任务…');
    try {const value = await api('', {input: el('input').value, interactive}); await load(value.id); history();}
    catch (error) {say(error.message, true);} finally {requesting = false; controls();}
  }
  function selectionChanged() {
    reset(); explicit = false; wanted = null; audioOptions(); details();
    const match = Object.entries(row.resources).find(([, a]) => a.ready && a.kind === 'video' && a.video_id === el('format').value && a.audio_id === audioId());
    if (match) el('asset').value = match[0];
    preview(); controls();
  }
  function prepare(kind) {
    if (locked() || !row?.metadata_ready) return;
    reset(); explicit = false;
    action({action: 'prepare', kind, video_id: el('format').value, audio_id: audioId(), output_format: kind === 'cover' ? el('cover-format').value : 'original'});
  }
  async function history() {
    try {
      const data = await api(''); el('disk').textContent = '缓存 ' + size(data.cache_bytes) + ' · 临时磁盘剩余 ' + size(data.free_bytes) + ' · ' + data.temp_dir + (data.warning ? '；超过缓存提醒容量，可清理或更换目录，仍可继续下载。' : '');
      if (!el('temp-dir').dataset.edited) for (const [id, key] of [['temp-dir', 'temp_dir'], ['cache-warn', 'cache_warn_gib'], ['idle', 'idle_hours'], ['proxy', 'proxy'], ['runtime', 'js_runtime']]) el(id).value = data.options[key] ?? '';
      el('history').replaceChildren();
      for (const record of data.items) {
        const item = document.createElement('div'), title = document.createElement('strong'), note = document.createElement('p'), buttons = document.createElement('div');
        title.textContent = record.metadata?.title || '待解析视频'; note.textContent = date(record.created) + ' · ' + record.message; note.className = 'ui-form-note'; buttons.className = 'module-actions';
        for (const [text, callback] of [['查看记录', () => load(record.id)], ['删除缓存', async () => {await api('/' + record.id, {action: 'delete_cache'}); if (row?.id === record.id) display(await api('/' + record.id)); history();}]]) {
          const b = document.createElement('button'); b.type = 'button'; b.className = 'ui-button ui-secondary'; b.textContent = text; b.disabled = saving || busy() || active.includes(record.phase); b.onclick = () => callback().catch(error => say(error.message, true)); buttons.append(b);
        }
        if (record.task_id) {const a = document.createElement('a'); a.className = 'ui-button ui-secondary'; a.href = '/tasks/' + encodeURIComponent(record.task_id); a.textContent = '任务与日志'; buttons.append(a);}
        const audit = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = '素材、任务与保存摘要'; audit.append(summary);
        for (const attempt of record.attempts) {const p = document.createElement('p'), a = document.createElement('a'); a.href = '/tasks/' + encodeURIComponent(attempt.task_id); a.textContent = date(attempt.created) + ' · ' + attempt.action; p.append(a); audit.append(p);}
        for (const [key, a] of Object.entries(record.resources)) {if (key === 'cover') continue; const p = document.createElement('p'); p.className = 'ui-form-note'; p.textContent = a.filename + ' · ' + size(a.bytes) + ' · ' + (a.ready ? '缓存可用' : '需重新准备'); audit.append(p);}
        for (const a of saveRecords(record)) {const p = document.createElement('p'); p.className = 'ui-form-note'; p.textContent = '本浏览器曾保存：' + a.filename + '（' + date(a.time) + '）'; audit.append(p);}
        item.append(title, note, buttons, audit); el('history').append(item);
      }
      if (!data.items.length) el('history').textContent = '暂无解析记录。';
    } catch (error) {say(error.message, true);}
  }
  async function save() {
    const item = asset(), key = el('asset').value, record = row;
    if (locked() || !item?.ready || item.kind === 'video' && (!playable || !el('reviewed').checked)) return;
    if (!window.showSaveFilePicker) {say('请使用桌面版Chrome或Edge，以便自行选择保存位置。', true); return;}
    saving = true; controller = new AbortController(); controls(); let writable = null, reader = null;
    try {
      const handle = await window.showSaveFilePicker({id: 'youtube-' + item.kind, suggestedName: item.filename, types: [{description: '媒体素材', accept: {[item.mime]: ['.' + item.filename.split('.').pop()]}}]});
      writable = await handle.createWritable();
      const response = await fetch(url(key, record), {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Collection-Token': token}, body: JSON.stringify(item.kind === 'video' ? {reviewed: true} : {save: true}), signal: controller.signal});
      if (!response.ok) {const error = await response.json(); throw new Error(error.error || '保存失败');}
      reader = response.body.getReader(); let received = 0;
      while (true) {if (controller.signal.aborted) throw new DOMException('已停止', 'AbortError'); const {done, value} = await reader.read(); if (done) break; await writable.write(value); received += value.length; say('正在保存 · ' + size(received) + ' / ' + size(item.bytes));}
      if (received !== item.bytes) throw new Error('文件接收不完整');
      if (controller.signal.aborted) throw new DOMException('已停止', 'AbortError');
      await writable.close(); writable = null;
      const values = saveRecords(record).filter(v => v.key !== key); values.push({key, filename: handle.name, time: new Date().toISOString()}); storage.set('youtube.saves.' + record.id, JSON.stringify(values)); say('已保存：' + handle.name);
    } catch (error) {controller.abort(); say(error.name === 'AbortError' ? '已取消保存，可复用缓存重新选择位置。' : error.message + '；未完成的文件不记为保存成功。', error.name !== 'AbortError');}
    finally {if (reader) try {await reader.cancel();} catch (_) {} if (writable) try {await writable.abort();} catch (_) {} saving = false; controller = null; controls(); saved(); history();}
  }
  el('start').onclick = () => start(false); el('login').onclick = () => start(true);
  el('format').onchange = selectionChanged; el('language').onchange = () => {el('audio-format').dataset.signature = ''; selectionChanged();}; el('audio-format').onchange = selectionChanged;
  el('asset').onchange = () => {reset(); explicit = true; preview();}; el('reviewed').onchange = controls;
  el('prepare').onclick = () => prepare('video'); el('audio').onclick = () => prepare('audio'); el('cover-prepare').onclick = () => prepare('cover');
  for (const id of ['pause', 'resume', 'cancel']) el(id).onclick = () => action({action: id});
  el('save').onclick = save; el('stop-save').onclick = () => controller?.abort();
  el('copy').onclick = async () => {try {await navigator.clipboard.writeText(row.metadata.url); say('已复制当前视频链接。');} catch (_) {say('复制失败，可从输入框复制链接。', true);}};
  for (const id of ['temp-dir', 'cache-warn', 'idle', 'proxy', 'runtime']) el(id).oninput = () => {el('temp-dir').dataset.edited = 'true';};
  el('settings').onclick = async () => {try {await api('/settings', {temp_dir: el('temp-dir').value, cache_warn_gib: Number(el('cache-warn').value), idle_hours: Number(el('idle').value), proxy: el('proxy').value.trim(), js_runtime: el('runtime').value.trim()}); delete el('temp-dir').dataset.edited; await history(); say('设置已应用，已有临时文件保持原位置。');} catch (error) {say(error.message, true);}};
  el('cleanup').onclick = async () => {try {const result = await api('/cleanup', {action: 'cleanup'}); await history(); if (row) display(await api('/' + row.id)); say('已清理 ' + result.cleaned + ' 条过期缓存。');} catch (error) {say(error.message, true);}};
  el('video').onloadedmetadata = () => {el('video').currentTime = Math.min(.1, el('video').duration / 2);};
  el('video').onloadeddata = () => {playable = !el('video').error && el('video').videoWidth > 0 && asset()?.ready && el('video').getAttribute('src') === url(el('asset').value); controls();};
  el('video').onerror = () => {playable = false; el('reviewed').checked = false; controls(); if (asset()?.kind === 'video') say('当前浏览器不能预览该编码，请选择H.264兼容版本后重新准备。', true);};
  el('video').onplay = () => el('audio-player').pause(); el('audio-player').onplay = () => el('video').pause();
  for (const id of ['cover', 'image']) el(id).onerror = () => {el(id).hidden = true;};
  controls(); history(); const previous = storage.get('youtube.active'); if (previous) load(previous).catch(error => say(error.message, true));
})();
