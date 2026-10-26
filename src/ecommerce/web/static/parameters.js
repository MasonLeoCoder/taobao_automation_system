/* 参数完整原文留在页面：按实际行数折叠，复制始终使用完整值。 */
(() => {
  const groups = [...document.querySelectorAll('[data-parameter-value]')];
  if (!groups.length) return;
  const feedback = document.getElementById('parameter-copy-feedback');
  let feedbackTimer;
  let latestCopy = 0;

  function showFeedback(message, state, duration = 0) {
    clearTimeout(feedbackTimer);
    feedback.textContent = message;
    feedback.dataset.state = state;
    feedback.hidden = false;
    if (duration) feedbackTimer = setTimeout(() => { feedback.hidden = true; feedback.textContent = ''; }, duration);
  }

  function measure(group) {
    const text = group.querySelector('.parameter-value-text');
    const button = group.querySelector('.parameter-toggle');
    if (!button || button.getAttribute('aria-expanded') === 'true') return;
    text.classList.add('is-collapsed');
    const clipped = text.scrollHeight > text.clientHeight + 1;
    text.classList.toggle('is-collapsed', clipped);
    button.hidden = !clipped;
    button.parentElement.hidden = !clipped;
  }

  // 本地HTTP或浏览器拒绝剪贴板API时，尝试兼容复制；不静默报告成功。
  function legacyCopy(value) {
    const active = document.activeElement;
    const selection = window.getSelection();
    const ranges = selection ? Array.from({length:selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
    const x = window.scrollX, y = window.scrollY;
    const area = document.createElement('textarea');
    area.value = value;
    area.readOnly = true;
    area.tabIndex = -1;
    area.setAttribute('aria-label', '临时复制参数值');
    Object.assign(area.style, {position:'fixed', top:'0', left:'0', width:'1px', height:'1px', opacity:'0', pointerEvents:'none'});
    document.body.append(area);
    try {
      area.focus({preventScroll:true});
      area.select();
      area.setSelectionRange(0, area.value.length);
      return Boolean(document.execCommand('copy'));
    } catch (_) { return false; }
    finally {
      area.remove();
      if (selection) {
        selection.removeAllRanges();
        ranges.forEach(range => selection.addRange(range));
      }
      active?.focus({preventScroll:true});
      window.scrollTo(x, y);
    }
  }

  async function copy(value) {
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(value); return true; }
      catch (_) { /* 按浏览器实际能力继续尝试兼容复制。 */ }
    }
    return legacyCopy(value);
  }

  groups.forEach(group => {
    const text = group.querySelector('.parameter-value-text');
    const toggle = group.querySelector('.parameter-toggle');
    let busy = false;
    toggle?.addEventListener('click', () => {
      const expanded = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.textContent = expanded ? '收起' : '展开全部';
      text.classList.toggle('is-collapsed', !expanded);
    });
    async function performCopy() {
      if (!text.hasAttribute('data-parameter-copy') || busy) return;
      busy = true;
      const current = ++latestCopy;
      text.setAttribute('aria-busy', 'true');
      showFeedback('正在复制…', 'pending');
      let success = false;
      try { success = await copy(text.textContent); }
      catch (_) { success = false; }
      finally {
        busy = false;
        text.removeAttribute('aria-busy');
        if (current === latestCopy) showFeedback(success ? '复制成功' : '复制失败，请展开后手动选择复制',
          success ? 'success' : 'error', success ? 2000 : 5000);
      }
    }
    text.addEventListener('click', () => {
      // 手动拖选原文时保留选择，不把选词操作当成整值复制。
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed && selection.containsNode(text, true)) return;
      performCopy();
    });
    text.addEventListener('keydown', event => {
      if (!['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      performCopy();
    });
    measure(group);
  });

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; groups.forEach(measure); });
  }
  if (window.ResizeObserver) {
    const observer = new ResizeObserver(schedule);
    groups.forEach(group => observer.observe(group));
  }
  window.addEventListener('resize', schedule);
})();
