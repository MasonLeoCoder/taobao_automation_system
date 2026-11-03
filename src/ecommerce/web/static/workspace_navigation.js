/* 只记忆菜单外观状态，不发起业务请求。 */
(() => {
  'use strict';
  const menu = document.querySelector('[data-workspace-menu]');
  if (!menu) return;
  const storageKey = 'ecommerce.workspace.navigation.v1';
  let saved = {};
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    if (value && typeof value === 'object' && !Array.isArray(value)) saved = value;
  } catch (_) { /* 存储被禁用或旧值损坏时沿用原生菜单。 */ }
  menu.querySelectorAll('[data-nav-group]').forEach(group => {
    const key = group.dataset.navGroup;
    group.open = group.hasAttribute('data-current-group') || saved[key] === true;
    group.addEventListener('toggle', () => {
      saved[key] = group.open;
      try { localStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) { /* 禁用存储仍可使用菜单。 */ }
    });
  });
  const mobile = matchMedia('(max-width: 700px)');
  const adapt = () => { menu.open = !mobile.matches; };
  adapt();
  mobile.addEventListener('change', adapt);
  const current = menu.querySelector('[aria-current="page"]');
  if (current && !mobile.matches) {
    requestAnimationFrame(() => {
      const link = current.getBoundingClientRect();
      const box = menu.getBoundingClientRect();
      if (link.bottom > box.bottom || link.top < box.top) menu.scrollTop += link.top - box.top - 20;
    });
  }
})();
