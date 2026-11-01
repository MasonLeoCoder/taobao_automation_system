/* 全站回顶部：只滚动页面，复用公共按钮，不发请求或更改业务状态。 */
(() => {
  const button=document.getElementById('back-to-top');
  if(!button)return;
  const bar=document.getElementById('collection-task-bar');
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  const popoverSelector=window.CSS?.supports('selector(:popover-open)') ? '[popover]:popover-open' : null;
  let scheduled=false;
  function update(){
    scheduled=false;
    const base=innerWidth<=700 ? 16 : 24;
    let bottom=base;
    if(bar && !bar.hidden && bar.getClientRects().length){
      const rect=bar.getBoundingClientRect();
      if(rect.bottom>0 && rect.top<innerHeight)bottom=Math.max(base,innerHeight-rect.top+12);
    }
    const visible=scrollY>500 && !document.querySelector('dialog[open]') && !(popoverSelector && document.querySelector(popoverSelector)) && bottom+56<innerHeight;
    button.hidden=!visible;
    const value=`calc(${bottom}px + env(safe-area-inset-bottom,0px))`;
    if(button.style.bottom!==value)button.style.bottom=value;
  }
  function schedule(){if(!scheduled){scheduled=true;requestAnimationFrame(update);}}
  button.addEventListener('click',()=>{
    const main=document.getElementById('main');
    if(main){if(!main.hasAttribute('tabindex'))main.setAttribute('tabindex','-1');main.focus({preventScroll:true});}
    window.scrollTo({top:0,left:window.scrollX,behavior:reduced.matches ? 'instant' : 'smooth'});
  });
  addEventListener('scroll',schedule,{passive:true});addEventListener('resize',schedule);addEventListener('pageshow',schedule);
  document.addEventListener('close',schedule,true);
  document.addEventListener('toggle',schedule,true);
  const modals=new MutationObserver(schedule),seen=new WeakSet();
  function observeDialogs(root){
    const dialogs=root.matches?.('dialog') ? [root] : root.querySelectorAll?.('dialog') || [];
    for(const dialog of dialogs)if(!seen.has(dialog)){seen.add(dialog);modals.observe(dialog,{attributes:true,attributeFilter:['open']});}
  }
  observeDialogs(document);
  new MutationObserver(changes=>{
    for(const change of changes)for(const node of change.addedNodes)if(node.nodeType===1)observeDialogs(node);
    schedule();
  }).observe(document.body,{childList:true,subtree:true});
  if(bar){
    new MutationObserver(schedule).observe(bar,{attributes:true,attributeFilter:['hidden','style','class']});
    if(window.ResizeObserver)new ResizeObserver(schedule).observe(bar);
  }
  update();
})();
