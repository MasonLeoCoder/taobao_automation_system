/* 全站图片在本页预览；保留原件入口、焦点和滚动位置。 */
(() => {
  const viewer = document.getElementById('image-preview');
  if (!viewer) return;
  const photo = document.getElementById('image-preview-photo');
  const status = document.getElementById('image-preview-status');
  const original = document.getElementById('image-preview-original');
  const closeButton = document.getElementById('image-preview-close');
  let state = null;
  let sequence = 0;
  let restoreFocus = true;

  function enhance(img) {
    if (viewer.contains(img) || img.dataset.imagePreviewReady) return;
    img.dataset.imagePreviewReady = 'true';
    const link = img.closest('a');
    if (link && !link.textContent.trim() && link.querySelectorAll('img').length === 1) {
      link.dataset.imagePreviewTrigger = 'true';
      link.setAttribute('aria-haspopup', 'dialog');
      link.title = '点击放大，再次点击恢复';
      if (!link.hasAttribute('aria-label')) link.setAttribute('aria-label', `放大查看：${img.alt || '图片'}`);
    } else {
      img.tabIndex = 0;
      img.setAttribute('role', 'button');
      img.setAttribute('aria-haspopup', 'dialog');
      img.setAttribute('aria-label', `放大查看：${img.alt || '图片'}`);
    }
  }

  function source(img) {
    try {
      const url = new URL(img.dataset.previewSrc || img.currentSrc || img.src, document.baseURI);
      if (['http:', 'https:', 'blob:'].includes(url.protocol) ||
          (url.protocol === 'data:' && url.href.startsWith('data:image/'))) return url.href;
    } catch (_) { /* 无有效图片地址时不创建空白预览。 */ }
    return null;
  }

  function lockPage(opener) {
    const properties = ['position', 'top', 'left', 'width', 'overflow', 'padding-right'];
    const padding = parseFloat(getComputedStyle(document.body).paddingRight) || 0;
    const gap = window.innerWidth - document.documentElement.clientWidth;
    state = {opener, x:window.scrollX, y:window.scrollY,
      styles:properties.map(key => [key, document.body.style.getPropertyValue(key), document.body.style.getPropertyPriority(key)])};
    Object.assign(document.body.style, {position:'fixed', top:`-${state.y}px`, left:`-${state.x}px`,
      width:'100%', overflow:'hidden', paddingRight:`${padding + gap}px`});
  }

  function reset() {
    sequence += 1;
    photo.onload = photo.onerror = null;
    photo.hidden = true;
    photo.removeAttribute('src');
    original.removeAttribute('href');
    if (!state) return;
    const saved = state;
    state = null;
    saved.styles.forEach(([key, value, priority]) => {
      if (value) document.body.style.setProperty(key, value, priority);
      else document.body.style.removeProperty(key);
    });
    window.scrollTo(saved.x, saved.y);
    if (restoreFocus && saved.opener.isConnected) saved.opener.focus({preventScroll:true});
  }

  function close() {
    if (viewer.open) {
      viewer.close();
      reset();
    }
  }

  function open(img) {
    const url = source(img);
    if (!url || viewer.open || document.querySelector('dialog[open]')) return;
    enhance(img);
    const opener = img.closest('[data-image-preview-trigger]') || img;
    restoreFocus = true;
    lockPage(opener);
    photo.hidden = true;
    photo.alt = img.alt || '图片原图';
    status.hidden = false;
    status.textContent = '正在加载图片…';
    original.href = url;
    try { viewer.showModal(); }
    catch (_) { reset(); return; }
    const current = ++sequence;
    photo.onload = () => {
      if (!viewer.open || current !== sequence) return;
      photo.hidden = false;
      status.hidden = true;
    };
    photo.onerror = () => {
      if (!viewer.open || current !== sequence) return;
      photo.hidden = true;
      status.hidden = false;
      status.textContent = '图片加载失败或本地文件不可用，请关闭后重新查看。';
    };
    photo.src = url;
    closeButton.focus({preventScroll:true});
  }

  document.querySelectorAll('img').forEach(enhance);
  new MutationObserver(changes => changes.forEach(change => change.addedNodes.forEach(node => {
    if (node.nodeType !== Node.ELEMENT_NODE || viewer.contains(node)) return;
    if (node.matches('img')) enhance(node);
    node.querySelectorAll('img').forEach(enhance);
  }))).observe(document.body, {childList:true, subtree:true});

  document.addEventListener('click', event => {
    if (!(event.target instanceof Element) || viewer.contains(event.target)) return;
    const img = event.target.closest('img') || event.target.closest('[data-image-preview-trigger]')?.querySelector('img');
    if (!img) return;
    event.preventDefault();
    event.stopPropagation();
    open(img);
  });
  document.addEventListener('keydown', event => {
    if (!['Enter', ' '].includes(event.key) || !(event.target instanceof Element) || viewer.contains(event.target)) return;
    const img = event.target.matches('img') ? event.target : event.target.closest('[data-image-preview-trigger]')?.querySelector('img');
    if (!img) return;
    event.preventDefault();
    open(img);
  });
  viewer.addEventListener('click', event => {
    if (event.target.closest('a,button')) return;
    close();
  });
  closeButton.addEventListener('click', close);
  viewer.addEventListener('cancel', event => { event.preventDefault(); close(); });
  viewer.addEventListener('close', () => { if (!viewer.open) reset(); });

  // 人工采集提示优先，不能被图片预览挡住或被预览抢回焦点。
  const attention = document.getElementById('collection-attention');
  if (attention) new MutationObserver(() => {
    if (attention.open && viewer.open) { restoreFocus = false; close(); }
  }).observe(attention, {attributes:true, attributeFilter:['open']});
})();
