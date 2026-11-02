/* 先预览核对，再由用户手势打开保存窗口；仅写入成功后记为已保存。 */
(() => {
  const root=document.querySelector('[data-douyin]');if(!root)return;
  const el=id=>document.getElementById('dv-'+id), token=document.querySelector('meta[name="workbench-token"]')?.content||'';
  let active=null,row=null,requestBusy=false,saving=false,controller=null,generation=0,playable=false;
  const say=text=>{el('status').textContent=text;};
  const storage={get:key=>{try{return localStorage.getItem(key);}catch(_){return null;}},set:(key,value)=>{try{localStorage.setItem(key,value);}catch(_){}}};
  async function api(path,body){const response=await fetch('/api/tools/douyin'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Collection-Token':token}:{},body:body?JSON.stringify(body):undefined});const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败，请重试');return data;}
  function controls(){el('save').disabled=saving||!row?.ready||!playable||!el('reviewed').checked;el('reviewed').disabled=saving||!playable;el('start').disabled=requestBusy||saving;el('login').disabled=requestBusy||saving;el('input').disabled=saving;el('stop-save').hidden=!saving;}
  function display(value){
    row=value;const busy=['queued','resolving','downloading','waiting'].includes(row.phase);
    el('cancel').hidden=!busy;el('preview').hidden=!row.ready;
    if(row.ready){const item=row.items[0];el('title').textContent=item.title;el('meta').textContent=`${item.author} · ${Math.round(item.duration)} 秒 · ${(row.bytes/1048576).toFixed(1)} MB`;
      const source='/api/tools/douyin/'+row.id+'/media';if(el('video').getAttribute('src')!==source){playable=false;el('reviewed').checked=false;el('video').src=source;}
    }
    if(!saving){say(storage.get('douyin.saved.'+row.id)?'此前已在本浏览器完成保存。可以重新预览并另存。':row.message);el('progress').hidden=!busy||!row.loaded;if(row.total)el('progress').value=row.loaded/row.total*100;else el('progress').removeAttribute('value');}
    controls();return busy;
  }
  async function load(identifier){if(saving)return;active=identifier;const version=++generation;el('reviewed').checked=false;el('preview').hidden=true;row=null;playable=el('video').getAttribute('src')==='/api/tools/douyin/'+identifier+'/media'&&el('video').readyState>=2;controls();storage.set('douyin.active',identifier);
    try{const value=await api('/'+identifier);if(version!==generation)return;const busy=display(value);if(busy)setTimeout(()=>poll(identifier,version),1000);}catch(error){say(error.message);}
  }
  async function poll(identifier,version){if(version!==generation)return;try{const value=await api('/'+identifier);if(version!==generation)return;const busy=display(value);if(busy)setTimeout(()=>poll(identifier,version),1000);else history();}catch(error){say(error.message+'；请刷新页面重新核对任务。');}}
  async function history(){try{const data=await api('');el('history').replaceChildren();for(const item of data.items){const group=document.createElement('div'),button=document.createElement('button');group.className='module-actions';button.type='button';button.className='ui-button ui-secondary';button.textContent=(item.items[0]?.title||'视频准备')+' · '+item.message;button.addEventListener('click',()=>load(item.id));group.append(button);if(item.task_id){const link=document.createElement('a');link.className='ui-button ui-secondary';link.href='/tasks/'+encodeURIComponent(item.task_id);link.textContent='任务与日志';group.append(link);}el('history').append(group);}if(!data.items.length)el('history').textContent='暂无记录。';}catch(error){say(error.message);}}
  async function start(interactive){if(requestBusy||saving)return;requestBusy=true;controls();say('正在提交…');try{const value=await api('',{input:el('input').value,interactive});await load(value.id);history();}catch(error){say(error.message);}finally{requestBusy=false;controls();}}
  el('start').onclick=()=>start(false);el('login').onclick=()=>start(true);el('reviewed').onchange=controls;
  el('cancel').onclick=async()=>{if(!active)return;try{await api('/'+active,{action:'cancel'});say('已请求取消，正在等待任务退出…');}catch(error){say(error.message);}};
  el('stop-save').onclick=()=>controller?.abort();
  el('video').onloadedmetadata=()=>{el('video').currentTime=Math.min(.1,el('video').duration/2);};
  el('video').onloadeddata=()=>{if(row?.ready){playable=el('video').videoWidth>0;controls();}};
  el('video').onerror=()=>{playable=false;el('reviewed').checked=false;controls();if(row?.ready)say('视频预览失败，请重新解析或使用桌面版Chrome、Edge核对播放能力。');};
  el('video').onloadedmetadata=()=>{el('video').currentTime=Math.min(.1,el('video').duration/2);};
  el('save').onclick=async()=>{
    if(saving||!row?.ready||!el('reviewed').checked)return;
    if(!('showSaveFilePicker' in window)){say('当前浏览器无法选择保存位置，请使用桌面版Chrome或Edge打开本机工具页面。');return;}
    const current=row;let writable=null,reader=null;saving=true;controls();
    try{
      const handle=await window.showSaveFilePicker({id:'douyin-video',suggestedName:current.items[0].filename,types:[{description:'MP4视频',accept:{'video/mp4':['.mp4']}}]});
      controller=new AbortController();say('正在保存到所选位置…');writable=await handle.createWritable();
      const response=await fetch('/api/tools/douyin/'+current.id+'/media',{method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':token},body:JSON.stringify({reviewed:true}),signal:controller.signal});
      if(!response.ok){const data=await response.json();throw new Error(data.error||'保存失败');}
      reader=response.body.getReader();let loaded=0;el('progress').hidden=false;el('progress').value=0;
      while(true){const {done,value}=await reader.read();if(done)break;await writable.write(value);loaded+=value.length;el('progress').value=loaded/current.bytes*100;}
      if(loaded!==current.bytes)throw new Error('文件接收不完整，请重新保存');
      await writable.close();writable=null;storage.set('douyin.saved.'+current.id,new Date().toISOString());say('已保存：'+handle.name);
    }catch(error){controller?.abort();if(reader)try{await reader.cancel();}catch(_){}if(writable)try{await writable.abort();}catch(_){}say(error.name==='AbortError'?'已取消保存。视频仍可预览和重新保存。':error.message+'；可以重新选择位置保存。');}
    finally{saving=false;controller=null;el('progress').hidden=true;controls();}
  };
  history();const previous=storage.get('douyin.active');if(previous)load(previous);
})();
