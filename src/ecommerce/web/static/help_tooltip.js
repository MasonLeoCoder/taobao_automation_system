/* 公共指标说明：文字安全展示，支持悬停、键盘聚焦和触屏点击，不改变业务表单。 */
(() => {
  'use strict';
  const selector = '[data-ui-help-text]';
  if (!document.querySelector(selector)) return;
  let active = null, popup = null, pinned = false, timer = null, suppressHover = false;
  const trigger = node => node instanceof Element ? node.closest(selector) : null;
  const cancelHide = () => { clearTimeout(timer); timer = null; };
  function described(button, add) {
    const ids = new Set((button.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
    if (add) ids.add(popup.id); else ids.delete(popup.id);
    if (ids.size) button.setAttribute('aria-describedby', [...ids].join(' '));
    else button.removeAttribute('aria-describedby');
  }
  function hide() {
    cancelHide();
    if (active && popup) described(active, false);
    if (popup) popup.hidden = true;
    active = null; pinned = false;
  }
  function dismiss() {
    // 浮层消失可能露出鼠标下另一指标；等真实移动再响应悬停，保证Esc能关闭。
    suppressHover = true; hide();
  }
  function position() {
    if (!active || !popup || popup.hidden) return;
    const rect = active.getBoundingClientRect();
    if (!active.getClientRects().length || rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth) { hide(); return; }
    const box = popup.getBoundingClientRect(), gap = 8;
    const left = Math.max(gap, Math.min(rect.left + rect.width / 2 - box.width / 2, innerWidth - box.width - gap));
    const preferred = rect.bottom + gap + box.height <= innerHeight - gap ? rect.bottom + gap : rect.top - box.height - gap;
    const top = Math.max(gap, Math.min(preferred, innerHeight - box.height - gap));
    popup.style.left = `${left}px`; popup.style.top = `${top}px`;
  }
  function show(button) {
    cancelHide();
    if (!popup) {
      popup = document.createElement('div');
      popup.id = 'ui-help-tooltip'; popup.className = 'ui-help-popup'; popup.setAttribute('role', 'tooltip');
      popup.hidden = true; document.body.append(popup);
      popup.addEventListener('pointerenter', cancelHide);
      popup.addEventListener('pointerleave', scheduleHide);
    }
    if (active !== button) { if (active) described(active, false); pinned = false; }
    active = button;
    popup.textContent = button.dataset.uiHelpText;
    popup.hidden = false; popup.style.visibility = 'hidden';
    described(button, true); position(); popup.style.visibility = '';
  }
  function scheduleHide() {
    cancelHide();
    timer = setTimeout(() => {
      if (active && !pinned && document.activeElement !== active && !active.matches(':hover') && !popup.matches(':hover')) hide();
    }, 130);
  }
  document.addEventListener('pointerover', event => {
    if (suppressHover) return;
    const button = trigger(event.target);
    if (button) { if (button !== active) show(button); else cancelHide(); }
  });
  document.addEventListener('pointermove', event => {
    if (suppressHover && (event.movementX || event.movementY)) {
      suppressHover = false;
      const button = trigger(event.target); if (button) show(button);
    }
  });
  document.addEventListener('pointerout', event => {
    if (active && trigger(event.target) === active && !active.contains(event.relatedTarget)) scheduleHide();
  });
  document.addEventListener('focusin', event => { const button = trigger(event.target); if (button) { suppressHover = false; show(button); } });
  document.addEventListener('focusout', event => {
    if (trigger(event.target) === active) { pinned = false; scheduleHide(); }
  });
  document.addEventListener('click', event => {
    const button = trigger(event.target);
    if (!button) return;
    if (active === button && pinned) dismiss();
    else { suppressHover = false; show(button); pinned = true; }
  });
  document.addEventListener('pointerdown', event => {
    if (active && !trigger(event.target) && !popup.contains(event.target)) dismiss();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && active) { dismiss(); event.preventDefault(); event.stopPropagation(); }
  }, true);
  window.addEventListener('resize', position);
  document.addEventListener('scroll', position, {capture: true, passive: true});
  document.addEventListener('toggle', position, true);
})();
