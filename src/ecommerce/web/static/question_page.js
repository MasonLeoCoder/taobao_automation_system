/* 批次待切换、整批分析摘要与紧凑排序；只读状态不调用AI。 */
(() => {
  if(window.questionPageBound)return;window.questionPageBound=true;
  const batchForm=document.querySelector('[data-qa-batch-form]');
  if(batchForm){const select=batchForm.elements.batch,initial=select.value,pending=batchForm.querySelector('[data-qa-batch-pending]'),button=batchForm.querySelector('button[type=submit]');
    select.addEventListener('change',()=>{pending.hidden=select.value===initial;});
    batchForm.addEventListener('submit',event=>{if(batchForm.dataset.loading){event.preventDefault();return;}batchForm.dataset.loading='1';button.disabled=true;button.textContent='正在读取…';});
    addEventListener('pageshow',()=>{delete batchForm.dataset.loading;button.disabled=false;button.textContent='查看';pending.hidden=select.value===initial;});
  }
  document.querySelectorAll('[data-qa-sort-form]').forEach(form=>{
    form.elements.sort.addEventListener('change',()=>form.requestSubmit());
    form.querySelector('[data-qa-order]').addEventListener('click',event=>{form.elements.order.value=event.currentTarget.dataset.qaOrder;form.requestSubmit();});
  });
  const summary=document.querySelector('[data-qa-batch-summary]');
  if(summary){let loading=false;
    const refresh=async()=>{if(loading)return;loading=true;
      try{const response=await fetch(summary.dataset.qaBatchSummary);if(!response.ok)throw new Error('读取失败');const data=await response.json();summary.textContent=data.message;
        for(const [selector,href] of [['[data-qa-batch-report]',data.result_url],['[data-qa-progress]',data.progress_url]]){const link=document.querySelector(selector);link.hidden=!href;if(href)link.href=href;}
      }catch(error){summary.textContent='分析状态读取失败，请刷新页面重试；未启动AI请求。';}
      finally{loading=false;}
    };refresh();setInterval(()=>{if(!document.hidden)refresh();},10000);
  }
})();
