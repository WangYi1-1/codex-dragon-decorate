'use strict';
const $ = id => document.getElementById(id);
const app = { thread:null, turn:null, threads:[], nextCursor:null, threadQuery:'', threadListRequest:0, threadsLoading:false, historyRequest:0, historyCursor:null, historyLoading:false, earlierLoading:false, models:[], attachments:[], items:new Map(), resumed:new Set(), writerConflict:null, requests:[], connected:false, connectionEpoch:0, reconnecting:false, busy:false, loading:false, petReady:false, petState:null, petBaseState:'idle',petHover:false,petPointerInside:false,sending:false,pendingMessage:null,lastEvent:0,finishedTurns:new Set() };
let prefs = {};
try { prefs = JSON.parse(localStorage.getItem('dragon-atelier-settings') || '{}'); } catch {}
prefs = {cwd:'',dark:false,wallpaper:true,opacity:40,pet:true,...prefs};
function applyBridgeWorkspace(snapshot) {
  if(snapshot.appId!==clientAppId||typeof snapshot.defaultCwd!=='string'||!(/^[A-Za-z]:[\\/]/.test(snapshot.defaultCwd)||snapshot.defaultCwd.startsWith('/')))return;
  if(!prefs.cwd||prefs.cwd==='D:\\codexwork\\c') {
    prefs.cwd=snapshot.defaultCwd;
    try {localStorage.setItem('dragon-atelier-settings',JSON.stringify(prefs));} catch {}
    showWorkspace();
  }
}
function el(tag,text,cls) { const node=document.createElement(tag); if(text!=null) node.textContent=text; if(cls)node.className=cls; return node; }
function setCaption(text,error=false) { $('composer-caption').textContent=text; $('composer-caption').classList.toggle('error',error); }
function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>$('toast').hidden=true,4200); }
const writerConflictMessage = '这个会话正在由另一个 Codex 窗口使用。可在原窗口继续，或另开新会话；当前输入已保留。';
const clientAppId = 'codex-dragon-client';
const disconnectedMessage = 'Codex 服务已断开，请点击「重新连接」。当前输入已保留。';
const unavailableMessage = '龙娘工坊后台未运行。请打开桌面的「Codex 龙娘工坊」，然后点击「重新连接」。当前输入已保留。';
function isWriterConflict(error) { return error?.kind==='thread_in_use'||error?.code==='THREAD_ACTIVE_WRITER'||error?.data?.reason==='active_writer'||error?.data?.kind==='active_writer'||/already has an active writer/i.test(error?.message||''); }
function errorText(error,fallback='这次操作没有完成。') { return isWriterConflict(error)?writerConflictMessage:error?.message||fallback; }
function showWriterConflict(id=app.thread?.id) {
  if(!id||id!==app.thread?.id)return;
  app.writerConflict=id;app.resumed.delete(id);busy(false);
  $('task-status').textContent='此会话在另一个窗口中使用';setCaption(writerConflictMessage,true);
  for(const [label,action] of [['另开新会话',newChat],['重试连接',retryThreadWriter]]) {
    const button=el('button',label,'writer-conflict-action');button.type='button';button.addEventListener('click',action);$('composer-caption').append(button);
  }
}
async function retryThreadWriter() {
  if(app.loading||!app.thread)return;
  if(!app.connected){toast('Codex 还没有连接，请稍后再试。');return;}
  app.loading=true;const id=app.thread.id,epoch=app.connectionEpoch;
  try {const {result,active}=await resumeForSending(id,epoch);app.writerConflict=null;if(!$('model').value&&!$('effort').value)selectNativeModel(result);busy(!!active,active?.id);setCaption(active?'龙娘正在和你一起完成…':'已连接此会话。当前输入尚未发送，请按发送继续。');}
  catch(error){if(isWriterConflict(error))showWriterConflict(id);else{setCaption(errorText(error),true);toast(errorText(error));}}
  finally{app.loading=false;}
}
function showWorkspace() {const cwd=app.thread?.cwd||prefs.cwd||'连接后显示工作目录';$('workspace-label').textContent=cwd;$('workspace-label').title=(app.thread?'当前会话目录：':'新会话目录：')+cwd;}
function applyPrefs() { document.body.classList.toggle('dark',prefs.dark); document.body.classList.toggle('wallpaper-hidden',!prefs.wallpaper); document.querySelector('.wallpaper').style.opacity=String(prefs.opacity/100); showWorkspace(); $('pet-container').hidden=!prefs.pet||!app.petReady;if(!prefs.pet)petPointerLeave(); }
function savePrefs() { localStorage.setItem('dragon-atelier-settings',JSON.stringify(prefs)); applyPrefs(); }
async function bridgeJson(url,options) {let response;try {response=await fetch(url,options);}catch {connection(false,unavailableMessage);throw Error(unavailableMessage);}let payload;try {payload=await response.json();}catch {throw Error('龙娘工坊的连接响应不完整，请点击「重新连接」。');}if(!response.ok||payload?.error){const error=Error(errorText(payload?.error));error.code=payload?.error?.code;error.kind=isWriterConflict(payload?.error)?'thread_in_use':payload?.error?.kind;error.data=payload?.error?.data;throw error;}return payload;}
async function rpc(method,params={}) {return (await bridgeJson('/api/rpc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({method,params})})).result;}
function forgetBridgeSession() {app.resumed.clear();app.writerConflict=null;app.requests=[];app.openRequestId=null;if($('request-dialog').open)$('request-dialog').close();busy(false);}
function requireConnection(epoch) {if(!app.connected||epoch!==app.connectionEpoch)throw Error(disconnectedMessage);}
function connection(online,message) {const wasOnline=app.connected;app.connected=online===true;if(!app.connected){if(wasOnline||app.resumed.size||app.requests.length||app.busy)app.connectionEpoch++;forgetBridgeSession();setCaption(message||disconnectedMessage,true);} $('connection-dot').className='connection-dot '+(app.connected?'online':'error'); $('connection-label').textContent=message||(app.connected?'已连接 Codex · 龙娘工坊':disconnectedMessage); $('reconnect').hidden=app.connected;if(app.connected&&!wasOnline)hydrate().catch(e=>toast(errorText(e)));}
function busy(value,turnId=null) { app.busy=value; app.turn=value?(turnId||app.turn):null; $('send').hidden=value; $('stop-turn').hidden=!value; $('task-status').textContent=value?'正在与你一起完成…':'准备好听你的想法'; $('composer-caption').classList.toggle('busy',value); setPetState(value?'running':'idle'); }
function shortDate(seconds) { if(!seconds)return ''; const date=new Date(seconds*1000),today=new Date(); if(date.toDateString()===today.toDateString())return '今天'; return `${date.getMonth()+1}/${date.getDate()}`; }
function threadName(thread) {
  if(thread.name||thread.title)return thread.name||thread.title;
  const preview=String(thread.preview||'新的会话'),marker=preview.indexOf('[用户附件] ');
  if(marker<0)return preview.split('\n')[0].slice(0,60);
  const text=preview.slice(0,marker).trimEnd();if(text.trim())return text.split('\n')[0].slice(0,60);
  const reference=nativeAttachmentReference(preview.slice(marker).split('\n')[0]+'\n请根据我的消息读取该本地附件。');
  return (reference?.name||'附件会话').slice(0,60);
}
function renderThreads() { $('chat-list').replaceChildren(); for(const thread of app.threads) { const button=el('button'); button.type='button'; button.append(el('span',threadName(thread),'chat-name'),el('small',shortDate(thread.updatedAt))); button.title=threadName(thread)+(thread.cwd?'\n'+thread.cwd:''); button.classList.toggle('selected',thread.id===app.thread?.id); button.addEventListener('click',()=>{document.body.classList.toggle('sidebar-open',false);openThread(thread.id).catch(e=>toast(errorText(e)));}); $('chat-list').append(button); } $('load-more').hidden=!app.nextCursor; $('load-more').disabled=app.threadsLoading; $('load-more').textContent=app.threadsLoading?'正在加载…':'展开更多会话'; }
function historyListStatus(text,error=false){$('history-status').textContent=text;$('history-status').classList.toggle('error',error);}
async function loadThreads(more=false,repair=false) {
  const query=$('chat-search').value.trim();
  if(more&&(app.threadsLoading||!app.nextCursor||query!==app.threadQuery))return;
  const request=++app.threadListRequest,epoch=app.connectionEpoch,cursor=more?app.nextCursor:null;
  app.threadsLoading=true;if(!more){app.threadQuery=query;app.nextCursor=null;app.threads=[];}
  historyListStatus(repair?'正在同步本地历史…':query?'正在搜索会话名称…':'正在加载历史会话…');renderThreads();
  try {
    const result=await rpc('thread/list',{limit:30,sortKey:'updated_at',archived:false,useStateDbOnly:!repair,sourceKinds:['cli','vscode','appServer','exec','unknown'],...(query?{searchTerm:query}:{}),...(cursor?{cursor}:{})});
    if(request!==app.threadListRequest||epoch!==app.connectionEpoch)return;
    const unique=new Map((more?app.threads:[]).map(thread=>[thread.id,thread]));for(const thread of result.data||[])if(thread.id)unique.set(thread.id,thread);
    app.threads=[...unique.values()];app.nextCursor=result.nextCursor||null;
    historyListStatus(app.threads.length?(query?'按会话名称搜索；可展开更多结果。':'本机 Codex 历史会话。'):query?'没有找到这个名称的会话。':'还没有本地历史会话。');
  } catch(error) {if(request!==app.threadListRequest||epoch!==app.connectionEpoch)return;historyListStatus('历史会话加载失败，请点击「刷新会话」重试。',true);throw error;}
  finally {if(request===app.threadListRequest){app.threadsLoading=false;renderThreads();}}
}
let threadSearchTimer;
function searchThreads(){clearTimeout(threadSearchTimer);app.threadListRequest++;app.threadsLoading=false;app.nextCursor=null;app.threads=[];historyListStatus('正在搜索会话名称…');renderThreads();threadSearchTimer=setTimeout(()=>loadThreads().catch(error=>toast(errorText(error))),250);}
function clearConversation() { $('conversation').replaceChildren(); app.items.clear(); app.pendingMessage=null; }
function welcome() { clearConversation(); const box=el('div',null,'welcome'); box.append(el('span','✧','welcome-star'),el('h1','把灵感，交给龙娘。'),el('p','写点什么，或从左侧继续已有的 Codex 会话。')); const actions=el('div',null,'starter-actions'); for(const [label,text] of [['梳理一个项目','帮我整理这个项目，先看看目录结构。'],['推敲一个想法','陪我一起推敲一个想法。'],['打磨一段文字','帮我修改一段文字，我稍后把内容发给你。']]) { const b=el('button',label); b.type='button'; b.addEventListener('click',()=>{$('prompt').value=text;$('prompt').focus();}); actions.append(b); } box.append(actions); $('conversation').append(box); }
function inlineText(parent,text) { const chunks=String(text).split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g); for(const chunk of chunks) { if(chunk.startsWith('`')&&chunk.endsWith('`'))parent.append(el('code',chunk.slice(1,-1))); else if(chunk.startsWith('**')&&chunk.endsWith('**'))parent.append(el('strong',chunk.slice(2,-2))); else {const match=chunk.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/); if(match){ const link=el('a',match[1]); link.href=match[2]; link.target='_blank'; link.rel='noopener noreferrer'; parent.append(link);}else parent.append(document.createTextNode(chunk));} } }
function markdown(node,text) { node.replaceChildren(); const lines=String(text).split('\n'); let paragraph=[],code=null,list=null; const flush=()=>{if(paragraph.length){const p=el('p');inlineText(p,paragraph.join('\n'));node.append(p);paragraph=[];}list=null;}; for(const line of lines) { if(line.startsWith('```')) {if(code===null){flush();code=[];}else{const pre=el('pre');pre.append(el('code',code.join('\n')));node.append(pre);code=null;}continue;} if(code!==null){code.push(line);continue;} if(!line.trim()){flush();continue;} const heading=line.match(/^(#{1,6})\s+(.+)/); if(heading){flush();const h=el(heading[1].length<3?'h2':'h3');inlineText(h,heading[2]);node.append(h);continue;} const bullet=line.match(/^\s*(?:[-*+]\s+|\d+\.\s+)(.+)/); if(bullet){if(paragraph.length)flush();if(!list){list=el('ul');node.append(list);}const li=el('li');inlineText(li,bullet[1]);list.append(li);continue;} if(list)list=null; paragraph.push(line); } if(code!==null){const pre=el('pre');pre.append(el('code',code.join('\n')));node.append(pre);}flush(); }
function nearBottom() { const c=$('conversation');return c.scrollHeight-c.scrollTop-c.clientHeight<180; }
function scrollDown(force=false) { if(force||nearBottom())$('conversation').scrollTop=$('conversation').scrollHeight; }
function itemKey(item) {return item.id||crypto.randomUUID();}
function nativeAttachmentReference(text){
  const match=String(text||'').match(/^\[用户附件\] (\{[^\n]*\})\n请根据我的消息读取该本地附件。$/);if(!match)return null;
  try{const value=JSON.parse(match[1]);return typeof value.name==='string'&&typeof value.path==='string'&&Object.keys(value).every(key=>['name','path'].includes(key))?value:null;}catch{return null;}
}
function userItem(item) { const node=el('div',null,'user-message'); for(const input of item.content||[]) {if(input.type==='text'){const reference=nativeAttachmentReference(input.text);node.append(reference?el('div','📎 '+reference.name,'message-file'):document.createTextNode(input.text||''));} if(input.type==='image'&&/^data:image\/(png|jpeg|webp);base64,/.test(input.url||'')){const image=el('img',null,'user-image');image.src=input.url;image.alt='发送的图片';node.append(image);} if(input.type==='localImage')node.append(el('div','[本地图片]','message-meta'));if(input.type==='attachment'){const attachment=app.attachments.find(a=>a.id===input.id);node.append(el('div','📎 '+(attachment?.name||'附件'),'message-file'));}if(input.type==='mention')node.append(el('div','📎 '+(input.name||input.path||'附件'),'message-file'));} return node; }
function safeToolResult(value){if(Array.isArray(value))return value.map(safeToolResult);if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([key,part])=>[key,/^(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|credentials?)$/i.test(key)?'[已隐藏]':safeToolResult(part)]));return value;}
function toolText(item){if(item.type==='commandExecution')return [item.command,item.aggregatedOutput].filter(value=>value!=null&&value!=='').join('\n\n');if(item.type==='fileChange')return (item.changes||[]).map(change=>change.path+'\n'+(change.diff||'')).join('\n\n');if(item.type==='webSearch')return item.query||item.action?.query||item.action?.url||'';if(item.type==='mcpToolCall'){const parts=[item.server+' · '+item.tool];if(item.result!=null)parts.push(JSON.stringify(safeToolResult(item.result),null,2));if(item.error)parts.push(item.error.message||JSON.stringify(safeToolResult(item.error),null,2));return parts.join('\n\n');}return String(item.text||item.aggregatedOutput||'');}
function renderItem(item) { const key=itemKey(item); let entry=app.items.get(key); if(item.type==='userMessage'){if(app.pendingMessage&&app.pendingMessage.threadId===app.thread?.id){app.pendingMessage.node.remove();app.pendingMessage=null;}if(entry)return entry;const node=userItem(item);$('conversation').append(node);entry={node,item,text:''};}
  else if(item.type==='agentMessage'){if(!entry){const node=el('article',null,'assistant-message');const heading=el('div',null,'message-heading');heading.append(el('span','✧','mini-seal'),el('span','Codex'));const copy=el('button','复制','message-copy');copy.type='button';const body=el('div',null,'message-body');entry={node,body,item,text:item.text||''};copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(entry.text);toast('已复制。');}catch{toast('请选中文字复制。');}});heading.append(copy);node.append(heading,body);$('conversation').append(node);}entry.text=item.text??entry.text;entry.item=item;markdown(entry.body,entry.text);}
  else if(item.type==='reasoning') { return null; }
  else {const labels={commandExecution:'正在处理项目',fileChange:'文件改动',mcpToolCall:'正在使用工具',webSearch:'正在查找资料',imageGeneration:'正在绘制图片',plan:'工作计划',collabAgentToolCall:'协作进展'};if(!entry){const node=el('details',null,'tool-card'),summary=el('summary',labels[item.type]||'工作进展'),state=el('span','','tool-state'),pre=el('pre');summary.append(state);node.append(summary,pre);$('conversation').append(node);entry={node,body:pre,state,item,text:''};}entry.item=item;entry.state.textContent=({inProgress:'进行中',completed:'已完成',failed:'未完成',declined:'已取消'})[item.status]||'';entry.text=toolText(item);entry.body.textContent=entry.text;}
  app.items.set(key,entry);return entry;
}
function activeThreadTurn(thread) {return (thread.turns||[]).findLast(t=>t.status==='inProgress'&&!app.finishedTurns.has(t.id));}
function historyControls(message=''){$('load-earlier').hidden=!app.historyCursor;$('load-earlier').disabled=app.earlierLoading||app.historyLoading;$('load-earlier').textContent=app.earlierLoading?'正在加载…':'加载更早消息';$('history-message-status').textContent=message;$('history-toolbar').hidden=!app.historyCursor&&!message;}
function renderThread(thread) { clearConversation(); app.thread=thread;app.writerConflict=null;localStorage.setItem('dragon-atelier-last-thread',thread.id);$('chat-title').textContent=threadName(thread);for(const turn of thread.turns||[])for(const item of turn.items||[])renderItem(item);if(!app.items.size)welcome();const active=activeThreadTurn(thread);busy(!!active&&app.resumed.has(thread.id),active?.id);showWorkspace();renderThreads();historyControls();scrollDown(true); }
function unsupportedHistoryMethod(error){return error?.code===-32601||/(?:unsupported|not supported|unknown method|method not found).*thread\/turns\/list|thread\/turns\/list.*(?:unsupported|not supported|unknown method|method not found)/i.test(error?.message||'');}
function uniqueTurns(turns){const unique=new Map();for(const turn of turns)if(turn.id&&!unique.has(turn.id))unique.set(turn.id,turn);return [...unique.values()];}
async function openThread(id,force=false) {
  if(app.loading&&!app.historyLoading||!force&&!app.historyLoading&&id===app.thread?.id)return;
  const request=++app.historyRequest,epoch=app.connectionEpoch;app.loading=true;app.historyLoading=true;app.earlierLoading=false;app.historyCursor=null;historyControls('正在加载会话记录…');
  const current=()=>request===app.historyRequest&&epoch===app.connectionEpoch;
  try {
    const metadata=await rpc('thread/read',{threadId:id,includeTurns:false});if(!current())return;
    let thread,cursor=null;
    try {const page=await rpc('thread/turns/list',{threadId:id,itemsView:'full',sortDirection:'desc',limit:5});if(!current())return;thread={...metadata.thread,turns:uniqueTurns([...(page.data||[])].reverse())};cursor=page.nextCursor||null;}
    catch(error){if(!current())return;if(!unsupportedHistoryMethod(error))throw error;const legacy=await rpc('thread/read',{threadId:id,includeTurns:true});if(!current())return;thread=legacy.thread;}
    app.historyCursor=cursor;renderThread(thread);document.body.classList.toggle('sidebar-open',false);setCaption(app.busy?'龙娘正在和你一起完成…':activeThreadTurn(thread)?'此会话正在进行中。可查看历史，原窗口负责当前回复。':'已打开历史。发送时会尝试继续此会话。');
  } catch(error){if(!current())return;historyControls('会话记录加载失败，请重新打开这段会话。');throw error;}
  finally {if(request===app.historyRequest){app.loading=false;app.historyLoading=false;if(app.historyCursor)historyControls();}}
}
async function loadEarlier(){
  if(!app.thread||!app.historyCursor||app.historyLoading||app.earlierLoading)return;
  const id=app.thread.id,cursor=app.historyCursor,request=app.historyRequest,epoch=app.connectionEpoch;app.earlierLoading=true;historyControls();
  const current=()=>request===app.historyRequest&&epoch===app.connectionEpoch&&app.thread?.id===id;
  try {const page=await rpc('thread/turns/list',{threadId:id,cursor,itemsView:'full',sortDirection:'desc',limit:5});if(!current())return;
    const previousIds=new Set((app.thread.turns||[]).map(turn=>turn.id)),earlier=uniqueTurns([...(page.data||[])].reverse()).filter(turn=>!previousIds.has(turn.id));
    const conversation=$('conversation'),height=conversation.scrollHeight,top=conversation.scrollTop,nodes=[];
    for(const turn of earlier)for(const item of turn.items||[]){if(item.id&&app.items.has(item.id))continue;const entry=renderItem(item);if(entry)nodes.push(entry.node);}
    if(nodes.length){conversation.querySelector?.('.welcome')?.remove();conversation.prepend(...nodes);}
    app.thread.turns=[...earlier,...(app.thread.turns||[])];app.historyCursor=page.nextCursor||null;conversation.scrollTop=top+conversation.scrollHeight-height;
    historyControls(app.historyCursor?'':'已加载到最早的消息。');
  } catch(error){if(current()){historyControls('更早消息加载失败，可以重试。');toast(errorText(error));}}
  finally{if(current()){app.earlierLoading=false;$('load-earlier').disabled=false;}}
}
function newChat() {if(app.loading&&!app.historyLoading){toast('正在连接会话，请稍后再切换。');return;}app.historyRequest++;app.historyLoading=false;app.earlierLoading=false;app.loading=false;app.historyCursor=null;historyControls();localStorage.removeItem('dragon-atelier-last-thread');app.thread=null;app.turn=null;app.writerConflict=null;busy(false);welcome();showWorkspace();renderThreads();$('chat-title').textContent='新的会话';$('prompt').focus();setCaption('愿每个小小的灵感，都有属于自己的翅膀。');}
function updateEfforts() { const selected=app.models.find(m=>m.model===$('model').value),efforts=selected?.supportedReasoningEfforts||[];$('effort').replaceChildren(el('option','默认深度'));$('effort').firstChild.value='';for(const effort of efforts){const value=typeof effort==='string'?effort:effort.reasoningEffort;const label={none:'无需深度思考',minimal:'轻量',low:'简短',medium:'标准',high:'深入',xhigh:'更深入',max:'最深入',ultra:'超深度'}[value]||value;const option=el('option',label);option.value=value;$('effort').append(option);} }
function selectNativeModel(result){const selected=app.models.find(m=>m.model===result.model);if(selected){$('model').value=selected.model;updateEfforts();if(result.reasoningEffort&&[...$('effort').options].some(o=>o.value===result.reasoningEffort))$('effort').value=result.reasoningEffort;}}
async function loadModels() { const previousModel=$('model').value,previousEffort=$('effort').value;const result=await rpc('model/list',{limit:100});app.models=(result.data||[]).filter(m=>!m.hidden);$('model').replaceChildren(el('option','Codex 默认模型'));$('model').firstChild.value='';for(const model of app.models){const option=el('option',model.displayName||model.model);option.value=model.model;$('model').append(option);}if(app.models.some(m=>m.model===previousModel))$('model').value=previousModel;updateEfforts();if([...$('effort').options].some(o=>o.value===previousEffort))$('effort').value=previousEffort; }
const attachmentLimits={total:10,images:3,imageBytes:20*1024*1024,fileBytes:5*1024*1024};
function imageMime(file){return ['image/png','image/jpeg','image/webp'].includes(file.type)?file.type:!file.type?({png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',webp:'image/webp'}[String(file.name||'').split('.').pop().toLowerCase()]||''):'';}
function fileSize(size){return size<1024?`${size} B`:size<1024*1024?`${Math.ceil(size/1024)} KB`:`${(size/1024/1024).toFixed(1)} MB`;}
function attachmentPreparing(){return app.attachments.some(a=>a.status==='preparing');}
function drawAttachments() {
  $('attachment-list').replaceChildren();
  app.attachments.forEach(a=>{
    const chip=el('div',null,'attachment-chip');chip.classList.toggle('preparing',a.status==='preparing');
    if(a.url){const image=el('img');image.src=a.url;image.alt=a.name;chip.append(image);}else chip.append(el('span',a.kind==='image'?'▧':'📎','attachment-icon'));
    const details=el('span',null,'attachment-details');details.append(el('span',a.name,'attachment-name'),el('small',a.status==='preparing'?'正在准备…':fileSize(a.size||0),'attachment-size'));
    const remove=el('button','×');remove.type='button';remove.setAttribute('aria-label','移除 '+a.name);remove.addEventListener('click',()=>{app.attachments=app.attachments.filter(item=>item!==a);drawAttachments();});
    chip.append(details,remove);$('attachment-list').append(chip);
  });
  updateSendButton();
}
function updateSendButton(){const preparing=attachmentPreparing();$('send').disabled=app.sending||preparing;$('send').title=preparing?'附件正在准备，完成后即可发送':'发送';$('composer').setAttribute('aria-busy',String(app.sending||preparing));}
function readAttachment(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>typeof reader.result==='string'&&/^data:[^,]*;base64,/.test(reader.result)?resolve(reader.result):reject(Error('文件读取不完整，请重新添加。'));reader.onerror=()=>reject(Error('无法读取这个文件，请重新添加。'));reader.onabort=()=>reject(Error('文件读取已取消。'));reader.readAsDataURL(file);});}
async function prepareAttachment(file,attachment){
  try {
    const dataUrl=await readAttachment(file);if(!app.attachments.includes(attachment))return;
    if(attachment.kind==='image')attachment.url=dataUrl.replace(/^data:[^;]*;/,`data:${attachment.mime};`);
    else {const result=(await bridgeJson('/api/attachments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:attachment.name,data:dataUrl.slice(dataUrl.indexOf(',')+1)})})).result;if(!result?.id)throw Error('附件没有保存成功，请重新添加。');attachment.id=result.id;}
    attachment.status='ready';
  }catch(error){if(app.attachments.includes(attachment)){app.attachments=app.attachments.filter(item=>item!==attachment);toast(`${attachment.name}：${errorText(error)}`);}}
  finally{drawAttachments();}
}
async function addAttachments(files){
  const tasks=[];
  for(const file of Array.from(files||[])){
    const mime=imageMime(file),image=!!mime;
    if(app.attachments.length>=attachmentLimits.total){toast('每次最多添加 10 个附件。');break;}
    if(file.size===0){toast('空文件或文件夹不能添加，请选择里面的文件。');continue;}
    if(!Number.isFinite(file.size)||file.size<0||file.size>(image?attachmentLimits.imageBytes:attachmentLimits.fileBytes)){toast(image?'图片不能超过 20 MB。':'文件不能超过 5 MB。');continue;}
    if(image&&app.attachments.filter(a=>a.kind==='image'||a.url).length>=attachmentLimits.images){toast('每次最多添加三张图片。');continue;}
    const attachment={kind:image?'image':'file',name:file.name||(image?'粘贴的图片':'附件'),size:file.size,mime,status:'preparing'};
    app.attachments.push(attachment);tasks.push([file,attachment]);
  }
  drawAttachments();
  await Promise.all(tasks.map(([file,attachment])=>prepareAttachment(file,attachment)));
}
function transferFiles(data){
  if(data?.files?.length)return Array.from(data.files);
  return Array.from(data?.items||[]).filter(item=>item.kind==='file').map(item=>item.getAsFile()).filter(Boolean);
}
function hasFileTransfer(data){return !!data&&(Array.from(data.types||[]).includes('Files')||data.files?.length||Array.from(data.items||[]).some(item=>item.kind==='file'));}
let attachmentDragDepth=0;
function resetAttachmentDrag(){attachmentDragDepth=0;$('composer-dropzone').classList.toggle('file-drag-over',false);}
const attachmentDropzone=$('composer-dropzone');
attachmentDropzone.addEventListener('dragenter',event=>{if(!hasFileTransfer(event.dataTransfer))return;event.preventDefault();event.stopPropagation();attachmentDragDepth++;attachmentDropzone.classList.toggle('file-drag-over',true);});
attachmentDropzone.addEventListener('dragover',event=>{if(!hasFileTransfer(event.dataTransfer))return;event.preventDefault();event.stopPropagation();event.dataTransfer.dropEffect='copy';attachmentDropzone.classList.toggle('file-drag-over',true);});
attachmentDropzone.addEventListener('dragleave',event=>{if(!attachmentDragDepth)return;event.stopPropagation();attachmentDragDepth=Math.max(0,attachmentDragDepth-1);if(!attachmentDragDepth)resetAttachmentDrag();});
attachmentDropzone.addEventListener('drop',event=>{if(!hasFileTransfer(event.dataTransfer))return;event.preventDefault();event.stopPropagation();resetAttachmentDrag();return addAttachments(transferFiles(event.dataTransfer));});
attachmentDropzone.addEventListener('paste',event=>{const files=transferFiles(event.clipboardData);if(!files.length)return;if(!event.clipboardData.getData?.('text/plain'))event.preventDefault();return addAttachments(files);});
document.addEventListener('dragover',event=>{if(!hasFileTransfer(event.dataTransfer))return;event.preventDefault();event.dataTransfer.dropEffect='none';});
document.addEventListener('drop',event=>{if(!hasFileTransfer(event.dataTransfer))return;event.preventDefault();resetAttachmentDrag();toast('把文件拖到输入框即可添加。');});
document.addEventListener('dragend',resetAttachmentDrag);
function sending(value) {
  app.sending=value;$('send').textContent=value?'…':'↑';updateSendButton();
}
function pendingMessage(input) {
  app.pendingMessage?.node.remove();
  const node=userItem({content:input}),label=el('div','正在发送…','message-meta');
  node.classList.toggle('pending-message',true);node.append(label);$('conversation').append(node);
  app.pendingMessage={node,label,threadId:app.thread?.id};scrollDown(true);
}
async function resumeForSending(id,epoch) {
  const result=await rpc('thread/resume',{threadId:id,excludeTurns:true});requireConnection(epoch);
  app.resumed.add(id);
  const live=result.thread||{},previous=app.thread;
  app.thread={...previous,...live,turns:previous?.turns||live.turns||[]};
  let active=live.status?.type==='idle'?null:activeThreadTurn(live);
  if(!active&&live.status?.type==='active') {
    const page=await rpc('thread/turns/list',{threadId:id,itemsView:'summary',sortDirection:'desc',limit:1});
    requireConnection(epoch);active=activeThreadTurn({turns:page.data||[]});
  }
  return {result,active};
}
async function sendMessage(event) {
  event?.preventDefault();
  const rawText=$('prompt').value,text=rawText.trim();
  if((!text&&!app.attachments.length)||app.busy||app.loading)return;
  if(attachmentPreparing()){toast('附件还在准备，完成后即可发送。');return;}
  if(app.writerConflict===app.thread?.id){showWriterConflict();toast(writerConflictMessage);return;}
  if(!app.connected){toast('请点击「重新连接」后再发送，当前输入已保留。');return;}
  const attachments=[...app.attachments],input=[...(text?[{type:'text',text}]:[]),...attachments.map(a=>a.kind==='file'?{type:'attachment',id:a.id}:{type:'image',url:a.url})];
  const model=$('model').value,effort=$('effort').value,epoch=app.connectionEpoch;
  let accepted=false;
  app.loading=true;sending(true);setCaption('正在发送…');
  if(!app.thread)clearConversation();
  pendingMessage(input);
  try {
    if(!app.thread) {
      const result=await rpc('thread/start',{...(prefs.cwd?{cwd:prefs.cwd}:{}),...(model?{model}:{})});requireConnection(epoch);
      app.thread=result.thread;localStorage.setItem('dragon-atelier-last-thread',app.thread.id);selectNativeModel(result);
      app.resumed.add(app.thread.id);if(app.pendingMessage)app.pendingMessage.threadId=app.thread.id;
      $('chat-title').textContent=text.slice(0,42)||'附件会话';
    } else if(!app.resumed.has(app.thread.id)) {
      const {active}=await resumeForSending(app.thread.id,epoch);
      if(active){busy(true,active.id);setCaption('此会话已有回复正在进行。当前输入已保留。');return;}
    }
    requireConnection(epoch);
    const result=await rpc('turn/start',{threadId:app.thread.id,input,...(model?{model}:{}),...(effort?{effort}:{})});requireConnection(epoch);
    accepted=true;busy(!app.finishedTurns.has(result.turn.id),result.turn.id);
    for(const item of result.turn.items||[])renderItem(item);
    if(app.pendingMessage){app.pendingMessage.label.textContent='已发送';app.pendingMessage.node.classList.toggle('pending-message',false);}
    if($('prompt').value===rawText)$('prompt').value='';
    app.attachments=app.attachments.filter(a=>!attachments.includes(a));drawAttachments();
    if(app.busy)setCaption('龙娘正在和你一起完成…');scrollDown(true);
    loadThreads().catch(()=>{});
  } catch(error) {
    busy(false);if(isWriterConflict(error))showWriterConflict();else setCaption(errorText(error),true);toast(errorText(error));
  } finally {
    if(!accepted&&app.pendingMessage){app.pendingMessage.node.remove();app.pendingMessage=null;}
    app.loading=false;sending(false);
  }
}
async function stopTurn() {if(!app.thread||!app.turn||!app.resumed.has(app.thread.id)||app.writerConflict===app.thread.id)return;try {await rpc('turn/interrupt',{threadId:app.thread.id,turnId:app.turn});setCaption('正在停下这次回复…');}catch(e){toast(errorText(e));} }
let renderScheduled=false;
function queueRender() {if(renderScheduled)return;renderScheduled=true;requestAnimationFrame(()=>{renderScheduled=false;const stick=nearBottom();for(const entry of app.items.values())if(entry.dirty){markdown(entry.body,entry.text);entry.dirty=false;}scrollDown(stick);});}
function acceptInstance(snapshot){if(snapshot.instanceId&&snapshot.instanceId!==app.instanceId){if(app.instanceId||app.resumed.size){app.connectionEpoch++;forgetBridgeSession();app.lastEvent=0;app.connected=false;}app.instanceId=snapshot.instanceId;}if(snapshot.connected===false){forgetBridgeSession();return;}if(Array.isArray(snapshot.ownedThreadIds)){app.resumed.clear();for(const id of snapshot.ownedThreadIds)if(typeof id==='string')app.resumed.add(id);if(app.busy&&app.thread&&!app.resumed.has(app.thread.id)){busy(false);setCaption('此会话当前由原窗口负责。可查看历史，当前输入已保留。');}}}
function notification(message) {if(message.type==='server_request'){if(!app.requests.some(r=>r.id===message.id))app.requests.push(message);showRequest();setPetState('waiting');return;}if(message.type==='connection'||message.method==='bridge/connection'){const snapshot={...message.params,...message};applyBridgeWorkspace(snapshot);acceptInstance(snapshot);const online=snapshot.connected===true||snapshot.connected==null&&snapshot.connection==='ready';connection(online,online?snapshot.message:snapshot.connection==='connecting'?'正在连接 Codex 服务，当前输入已保留…':disconnectedMessage);if(online&&Array.isArray(snapshot.pendingRequests)){app.requests=snapshot.pendingRequests;if($('request-dialog').open&&!app.requests.some(r=>r.id===app.openRequestId))$('request-dialog').close();showRequest();}return;}if(message.type==='approval_submitted'){app.requests=app.requests.filter(r=>r.id!==message.id);if($('request-dialog').open&&app.openRequestId===message.id)$('request-dialog').close();showRequest();if(!app.requests.length)setPetState(app.busy?'running':'idle');return;}const p=message.params||{},method=message.method;if(method==='serverRequest/resolved'){app.requests=app.requests.filter(r=>r.id!==p.requestId);if($('request-dialog').open&&app.openRequestId===p.requestId)$('request-dialog').close();showRequest();if(!app.requests.length)setPetState(app.busy?'running':'idle');return;}if(method==='thread/closed'||method==='thread/archived'){app.resumed.delete(p.threadId);if(p.threadId===app.thread?.id){app.writerConflict=null;busy(false);setCaption(method==='thread/archived'?'此会话已归档。当前输入已保留，可另开新会话继续。':'此会话已断开。当前输入已保留，发送时会尝试重新连接。');}return;}if(p.threadId&&p.threadId!==app.thread?.id)return;const stick=nearBottom();if(method==='turn/started'){if(app.resumed.has(app.thread?.id)){busy(true,p.turn?.id);setCaption('龙娘正在和你一起完成…');}else{busy(false);setCaption('此会话正在进行中。可查看历史，原窗口负责当前回复。');}}
 if(method==='item/started'||method==='item/completed'){if(p.item){renderItem(p.item);if(p.item.type==='userMessage')$('welcome')?.remove();}scrollDown(stick);}
 if(method==='item/agentMessage/delta'){let entry=app.items.get(p.itemId);if(!entry)entry=renderItem({id:p.itemId,type:'agentMessage',text:''});entry.text+=p.delta||'';entry.dirty=true;queueRender();}
 if(method==='item/commandExecution/outputDelta'){const entry=app.items.get(p.itemId);if(entry){entry.item={...entry.item,aggregatedOutput:(entry.item.aggregatedOutput||'')+(p.delta||'')};entry.text=toolText(entry.item);entry.body.textContent=entry.text;}}
 if(method==='turn/completed'){app.finishedTurns.add(p.turn?.id);busy(false);const status=p.turn?.status;if(status==='failed'){setCaption(p.turn?.error?.message||'这次回复未能完成，请重试。',true);setPetState('failed');}else setCaption(status==='interrupted'?'已停止这次回复。':'愿每个小小的灵感，都有属于自己的翅膀。');loadThreads().catch(()=>{});}
 if(method==='error'){if(isWriterConflict(p.error))showWriterConflict();else setCaption(errorText(p.error,'连接发生了问题。'),true);}
}
let events;
function connectEvents() {events?.close();events=new EventSource('/api/events');events.onopen=()=>{};events.addEventListener('message',event=>{try{const message=JSON.parse(event.data),sequence=Number(event.lastEventId);if(message.type==='connection')acceptInstance(message);if(message.type!=='connection'&&sequence&&sequence<=app.lastEvent)return;app.lastEvent=Math.max(app.lastEvent,sequence||0);notification(message);}catch{}});events.onerror=()=>connection(false,'连接中断，请点击「重新连接」。当前输入已保留。');}
function openSettings() {$('cwd').value=prefs.cwd;$('dark-setting').checked=prefs.dark;$('wallpaper-setting').checked=prefs.wallpaper;$('wallpaper-opacity').value=prefs.opacity;$('pet-setting').checked=prefs.pet;$('pet-setting-note').textContent=app.petReady?'龙娘会跟随工作状态切换动作；鼠标停留时会挥手，移开后恢复。':'龙娘动作正在制作，完成后会自动显示。';$('settings-dialog').showModal();}
async function answerRequest(request,result) {try {const response=await fetch('/api/approval',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:request.id,result})});const body=await response.json();if(!response.ok||body.error)throw Error(body.error?.message||'这个决定没有提交成功。');app.requests=app.requests.filter(r=>r.id!==request.id);if($('request-dialog').open&&app.openRequestId===request.id)$('request-dialog').close();showRequest();if(!app.requests.length)setPetState(app.busy?'running':'idle');}catch(e){toast(e.message);} }
function showRequest() {if($('request-dialog').open||!app.requests.length)return;const request=app.requests[0],p=request.params||{},method=request.method;app.openRequestId=request.id;const fields=$('request-fields'),actions=$('request-actions'),description=$('request-description');fields.replaceChildren();actions.replaceChildren();description.replaceChildren();$('request-title').textContent='需要你的决定';if(p.reason)description.append(el('p',p.reason));if(p.command)description.append(el('pre',Array.isArray(p.command)?p.command.join(' '):p.command));if(p.cwd)description.append(el('p','工作目录：'+p.cwd));if(p.grantRoot)description.append(el('p','修改目录：'+p.grantRoot));if(p.networkApprovalContext)description.append(el('p','访问：'+p.networkApprovalContext.protocol+' · '+p.networkApprovalContext.host));if(p.additionalPermissions)description.append(el('pre',JSON.stringify(p.additionalPermissions,null,2)));if(p.fileChanges)description.append(el('pre',Object.entries(p.fileChanges).map(([path,change])=>path+'\n'+(change.unified_diff||change.content||change.type)).join('\n\n')));const button=(label,result,primary=false)=>{const b=el('button',label,primary?'primary':'');b.type='button';b.addEventListener('click',()=>answerRequest(request,typeof result==='function'?result():result));actions.append(b);};
 if(method==='item/commandExecution/requestApproval'||method==='item/fileChange/requestApproval'){const available=p.availableDecisions||['accept','acceptForSession','decline','cancel'];for(const [value,label,primary] of [['accept','允许这次',true],['acceptForSession','本次会话中允许',false],['decline','拒绝',false],['cancel','停止本次回复',false]])if(available.includes(value))button(label,{decision:value},primary);if(!actions.children.length)description.append(el('p','当前请求只有永久规则选项，请到官方客户端处理。'));}
 else if(method==='execCommandApproval'||method==='applyPatchApproval'){button('允许这次',{decision:'approved'},true);button('拒绝',{decision:{denied:{rejection:'用户拒绝了这次请求。'}}});button('停止本次回复',{decision:'abort'});}
 else if(method==='item/tool/requestUserInput'||method==='tool/requestUserInput'){const controls=[];for(const q of p.questions||[]){const label=el('label',q.question||q.header||'请输入'),control=(q.options?.length?el('select'):el('input'));let other=null;if(q.options?.length){for(const o of q.options){const option=el('option',o.label+(o.description?' · '+o.description:''));option.value=o.label;control.append(option);}if(q.isOther){const option=el('option','填写自己的回答');option.value='__custom__';control.append(option);other=el('input');other.type=q.isSecret?'password':'text';other.placeholder='你的回答';other.hidden=true;control.addEventListener('change',()=>{other.hidden=control.value!=='__custom__';});}}else control.type=q.isSecret?'password':'text';label.append(control);if(other)label.append(other);fields.append(label);controls.push([q.id,control,other]);}button('提交',()=>({answers:Object.fromEntries(controls.map(([id,control,other])=>[id,{answers:[other&&control.value==='__custom__'?other.value:control.value]}]))}),true);}
 else if(method==='item/permissions/requestApproval'){description.append(el('pre',JSON.stringify(p.permissions||{},null,2)));button('允许这次',{permissions:p.permissions||{},scope:'turn'},true);button('拒绝',{permissions:{},scope:'turn'});}
 else if(method==='mcpServer/elicitation/request'){if(p.message)description.append(el('p',p.message));if(p.mode==='url'){const link=el('a','打开验证页面');try{const url=new URL(p.url);if(url.protocol==='https:'){link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';description.append(link);}}catch{}button('已完成',{action:'accept',content:null},true);}else{const properties=p.requestedSchema?.properties||{},controls=[];for(const [key,schema] of Object.entries(properties)){const label=el('label',schema.title||key),input=schema.enum?el('select'):el('input');if(schema.enum){for(const v of schema.enum){const o=el('option',String(v));o.value=String(v);input.append(o);}}else input.type=schema.type==='boolean'?'checkbox':schema.type==='integer'||schema.type==='number'?'number':'text';label.append(input);fields.append(label);controls.push([key,schema,input]);}button('提交',()=>({action:'accept',content:Object.fromEntries(controls.map(([key,s,input])=>[key,s.type==='boolean'?input.checked:s.type==='number'||s.type==='integer'?Number(input.value):input.value]))}),true);}button('取消',{action:'cancel',content:null});}
 else {description.append(el('p','Codex 请求了当前界面尚不支持的操作。'));button('取消',{decision:'cancel'});} $('request-dialog').showModal(); }
$('request-dialog').addEventListener('cancel',event=>{event.preventDefault();toast('请先选择一个决定。');});
const petStates={idle:[0,6,190],right:[1,8,95],left:[2,8,95],waving:[3,4,170],jumping:[4,5,160],failed:[5,8,180],waiting:[6,6,210],running:[7,6,170],review:[8,6,190]};
let petTimer,petFrame=0,petHoverTimer;
function playPetState(state) {
  if(!app.petReady)return;
  const sprite=$('pet-sprite');if(app.petState===state&&sprite.style.backgroundPosition)return;
  app.petState=state;const [row,count,delay]=petStates[state]||petStates.idle;
  clearInterval(petTimer);petTimer=null;petFrame=0;
  const draw=()=>{sprite.style.backgroundPosition=`-${(petFrame%count)*192}px -${row*208}px`;petFrame++;};
  draw();if(!matchMedia('(prefers-reduced-motion: reduce)').matches)petTimer=setInterval(draw,delay);
}
function setPetState(state){app.petBaseState=state;playPetState(app.petHover?'waving':state);}
function petPointerEnter(event) {
  if(event?.pointerType==='touch'||!app.petReady||!prefs.pet)return;
  app.petPointerInside=true;clearTimeout(petHoverTimer);
  petHoverTimer=setTimeout(()=>{
    if(!app.petPointerInside||!prefs.pet)return;
    app.petHover=true;$('pet-sprite').classList.toggle('pet-hover',true);playPetState('waving');
  },220);
}
function petPointerLeave() {
  clearTimeout(petHoverTimer);app.petPointerInside=false;app.petHover=false;
  $('pet-sprite').classList.toggle('pet-hover',false);
  playPetState(app.requests.length?'waiting':app.busy?'running':app.petBaseState||'idle');
}
function petPointerMove(event) {
  if(!app.petReady||app.petPointerInside||app.petHover||app.busy||app.requests.length||$('pet-container').hidden)return;
  const rect=$('pet-sprite').getBoundingClientRect();if(!rect.width||!rect.height)return;
  const dx=event.clientX-(rect.left+rect.width/2),dy=event.clientY-(rect.top+rect.height/2);
  clearInterval(petTimer);petTimer=null;
  let position;
  if(Math.hypot(dx,dy)<80){app.petState='neutral';position='-1152px 0px';}
  else{app.petState='look';const angle=(Math.atan2(dx,-dy)*180/Math.PI+360)%360,index=Math.round(angle/22.5)%16;position=`-${(index%8)*192}px -${(9+Math.floor(index/8))*208}px`;}
  if($('pet-sprite').style.backgroundPosition!==position)$('pet-sprite').style.backgroundPosition=position;
}
async function loadPet(){const image=new Image();image.onload=()=>{app.petReady=image.naturalWidth===1536&&image.naturalHeight===2288;$('pet-sprite').style.backgroundImage="url('/assets/dragon-pet.webp')";applyPrefs();setPetState(app.requests.length?'waiting':app.busy?'running':'idle');};image.onerror=()=>setTimeout(loadPet,30000);image.src='/assets/dragon-pet.webp';}
$('pet-sprite').addEventListener('pointerenter',petPointerEnter);
$('pet-sprite').addEventListener('pointerleave',petPointerLeave);
$('pet-sprite').addEventListener('pointercancel',petPointerLeave);
document.addEventListener('pointermove',petPointerMove);

$('composer').addEventListener('submit',sendMessage);$('prompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();sendMessage();}});$('stop-turn').addEventListener('click',stopTurn);$('new-chat').addEventListener('click',()=>{document.body.classList.toggle('sidebar-open',false);newChat();});$('chat-search').addEventListener('input',searchThreads);$('load-more').addEventListener('click',()=>loadThreads(true).catch(e=>toast(errorText(e))));$('load-earlier').addEventListener('click',loadEarlier);$('refresh-chats').addEventListener('click',()=>loadThreads(false,true).catch(e=>toast(e.message)));$('model').addEventListener('change',updateEfforts);$('toggle-sidebar').addEventListener('click',()=>document.body.classList.toggle(matchMedia('(max-width:700px)').matches?'sidebar-open':'sidebar-hidden'));$('toggle-theme').addEventListener('click',()=>{prefs.dark=!prefs.dark;savePrefs();});for(const id of ['workspace-open','appearance-open','settings-open','choose-directory'])$(id).addEventListener('click',openSettings);$('save-settings').addEventListener('click',()=>{const cwd=$('cwd').value.trim();if(!/^[A-Za-z]:[\\/]/.test(cwd)&&!cwd.startsWith('/')){toast('请填写完整的工作目录。');return;}prefs={cwd,dark:$('dark-setting').checked,wallpaper:$('wallpaper-setting').checked,opacity:Number($('wallpaper-opacity').value),pet:$('pet-setting').checked};savePrefs();$('settings-dialog').close();});$('add-file').addEventListener('click',()=>$('image-input').click());$('image-input').addEventListener('change',async()=>{const input=$('image-input'),files=Array.from(input.files||[]);input.value='';await addAttachments(files);});$('archive-chat').addEventListener('click',async()=>{if(!app.thread){toast('还没有可归档的会话。');return;}if(app.busy){toast('请先停止当前回复。');return;}if(!window.confirm('归档当前会话？会话可以在官方客户端的归档中恢复。'))return;try{await rpc('thread/archive',{threadId:app.thread.id});newChat();await loadThreads();toast('会话已归档。');}catch(e){toast(e.message);}});
let hydrating=null;
async function hydrate(){if(hydrating)return hydrating;hydrating=(async()=>{const results=await Promise.allSettled([loadThreads(),loadModels(),rpc('account/read',{})]);for(const result of results)if(result.status==='rejected')toast(errorText(result.reason));if(results[2].status==='fulfilled'){const account=results[2].value.account;$('account-label').textContent=account?.type==='chatgpt'?'ChatGPT 账号':'Codex';$('account-plan').textContent=account?.planType||'当前账号';}const previous=app.thread?.id||localStorage.getItem('dragon-atelier-last-thread');if(previous)await openThread(previous,true).catch(error=>toast(errorText(error)));})();try{await hydrating;}finally{hydrating=null;}}
function verifyBridgeStatus(status) {if(!status||status.appId!==clientAppId)throw Error('这个地址不是龙娘工坊的后台。请打开桌面的「Codex 龙娘工坊」后再连接。');return status;}
async function initialize(){try {const status=verifyBridgeStatus(await bridgeJson('/api/status'));notification({type:'connection',...status});connectEvents();if(app.connected)await hydrate();}catch(error){connection(false,errorText(error));}}
async function reconnect(){if(app.reconnecting)return;app.reconnecting=true;$('reconnect').disabled=true;try {let status=verifyBridgeStatus(await bridgeJson('/api/status'));if(status.connected===false){notification({type:'connection',...status});status=verifyBridgeStatus((await bridgeJson('/api/reconnect',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).result);}notification({type:'connection',...status});connectEvents();if(app.connected){await hydrate();if(!app.busy)setCaption('已重新连接。当前输入尚未发送，请按发送继续。');}}catch(error){events?.close();connection(false,errorText(error));toast(errorText(error));}finally{app.reconnecting=false;$('reconnect').disabled=false;}}
$('reconnect').addEventListener('click',reconnect);applyPrefs();welcome();loadPet();initialize();
