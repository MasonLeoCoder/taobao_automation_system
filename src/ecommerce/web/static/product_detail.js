/* 完整名称保留于页面；展开和复制只在本地进行。 */
(() => {
  const title = document.querySelector('[data-product-title]');
  if (!title) return;
  const toggle = document.querySelector('[data-title-toggle]');
  const measure = () => {
    if (toggle.getAttribute('aria-expanded') === 'true') return;
    title.classList.add('is-collapsed');
    const clipped = title.scrollHeight > title.clientHeight + 1;
    toggle.hidden = !clipped;
    title.classList.toggle('is-collapsed',clipped);
  };
  toggle.addEventListener('click',() => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded',String(open));
    toggle.textContent = open ? '收起名称' : '展开完整名称';
    title.classList.toggle('is-collapsed',!open);
    if (!open) measure();
  });
  measure();window.addEventListener('resize',measure);document.fonts?.ready.then(measure);
  const feedback = document.querySelector('[data-copy-feedback]');
  let feedbackTimer;
  document.querySelectorAll('[data-copy-target]').forEach(button => {
    button.hidden = false;
    button.addEventListener('click',async () => {
      const text = document.getElementById(button.dataset.copyTarget)?.textContent;
      if (!text || button.disabled) return;
      clearTimeout(feedbackTimer);
      button.disabled = true;
      try {
        if (!navigator.clipboard?.writeText) throw new Error('unavailable');
        await navigator.clipboard.writeText(text);
        feedback.textContent = '复制成功';
      } catch (_) {
        feedback.textContent = '复制失败，请展开完整内容后手动选择复制。';
      } finally {
        button.disabled = false;
        feedbackTimer = setTimeout(() => { feedback.textContent = ''; },5000);
      }
    });
  });
})();
