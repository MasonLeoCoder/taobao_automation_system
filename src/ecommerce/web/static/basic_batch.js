/* 仅提示待查看的批次，明确点击查看后才加载；无采集或分析请求。 */
(() => {
  const form=document.querySelector('[data-bb-form],[data-video-batch-form],[data-image-batch-form],[data-sku-batch-form],[data-parameter-batch-form],[data-detail-image-batch-form]');
  if (!form) return;
  const select=form.elements.batch,loaded=select.value,pending=form.querySelector('[data-bb-pending],[data-video-pending],[data-image-pending],[data-sku-pending],[data-parameter-pending],[data-detail-image-pending]');
  const button=form.querySelector('button[type=submit]'),initialDisabled=button.disabled;
  const update=()=>{
    pending.hidden=select.value===loaded;
    pending.textContent=pending.hidden ? '' : `已选择${select.value==='0' ? '默认有效资料' : `批次 ${select.value}`}，点击“查看”后切换；下方仍为已加载内容。`;
  };
  select.addEventListener('change',update);
  form.addEventListener('submit',event=>{
    if (form.dataset.loading) {event.preventDefault();return;}
    form.dataset.loading='true';button.disabled=true;button.textContent='正在读取…';
  });
  addEventListener('pageshow',()=>{delete form.dataset.loading;button.disabled=initialDisabled;button.textContent='查看';update();});
  update();
})();
