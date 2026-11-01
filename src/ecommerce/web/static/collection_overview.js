/* 九模块只读快照就地刷新；筛选和焦点保留，不自动跳页或发起采集。 */
(() => {
  const root=document.querySelector('[data-collection-overview]');
  if (!root) return;
  let filter=new URLSearchParams(location.search).get('overview_filter') || 'all';
  let verified=false;
  const feedback=root.querySelector('[data-co-feedback]');
  const stateClass=row=>row.busy ? 'is-active' : row.issue ? 'is-pending' : row.has_valid ? 'is-ready' : 'is-muted';
  function applyFilter() {
    let count=0;
    root.querySelectorAll('[data-overview-row]').forEach(row=>{
      row.hidden=filter==='remaining' && row.dataset.coRemaining!=='true' || filter==='issues' && row.dataset.coIssue!=='true';
      if (!row.hidden) count++;
    });
    root.querySelectorAll('[data-co-filter]').forEach(link=>link.setAttribute('aria-current',String(link.dataset.coFilter===filter)));
    root.querySelector('[data-co-empty]').hidden=count>0;
    feedback.querySelector('a').href=location.href;
  }
  function refresh(model) {
    verified=!model.runtime_error;
    feedback.hidden=verified;
    for (const name of ['ready','remaining','issues']) root.querySelector(`[data-co-count="${name}"]`).textContent=model[name];
    for (const item of model.rows) {
      const row=root.querySelector(`[data-overview-row="${item.module}"]`);
      if (!row) continue;
      const set=(key,value)=>{ row.querySelector(`[data-co-${key}]`).textContent=value || ''; };
      const link=(key,url,label)=>{
        const node=row.querySelector(`[data-co-${key}]`);node.hidden=!url;
        if (url) node.href=url; else node.removeAttribute('href');
        if (label) node.textContent=label;
      };
      row.dataset.coRemaining=String(item.remaining);row.dataset.coIssue=String(item.issue);
      set('saved',item.saved_state);row.querySelector('[data-co-saved]').className='ui-report-state '+(item.has_valid?'is-ready':'is-muted');
      set('quantity',item.quantity);set('scope',item.scope);set('media',item.media_note);
      set('time',item.captured_text);set('batch',item.source_batch ? `批次 ${item.source_batch}` : '暂无有效批次');
      set('attempt',item.attempt_label);row.querySelector('[data-co-attempt]').className='ui-report-state '+stateClass(item);
      set('attempt-time',item.attempt_at ? item.attempt_time : '');set('message',item.message);
      set('default',item.busy ? '进行中范围以当前任务为准' : item.default_range);
      link('source',item.source_url);link('source-logs',item.source_logs);link('attempt-logs',item.attempt_logs);
      link('active',item.active_url,item.active_label);
      const collect=row.querySelector('[data-overview-collect]');
      collect.hidden=item.busy;collect.dataset.busy=String(item.busy);
      collect.textContent=item.collect_label;collect.disabled=!verified || item.busy || collect.dataset.submitting==='true';
      if (verified && collect.dataset.submitting!=='true') set('action-feedback','');
    }
    applyFilter();
  }
  root.querySelectorAll('[data-co-filter]').forEach(link=>link.addEventListener('click',event=>{
    event.preventDefault();filter=link.dataset.coFilter;
    const url=new URL(location.href);url.searchParams.set('overview_filter',filter);history.replaceState(null,'',url);
    applyFilter();
  }));
  document.addEventListener('collection-overview-updated',event=>refresh(event.detail));
  document.addEventListener('collection-overview-error',()=>{
    verified=false;feedback.hidden=false;
    root.querySelectorAll('[data-overview-collect]').forEach(button=>button.disabled=true);
  });
  document.addEventListener('collection-overview-request-end',()=>{
    root.querySelectorAll('[data-overview-collect]').forEach(button=>button.disabled=!verified || button.dataset.busy==='true' || button.dataset.submitting==='true');
  });
  if (root.overviewSnapshot) refresh(root.overviewSnapshot);
  applyFilter();
})();
