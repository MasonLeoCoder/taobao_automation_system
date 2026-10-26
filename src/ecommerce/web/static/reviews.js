/* 评论全文留在页面，长内容独立折叠，媒体失效保留明确提示。 */
(() => {
  const bodies = [...document.querySelectorAll('.review-body')];
  function measure(body) {
    const text = body.querySelector('.review-text');
    const toggle = body.querySelector('.review-toggle');
    if (toggle.getAttribute('aria-expanded') === 'true') return;
    text.classList.add('is-collapsed');
    const clipped = text.scrollHeight > text.clientHeight + 1;
    text.classList.toggle('is-collapsed', clipped);
    toggle.hidden = !clipped;
  }
  bodies.forEach(body => {
    const toggle = body.querySelector('.review-toggle');
    toggle.addEventListener('click', () => {
      const expanded = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.textContent = expanded ? '收起' : '展开全文';
      body.querySelector('.review-text').classList.toggle('is-collapsed', !expanded);
    });
    measure(body);
  });
  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; bodies.forEach(measure); });
  }
  if (window.ResizeObserver) {
    const observer = new ResizeObserver(schedule);
    bodies.forEach(body => observer.observe(body));
  }
  window.addEventListener('resize', schedule);
  document.querySelectorAll('[data-review-media]').forEach(media => {
    function failed() {
      media.hidden = true;
      media.closest('figure').querySelector('.review-media-error').hidden = false;
    }
    media.addEventListener('error', failed);
    if (media.tagName === 'IMG' && media.complete && !media.naturalWidth) failed();
  });
})();
