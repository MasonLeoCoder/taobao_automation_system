/* SKU更新只改进度区域，始终保留已加载的规格、批次和来源链接。 */
(() => {
  const root=document.querySelector('[data-sku-update]'),modal=document.querySelector('[data-sku-update-modal]');
  if(!root || !modal)return;
  const open=root.querySelector('[data-sku-update-open]'),note=root.querySelector('[data-sku-update-status]');
  const runtime=root.querySelector('[data-sku-update-runtime]'),confirm=modal.querySelector('[data-sku-update-confirm]');
  const closers=modal.querySelectorAll('[data-sku-update-close]'),error=modal.querySelector('[data-sku-update-error]');
  let snapshot=null,known=false,sending=false,mode='confirm';
  const one=(parent,key)=>parent.querySelector(`[data-sku-${key}]`);
  function bindLink(node,url,label){node.hidden=!url;if(url)node.href=url;if(label)node.textContent=label;}
  function batchText(batch){return batch ? `批次 ${batch.id} · ${batch.time_text || '时间未记录'} · ${batch.count} 种规格` : '暂无有效SKU资料';}
  function render(){
    const active=Boolean(snapshot?.active),outcome=snapshot?.outcome;
    open.disabled=!known || sending;open.textContent=sending ? '正在提交…' : active ? '查看更新进度' : '更新SKU信息';
    note.textContent=known ? (active ? '已有SKU采集申请，当前资料保持原批次。' : '') : '更新任务暂未核对，当前资料仍可查看；不会自动重新采集。';
    runtime.hidden=!known || !outcome || (!active && outcome.complete && Number(outcome.batch?.id)===Number(root.dataset.skuLoadedBatch));
    if(outcome){
      one(runtime,'runtime-label').textContent=outcome.label;
      one(runtime,'runtime-meta').textContent=outcome.batch ? batchText(outcome.batch) : '';
      one(runtime,'runtime-message').textContent=outcome.message || '当前列表仍为已加载批次，需手动查看新结果。';
      bindLink(one(runtime,'runtime-result'),outcome.batch?.url,outcome.complete ? '查看新批次' : '查看本次结果');
      bindLink(one(runtime,'runtime-task'),outcome.task_url);bindLink(one(runtime,'runtime-logs'),outcome.logs_url);
    }
    one(modal,'confirm-latest').textContent=batchText(snapshot?.latest_valid);
    if(modal.open && active)mode='progress';
    const progress=mode==='progress';
    document.getElementById('sku-update-title').textContent=progress ? 'SKU更新进度' : '更新SKU信息';
    one(modal,'confirm-description').hidden=progress;
    one(modal,'modal-progress').hidden=!progress;
    if(progress){
      const state=one(modal,'modal-state');state.textContent=known ? outcome?.label || '进度待核对' : '状态暂时无法读取';
      state.className='ui-report-state '+(active ? 'is-active' : outcome?.complete ? 'is-ready' : 'is-pending');
      one(modal,'modal-message').textContent=known ? [outcome?.message,outcome?.batch ? batchText(outcome.batch) : '实际采集任务建立后提供任务入口。'].filter(Boolean).join(' ') : '保留当前资料，恢复连接后再查看。';
      bindLink(one(modal,'modal-result'),outcome?.batch?.url,outcome?.complete ? '查看新批次' : '查看本次结果');
      bindLink(one(modal,'modal-task'),outcome?.task_url);bindLink(one(modal,'modal-queue'),active ? snapshot?.queue_url : null);
    }
    confirm.hidden=progress;confirm.disabled=!known || sending || active;
    confirm.textContent=sending ? '正在提交…' : '确认更新';closers.forEach(button=>button.disabled=sending);
  }
  open.addEventListener('click',()=>{
    if(!known || sending || document.querySelector('dialog[open]'))return;
    mode=snapshot.active ? 'progress' : 'confirm';error.textContent='';modal.showModal();render();
    (confirm.hidden ? closers[0] : confirm).focus();
  });
  const close=()=>{if(sending)return;modal.close();open.focus({preventScroll:true});};
  closers.forEach(button=>button.addEventListener('click',close));modal.addEventListener('cancel',event=>{event.preventDefault();close();});
  confirm.addEventListener('click',async()=>{
    if(!known || sending || snapshot?.active || mode!=='confirm')return;
    sending=true;error.textContent='';render();
    try{
      const job=await new Promise((resolve,reject)=>document.dispatchEvent(new CustomEvent('collection-sku-update-request',{detail:{resolve,reject}})));
      snapshot={...snapshot,active:!job.done,outcome:{state:job.state,label:job.done ? '结果正在核对' : '申请已保存，正在核对进度',
        message:job.message || '',task_url:null,logs_url:null,batch:null,complete:false}};
      mode='progress';
    }catch(failure){error.textContent=failure.message;}
    finally{sending=false;render();}
  });
  document.addEventListener('collection-sku-update-state',event=>{snapshot=event.detail;known=Boolean(snapshot);render();});
  document.addEventListener('collection-sku-update-error',()=>{known=false;render();});
  if(root.skuUpdateSnapshot){snapshot=root.skuUpdateSnapshot;known=true;}
  render();root.dataset.skuUpdateReady='true';
})();
