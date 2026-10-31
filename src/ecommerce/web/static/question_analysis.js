/* 问答预览和显式收费操作；显示文字只用安全文本节点。 */
(() => {
  const token = () => document.querySelector('meta[name="workbench-token"]').content;
  const post = async (url, value) => {
    const response = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':token()},body:JSON.stringify(value)});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || '操作失败，请刷新后核对。'); return data;
  };
  const configure = (container) => {
    const form = container.querySelector('[data-qa-form]'); if (!form) return;
    let choice=false, busy=false, revision=0;
    const feedback=form.querySelector('[data-qa-feedback]'), force=form.querySelector('[data-qa-force]');
    const submit=async (fresh=false) => {
      if (busy || !form.reportValidity()) return;
      busy=true;feedback.textContent='正在核对发送计划…';
      form.querySelector('button[type="submit"]').disabled=true;force.disabled=true;
      try {
        const data=await post(form.dataset.url,{batch_id:Number(form.dataset.batch),model:form.elements.model.value,output_tokens:Number(form.elements.output_tokens.value),input_hash:form.dataset.inputHash,plan_hash:form.dataset.planHash,allow_paid:form.elements.allow_paid.checked,force:fresh});
        if (!data.choice_required) {location.href=data.url;return;}
        choice=true;feedback.textContent=data.message;force.hidden=false;
        const existing=form.querySelector('[data-qa-existing]');existing.href=data.url;existing.hidden=false;
      } catch(error) {choice=false;force.hidden=true;feedback.textContent=error.message;}
      finally {busy=false;form.querySelector('button[type="submit"]').disabled=false;force.disabled=false;}
    };
    form.addEventListener('submit',event=>{event.preventDefault();submit();});
    force.addEventListener('click',()=>{if(choice)submit(true);});
    form.addEventListener('change',async event=>{
      if (!['model','output_tokens'].includes(event.target.name)) return;
      const currentRevision=++revision;
      const url=new URL(form.dataset.url.replace('/api/','/').replace(/\/$/, '')+'/new',location.origin);
      url.search=new URLSearchParams({batch:form.dataset.batch,model:form.elements.model.value,output_tokens:form.elements.output_tokens.value,dialog:'1'});
      form.querySelector('button[type="submit"]').disabled=true;force.hidden=true;
      try {const response=await fetch(url);if(!response.ok)throw new Error('计划无法生成，请刷新并调整模型或输出上限。');const markup=await response.text();if(currentRevision!==revision)return;container.innerHTML=markup;configure(container);}
      catch(error){if(currentRevision===revision)feedback.textContent=error.message;}
    });
  };
  document.querySelectorAll('[data-qa-container]').forEach(configure);
  if(document.querySelector('[data-qa-form]')&&!document.querySelector('[data-qa-container]'))configure(document);
  document.querySelectorAll('[data-qa-open]').forEach(link=>link.addEventListener('click',async event=>{
    event.preventDefault();let dialog=document.getElementById('question-analysis-dialog');
    if(!dialog){dialog=document.createElement('dialog');dialog.id='question-analysis-dialog';dialog.className='ui-modal';dialog.innerHTML='<header class="ui-modal-header"><h2>分析问答</h2><button type="button" class="ui-button ui-secondary" aria-label="关闭">关闭</button></header><div class="ui-modal-content qa-modal-content" data-qa-container></div>';document.body.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();}
    const container=dialog.querySelector('[data-qa-container]');container.textContent='正在读取本批问答及发送计划…';dialog.showModal();
    try {const url=new URL(link.href);url.searchParams.set('dialog','1');const response=await fetch(url);if(!response.ok)throw new Error('无法读取分析计划，请打开分析页面核对。');container.innerHTML=await response.text();configure(container);}
    catch(error){container.textContent=error.message;}
  }));
  document.querySelectorAll('[data-qa-check]').forEach(button=>button.onclick=async()=>{button.disabled=true;const feedback=document.querySelector('[data-qa-check-feedback]');try{await post(button.dataset.qaCheck,{report_id:button.dataset.reportId});location.href=location.pathname;}catch(error){feedback.textContent=error.message;button.disabled=false;}});
})();
