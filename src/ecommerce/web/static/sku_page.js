/* 只复制所选规格的原始名称，不创建任务或修改商品记录。 */
(() => {
  document.querySelectorAll('[data-sku-copy]').forEach(button=>{
    button.addEventListener('click',async()=>{
      const source=document.getElementById(button.dataset.skuCopy),note=button.parentElement.querySelector('[data-sku-copy-feedback]');
      if(!source || button.disabled)return;
      button.disabled=true;
      try{await navigator.clipboard.writeText(source.textContent);note.textContent='已复制规格名称。';}
      catch(_){note.textContent='复制失败，请在上方选中原规格名称后手动复制。';}
      finally{button.disabled=false;}
    });
  });
  const root=document.querySelector('[data-sku-page]');if(root)root.dataset.skuReady='true';
})();
