/* 来源报告往返：本标签页只保存界面位置与非付费设置，不保存报告正文或费用授权。 */
(() => {
  const prefix='report-navigation.v1.',maxAge=4*60*60*1000;
  const normalized=value=>{const url=new URL(value,location.origin);url.searchParams.delete('_report_back');url.searchParams.delete('_report_return_notice');url.searchParams.sort();url.hash='';return url.pathname+url.search;};
  const notice=text=>{const main=document.getElementById('main');if(!main)return;const p=document.createElement('p');p.className='ui-report-notice';p.setAttribute('role','status');p.textContent=text;main.querySelector('header')?.after(p);};
  const hash=text=>{let n=0;for(const char of text)n=(n*31+char.codePointAt(0))|0;return n.toString(16);};
  const folds=root=>{const counts=new Map();return [...root.querySelectorAll('details')].map(node=>{const base=node.id||hash(node.querySelector(':scope > summary')?.textContent||'');const n=counts.get(base)||0;counts.set(base,n+1);return {node,key:base+':'+n};});};
  const snapshotFolds=root=>folds(root).map(({node,key})=>({key,open:node.open}));
  const applyFolds=(root,values)=>{const state=new Map((values||[]).map(v=>[v.key,v.open]));for(const {node,key} of folds(root))if(state.has(key))node.open=state.get(key);};
  const formState=form=>({fields:Object.fromEntries(['review_report','question_report','model','output_tokens'].map(key=>[key,form.elements[key].value])),
    isDialog:!!form.closest('dialog'),folds:snapshotFolds(form),scroll:form.querySelector('.ui-confirm-body')?.scrollTop||0});
  const prune=()=>{const keys=[],names=[];for(let i=0;i<sessionStorage.length;i++)names.push(sessionStorage.key(i));for(const key of names){if(!key?.startsWith(prefix))continue;try{const value=JSON.parse(sessionStorage.getItem(key));if(!Number.isFinite(value.created)||Date.now()-value.created>maxAge)sessionStorage.removeItem(key);else keys.push({key,created:value.created});}catch{sessionStorage.removeItem(key);}}
    keys.sort((a,b)=>b.created-a.created);for(const entry of keys.slice(9))sessionStorage.removeItem(entry.key);};
  document.addEventListener('click',event=>{
    const link=event.target.closest('a[data-report-source]');if(!link||event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    const url=new URL(link.href,location.origin),target=url.searchParams.get('return_to');if(!target||url.origin!==location.origin)return;
    try{prune();const id=crypto.randomUUID().replaceAll('-',''),form=link.closest('[data-ca-form]');
      const main=document.getElementById('main');const value={created:Date.now(),target:normalized(target),y:scrollY,
        folds:main?snapshotFolds(main):[],form:form?formState(form):null};
      sessionStorage.setItem(prefix+id,JSON.stringify(value));url.searchParams.set('return_state',id);link.href=url.href;
    }catch{/* 禁用本地存储时仍走服务端受控返回。 */}
  });
  const clearConsent=()=>document.querySelectorAll('[data-ca-form] [name=allow_paid]').forEach(input=>{input.checked=false;});
  addEventListener('pageshow',clearConsent);
  const restore=async()=>{
    const url=new URL(location.href),id=url.searchParams.get('_report_back'),invalid=url.searchParams.get('_report_return_notice')==='1';
    if(!id&&!invalid)return;url.searchParams.delete('_report_back');url.searchParams.delete('_report_return_notice');history.replaceState(history.state,'',url.pathname+url.search+url.hash);
    if(invalid){notice('原返回页面已失效，已返回该商品的综合分析页。');return;}
    let state;
    try{if(!/^[a-f0-9]{32}$/.test(id))throw Error();state=JSON.parse(sessionStorage.getItem(prefix+id));
      const validFolds=rows=>Array.isArray(rows)&&rows.every(v=>typeof v?.key==='string'&&typeof v.open==='boolean');
      if(!state||!Number.isFinite(state.created)||!Number.isFinite(state.y)||!validFolds(state.folds)||Date.now()-state.created>maxAge||state.target!==normalized(location.href))throw Error();
      if(state.form&&(!validFolds(state.form.folds)||typeof state.form.isDialog!=='boolean'||!Number.isFinite(state.form.scroll)||!state.form.fields||!['review_report','question_report','model','output_tokens'].every(k=>typeof state.form.fields[k]==='string')))throw Error();
    }catch{notice('阅读位置记录已失效，已返回原页面；请重新选择需要查看的位置。');return;}
    const main=document.getElementById('main');if(main)applyFolds(main,state.folds);
    if(state.form?.isDialog){
      // 先恢复背景，再打开模态框；打开后滚动文档可能使原生dialog及遮罩偏移。
      scrollTo({top:state.y,behavior:'instant'});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      const detail={fields:state.form.fields,result:null};document.dispatchEvent(new CustomEvent('report-navigation:restore-dialog',{detail}));
      const form=detail.result?await detail.result:null;
      if(form){applyFolds(form,state.form.folds);form.querySelector('.ui-confirm-body').scrollTop=state.form.scroll;
        form.closest('dialog').addEventListener('close',()=>requestAnimationFrame(()=>{if(!document.querySelector('dialog[open]'))scrollTo({top:state.y,behavior:'instant'});}),{once:true});}
      else notice('原分析设置无法恢复，资料可能已变化；请重新选择来源报告，未发出AI请求。');
    }else if(state.form){const form=document.querySelector('[data-ca-form]');if(form)applyFolds(form,state.form.folds);}
    clearConsent();if(!state.form?.isDialog)requestAnimationFrame(()=>requestAnimationFrame(()=>scrollTo({top:state.y,behavior:'instant'})));
  };
  restore();
})();
