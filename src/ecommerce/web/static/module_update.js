/* 九模块共用确认窗口；轮询由collection.js提供，资料与来源链接不改写。 */
(() => {
  const modal=document.querySelector('[data-module-update-modal]'),client=document.getElementById('collection-client');
  if(!modal || !client)return;
  const buttons=[...document.querySelectorAll('[data-module-update-open]')],notices=document.querySelector('[data-module-update-notices]');
  const overview=Boolean(document.querySelector('[data-collection-overview]'));
  const one=key=>modal.querySelector(`[data-update-${key}]`),confirm=one('confirm'),error=one('error'),range=document.getElementById('module-update-range');
  let models=client.moduleUpdates || null,known=Boolean(models),selected=null,sending=false,progress=false,uncertain=false;
  const text=batch=>batch ? `批次 ${batch.id} · ${batch.time_text || '时间未记录'}` : '暂无有效资料';
  function link(parent,url,label){const a=document.createElement('a');a.className='ui-button ui-secondary';a.textContent=label;a.href=url;parent.append(a);}
  function bind(key,url){const node=one(key);node.hidden=!url;if(url)node.href=url;}
  function render(){
    for(const button of buttons){
      const model=models?.[button.dataset.moduleUpdateOpen];
      button.disabled=!known || sending;button.textContent=model?.active ? '查看更新进度' : `更新${model?.label || button.textContent.replace(/^更新/,'')}`;
      const tag=button.parentElement.querySelector('[data-update-history]'),latest=model?.latest_valid;
      tag.hidden=!(Number(button.dataset.loadedId)>0 && latest && Number(button.dataset.loadedId)!==Number(latest.id));
      tag.replaceChildren();if(!tag.hidden){tag.append('正在查看历史资料 · ');link(tag,latest.url,'查看最近有效资料');}
    }
    notices.replaceChildren();
    for(const code of new Set(buttons.map(b=>b.dataset.moduleUpdateOpen))){
      // 概况五列已有资料、尝试和操作，不把详情提示追加成第六个网格项目。
      if(overview)continue;
      const model=models?.[code],outcome=model?.outcome,loaded=buttons.find(b=>b.dataset.moduleUpdateOpen===code)?.dataset.loadedId;
      if(!known || !outcome || !model.active && outcome.complete && Number(outcome.batch?.id)===Number(loaded))continue;
      const card=document.createElement('section');card.className=(outcome.complete || model.active ? 'ui-report-focus' : 'ui-report-notice')+' mu-notice';
      const title=document.createElement('strong');title.textContent=`${model.label}：${outcome.complete && !model.active ? '有新批次可查看' : outcome.label}`;card.append(title);
      const p=document.createElement('p');p.className='ui-form-note';p.textContent=outcome.message || (outcome.complete ? '新结果已保存，点击查看后切换批次。' : model.active ? '采集进度自动刷新，当前资料保持原批次。' : '请核对本次结果和日志，当前资料保持原批次。');card.append(p);
      const actions=document.createElement('div');actions.className='module-actions';
      if(outcome.batch?.url)link(actions,outcome.batch.url,outcome.complete ? '查看新资料' : '查看本次结果');
      if(outcome.task_url)link(actions,outcome.task_url,'更新任务');
      if(outcome.logs_url)link(actions,outcome.logs_url,'更新日志');
      if(model.active)link(actions,model.queue_url,'查看采集队列');card.append(actions);
      const anchor=buttons.find(b=>b.dataset.moduleUpdateOpen===code)?.closest('section,[data-overview-row]');
      (anchor || notices).append(card);card.dataset.updateNotice=code;
    }
    if(!selected)return;
    const model=models?.[selected.dataset.moduleUpdateOpen],outcome=model?.outcome;
    if(model?.active)progress=true;
    document.getElementById('module-update-title').textContent=progress ? `${model?.label || ''}更新进度` : `更新${model?.label || ''}`;
    one('latest').textContent=text(model?.latest_valid);
    one('confirm-body').hidden=progress;one('progress').hidden=!progress;
    one('state').textContent=known ? outcome?.label || '正在核对本次结果' : '状态暂时无法读取';
    one('message').textContent=outcome?.message || '当前资料保留，请通过下方入口查看进度。';
    one('actual-scope').textContent=`已有申请实际范围：${model?.scope || '待核对'}。新的范围选择尚未提交。`;
    bind('result',outcome?.batch?.url);bind('task',outcome?.task_url);bind('queue',model?.queue_url);
    confirm.hidden=progress;confirm.disabled=!known || sending || uncertain || model?.active;
    confirm.textContent=sending ? '正在提交并核对…' : '确认更新';
    modal.querySelectorAll('[data-update-close]').forEach(b=>b.disabled=sending);
  }
  function redraw(){document.querySelectorAll('[data-update-notice]').forEach(n=>n.remove());render();}
  buttons.forEach(button=>button.addEventListener('click',()=>{
    if(!known || sending || document.querySelector('dialog[open]'))return;
    selected=button;progress=Boolean(models[button.dataset.moduleUpdateOpen]?.active);error.textContent='';
    one('loaded').textContent=Number(button.dataset.loadedId)>0 ? `批次 ${button.dataset.loadedId} · ${button.dataset.loadedTime}` : '当前未加载该模块批次';
    const code=button.dataset.moduleUpdateOpen,limited=['reviews','questions'].includes(code);
    one('range').hidden=!limited;range.value='limited';
    range.options[0].textContent=code==='reviews' ? '默认上限：100条有效评价' : '默认上限：50个问题';
    one('description').textContent=code==='skus' ? '重新采集本商品全部SKU、价格和可选状态，不受当前筛选或分页影响。' : `仅更新本商品的${models[code].label}模块。`;
    modal.showModal();redraw();
  }));
  const close=()=>{if(sending)return;modal.close();selected?.focus({preventScroll:true});};
  modal.querySelectorAll('[data-update-close]').forEach(b=>b.addEventListener('click',close));modal.addEventListener('cancel',e=>{e.preventDefault();close();});
  confirm.addEventListener('click',async()=>{
    if(!known || sending || uncertain || progress)return;
    const module=selected.dataset.moduleUpdateOpen;
    sending=true;error.textContent='';redraw();
    try{
      const job=await new Promise((resolve,reject)=>document.dispatchEvent(new CustomEvent('collection-module-update-request',{detail:{module,unlimited:['reviews','questions'].includes(module) && range.value==='all',resolve,reject}})));
      models[module]={...models[module],active:!job.done,scope:job.options?.[module==='reviews' ? 'all_reviews':'all_questions'] ? '不限数量' : module==='reviews' ? '默认上限100条有效评价' : module==='questions' ? '默认上限50个问题' : '本商品对应模块',
        outcome:{label:'申请已保存，正在核对结果',message:job.message || '',complete:false,batch:null}};
      progress=true;
    }catch(failure){uncertain=Boolean(failure.uncertain);error.textContent=failure.message;}
    finally{sending=false;redraw();}
  });
  document.addEventListener('collection-module-update-checked',event=>{uncertain=false;error.textContent=event.detail.job ? '已找到原申请，正在刷新其实际进度。' : '原编号暂未查到申请，可再次确认；将沿用原编号核对。';if(event.detail.job)progress=true;});
  document.addEventListener('collection-module-updated',event=>{models=event.detail;known=Boolean(models);redraw();});
  document.addEventListener('collection-module-update-error',()=>{known=false;error.textContent='任务状态读取失败，保留资料，恢复连接后可继续查看。';redraw();});
  redraw();
})();
