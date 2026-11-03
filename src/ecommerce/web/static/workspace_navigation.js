/* 菜单外观与可中断折叠动画；不发起业务请求。 */
(() => {
  'use strict';
  const menu = document.querySelector('[data-workspace-menu]');
  if (!menu) return;
  const storageKey = 'ecommerce.workspace.navigation.v1';
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const mobile = matchMedia('(max-width: 700px)');
  const duration = parseFloat(getComputedStyle(menu).getPropertyValue('--workspace-motion')) || 240;
  const controllers = [];
  let saved = {};
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    if (value && typeof value === 'object' && !Array.isArray(value)) saved = value;
  } catch (_) { /* 存储不可用时保留本页操作。 */ }

  // 关闭动画期间暂留open以便绘制，立即通过inert隐藏内容的交互与焦点。
  const disclosure = (details, panel, initial, onChange = () => {}) => {
    const summary = details.querySelector(':scope > summary');
    let expanded = initial;
    let animation = null;
    let revision = 0;
    const describe = () => {
      details.dataset.expanded = String(expanded);
      summary.setAttribute('aria-expanded', String(expanded));
      panel.inert = !expanded;
    };
    details.open = expanded;
    describe();
    const set = (next, animate = true) => {
      if (next === expanded && !animation) return;
      const height = details.open ? panel.getBoundingClientRect().height : 0;
      const opacity = details.open ? getComputedStyle(panel).opacity : '0';
      const currentRevision = ++revision;
      if (animation) { animation.cancel(); animation = null; }
      expanded = next;
      describe();
      onChange(expanded);
      if (!animate || reducedMotion.matches || !panel.animate) {
        details.open = expanded;
        return;
      }
      details.open = true;
      const target = expanded ? panel.getBoundingClientRect().height : 0;
      animation = panel.animate([
        { height: `${height}px`, opacity },
        { height: `${target}px`, opacity: expanded ? 1 : 0 },
      ], { duration, easing: 'cubic-bezier(.22,.68,0,1)', fill: 'both' });
      const running = animation;
      running.onfinish = () => {
        if (currentRevision !== revision) return;
        details.open = expanded;
        running.cancel();
        animation = null;
      };
    };
    summary.addEventListener('click', event => {
      event.preventDefault();
      set(!expanded);
    });
    // 保留浏览器原生查找展开等行为。
    details.addEventListener('toggle', () => {
      if (animation || details.open === expanded) return;
      expanded = details.open;
      describe();
      onChange(expanded);
    });
    const control = { set, finish: () => { if (animation) animation.finish(); } };
    controllers.push(control);
    return control;
  };
  menu.querySelectorAll('[data-nav-group]').forEach(group => {
    const key = group.dataset.navGroup;
    disclosure(group, group.querySelector('.workspace-group-panel'),
      group.hasAttribute('data-current-group') || saved[key] === true, value => {
        saved[key] = value;
        try { localStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) { /* 菜单仍可使用。 */ }
      });
  });
  const outer = disclosure(menu, menu.querySelector('.workspace-menu-body'), !mobile.matches);
  mobile.addEventListener('change', () => {
    controllers.forEach(control => control.finish());
    outer.set(!mobile.matches, false);
  });
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) controllers.forEach(control => control.finish());
  });
  const current = menu.querySelector('[aria-current="page"]');
  if (current && !mobile.matches) {
    requestAnimationFrame(() => {
      const link = current.getBoundingClientRect();
      const box = menu.getBoundingClientRect();
      if (link.bottom > box.bottom || link.top < box.top) menu.scrollTop += link.top - box.top - 20;
    });
  }
})();
