/* 固定来源预览与显式付费；改设置清确认，未知提交只指向历史，不自动POST。 */
(() => {
  const directory=document.querySelector('[data-ca-directory]');
  if(directory){const narrow=matchMedia('(max-width:1000px)');directory.open=!narrow.matches;narrow.addEventListener('change',e=>directory.open=!e.matches);}
  document.addEventListener('click',event=>{const link=event.target.closest('a[href^="#composite-"]');if(!link)return;const target=document.getElementById(link.hash.slice(1));if(!target)return;for(let node=target;node;node=node.parentElement)if(node.tagName==='DETAILS')node.open=true;});
  const post=async(url,value)=>{
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
    try{const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':document.querySelector('meta[name="workbench-token"]').content},body:JSON.stringify(value),signal:controller.signal});
      let data;try{data=await response.json();}catch{throw new Error('提交结果未知，请返回综合分析记录核对，勿重复提交。');}
      if(!response.ok){const error=new Error(data.error||'操作未完成。');error.confirmed=true;throw error;}return data;
    }finally{clearTimeout(timer);}
  };
  const configure=container=>{
    container.dispatchEvent(new CustomEvent('ui:controls-added',{bubbles:true}));
    const form=container.querySelector('[data-ca-form]');if(!form)return;
    let busy=false,choice=false,revision=0;
    const feedback=form.querySelector('[data-ca-feedback]'),submit=form.querySelector('[type=submit]'),force=form.querySelector('[data-ca-force]');
    const setBusy=value=>{submit.disabled=value;force.disabled=value;for(const element of form.querySelectorAll('select,input,.ss-trigger'))element.disabled=value;};
    form.querySelector('[data-ca-cancel]').onclick=()=>{const dialog=form.closest('dialog');if(dialog)dialog.close();else location.href=form.dataset.url.replace('/api/','/');};
    const send=async fresh=>{
      if(busy||!form.reportValidity())return;
      const value={review_report:form.elements.review_report.value,question_report:form.elements.question_report.value,model:form.elements.model.value,
        output_tokens:Number(form.elements.output_tokens.value),input_hash:form.dataset.inputHash,plan_hash:form.dataset.planHash,allow_paid:form.elements.allow_paid.checked,force:fresh};
      busy=true;setBusy(true);feedback.textContent='正在创建固定来源的综合分析…';
      try{const data=await post(form.dataset.url,value);if(!data.choice_required){location.href=data.url;return;}
        choice=true;form.querySelector('[data-ca-choice]').hidden=false;const existing=form.querySelector('[data-ca-existing]');existing.href=data.url;feedback.textContent=data.message;
        busy=false;setBusy(false);
      }catch(error){feedback.textContent=error.confirmed?error.message:'提交结果尚未确认，请取消并返回综合分析记录核对；未自动重新发送。';if(error.confirmed){busy=false;setBusy(false);}}
    };
    form.onsubmit=event=>{event.preventDefault();send(false);};force.onclick=()=>{if(choice)send(true);};
    form.addEventListener('change',async event=>{
      if(!['review_report','question_report','model','output_tokens'].includes(event.target.name)||busy)return;
      form.elements.allow_paid.checked=false;choice=false;form.querySelector('[data-ca-choice]').hidden=true;submit.disabled=true;force.disabled=true;
      const current=++revision,url=new URL(form.dataset.previewUrl,location.origin);url.search=new URLSearchParams({dialog:'1',review_report:form.elements.review_report.value,
        question_report:form.elements.question_report.value,model:form.elements.model.value,output_tokens:form.elements.output_tokens.value,origin:form.dataset.caOrigin});feedback.textContent='正在重新核对来源与容量…';
      try{const response=await fetch(url);if(!response.ok)throw new Error('新计划无法生成，请调整来源、模型或输出上限后重试；未发送资料。');const markup=await response.text();
        if(current!==revision||!form.isConnected)return;const advanced=form.querySelector('[data-ca-advanced]').open;container.dispatchEvent(new CustomEvent('ui:controls-remove',{bubbles:true}));container.innerHTML=markup;container.querySelector('[data-ca-advanced]').open=advanced;configure(container);
      }catch(error){if(current===revision)feedback.textContent=error.message;}
    });
    form.addEventListener('invalid',()=>{for(const detail of form.querySelectorAll('details'))if(detail.querySelector(':invalid'))detail.open=true;},true);
  };
  document.querySelectorAll('[data-ca-container]').forEach(configure);
  let openRevision=0;
  const openDialog=async(href,fields=null)=>{
    let dialog=document.getElementById('composite-analysis-dialog');
    if(!dialog){dialog=document.createElement('dialog');dialog.id='composite-analysis-dialog';dialog.className='ui-modal';dialog.setAttribute('aria-labelledby','ca-dialog-title');
      dialog.innerHTML='<header class="ui-modal-header"><div><p class="ui-modal-eyebrow">竞品研究 · 综合选品</p><h2 id="ca-dialog-title">生成综合选品报告</h2><p>选择两份报告并确认发送范围和预计请求次数。</p></div><button type="button" class="ui-button ui-secondary ui-modal-close" aria-label="关闭">×</button></header><div class="ui-modal-content" data-ca-container></div>';
      document.body.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{openRevision++;});}
    const current=++openRevision,container=dialog.querySelector('[data-ca-container]');container.dispatchEvent(new CustomEvent('ui:controls-remove',{bubbles:true}));container.textContent='正在读取来源及发送计划…';dialog.showModal();
    try{const url=new URL(href,location.origin);url.searchParams.set('dialog','1');url.searchParams.set('origin',location.pathname+location.search);
      if(fields)for(const key of ['review_report','question_report','model','output_tokens'])if(typeof fields[key]==='string')url.searchParams.set(key,fields[key]);
      const response=await fetch(url);if(!response.ok)throw new Error('无法生成计划，请打开来源报告核对后再试。');const markup=await response.text();if(current!==openRevision||!dialog.open)return null;container.innerHTML=markup;configure(container);return container.querySelector('[data-ca-form]');}
    catch(error){if(current===openRevision&&dialog.open){container.textContent=error.message;if(fields)dialog.close();}return null;}
  };
  document.querySelectorAll('[data-ca-open]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();openDialog(link.href);}));
  document.addEventListener('report-navigation:restore-dialog',event=>{const link=document.querySelector('[data-ca-open]');if(link)event.detail.result=openDialog(link.href,event.detail.fields);});
  document.querySelectorAll('[data-ca-check]').forEach(button=>button.onclick=async()=>{button.disabled=true;const feedback=document.querySelector('[data-ca-check-feedback]');
    try{await post(button.dataset.caCheck,{report_id:button.dataset.reportId});location.href=location.pathname;}
    catch(error){feedback.textContent=error.confirmed?error.message:'本地检查保存结果未知，请刷新核对；该操作没有调用AI。';button.disabled=false;}
  });
})();
