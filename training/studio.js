
const $=id=>document.getElementById(id), csrf=document.querySelector('meta[name="training-token"]').content;
let snapshot={datasets:[],runs:[],deletions:[],active_run:null},selectedDataset=null,selectedRun=null,preset='demo',busy=false,deleteBusy=false,stopped=false,polling=false,toastTimer=null,dataSignature='',runSignature='';
const titles=[['准备资料','从第一份资料开始','先用体验资料走一遍，再放入自己的 C++ / Qt 内容。','STEP 01 / DATA'],['设置训练','给这次学习定个小目标','第一次保留默认值，先完成一个可暂停、可试用的训练。','STEP 02 / TRAIN'],['观察进度','看见模型一步步学习','这里显示真实训练结果，进度和模型会定期保存。','STEP 03 / PROGRESS'],['试用与导出','试试你亲手训练的模型','用真实输出判断效果，再决定补充资料还是继续学习。','STEP 04 / TRY']];
let importBusy=false,importIdentity=null,previewIdentity=null,previewKind='dataset',previewOffset=0,pendingContinueBase=null;
const modeNames={pretrain:'从零预训练',continue:'添加资料继续训练',sft:'问答训练'};
const statusNames={preparing:'准备中',running:'训练中',paused:'已暂停',stopped:'已停止',completed:'已完成',failed:'需要处理',interrupted:'上次已中断'};
function toast(message,error=false){$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,error?10000:4500)}
async function api(path,body,method){method=method||(body===undefined?'GET':'POST');const headers={'X-Training-Token':csrf},options={method,headers};if(body!==undefined){headers['Content-Type']='application/json';options.body=JSON.stringify(body)}const response=await fetch(path,options);let value;try{value=await response.json()}catch{throw Error('服务没有返回有效结果，请检查训练工作室是否仍在运行。')}if(!response.ok){const error=Error(typeof value.detail==='string'?value.detail:'操作未完成，请检查输入。');error.status=response.status;error.value=value;throw error}return value}
async function action(fn){try{await fn()}catch(e){toast(e.message,true)}}
const deletionTimers=new Map();
function showDeletionStatus(value){const element=$('deletionStatus');element.textContent=DeletionUI.statusText(value);element.hidden=!element.textContent}
function trackDeletion(value){showDeletionStatus(value);if(!DeletionUI.shouldPoll(value)){const old=deletionTimers.get(value?.deletion_id);if(old)clearTimeout(old);deletionTimers.delete(value?.deletion_id);return}if(deletionTimers.has(value.deletion_id))return;const timer=setTimeout(async()=>{deletionTimers.delete(value.deletion_id);try{trackDeletion(await api('/api/deletions/'+value.deletion_id))}catch{if(!stopped)trackDeletion(value)}},1400);deletionTimers.set(value.deletion_id,timer)}
function syncDeletionStates(){for(const value of snapshot.deletions||[])if(DeletionUI.shouldPoll(value))trackDeletion(value);const manual=(snapshot.deletions||[]).find(value=>value.cleanup_status==='manual_intervention_required');if(manual)showDeletionStatus(manual)}
function go(number){document.querySelectorAll('.panel').forEach((x,i)=>x.hidden=i!==number-1);document.querySelectorAll('.step').forEach((x,i)=>x.classList.toggle('active',i===number-1));const t=titles[number-1];$('crumb').textContent=t[0];$('title').textContent=t[1];$('subtitle').textContent=t[2];$('eyebrow').textContent=t[3];window.scrollTo({top:0,behavior:'smooth'});render()}
document.querySelectorAll('[data-step]').forEach(x=>x.onclick=()=>go(Number(x.dataset.step)));document.querySelectorAll('[data-goto]').forEach(x=>x.onclick=()=>go(Number(x.dataset.goto)));
function dataset(){return snapshot.datasets.find(d=>d.id===selectedDataset)}function run(){return snapshot.runs.find(r=>r.id===selectedRun)}
function escapeHtml(value){const el=document.createElement('span');el.textContent=value??'';return el.innerHTML}
function duration(seconds){if(!Number.isFinite(seconds))return '—';if(seconds<60)return Math.round(seconds)+'秒';if(seconds<3600)return Math.floor(seconds/60)+'分'+Math.round(seconds%60)+'秒';return Math.floor(seconds/3600)+'时'+Math.floor(seconds%3600/60)+'分'}
function selectedData(id){selectedDataset=id;dataSignature='';render();$('runName').value=dataset()?.kind==='qa'?'我的 C++ 问答模型':'我的 C++ 小模型'}
function selectRun(id){selectedRun=id;render()}
function renderPhases(r){
 const phases=[['prepare','整理资料'],['tokenize','准备模型'],['train','学习与验证'],['evaluate','最终检查'],['save','保存模型'],['complete','可试用']];
 const current=phases.findIndex(x=>x[0]===(r?.phase||(r?.status==='completed'?'complete':r?.status==='running'?'train':'prepare')));
 $('phaseFlow').innerHTML=phases.map(([id,label],index)=>`<span class="${r?(index<current?'done':index===current?'current':''):''}">${index+1} ${label}</span>`).join('');
 $('retryButton').hidden=!r||!['failed','stopped','interrupted'].includes(r.status)||!!snapshot.active_run;
 $('runtimeInfo').textContent=r?.tokens_per_second?`训练速度 ${r.tokens_per_second} token/秒 · PyTorch 峰值显存 ${r.gpu_memory_mib} MB${r.test_loss!==undefined?' · 测试损失 '+r.test_loss.toFixed(3):''}`:'';
 $('trainingEvents').innerHTML=(r?.events||[]).map(e=>`<div><span class="small">${escapeHtml(new Date(e.time).toLocaleTimeString())}</span> ${escapeHtml(e.message)}</div>`).join('')||'<span class="small">本次训练开始后显示过程记录。</span>';
 $('chooseFiles').disabled=importBusy;$('writeQaButton').disabled=importBusy;$('demoButton').disabled=importBusy;
}
function render(){
 const d=dataset(),r=run();$('datasetCount').textContent=snapshot.datasets.length?`${snapshot.datasets.length} 份资料`: '尚未导入';
 const signature=JSON.stringify(snapshot.datasets)+selectedDataset;if(signature!==dataSignature){dataSignature=signature;if(snapshot.datasets.length){$('datasets').innerHTML=snapshot.datasets.map(d=>`<div class="data-card ${d.id===selectedDataset?'selected':''}"><div><strong>${escapeHtml(d.name)}</strong><div class="small">${d.count} 段 · ${d.kind==='qa'?'问答资料':'普通文本'} · 已去重 ${d.duplicates_removed} 段 ${d.demo?' · 体验用途':''}</div></div><div class="row"><button data-preview="${d.id}">预览</button><button data-dataset="${d.id}">${d.id===selectedDataset?'已选择':'选择'}</button></div></div>`).join('');document.querySelectorAll('[data-dataset]').forEach(x=>x.onclick=()=>selectedData(x.dataset.dataset));document.querySelectorAll('[data-preview]').forEach(x=>x.onclick=()=>action(()=>openPreview(x.dataset.preview,'dataset',0)))}else $('datasets').innerHTML='<div class="empty">资料已清空。<br>选择自己的文件，即可从头训练。</div>'}
 $('toSettings').disabled=!d;$('selectedDataNote').textContent=d?`已选择：${d.name}`:'先选择一份资料';$('selectedDataBadge').textContent=d?`${d.count} 段${d.kind==='qa'?'问答':'文本'}`:'尚未选择资料';
 const qa=d?.kind==='qa',inherit=qa||$('trainingMode').value==='continue';$('trainingModeField').hidden=qa;$('pretrainOptions').hidden=inherit;$('qaNotice').hidden=!inherit;$('baseModelField').hidden=!inherit;$('advanced').hidden=inherit;$('settingsTitle').textContent=qa?'在已有模型上继续学问答':inherit?'在已有模型上继续学习新资料':'选择一个合适的起点';
 $('startButton').disabled=!d||!!snapshot.active_run||snapshot.inference_busy||busy||importBusy;
 $('initModelTitle').textContent=inherit?'继承已有模型':'从零建立模型';$('initModelHelp').textContent=inherit?'沿用你所选模型的权重、结构和分词器，原模型保留。':'从随机权重开始，不下载别人的预训练模型。';
 const ready=snapshot.runs.filter(x=>x.has_model);const options=ready.map(x=>`<option value="${x.id}">${escapeHtml(x.name)} · ${x.mode==='sft'?'问答':'基础'}</option>`).join('');
 const bases=ready.filter(x=>qa||x.mode!=='sft'),baseOptions=bases.map(x=>`<option value="${x.id}">${escapeHtml(x.name)} · ${x.mode==='sft'?'问答':'基础'}</option>`).join('');const oldBase=$('baseModel').value;if($('baseModel').innerHTML!==baseOptions){$('baseModel').innerHTML=baseOptions;$('baseModel').value=bases.some(x=>x.id===oldBase)?oldBase:bases[0]?.id||''}if(inherit&&!$('baseModel').value)$('startButton').disabled=true;
 const oldTry=$('tryModel').value;if($('tryModel').innerHTML!==options){$('tryModel').innerHTML=options;$('tryModel').value=ready.some(x=>x.id===oldTry)?oldTry:ready[0]?.id||''}
 if(r?.has_model&&document.activeElement!==$('tryModel'))$('tryModel').value=r.id;
 $('activeName').textContent=r?.name||'还没有训练记录';const lineage=!r?.base_run?'':r.base_run_deleted===true?` · 父模型“${r.base_run_snapshot?.name||r.base_run}”已删除，谱系已保留`:r.base_run_deleted===false?' · 父模型仍保留':' · 父模型谱系状态未知';$('runDescription').textContent=r?`${r.dataset_name} · ${modeNames[r.mode]||r.mode}${lineage}`:'完成前两步后，这里会出现真实进度。';
 $('runStatus').textContent=r?(r.pending_action?'正在保存…':statusNames[r.status]||r.status):'等待开始';$('runStatus').classList.toggle('failed',r?.status==='failed');
 const percentage=r?Math.min(100,100*r.step/r.total_steps):0;$('progressBar').style.width=percentage+'%';$('stepText').textContent=r?`${r.step} / ${r.total_steps} 步 · ${Math.round(percentage)}%`:'0 / 0 步';
 $('timeRemaining').textContent=r?.status==='running'&&r.step>2?'预计还需 '+duration(r.estimated_seconds_remaining):r?.status==='completed'?'本次训练已完成':'首次准备可能需要一些时间';
 $('trainLoss').textContent=r?.train_loss?.toFixed(3)??'—';$('validationLoss').textContent=r?.validation_loss?.toFixed(3)??'—';$('elapsedTime').textContent=duration(r?.elapsed_seconds);$('paramCount').textContent=r?.parameters?(r.parameters/1e6).toFixed(1)+'M':'—';
 const active=r&&snapshot.active_run===r.id;$('pauseButton').disabled=!active||!!r.pending_action;$('stopButton').disabled=!active||!!r.pending_action;$('resumeButton').hidden=!r?.can_resume||active;$('resumeButton').disabled=!!snapshot.active_run||snapshot.inference_busy;$('deleteRunButton').disabled=!r||active||snapshot.inference_busy||busy||deleteBusy;
 $('saveHint').textContent=r?.has_model?'已有保存的模型与训练进度':'每 25 步及结束时保存';$('runMessage').textContent=r?.pending_action?'正在保存模型和进度，请稍等。':r?.message||'不会自动开始训练，也不会自动切换当前项目的模型。';
 $('toTry').disabled=!r?.has_model||!!snapshot.active_run;$('dataUseNote').textContent=r?.token_counts?`学习资料 ${r.split_counts.train} 段 / 验证 ${r.split_counts.validation} 段 / 测试 ${r.split_counts.test} 段 · 约重复学习 ${r.repetitions??0} 次`:'资料会自动划分为学习、验证和最终测试三部分';
 drawChart(r?.metrics||[]);renderPhases(r);
 const historySignature=JSON.stringify(snapshot.runs.map(x=>[x.id,x.status,x.step]))+selectedRun;if(historySignature!==runSignature){runSignature=historySignature;if(snapshot.runs.length){$('runHistory').innerHTML=snapshot.runs.map(x=>`<div class="history-row"><div><b>${escapeHtml(x.name)}</b><small>${statusNames[x.status]||x.status} · ${x.step}/${x.total_steps} 步</small></div><button data-run="${x.id}" class="quiet">${x.id===selectedRun?'当前':'查看'}</button></div>`).join('');document.querySelectorAll('[data-run]').forEach(x=>x.onclick=()=>selectRun(x.dataset.run))}else $('runHistory').innerHTML='<div class="empty">还没有训练记录。<br>上传资料后可以从零开始。</div>'}
 const target=snapshot.runs.find(x=>x.id===$('tryModel').value);$('tryModeHint').textContent=target?.mode==='sft'?'这个模型学过问答，可以输入一个简短问题。':'基础模型会续写你的内容。先输入一小段代码或文字。';$('generateButton').disabled=!target||!!snapshot.active_run||snapshot.inference_busy||busy;$('exportButton').disabled=!target||snapshot.active_run===target.id;$('qaDemoButton').disabled=!target||!!snapshot.active_run;$('continueDataButton').disabled=!target||!!snapshot.active_run;$('continueDataButton').textContent=target?.mode==='sft'?'上传新问答，继续训练这个模型 →':'上传新资料，继续训练这个模型 →';$('connectionButton').disabled=!target;
}
function drawChart(metrics){const svg=$('lossChart');if(!metrics.length){svg.innerHTML='<text x="330" y="105" text-anchor="middle" fill="#8a968b" font-size="14">第一条训练结果出现后，曲线会显示在这里</text>';return}const values=metrics.flatMap(m=>[m.train,m.validation]).filter(Number.isFinite),min=Math.max(0,Math.min(...values)*.85),max=Math.max(...values)*1.08,span=Math.max(.1,max-min),last=Math.max(1,metrics.at(-1).step),X=n=>45+n/last*595,Y=n=>180-(n-min)/span*155;let contents='';for(let i=0;i<4;i++){let value=min+span*i/3,y=Y(value);contents+=`<line x1="45" x2="640" y1="${y}" y2="${y}" stroke="#e9ede6"/><text x="33" y="${y+4}" text-anchor="end" fill="#8b958b" font-size="11">${value.toFixed(1)}</text>`}for(const [key,color] of [['train','#285944'],['validation','#c78843']]){const points=metrics.filter(m=>Number.isFinite(m[key]));contents+=`<polyline fill="none" stroke="${color}" stroke-width="2.5" points="${points.map(m=>X(m.step)+','+Y(m[key])).join(' ')}"/>`;for(const m of points)contents+=`<circle cx="${X(m.step)}" cy="${Y(m[key])}" r="3" fill="${color}"/>`}contents+=`<text x="45" y="204" fill="#8b958b" font-size="11">0 步</text><text x="640" y="204" fill="#8b958b" font-size="11" text-anchor="end">${last} 步</text>`;svg.innerHTML=contents}
async function refresh(){if(stopped||polling)return;polling=true;try{snapshot=await api('/api/state');if(!snapshot.datasets.some(x=>x.id===selectedDataset))selectedDataset=snapshot.datasets[0]?.id||null;if(!snapshot.runs.some(x=>x.id===selectedRun))selectedRun=snapshot.runs[0]?.id||null;render();syncDeletionStates();$('connectionStatus').textContent='工作室已连接 · 本地运行'}catch(e){$('connectionStatus').textContent='连接中断，请确认工作室仍在运行'}finally{polling=false}}
async function importFiles(files){
 if(importBusy)throw Error('正在处理资料，请稍等。');if(!files.length)return;if(files.length>32)throw Error('一次最多 32 个文件。');let total=0;const contents=[];
 importBusy=true;render();try{
 for(const [index,file] of files.entries()){
  if(file.size>8*1024*1024)throw Error(`${file.name} 超过 8 MB。`);total+=file.size;if(total>20*1024*1024)throw Error('文件合计不能超过 20 MB，请分批导入。');
  showImportProgress(index/files.length*20,`读取文件 ${index+1}/${files.length}：${file.name}`);const bytes=new Uint8Array(await file.arrayBuffer());
  if(/\.(pdf|docx)$/i.test(file.name)){let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));contents.push({name:file.name,base64:btoa(binary)})}
  else{let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(bytes)}catch{throw Error(`${file.name} 不是 UTF-8 文本，请另存为 UTF-8。`)}contents.push({name:file.name,text})}
 }
 await importPayload({name:files.length===1?files[0].name:`我的资料（${files.length} 个文件）`,files:contents});
 }catch(e){showImportProgress(0,e.message);throw e}finally{importBusy=false;render()}
}
function showImportProgress(percent,message){$('importStatus').hidden=false;$('importProgress').value=percent;$('importMessage').textContent=message}
async function importPayload(value){
 importBusy=true;render();showImportProgress(0,'正在将资料传入本机工作室…');
 try{
  const job=await new Promise((resolve,reject)=>{
   const xhr=new XMLHttpRequest();xhr.open('POST','/api/imports');xhr.setRequestHeader('Content-Type','application/json');xhr.setRequestHeader('X-Training-Token',csrf);xhr.timeout=120000;
   xhr.upload.onprogress=e=>{if(e.lengthComputable)showImportProgress(e.loaded/e.total*100,'正在传入本机：'+Math.round(e.loaded/e.total*100)+'%')};
   xhr.onerror=()=>reject(Error('传入失败，请检查工作室是否运行。'));xhr.ontimeout=()=>reject(Error('传入超时，请缩小文件后再试。'));
   xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);if(xhr.status>=400)reject(Error(data.detail||'资料处理失败。'));else resolve(data)}catch{reject(Error('工作室未返回有效结果。'))}};xhr.send(JSON.stringify(value));
  });importIdentity=job.id;
  for(;;){const status=await api('/api/imports/'+job.id);showImportProgress(status.progress,status.message);
   if(status.status==='failed')throw Error(status.message);
   if(status.status==='ready'){await openPreview(job.id,'import',0,status.preview);return}
   await new Promise(resolve=>setTimeout(resolve,700));
  }
 }catch(e){showImportProgress(0,e.message);throw e}finally{importBusy=false;render()}
}
async function openPreview(id,kind,offset=0,ready=null){
 const value=ready||(kind==='import'?(await api(`/api/imports/${id}?offset=${offset}`)).preview:await api(`/api/datasets/${id}?offset=${offset}`));
 if(!value)throw Error('资料仍在处理。');previewIdentity=id;previewKind=kind;previewOffset=value.offset;
 $('previewTitle').textContent=value.name||'资料预览';$('previewSummary').textContent=`${value.count} 段 · ${value.characters.toLocaleString()} 字符 · 已去重 ${value.duplicates_removed} 段；学习 ${value.split_counts.train} / 验证 ${value.split_counts.validation} / 测试 ${value.split_counts.test}。`;
 $('previewFiles').textContent=(value.files||[]).map(f=>`${f.name}（${f.segments} 段）`).join('；');
 $('previewNotes').textContent=(value.notes||[]).join('\n');
 $('previewSamples').innerHTML=value.samples.map((d,i)=>`<div><b class="small">第 ${value.offset+i+1} 段 · ${escapeHtml(d.source)}</b><pre class="preview-sample">${escapeHtml(d.text??('问题：'+d.question+'\n参考答案：'+d.answer))}</pre></div>`).join('');
 $('previewPage').textContent=`${value.offset+1}–${Math.min(value.count,value.offset+5)} / ${value.count} 段`;$('previewPrevious').disabled=value.offset===0;$('previewNext').disabled=value.offset+5>=value.count;
 $('saveImport').textContent=kind==='import'?'确认资料，设置训练 →':'使用这份资料训练 →';if(!$('previewDialog').open)$('previewDialog').showModal();
}
$('closePreview').onclick=()=>$('previewDialog').close();
$('previewPrevious').onclick=()=>action(()=>openPreview(previewIdentity,previewKind,Math.max(0,previewOffset-5)));
$('previewNext').onclick=()=>action(()=>openPreview(previewIdentity,previewKind,previewOffset+5));
$('saveImport').onclick=()=>action(async()=>{
 $('saveImport').disabled=true;try{if(previewKind==='import')await api(`/api/imports/${previewIdentity}/save`,{});selectedDataset=previewIdentity;await refresh();selectedData(previewIdentity);
  if(pendingContinueBase){$('trainingMode').value='continue';render();$('baseModel').value=pendingContinueBase;pendingContinueBase=null;$('runName').value='我的模型 · 新资料版本'}
  $('previewDialog').close();$('importStatus').hidden=true;go(2);
 }finally{$('saveImport').disabled=false}
});
$('trainingMode').onchange=()=>render();
$('continueDataButton').onclick=()=>{pendingContinueBase=$('tryModel').value;$('trainingMode').value='continue';go(1);toast('上传新资料或预览一份已有资料，确认后将继承刚才选择的模型。')};
$('retryButton').onclick=()=>{const r=run();if(!r)return;selectedData(r.dataset_id);$('trainingMode').value=r.mode==='continue'?'continue':'fresh';render();if(r.base_run)$('baseModel').value=r.base_run;$('stepsInput').value=r.total_steps;$('runName').value=r.name+'（重试）';go(2)};
function addQaRow(){
 const row=document.createElement('div');row.className='qa-row';row.innerHTML='<label class="field">问题<textarea data-question placeholder="例如：怎样把整数转换为 QString？"></textarea></label><label class="field">参考答案<textarea data-answer placeholder="写下希望模型学会的正确回答和代码。"></textarea></label><button class="quiet">移除这一条</button>';
 row.querySelector('button').onclick=()=>row.remove();$('qaRows').append(row);
}
$('writeQaButton').onclick=()=>{if(!$('qaRows').children.length)for(let i=0;i<3;i++)addQaRow();$('qaEditor').showModal()};
$('closeQaEditor').onclick=()=>$('qaEditor').close();$('addQaRow').onclick=addQaRow;
$('saveQaRows').onclick=()=>action(async()=>{
 const pairs=[...$('qaRows').children].map(row=>({question:row.querySelector('[data-question]').value.trim(),answer:row.querySelector('[data-answer]').value.trim()}));
 if(pairs.length<3||pairs.some(p=>!p.question||!p.answer))throw Error('请至少填好 3 条不同的问题和参考答案。');
 $('qaEditor').close();await importPayload({name:$('qaName').value||'我的问答',files:[{name:'我的问答.jsonl',text:pairs.map(p=>JSON.stringify(p)).join('\n')}]});
});
$('chooseFiles').onclick=()=>$('fileInput').click();$('fileInput').onchange=()=>action(async()=>{await importFiles([...$('fileInput').files]);$('fileInput').value=''});
for(const type of ['dragover','dragenter'])$('dropZone').addEventListener(type,e=>{e.preventDefault();$('dropZone').classList.add('dragging')});$('dropZone').addEventListener('dragleave',()=>$('dropZone').classList.remove('dragging'));$('dropZone').addEventListener('drop',e=>{e.preventDefault();$('dropZone').classList.remove('dragging');action(()=>importFiles([...e.dataTransfer.files]))});
async function loadDemo(kind){const data=await api('/api/demo',{kind});selectedDataset=data.id;await refresh();toast(kind==='qa'?'体验问答已准备好，选择基础模型即可继续。':'体验资料已准备好。下一步保留推荐设置即可。')}
$('demoButton').onclick=()=>action(()=>loadDemo('text'));$('toSettings').onclick=()=>go(2);
document.querySelectorAll('[data-preset]').forEach(x=>x.onclick=()=>{preset=x.dataset.preset;document.querySelectorAll('[data-preset]').forEach(b=>b.classList.toggle('selected',b===x));const demo=preset==='demo';$('stepsInput').value=demo?40:1000;$('layersInput').value=demo?4:8;$('widthInput').value=demo?128:384;$('contextInput').value=demo?128:512});
$('startButton').onclick=()=>action(async()=>{busy=true;render();try{const data=await api('/api/runs',{dataset_id:selectedDataset,name:$('runName').value,training_mode:$('trainingMode').value,preset,steps:Number($('stepsInput').value),learning_rate:Number($('lrInput').value),layers:Number($('layersInput').value),width:Number($('widthInput').value),context:Number($('contextInput').value),gentle:$('gentle').checked,base_run:$('baseModel').value||null});selectedRun=data.id;await refresh();go(3)}finally{busy=false;render()}});
for(const [id,operation] of [['pauseButton','pause'],['stopButton','stop'],['resumeButton','resume']])$(id).onclick=()=>action(async()=>{await api(`/api/runs/${selectedRun}/${operation}`,{});await refresh()});
$('deleteRunButton').onclick=()=>{const target=run();if(!target||deleteBusy)return;const shared=snapshot.runs.some(x=>x.id!==target.id&&x.dataset_id===target.dataset_id),dependents=snapshot.runs.filter(x=>x.base_run===target.id).length;$('deleteRunSummary').textContent=`即将删除“${target.name}”。${shared?'共享资料会保留。':'没有其他模型使用的资料也会删除。'}${dependents?` ${dependents} 个派生模型有独立权重，会继续保留并记录谱系。`:''}`;$('deleteRunDialog').showModal()};
$('cancelDeleteRun').onclick=()=>$('deleteRunDialog').close();
$('confirmDeleteRun').onclick=()=>action(async()=>{if(deleteBusy)return;const target=run();if(!target){$('deleteRunDialog').close();return}const previousIndex=Math.max(0,snapshot.runs.findIndex(x=>x.id===target.id));deleteBusy=true;busy=true;$('confirmDeleteRun').disabled=true;$('cancelDeleteRun').disabled=true;$('confirmDeleteRun').textContent='正在安全删除…';render();try{let result;try{result=await api(`/api/runs/${target.id}`,undefined,'DELETE')}catch(error){await refresh();result=DeletionUI.recoverForRun(snapshot.deletions,target.id);if(!result||result.logical_status!=='deleted')throw error}$('deleteRunDialog').close();trackDeletion(result);if(result.deleted_dataset&&selectedDataset===target.dataset_id)selectedDataset=null;await refresh();selectedRun=DeletionUI.nextRunId(snapshot.runs,target.id,previousIndex);render();toast(DeletionUI.statusText(result),result.cleanup_status==='manual_intervention_required')}finally{deleteBusy=false;busy=false;$('confirmDeleteRun').disabled=false;$('cancelDeleteRun').disabled=false;$('confirmDeleteRun').textContent='确认删除';render()}});
$('toTry').onclick=()=>go(4);$('tryModel').onchange=()=>{selectedRun=$('tryModel').value;render();$('connectionInfo').hidden=true;$('generatedText').textContent='输入内容，查看这个模型的真实输出。';$('generatedText').classList.add('placeholder')};
$('generateButton').onclick=()=>action(async()=>{const prompt=$('promptInput').value.trim();if(!prompt)throw Error('先输入一小段内容。');busy=true;render();$('generatedText').textContent='正在加载你训练的模型并生成内容…';$('generatedText').classList.add('placeholder');try{const result=await api('/api/generate/'+$('tryModel').value,{messages:[{role:'user',content:prompt}],max_tokens:80});$('generatedText').textContent=result.text||'模型立即结束了输出。训练资料或训练量可能还不够，可以继续整理资料后再实验。';$('generatedText').classList.remove('placeholder')}catch(e){$('generatedText').textContent=e.message;throw e}finally{busy=false;await refresh();render()}});
$('exportButton').onclick=()=>{if($('tryModel').value)window.location.href='/api/export/'+$('tryModel').value};
$('connectionButton').onclick=()=>action(async()=>{const value=await api('/api/connection/'+$('tryModel').value);const box=$('connectionInfo');box.hidden=false;box.replaceChildren();for(const text of [`Base URL：${value.base_url}`,`模型名：${value.model}`,value.capabilities]){const line=document.createElement('div');line.textContent=text;box.append(line)}const button=document.createElement('button');button.textContent='复制本地接口密钥';button.style.marginTop='10px';button.onclick=()=>action(async()=>{await navigator.clipboard.writeText(value.api_key);toast('本地接口密钥已复制。')});box.append(button)});
$('qaDemoButton').onclick=()=>action(async()=>{const base=$('tryModel').value;await loadDemo('qa');$('baseModel').value=base;$('runName').value='我的 C++ 问答模型';$('stepsInput').value=40;go(2)});
function template(){const line=JSON.stringify({question:'Qt 的信号和槽是什么？',answer:'信号用于通知事件发生，槽用于接收并处理事件。'})+'\n';const url=URL.createObjectURL(new Blob([line],{type:'application/jsonl;charset=utf-8'})),anchor=document.createElement('a');anchor.href=url;anchor.download='问答格式示例.jsonl';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('这是格式示例，训练前请补充至少 3 条不同问答。')}
$('downloadTemplate').onclick=template;$('qaTemplateButton').onclick=template;for(const id of ['guideTop','guideSide'])$(id).onclick=()=>$('guide').showModal();$('closeGuide').onclick=()=>$('guide').close();
$('exitButton').onclick=()=>action(async()=>{await api('/api/shutdown',{});stopped=true;$('connectionStatus').textContent='工作室已关闭';toast('正在保存并退出。关闭这个网页即可，原项目配置不变。');$('exitButton').disabled=true;$('startButton').disabled=true;$('generateButton').disabled=true});
action(async()=>{const env=await api('/api/environment');$('deviceName').textContent=env.gpu.replace('NVIDIA GeForce ','');$('deviceDetail').textContent=env.verified?'已通过 GPU 环境检查 · BF16 '+(env.bf16?'可用':'未启用'):'请先运行环境检查'});refresh();setInterval(refresh,1800);
