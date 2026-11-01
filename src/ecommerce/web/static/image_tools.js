/* 工具箱和主图共用格式记忆及本地下载；文件名仅使用文本节点展示。 */
(() => {
  const notes={png:'PNG：保留透明背景，文件通常较大。',jpg:'JPG：适合普通商品图，透明区域填充白色，文件通常较小。',original:'保持原格式：保留文件内容，不重新编码。'};
  const token=document.querySelector('meta[name="workbench-token"]')?.content || '';
  function formatControl(root,key) {
    const select=root.querySelector('[data-image-format]'),note=root.querySelector('[data-image-format-note]');
    try {const saved=localStorage.getItem(key);if(Object.hasOwn(notes,saved))select.value=saved;} catch (_) { /* 禁用存储时仍可正常使用。 */ }
    const update=()=>{note.textContent=notes[select.value];};
    select.addEventListener('change',()=>{update();try {localStorage.setItem(key,select.value);} catch (_) {}});
    update();select.dispatchEvent(new Event('change',{bubbles:true}));
    return select;
  }
  async function requestDownload(url,payload,isJson,onProgress) {
    const response=await new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest();xhr.open('POST',url);xhr.responseType='blob';xhr.timeout=180000;
      xhr.setRequestHeader('X-Collection-Token',token);
      if(isJson)xhr.setRequestHeader('Content-Type','application/json');
      xhr.upload.onprogress=event=>onProgress(event.lengthComputable ? `正在提交 ${Math.round(event.loaded/event.total*100)}%…` : '正在提交…');
      xhr.upload.onload=()=>onProgress('正在本地处理图片并准备下载…');
      xhr.onload=()=>resolve(xhr);xhr.onerror=()=>reject(new Error('连接中断，请检查本地服务后重试。'));
      xhr.ontimeout=()=>reject(new Error('处理等待超时，请减少图片数量后重试。'));
      xhr.send(isJson ? JSON.stringify(payload) : payload);
    });
    if(response.status<200 || response.status>=300) {
      let message='处理失败，请稍后重试。';
      try {message=JSON.parse(await response.response.text()).error || message;}catch(_) {}
      throw new Error(message);
    }
    let filename='图片下载.zip';
    const disposition=response.getResponseHeader('Content-Disposition') || '';
    const encoded=disposition.match(/filename\*=UTF-8''([^;]+)/i),plain=disposition.match(/filename="([^"]+)"/i);
    try {filename=encoded ? decodeURIComponent(encoded[1]) : plain ? plain[1] : filename;}catch(_) {}
    const urlObject=URL.createObjectURL(response.response),link=document.createElement('a');
    link.href=urlObject;link.download=filename;document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(urlObject),60000);
    return response.getResponseHeader('X-Image-Count') || '';
  }
  const tool=document.querySelector('[data-image-converter]');
  if(tool) {
    const input=tool.querySelector('[data-image-files]'),list=tool.querySelector('[data-image-list]'),drop=tool.querySelector('[data-image-drop]');
    const pick=tool.querySelector('[data-image-pick]'),clear=tool.querySelector('[data-image-clear]'),run=tool.querySelector('[data-image-convert]');
    const count=tool.querySelector('[data-image-count]'),empty=tool.querySelector('[data-image-empty]'),feedback=tool.querySelector('[data-image-feedback]');
    const format=formatControl(tool,'toolbox.image-converter.format.v1');let files=[],busy=false;
    const say=(text,error=false)=>{feedback.textContent=text;feedback.classList.toggle('is-error',error);};
    function render() {
      list.replaceChildren();count.textContent=`${files.length} 张`;empty.hidden=files.length>0;
      clear.disabled=busy || !files.length;run.disabled=busy || !files.length;pick.disabled=busy;input.disabled=busy;
      format.disabled=busy;format.dispatchEvent(new Event('change',{bubbles:true}));
      run.textContent=busy ? '正在处理…' : `转换并下载${files.length ? `（${files.length}张）` : ''}`;
      files.forEach((file,index)=>{
        const row=document.createElement('li');row.className='ui-tool-file it-file';
        const identity=document.createElement('div'),name=document.createElement('strong'),meta=document.createElement('small');
        name.textContent=file.name;meta.className='ui-form-note';
        const suffix=file.name.includes('.') ? file.name.split('.').pop().slice(0,12).toUpperCase() : '未标注扩展名';
        meta.textContent=`${suffix} · ${(file.size/1024/1024).toFixed(2)} MB`;
        identity.append(name,meta);
        const remove=document.createElement('button');remove.type='button';remove.className='ui-button ui-secondary';remove.textContent='移除';
        remove.setAttribute('aria-label',`移除 ${file.name}`);remove.disabled=busy;
        remove.addEventListener('click',()=>{files.splice(index,1);say('');render();});row.append(identity,remove);list.append(row);
      });
    }
    function add(selected) {
      if(busy)return;
      const incoming=Array.from(selected);if(!incoming.length)return;
      if(files.length+incoming.length>20){say('一次最多20张图片，请减少选择数量。',true);return;}
      const large=incoming.find(file=>file.size>10*1024*1024 || !file.size);
      if(large){say(`“${large.name}”为空或超过单张10 MB上限，本次选择未加入。`,true);return;}
      if([...files,...incoming].reduce((sum,file)=>sum+file.size,0)>50*1024*1024){say('所选图片合计超过50 MB，本次选择未加入。',true);return;}
      files.push(...incoming);say('');render();
    }
    pick.addEventListener('click',()=>input.click());input.addEventListener('change',()=>{add(input.files);input.value='';});
    clear.addEventListener('click',()=>{files=[];say('');render();});
    ['dragenter','dragover'].forEach(event=>drop.addEventListener(event,e=>{e.preventDefault();if(!busy)drop.classList.add('is-dragging');}));
    drop.addEventListener('dragleave',()=>drop.classList.remove('is-dragging'));
    drop.addEventListener('drop',event=>{event.preventDefault();drop.classList.remove('is-dragging');add(event.dataTransfer.files);});
    run.addEventListener('click',async()=>{
      if(busy || !files.length)return;const body=new FormData();body.append('format',format.value);files.forEach(file=>body.append('files',file,file.name));
      busy=true;render();say('正在准备图片…');
      try {const count=await requestDownload(tool.dataset.convertUrl,body,false,text=>say(text));say(`已准备 ${count} 张图片并发起下载。列表已保留，可以更换格式再次下载。`);}
      catch(error){say(error.message,true);}finally{busy=false;render();}
    });render();tool.dataset.imageReady='true';
  }
  const page=document.querySelector('[data-main-image-page]'),modal=document.querySelector('[data-image-download-modal]');
  if(page && modal) {
    const format=formatControl(modal,'toolbox.main-images.format.v1'),confirm=modal.querySelector('[data-image-download-confirm]');
    const scope=modal.querySelector('[data-image-download-scope]'),feedback=modal.querySelector('[data-image-download-feedback]');
    const pageFeedback=page.querySelector('[data-image-page-feedback]'),closers=modal.querySelectorAll('[data-image-download-close]');
    let active=null,busy=false,opener=null;
    function close(){if(busy)return;modal.close();opener?.focus({preventScroll:true});}
    closers.forEach(button=>button.addEventListener('click',close));modal.addEventListener('cancel',event=>{event.preventDefault();close();});
    page.querySelectorAll('[data-image-download]').forEach(button=>button.addEventListener('click',()=>{
      if(busy || document.querySelector('dialog[open]'))return;opener=button;active=button.dataset.imageDownload;
      scope.textContent=active==='all' ? button.textContent+' · 包含本批所有分页' : `下载 ${button.closest('[data-main-image]').querySelector('h3').textContent}`;
      feedback.textContent='';feedback.classList.remove('is-error');modal.showModal();confirm.focus();
    }));
    confirm.addEventListener('click',async()=>{
      if(busy || !active)return;busy=true;confirm.disabled=true;format.disabled=true;closers.forEach(button=>button.disabled=true);
      format.dispatchEvent(new Event('change',{bubbles:true}));const body={format:format.value};if(active!=='all')body.media_id=Number(active);
      const say=(text,error=false)=>{feedback.textContent=text;feedback.classList.toggle('is-error',error);};
      say('正在核对本批原件…');confirm.textContent='正在处理…';
      try {const count=await requestDownload(page.dataset.imageDownloadUrl,body,true,text=>say(text));pageFeedback.textContent=`已准备 ${count} 张图片并发起下载，采集原件保持不变。`;busy=false;close();}
      catch(error){say(error.message,true);}finally{busy=false;confirm.disabled=false;format.disabled=false;closers.forEach(button=>button.disabled=false);confirm.textContent='确认下载';format.dispatchEvent(new Event('change',{bubbles:true}));}
    });
    page.querySelectorAll('[data-gallery-preview]').forEach(button=>button.addEventListener('click',()=>button.closest('[data-main-image]').querySelector('img')?.click()));
    page.querySelectorAll('[data-gallery-image]').forEach(img=>{
      const failed=()=>{img.hidden=true;img.closest('.pi-picture').querySelector('[data-image-load-error]').hidden=false;};
      img.addEventListener('error',failed);if(img.complete && !img.naturalWidth)failed();
    });
  }
})();
