/* 问答预览和显式收费操作；显示文字只用安全文本节点。 */
(() => {
  const directory=document.querySelector('[data-qa-directory]');
  if(directory){const narrow=matchMedia('(max-width:1000px)');directory.open=!narrow.matches;narrow.addEventListener('change',event=>{directory.open=!event.matches;});}
  const reveal=(target)=>{for(let node=target.parentElement;node;node=node.parentElement)if(node.tagName==='DETAILS')node.open=true;};
  document.querySelectorAll('[data-qa-source],[data-qa-jump]').forEach(link=>link.addEventListener('click',()=>{
    const target=document.getElementById(link.hash.slice(1));if(!target)return;reveal(target);if(target.tagName==='DETAILS')target.open=true;
    if(link.hasAttribute('data-qa-source')){const back=document.querySelector('[data-qa-source-back]');if(back){back.href='#'+link.dataset.qaReturn;back.hidden=false;target.before(back);}}
  }));
  const sourceBack=document.querySelector('[data-qa-source-back]');
  if(sourceBack)sourceBack.addEventListener('click',()=>{const target=document.getElementById(sourceBack.hash.slice(1));if(target)reveal(target);});
  const token = () => document.querySelector('meta[name="workbench-token"]').content;
  const post = async (url, value) => {
    const response = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':token()},body:JSON.stringify(value)});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || '操作失败，请刷新后核对。'); return data;
  };
  const configure = (container) => {
    const form = container.querySelector('[data-qa-form]'); if (!form) return;
    let choice=false, busy=false, revision=0;
    const close=()=>{const dialog=form.closest('dialog');if(dialog)dialog.close();else location.href='/collections/products/'+location.pathname.split('/')[4]+'?module=questions&batch='+form.dataset.batch;};
    form.querySelector('[data-qa-cancel]').addEventListener('click',close);
    const revealInvalid=()=>{form.querySelectorAll('details').forEach(detail=>{if(detail.querySelector(':invalid'))detail.open=true;});};
    const feedback=form.querySelector('[data-qa-feedback]'), force=form.querySelector('[data-qa-force]');
    const submit=async (fresh=false) => {
      if(busy)return;revealInvalid();if(!form.reportValidity())return;
      busy=true;feedback.textContent='正在创建任务…';
      form.querySelector('button[type="submit"]').disabled=true;force.disabled=true;
      try {
        const data=await post(form.dataset.url,{batch_id:Number(form.dataset.batch),model:form.elements.model.value,output_tokens:Number(form.elements.output_tokens.value),input_hash:form.dataset.inputHash,plan_hash:form.dataset.planHash,allow_paid:form.elements.allow_paid.checked,force:fresh});
        if (!data.choice_required) {location.href=data.url;return;}
        choice=true;feedback.textContent=data.message;force.hidden=false;form.querySelector('[data-qa-choice]').hidden=false;force.scrollIntoView({block:'nearest'});
        const existing=form.querySelector('[data-qa-existing]');existing.href=data.url;existing.hidden=false;
      } catch(error) {choice=false;force.hidden=true;form.querySelector('[data-qa-choice]').hidden=true;feedback.textContent=error.message;}
      finally {busy=false;form.querySelector('button[type="submit"]').disabled=false;force.disabled=false;}
    };
    form.addEventListener('submit',event=>{event.preventDefault();submit();});
    form.addEventListener('invalid',revealInvalid,true);
    force.addEventListener('click',()=>{if(choice)submit(true);});
    form.addEventListener('change',async event=>{
      if (!['model','output_tokens'].includes(event.target.name)) return;
      const currentRevision=++revision;
      form.elements.allow_paid.checked=false;form.querySelector('[data-qa-choice]').hidden=true;
      const url=new URL(form.dataset.url.replace('/api/','/').replace(/\/$/, '')+'/new',location.origin);
      url.search=new URLSearchParams({batch:form.dataset.batch,model:form.elements.model.value,output_tokens:form.elements.output_tokens.value,dialog:'1'});
      form.querySelector('button[type="submit"]').disabled=true;force.hidden=true;
      try {const response=await fetch(url);if(!response.ok)throw new Error('计划无法生成，请刷新并调整模型或输出上限。');const markup=await response.text();if(currentRevision!==revision)return;const advanced=form.querySelector('[data-qa-advanced]').open;container.innerHTML=markup;container.querySelector('[data-qa-advanced]').open=advanced;configure(container);}
      catch(error){if(currentRevision===revision)feedback.textContent=error.message;}
    });
  };
  document.querySelectorAll('[data-qa-container]').forEach(configure);
  if(document.querySelector('[data-qa-form]')&&!document.querySelector('[data-qa-container]'))configure(document);
  document.querySelectorAll('[data-qa-open]').forEach(link=>link.addEventListener('click',async event=>{
    event.preventDefault();let dialog=document.getElementById('question-analysis-dialog');
    if(!dialog){dialog=document.createElement('dialog');dialog.id='question-analysis-dialog';dialog.className='ui-modal';dialog.setAttribute('aria-labelledby','qa-dialog-title');dialog.innerHTML='<header class="ui-modal-header"><div><p class="ui-modal-eyebrow">竞品研究 · 问大家</p><h2 id="qa-dialog-title">分析问答</h2><p>确认本批资料与分析设置，生成选品参考报告。</p></div><button type="button" class="ui-button ui-secondary ui-modal-close" aria-label="关闭">×</button></header><div class="ui-modal-content qa-modal-content" data-qa-container></div>';document.body.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();}
    const container=dialog.querySelector('[data-qa-container]');container.textContent='正在读取本批问答及发送计划…';dialog.showModal();
    try {const url=new URL(link.href);url.searchParams.set('dialog','1');const response=await fetch(url);if(!response.ok)throw new Error('无法读取分析计划，请打开分析页面核对。');container.innerHTML=await response.text();configure(container);}
    catch(error){container.textContent=error.message;}
  }));
  document.querySelectorAll('[data-qa-check]').forEach(button=>button.onclick=async()=>{button.disabled=true;const feedback=document.querySelector('[data-qa-check-feedback]');try{await post(button.dataset.qaCheck,{report_id:button.dataset.reportId});location.href=location.pathname;}catch(error){feedback.textContent=error.message;button.disabled=false;}});
})();
