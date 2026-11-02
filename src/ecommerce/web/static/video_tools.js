/* 解析与素材准备分开；所选版本预览成功并确认后才允许保存视频。 */
(() => {
  if(!document.querySelector('[data-douyin]'))return;
  const el=id=>document.getElementById('dv-'+id);
  const token=document.querySelector('meta[name="workbench-token"]')?.content||'';
  const storage={get:key=>{try{return localStorage.getItem(key);}catch(_){return null;}},set:(key,value)=>{try{localStorage.setItem(key,value);}catch(_){}}};
  let row=null,active=null,generation=0,requestBusy=false,saving=false,playable=false,controller=null;
  const busy=()=>row&&['queued','resolving','downloading','waiting'].includes(row.phase);
  const selected=()=>el('format').value;
  const videoKey=()=> 'video_'+selected();
  const coverKey=()=>el('cover-format').value==='original'?'cover':'cover_'+el('cover-format').value;
  const resource=key=>row?.resources?.[key];
  const ready=key=>!!resource(key)?.ready;
  const url=(key,record=row)=>'/api/tools/douyin/'+record.id+(record.legacy?'/media':'/assets/'+encodeURIComponent(key));
  const say=(message,error=false)=>{el('status').textContent=message;el('status').classList.toggle('is-error',error);};
  const size=value=>value===null||value===undefined?'大小未知':value>=1048576?(value/1048576).toFixed(1)+' MB':Math.round(value/1024)+' KB';
  const date=value=>value?new Date(typeof value==='number'?value*1000:value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
  const duration=value=>!Number.isFinite(value)?'—':value>=3600?[Math.floor(value/3600),Math.floor(value%3600/60).toString().padStart(2,'0'),Math.floor(value%60).toString().padStart(2,'0')].join(':'):[Math.floor(value/60),Math.floor(value%60).toString().padStart(2,'0')].join(':');
  const count=value=>value===null||value===undefined?'—':new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(value);
  const label=format=>format.id==='legacy'?'原缓存版本':`${format.width&&format.height?format.width+'×'+format.height:'分辨率未知'} · ${format.codec} · ${format.bitrate?Math.round(format.bitrate/1000)+' kbps':'码率未知'} · ${format.bytes?'约'+size(format.bytes):'大小未知'}`;

  async function api(path,body){
    const response=await fetch('/api/tools/douyin'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Collection-Token':token}:{},body:body?JSON.stringify(body):undefined});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败，请重试');return data;
  }
  function normalize(value){
    if(!value.legacy)return value;
    const item=value.items[0];
    return {...value,metadata_ready:!!item,can_prepare:false,formats:item?[{id:'legacy',codec:'原版本',bytes:value.bytes}]:[],
      resources:item?{'video_legacy':{kind:'video',ready:value.ready,bytes:value.bytes,filename:item.filename,mime:'video/mp4'}}:{},
      items:item?[{...item,url:'https://www.douyin.com/video/'+item.video_id,stats:{},music:{available:false}}]:[],
      message:value.ready?'旧版缓存仍可预览保存；选择其他素材请重新解析。':value.message};
  }
  function controls(){
    const locked=saving||requestBusy||busy(),canPrepare=!!row?.can_prepare;
    for(const id of ['start','login','input','format','cover-format','cleanup'])el(id).disabled=!!locked;
    el('cancel').hidden=!busy();el('cancel').disabled=saving;
    el('prepare-video').disabled=!!locked||(!canPrepare&&!ready(videoKey()));
    el('prepare-video').textContent=ready(videoKey())?'预览视频':'下载视频';
    el('music').disabled=!!locked||(!canPrepare&&!ready('music'))||(!row?.items[0]?.music?.available&&!ready('music'));
    el('cover-open').disabled=!!locked||(!canPrepare&&!ready('cover'))||(!row?.items[0]?.cover_available&&!ready('cover'));
    el('audio').disabled=!!locked||!ready(videoKey())||(!canPrepare&&!ready('audio_'+selected()));
    el('audio').textContent=ready('audio_'+selected())?'试听完整声音':'提取完整声音';
    el('reviewed').disabled=saving||!playable;
    el('save').disabled=saving||!ready(videoKey())||!playable||!el('reviewed').checked;
    el('save-music').disabled=saving||!ready('music');el('save-audio').disabled=saving||!ready('audio_'+selected());
    el('save-cover').disabled=!!locked||(!canPrepare&&!ready(coverKey()));
    el('save-cover').textContent=ready(coverKey())?'保存封面':'准备所选格式';
    el('stop-save').hidden=!saving;
  }
  function image(id,key,record=row){
    const element=el(id),available=record?.resources?.[key]?.ready;
    element.hidden=!available;
    if(available&&element.getAttribute('src')!==url(key,record))element.src=url(key,record);
  }
  function resetPreview(){playable=false;el('reviewed').checked=false;el('video').pause();}
  function titleControl(){el('title-toggle').hidden=!el('title').classList.contains('is-expanded')&&el('title').scrollHeight<=el('title').clientHeight+1;}
  function updateVideo(){
    const key=videoKey(),available=ready(key),source=available?url(key):null;
    el('preview').hidden=!row?.metadata_ready;el('video').hidden=!available;
    if(available&&el('video').getAttribute('src')!==source){resetPreview();el('video').src=source;}
    if(!available&&el('video').getAttribute('src')){resetPreview();el('video').removeAttribute('src');el('video').load();}
    const format=row?.formats.find(f=>f.id===selected());
    el('preview-format').textContent=(format?label(format):'')+(available?' · 已准备，待预览确认':' · 尚未准备，请点击“下载视频”');
    for(const [id,asset] of [['music-player','music'],['audio-player','audio_'+selected()]]){
      el(id).hidden=!ready(asset);if(ready(asset)&&el(id).getAttribute('src')!==url(asset))el(id).src=url(asset);
      if(!ready(asset)){el(id).pause();el(id).removeAttribute('src');}
    }
  }
  function savedRecords(){
    if(!row)return;let records=[];
    try{const stored=JSON.parse(storage.get('douyin.saves.'+row.id)||'[]');if(Array.isArray(stored))records=stored.filter(item=>typeof item.filename==='string'&&typeof item.time==='string').map(item=>item.filename+'（'+date(item.time)+'）');}catch(_){}
    for(const key of Object.keys(row.resources||{})){
      try{const record=JSON.parse(storage.get('douyin.saved.v2.'+row.id+'.'+key)||'null');if(record){const text=record.filename+'（'+date(record.time)+'）';if(!records.includes(text))records.push(text);}}catch(_){}
    }
    if(row.legacy&&!records.length){const previous=storage.get('douyin.saved.'+row.id);if(previous)records.push((row.items[0]?.filename||'视频')+'（'+date(previous)+'）');}
    el('saved').textContent=records.length?'本浏览器曾完成保存：'+records.join('；'):'';
  }
  function display(value){
    row=normalize(value);const item=row.items[0];
    el('result').hidden=!row.metadata_ready;
    if(row.metadata_ready&&item){
      el('title').textContent=item.title;titleControl();el('author').textContent=item.author;
      const stats=item.stats||{};el('stats').textContent=`时长 ${duration(item.duration)} · 赞 ${count(stats.likes)} · 评论 ${count(stats.comments)} · 收藏 ${count(stats.favorites)} · 分享 ${count(stats.shares)}`;
      el('meta').textContent='互动数量为解析时快照，默认选择兼容性优先的可用版本。';
      el('times').textContent='发布时间：'+date(item.published)+'；解析时间：'+date(item.parsed_at);
      el('id').textContent='视频编号：'+item.video_id;el('notes').textContent=(row.notes||[]).join('；');
      const signature=row.formats.map(f=>f.id).join(',');
      if(el('format').dataset.signature!==signature){
        const old=el('format').value||storage.get('douyin.format.'+row.id);
        el('format').replaceChildren(...row.formats.map(format=>{const option=document.createElement('option');option.value=format.id;option.textContent=label(format);return option;}));
        if(row.formats.some(f=>f.id===old))el('format').value=old;
        el('format').dataset.signature=signature;resetPreview();
      }
      image('cover','cover');image('avatar','avatar');image('cover-large','cover');el('cover-empty').hidden=ready('cover');
      el('music-title').textContent=(item.music?.title||'背景音乐')+' · '+duration(item.music?.duration);
      updateVideo();savedRecords();
    }else{el('preview').hidden=true;el('extras').hidden=true;}
    if(!saving){say(row.message,row.phase==='failed');el('progress').hidden=!busy()||!row.loaded;if(row.total)el('progress').value=row.loaded/row.total*100;else el('progress').removeAttribute('value');}
    controls();return busy();
  }
  async function load(identifier){
    if(saving)return;const version=++generation;active=identifier;row=null;resetPreview();el('format').dataset.signature='';el('result').hidden=true;el('preview').hidden=true;el('extras').hidden=true;el('title').classList.remove('is-expanded');el('title-toggle').textContent='展开标题';el('title-toggle').setAttribute('aria-expanded','false');
    for(const id of ['music-panel','cover-panel','audio-panel'])el(id).hidden=true;
    storage.set('douyin.active',identifier);controls();
    try{const value=await api('/'+identifier);if(version!==generation)return;el('input').value=value.input||'';const waiting=display(value);if(waiting)setTimeout(()=>poll(identifier,version),900);}catch(error){say(error.message,true);}
  }
  async function poll(identifier,version){
    if(version!==generation)return;
    try{const value=await api('/'+identifier);if(version!==generation)return;const waiting=display(value);if(waiting)setTimeout(()=>poll(identifier,version),900);else history();}catch(error){say(error.message+'；刷新后可重新核对任务。',true);}
  }
  async function history(){
    try{
      const data=await api('');el('cache').textContent='缓存占用 '+size(data.cache_bytes);el('history').replaceChildren();
      for(const record of data.items){
        const wrapper=document.createElement('div');wrapper.className='dv-history-row';const thumbnail=document.createElement('div');
        if(record.resources?.cover?.ready){const img=document.createElement('img');img.className='ui-tool-thumbnail';img.alt='历史视频封面';img.src=url('cover',record);thumbnail.append(img);}
        const content=document.createElement('div'),title=document.createElement('strong'),note=document.createElement('p'),actions=document.createElement('div');
        title.textContent=record.items[0]?.title||'正在解析视频';note.className='ui-form-note';note.textContent=date(record.created)+' · '+record.message;actions.className='module-actions';
        const open=document.createElement('button');open.type='button';open.className='ui-button ui-secondary';open.textContent='查看记录';open.onclick=()=>load(record.id);actions.append(open);
        if(record.task_id){const link=document.createElement('a');link.href='/tasks/'+encodeURIComponent(record.task_id);link.className='ui-button ui-secondary';link.textContent='任务与日志';actions.append(link);}
        const remove=document.createElement('button');remove.type='button';remove.className='ui-button ui-secondary';remove.textContent='删除缓存';remove.disabled=!!saving||['queued','resolving','downloading'].includes(record.phase);
        remove.onclick=async()=>{if(saving)return;try{await api('/'+record.id,{action:'delete_cache'});if(active===record.id)await load(record.id);history();}catch(error){say(error.message,true);}};actions.append(remove);
        content.append(title,note,actions);wrapper.append(thumbnail,content);el('history').append(wrapper);
      }
      if(!data.items.length)el('history').textContent='暂无解析记录。';
    }catch(error){say(error.message,true);}
  }
  async function start(interactive){
    if(requestBusy||saving||busy())return;requestBusy=true;controls();say('正在提交解析任务…');
    try{const result=await api('',{input:el('input').value,interactive});await load(result.id);history();}catch(error){say(error.message,true);}finally{requestBusy=false;controls();}
  }
  async function prepare(kind,output_format='original'){
    if(!row||requestBusy||saving||busy())return;requestBusy=true;controls();
    try{
      const body={action:'prepare',kind,output_format};if(['video','audio'].includes(kind))body.format_id=selected();
      await api('/'+active,body);const version=++generation;const waiting=display(await api('/'+active));if(waiting)setTimeout(()=>poll(active,version),700);else history();
    }catch(error){say(error.message,true);}finally{requestBusy=false;controls();}
  }
  function panel(kind){el('extras').hidden=false;el(kind+'-panel').hidden=false;}
  async function save(key){
    const asset=resource(key);if(saving||!asset?.ready)return;
    if(asset.kind==='video'&&(!playable||!el('reviewed').checked||key!==videoKey()))return;
    if(!('showSaveFilePicker' in window)){say('请选择桌面版Chrome或Edge打开本机页面，以便自行选择保存位置。',true);return;}
    const record=row;let writable=null,reader=null;saving=true;controls();
    try{
      const suffix='.'+asset.filename.split('.').pop();
      const handle=await window.showSaveFilePicker({id:'douyin-'+asset.kind,suggestedName:asset.filename,types:[{description:'素材文件',accept:{[asset.mime]:[suffix]}}]});
      controller=new AbortController();writable=await handle.createWritable();say('正在保存到所选位置…');
      const response=await fetch(url(key,record),{method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':token},body:JSON.stringify(asset.kind==='video'?{reviewed:true}:{save:true}),signal:controller.signal});
      if(!response.ok){const data=await response.json();throw new Error(data.error||'保存失败');}
      reader=response.body.getReader();let received=0;el('progress').hidden=false;el('progress').value=0;
      while(true){const {done,value}=await reader.read();if(done)break;await writable.write(value);received+=value.length;el('progress').value=received/asset.bytes*100;}
      if(received!==asset.bytes)throw new Error('文件接收不完整，请重新保存');
      if(controller.signal.aborted)throw new DOMException('已取消保存','AbortError');
      await writable.close();writable=null;const saved={key,filename:handle.name,time:new Date().toISOString()};storage.set('douyin.saved.v2.'+record.id+'.'+key,JSON.stringify(saved));
      let index=[];try{const stored=JSON.parse(storage.get('douyin.saves.'+record.id)||'[]');if(Array.isArray(stored))index=stored.filter(item=>item.key!==key);}catch(_){}
      index.push(saved);storage.set('douyin.saves.'+record.id,JSON.stringify(index));say('已保存：'+handle.name);savedRecords();
    }catch(error){controller?.abort();if(reader)try{await reader.cancel();}catch(_){}if(writable)try{await writable.abort();}catch(_){}say(error.name==='AbortError'?'已取消保存，可复用缓存重新保存。':error.message+'；可以重新选择位置保存。',error.name!=='AbortError');}
    finally{saving=false;controller=null;el('progress').hidden=true;controls();}
  }
  el('start').onclick=()=>start(false);el('login').onclick=()=>start(true);
  el('format').onchange=()=>{resetPreview();storage.set('douyin.format.'+row.id,selected());updateVideo();controls();};
  el('reviewed').onchange=controls;el('cover-format').onchange=controls;
  el('prepare-video').onclick=()=>{el('preview').hidden=false;el('preview').open=true;if(!ready(videoKey()))prepare('video');};
  el('thumbnail').onclick=()=>{el('preview').open=true;el('preview').scrollIntoView({behavior:'smooth',block:'start'});};
  el('music').onclick=()=>{panel('music');if(!ready('music'))prepare('music');};
  el('cover-open').onclick=()=>{panel('cover');if(!ready('cover'))prepare('cover');};
  el('audio').onclick=()=>{panel('audio');if(!ready('audio_'+selected()))prepare('audio');};
  el('save').onclick=()=>save(videoKey());el('save-music').onclick=()=>save('music');el('save-audio').onclick=()=>save('audio_'+selected());
  el('save-cover').onclick=()=>{if(ready(coverKey()))save(coverKey());else prepare('cover',el('cover-format').value);};
  el('stop-save').onclick=()=>controller?.abort();
  el('cancel').onclick=async()=>{try{await api('/'+active,{action:'cancel'});say('已请求取消，正在等待任务退出…');}catch(error){say(error.message,true);}};
  el('cleanup').onclick=async()=>{try{await api('/cleanup',{action:'cleanup'});await history();if(active)display(await api('/'+active));say('过期缓存已清理。');}catch(error){say(error.message,true);}};
  for(const [id,field] of [['copy-url','url'],['copy-id','video_id']])el(id).onclick=async()=>{try{await navigator.clipboard.writeText(row.items[0][field]);say('已复制。');}catch(_){say('复制失败，请从解析信息中手动复制。',true);}};
  el('title-toggle').onclick=()=>{const expanded=el('title').classList.toggle('is-expanded');el('title-toggle').textContent=expanded?'收起标题':'展开标题';el('title-toggle').setAttribute('aria-expanded',String(expanded));titleControl();};
  if('ResizeObserver' in window)new ResizeObserver(titleControl).observe(el('title'));else window.addEventListener('resize',titleControl);
  el('video').onloadedmetadata=()=>{el('video').currentTime=Math.min(.1,el('video').duration/2);};
  el('video').onloadeddata=()=>{playable=ready(videoKey())&&el('video').getAttribute('src')===url(videoKey())&&el('video').videoWidth>0;controls();};
  el('video').onerror=()=>{playable=false;el('reviewed').checked=false;controls();if(ready(videoKey()))say('所选版本无法在当前浏览器预览，请选择H.264版本或更换浏览器后核对。',true);};
  for(const id of ['video','music-player','audio-player'])el(id).onplay=()=>{for(const other of ['video','music-player','audio-player'])if(other!==id)el(other).pause();};
  for(const id of ['cover','avatar','cover-large'])el(id).onerror=()=>{el(id).hidden=true;if(id==='cover')el('cover-empty').hidden=false;};
  controls();history();const previous=storage.get('douyin.active');if(previous)load(previous);
})();
