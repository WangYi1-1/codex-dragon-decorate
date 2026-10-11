'use strict';
// Pure fixtures: no browser automation, external fetch, or model inference.
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.style = {};
    this.hidden = false;
    this.open = false;
    this.closeCount = 0;
    this.scrollHeight = 600;
    this.scrollTop = 0;
    this.clientHeight = 600;
    this._text = '';
    this._value = null;
    this.classes = new Set();
    this.classList = { toggle: (key, value) => {
      if (value === undefined) value = !this.classes.has(key);
      value ? this.classes.add(key) : this.classes.delete(key);
    } };
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(_) { throw Error('Unsafe HTML insertion'); }
  append(...nodes) { for (const node of nodes) { node.remove();this.children.push(node); node.parentNode = this; } }
  prepend(...nodes) {for(const node of nodes)node.remove();this.children.unshift(...nodes);for(const node of nodes)node.parentNode=this;}
  replaceChildren(...nodes) { this._text = ''; this._value = null; this.children = []; this.append(...nodes); }
  get firstChild() { return this.children[0]; }
  get options() { return this.children; }
  get value() {
    if (this._value !== null) return this._value;
    return this.tagName === 'SELECT' ? this.children[0]?.value || '' : '';
  }
  set value(value) { this._value = value; }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  async trigger(type, event = {}) { for (const handler of this.listeners.get(type) || []) await handler(event); }
  setAttribute(key, value) { this[key] = value; }
  focus() {}
  click() {this.clickCount=(this.clickCount||0)+1;}
  showModal() { this.open = true; }
  close() { this.open = false; this.closeCount++; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
  getBoundingClientRect() { return { left: 0, top: 0, width: 192, height: 208 }; }
}

function fixture({reducedMotion=true,savedSettings}={}) {
  const ids = new Map(), sources = [], calls = [], storage = new Map(), frames = [], timers = new Map(), intervals = new Map(), readers=[];let timerId=0;
  if(savedSettings)storage.set('dragon-atelier-settings',JSON.stringify(savedSettings));
  const documentEvents=new Node();
  const selectors = new Map();
  const document = {
    body: new Node('body'),
    getElementById(id) { if (!ids.has(id)) ids.set(id, new Node(['model', 'effort'].includes(id) ? 'select' : 'div')); return ids.get(id); },
    createElement: tag => new Node(tag),
    createTextNode: text => { const node = new Node('#text'); node.textContent = text; return node; },
    querySelector(selector) { if (!selectors.has(selector)) selectors.set(selector, new Node()); return selectors.get(selector); },
    addEventListener: (...args)=>documentEvents.addEventListener(...args),
    trigger: (...args)=>documentEvents.trigger(...args),
  };
  class Source {
    constructor(url) { this.url = url; this.listeners = new Map(); sources.push(this); }
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    close() { this.closed = true; }
    emit(message, sequence) { this.listeners.get('message')({ data: JSON.stringify(message), lastEventId: String(sequence) }); }
  }
  let customFetch;
  const context = vm.createContext({
    document, EventSource: Source, URL,
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    fetch: async (url, options = {}) => {
      const payload = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, payload, method: options.method || 'GET' });
      if (customFetch) return customFetch(url, payload, options);
      const result = url==='/api/attachments'?{id:'fixture-attachment-'+calls.length,name:payload.name,size:Buffer.from(payload.data,'base64').length}:['thread/list','thread/turns/list'].includes(payload?.method) ? { data: [], nextCursor: null }
        : payload?.method === 'model/list' ? { data: [] }
        : payload?.method === 'thread/read' ? { thread: { id: payload.params.threadId, turns: [] } }
        : payload?.method === 'thread/resume' ? { thread: { id: payload.params.threadId, turns: [] }, model: 'fixture-model' }
        : { account: { type: 'chatgpt', planType: 'fixture' } };
      return { ok: true, json: async () => ({ result }) };
    },
    navigator: { clipboard: { writeText: async () => {} } },
    window: { confirm: () => false },
    crypto: { randomUUID: () => 'fixture-item' },
    matchMedia: query => ({ matches: query.includes('prefers-reduced-motion')?reducedMotion:true }),
    requestAnimationFrame: callback => frames.push(callback),
    setTimeout: (callback,delay) => {const id=++timerId;timers.set(id,{callback,delay});return id;}, clearTimeout: id=>timers.delete(id), setInterval: (callback,delay) => {const id=++timerId;intervals.set(id,{callback,delay});return id;}, clearInterval: id=>intervals.delete(id),
    Image: class {}, FileReader: class {
      readAsDataURL(file){this.file=file;readers.push(this);this.result=file.dataUrl||`data:${file.type||'application/octet-stream'};base64,${Buffer.from(file.data||'fixture').toString('base64')}`;if(!file.readPending)queueMicrotask(()=>file.readError?this.onerror():this.onload());}
    },
  });
  let source = fs.readFileSync(path.join(__dirname, 'public', 'app.js'), 'utf8');
  source = source.replace(/applyPrefs\(\);welcome\(\);loadPet\(\);initialize\(\);\s*$/, '');
  vm.runInContext(source + '\nglobalThis.subject = {app,prefs,notification,connectEvents,markdown,showRequest,answerRequest,sendMessage,hydrate,openThread,newChat,retryThreadWriter,stopTurn,rpc,initialize,reconnect,loadThreads,searchThreads,loadEarlier,renderItem,setPetState,petPointerEnter,petPointerLeave,petPointerMove,addAttachments,transferFiles,drawAttachments,nativeAttachmentReference,threadName,renderThread};', context);
  return { ...context.subject, document, ids, sources, calls, storage, frames, timers, intervals, readers, setFetch(callback) { customFetch = callback; } };
}

function request(id, choices = ['accept', 'decline', 'cancel']) {
  return { type: 'server_request', id, method: 'item/commandExecution/requestApproval', params: { availableDecisions: choices, command: 'fixture command' } };
}
function allNodes(node) { return [node, ...node.children.flatMap(allNodes)]; }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }

function attachmentFile(name='image.png',type='image/png',extra={}){return {name,type,size:7,lastModified:1,...extra};}
function transferEvent(data){return {dataTransfer:data,clipboardData:data,prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}};}

test('file picker, drop and image paste share attachment preparation and native text paste stays intact',async()=>{
  const f=fixture(),picker=f.document.getElementById('image-input'),zone=f.document.getElementById('composer-dropzone');
  await f.document.getElementById('add-file').trigger('click');assert.equal(picker.clickCount,1);
  picker.files=[attachmentFile('picked.png')];picker.value='selection';await picker.trigger('change');assert.equal(picker.value,'');
  const drop=transferEvent({types:['Files'],files:[attachmentFile('dropped.jpg','image/jpeg')]});await zone.trigger('drop',drop);assert.equal(drop.prevented,true);assert.equal(drop.stopped,true);
  const paste=transferEvent({items:[{kind:'file',getAsFile:()=>attachmentFile('pasted.webp','image/webp')}],getData:()=>''});await zone.trigger('paste',paste);assert.equal(paste.prevented,true);
  assert.deepEqual(Array.from(f.app.attachments,a=>a.name),['picked.png','dropped.jpg','pasted.webp']);assert.equal(f.readers.length,3);assert.equal(f.calls.filter(call=>call.url==='/api/attachments').length,0);
  const plain=transferEvent({files:[],items:[{kind:'string'}],getData:()=>'<b>ordinary text</b>'});await zone.trigger('paste',plain);assert.equal(plain.prevented,false);assert.equal(f.app.attachments.length,3);
});

test('clipboard files list is used once when clipboard items expose the same image',async()=>{
  const f=fixture(),file=attachmentFile();let itemReads=0;
  const event=transferEvent({files:[file],items:[{kind:'file',getAsFile:()=>{itemReads++;return {...file};}}],getData:()=>''});
  await f.document.getElementById('composer-dropzone').trigger('paste',event);assert.equal(f.app.attachments.length,1);assert.equal(itemReads,0);
});

test('file clipboard fallback ignores string and null items, and accompanying text remains native',async()=>{
  const f=fixture(),event=transferEvent({files:[],items:[{kind:'string'},{kind:'file',getAsFile:()=>null},{kind:'file',getAsFile:()=>attachmentFile('notes.txt','text/plain')}],getData:()=> 'also pasted text'});
  await f.document.getElementById('composer-dropzone').trigger('paste',event);assert.equal(event.prevented,false);assert.equal(f.app.attachments.length,1);assert.equal(f.app.attachments[0].kind,'file');
  assert.equal(f.calls[0].url,'/api/attachments');assert.equal(f.calls[0].payload.name,'notes.txt');assert.equal(Buffer.from(f.calls[0].payload.data,'base64').toString(),'fixture');
});

test('nested drag highlight clears and files dropped outside the composer cannot open the browser',async()=>{
  const f=fixture(),zone=f.document.getElementById('composer-dropzone'),data={types:['Files'],files:[]};
  await zone.trigger('dragenter',transferEvent(data));await zone.trigger('dragenter',transferEvent(data));await zone.trigger('dragleave',transferEvent(data));assert.equal(zone.classes.has('file-drag-over'),true);
  await zone.trigger('dragleave',transferEvent(data));assert.equal(zone.classes.has('file-drag-over'),false);
  await zone.trigger('dragenter',transferEvent(data));const outside=transferEvent({types:['Files'],files:[attachmentFile()]});await f.document.trigger('drop',outside);assert.equal(outside.prevented,true);assert.equal(zone.classes.has('file-drag-over'),false);assert.equal(f.app.attachments.length,0);
  const text=transferEvent({types:['text/plain'],files:[]});await zone.trigger('dragover',text);await f.document.trigger('drop',text);assert.equal(text.prevented,false);
});

test('attachment reservation respects total capacity across concurrent additions and keeps initial order',async()=>{
  const f=fixture(),first=Array.from({length:8},(_,i)=>attachmentFile(`first-${i}.txt`,'text/plain',{readPending:true}));
  const firstBatch=f.addAttachments(first);assert.equal(f.app.attachments.length,8);assert.equal(f.document.getElementById('send').disabled,true);
  await f.addAttachments(Array.from({length:3},(_,i)=>attachmentFile(`next-${i}.txt`,'text/plain')));assert.equal(f.app.attachments.length,10);
  assert.deepEqual(Array.from(f.app.attachments,a=>a.name),[...first.map(a=>a.name),'next-0.txt','next-1.txt']);
  f.readers.slice(0,8).forEach(reader=>reader.onload());await firstBatch;assert.equal(f.app.attachments.every(a=>a.status==='ready'),true);assert.equal(f.document.getElementById('send').disabled,false);
});

test('concurrent image preparation reserves the three-image limit and send preserves the draft while preparing',async()=>{
  const f=fixture();f.app.connected=true;f.document.getElementById('prompt').value='keep this';
  const pending=f.addAttachments(Array.from({length:3},(_,i)=>attachmentFile(`${i}.png`,'image/png',{readPending:true})));
  await f.addAttachments([attachmentFile('fourth.png')]);assert.equal(f.app.attachments.length,3);assert.equal(f.readers.length,3);
  await f.sendMessage();assert.equal(f.calls.length,0);assert.equal(f.document.getElementById('prompt').value,'keep this');assert.equal(f.document.getElementById('toast').textContent,'附件还在准备，完成后即可发送。');
  f.readers.forEach(reader=>reader.onload());await pending;assert.equal(f.document.getElementById('send').disabled,false);
});

test('attachment sizes are checked before reading; empty image MIME is normalized and GIF is a generic file',async()=>{
  const f=fixture();await f.addAttachments([attachmentFile('too-big.png','image/png',{size:20*1024*1024+1}),attachmentFile('too-big.pdf','application/pdf',{size:5*1024*1024+1}),attachmentFile('empty-type.PNG',''),attachmentFile('animated.gif','image/gif')]);
  assert.equal(f.readers.length,2);assert.equal(f.app.attachments.length,2);assert.equal(f.app.attachments[0].url.startsWith('data:image/png;base64,'),true);assert.equal(f.app.attachments[1].kind,'file');assert.equal(f.calls.filter(call=>call.url==='/api/attachments').length,1);
});

test('PNG, JPEG and WebP above 2 MiB are accepted through drop, paste and picker up to the 20 MiB project limit',async()=>{
  const f=fixture(),zone=f.document.getElementById('composer-dropzone'),picker=f.document.getElementById('image-input');
  await zone.trigger('drop',transferEvent({types:['Files'],files:[attachmentFile('large.png','image/png',{size:3*1024*1024})]}));
  await zone.trigger('paste',transferEvent({files:[attachmentFile('large.jpg','image/jpeg',{size:12*1024*1024})],getData:()=>''}));
  picker.files=[attachmentFile('large.webp','image/webp',{size:19*1024*1024})];await picker.trigger('change');
  assert.deepEqual(Array.from(f.app.attachments,a=>[a.name,a.status,a.mime]),[['large.png','ready','image/png'],['large.jpg','ready','image/jpeg'],['large.webp','ready','image/webp']]);assert.equal(f.calls.length,0);assert.equal(f.readers.length,3);
  const boundary=fixture();await boundary.addAttachments([attachmentFile('limit.png','image/png',{size:20*1024*1024}),attachmentFile('oversize.png','image/png',{size:20*1024*1024+1})]);
  assert.equal(boundary.app.attachments.length,1);assert.equal(boundary.app.attachments[0].name,'limit.png');assert.equal(boundary.app.attachments[0].status,'ready');assert.equal(boundary.readers.length,1);assert.equal(boundary.document.getElementById('toast').textContent,'图片不能超过 20 MB。');
  const generic=fixture();await generic.addAttachments([attachmentFile('limit.pdf','application/pdf',{size:5*1024*1024}),attachmentFile('oversize.pdf','application/pdf',{size:5*1024*1024+1})]);
  assert.equal(generic.app.attachments.length,1);assert.equal(generic.app.attachments[0].name,'limit.pdf');assert.equal(generic.readers.length,1);assert.equal(generic.document.getElementById('toast').textContent,'文件不能超过 5 MB。');
});

test('empty files and folder-like drop entries show a clear message without reading or uploading',async()=>{
  const f=fixture();await f.addAttachments([attachmentFile('folder','',{size:0}),attachmentFile('empty.txt','text/plain',{size:0})]);
  assert.equal(f.readers.length,0);assert.equal(f.calls.length,0);assert.equal(f.app.attachments.length,0);assert.equal(f.document.getElementById('toast').textContent,'空文件或文件夹不能添加，请选择里面的文件。');
});

test('out-of-order FileReader completion does not reorder attachments',async()=>{
  const f=fixture(),pending=f.addAttachments([attachmentFile('first.png','image/png',{readPending:true}),attachmentFile('second.png','image/png',{readPending:true})]);
  f.readers[1].onload();await Promise.resolve();assert.equal(f.app.attachments[1].status,'ready');assert.equal(f.app.attachments[0].status,'preparing');
  f.readers[0].onload();await pending;assert.deepEqual(Array.from(f.app.attachments,a=>a.name),['first.png','second.png']);
});

test('read and upload failures remove only the failed attachment',async()=>{
  const f=fixture();f.setFetch((_url,payload)=>({ok:false,json:async()=>({error:{message:'上传失败'}})}));
  await f.addAttachments([attachmentFile('kept.png'),attachmentFile('unreadable.png','image/png',{readError:true}),attachmentFile('failed.txt','text/plain')]);
  assert.deepEqual(Array.from(f.app.attachments,a=>a.name),['kept.png']);assert.equal(f.document.getElementById('send').disabled,false);assert.match(f.document.getElementById('toast').textContent,/failed\.txt.*上传失败/);
});

test('removing an attachment while it is being read prevents a later upload or reappearance',async()=>{
  const f=fixture(),pending=f.addAttachments([attachmentFile('cancelled.txt','text/plain',{readPending:true})]);
  const remove=allNodes(f.document.getElementById('attachment-list')).find(node=>node['aria-label']==='移除 cancelled.txt');await remove.trigger('click');assert.equal(f.app.attachments.length,0);assert.equal(f.document.getElementById('send').disabled,false);
  f.readers[0].onload();await pending;assert.equal(f.calls.length,0);assert.equal(f.app.attachments.length,0);
});

test('generic file chips show their name and size without image elements or HTML insertion',async()=>{
  const f=fixture();await f.addAttachments([attachmentFile('<img src=x>.pdf','application/pdf',{size:1024})]);
  const chip=f.document.getElementById('attachment-list');assert.equal(allNodes(chip).some(node=>node.tagName==='IMG'),false);assert.match(chip.textContent,/<img src=x>\.pdf/);assert.match(chip.textContent,/1 KB/);
});

test('file send uses staged ID and keeps new attachments and text added while acceptance is pending',async()=>{
  const f=fixture(),accepted=deferred();f.app.connected=true;f.app.thread={id:'attachment-thread'};f.app.resumed.add('attachment-thread');
  await f.addAttachments([attachmentFile('sent.pdf','application/pdf')]);const id=f.app.attachments[0].id;f.document.getElementById('prompt').value='review this';
  f.setFetch((_url,payload)=>payload?.method==='turn/start'?accepted.promise:{ok:true,json:async()=>({result:{data:[]}})});
  const sending=f.sendMessage();const call=f.calls.find(call=>call.payload?.method==='turn/start');assert.deepEqual(JSON.parse(JSON.stringify(call.payload.params.input)),[{type:'text',text:'review this'},{type:'attachment',id}]);
  assert.match(f.app.pendingMessage.node.textContent,/sent\.pdf/);f.document.getElementById('prompt').value='next draft';await f.addAttachments([attachmentFile('next.png')]);
  accepted.resolve({ok:true,json:async()=>({result:{turn:{id:'file-turn',items:[]}}})});await sending;
  assert.equal(f.document.getElementById('prompt').value,'next draft');assert.deepEqual(Array.from(f.app.attachments,a=>a.name),['next.png']);
});

test('generic file send failure retains both attachment and draft',async()=>{
  const f=fixture();f.app.connected=true;f.app.thread={id:'attachment-thread'};f.app.resumed.add('attachment-thread');await f.addAttachments([attachmentFile('retry.pdf','application/pdf')]);f.document.getElementById('prompt').value='keep draft';
  f.setFetch(()=>({ok:false,json:async()=>({error:{message:'无法发送'}})}));await f.sendMessage();assert.equal(f.app.attachments.length,1);assert.equal(f.document.getElementById('prompt').value,'keep draft');assert.equal(f.app.pendingMessage,null);
});

test('native file reference history renders a safe filename while ordinary text stays unchanged',()=>{
  const f=fixture(),name='<script>alert(1)</script>.txt',reference=`[用户附件] ${JSON.stringify({name,path:'D:/private/attachments/fixture.txt'})}\n请根据我的消息读取该本地附件。`;
  const entry=f.renderItem({id:'reference-user',type:'userMessage',content:[{type:'text',text:'ordinary question'},{type:'text',text:reference}]});
  assert.equal(entry.node.textContent,'ordinary question📎 '+name);assert.equal(allNodes(entry.node).some(node=>node.tagName==='SCRIPT'),false);assert.equal(entry.node.textContent.includes('D:/private'),false);
  const malformed='[用户附件] {broken}\n请根据我的消息读取该本地附件。';assert.equal(f.nativeAttachmentReference(malformed),null);
});

test('attachment preview fallback hides native file markers in both header and sidebar, preserving explicit names',()=>{
  const f=fixture(),marker=`[用户附件] ${JSON.stringify({name:'notes.txt',path:'D:/private/attachments/file-notes.txt'})}\n请根据我的消息读取该本地附件。`,thread={id:'file-preview',preview:'Read the attached note '+marker,turns:[]};
  f.app.threads=[thread];f.renderThread(thread);
  assert.equal(f.document.getElementById('chat-title').textContent,'Read the attached note');assert.match(f.document.getElementById('chat-list').textContent,/Read the attached note/);assert.equal(f.document.getElementById('chat-list').textContent.includes('D:/private'),false);
  assert.equal(f.threadName({preview:marker}),'notes.txt');assert.equal(f.threadName({preview:'[用户附件] {"name":"truncated'}),'附件会话');
  assert.equal(f.threadName({name:'My explicit [用户附件] name',preview:marker}),'My explicit [用户附件] name');assert.equal(f.threadName({title:'Existing title',preview:marker}),'Existing title');
  const ordinary='Regular preview\nsecond line';assert.equal(f.threadName({preview:ordinary}),'Regular preview');assert.equal(f.threadName({preview:'x'.repeat(80)}),'x'.repeat(60));assert.equal(thread.preview,'Read the attached note '+marker);
});

test('replayed SSE deltas are not appended twice, including explicit reconnect', () => {
  const f = fixture(); f.app.thread = { id: 'fixture-thread' }; f.connectEvents();
  const message = { type: 'notification', method: 'item/agentMessage/delta', params: { threadId: 'fixture-thread', itemId: 'message', delta: 'fixture' } };
  f.sources[0].emit(message, 8); f.sources[0].emit(message, 8);
  f.connectEvents(); f.sources[1].emit(message, 8);
  assert.equal(f.app.items.get('message').text, 'fixture');
  f.sources[1].emit({ ...message, params: { ...message.params, delta: ' next' } }, 9);
  assert.equal(f.app.items.get('message').text, 'fixture next');
});

test('number/string approval IDs remain separate and resolved only closes matching dialog', () => {
  const f = fixture(), dialog = f.document.getElementById('request-dialog');
  f.notification(request(7)); f.notification(request('7'));
  assert.equal(f.app.requests.length, 2);
  assert.equal(f.app.openRequestId, 7);
  f.notification({ type: 'notification', method: 'serverRequest/resolved', params: { requestId: '7' } });
  assert.equal(f.app.requests.length, 1);
  assert.equal(dialog.open, true); assert.equal(dialog.closeCount, 0);
  f.notification({ type: 'notification', method: 'serverRequest/resolved', params: { requestId: 7 } });
  assert.equal(dialog.open, false); assert.equal(dialog.closeCount, 1);
});

test('approval_submitted of a queued request preserves active dialog', () => {
  const f = fixture(), dialog = f.document.getElementById('request-dialog');
  f.notification(request('a')); f.notification(request('b'));
  f.notification({ type: 'approval_submitted', id: 'b' });
  assert.equal(f.app.openRequestId, 'a'); assert.equal(dialog.closeCount, 0);
});

test('HTTP approval response does not close the next dialog already opened by SSE', async () => {
  const f = fixture(), response = deferred(), dialog = f.document.getElementById('request-dialog');
  f.setFetch(() => response.promise);
  const first = request(7); f.notification(first); f.notification(request(8));
  const pending = f.answerRequest(first, { decision: 'accept' });
  f.notification({ type: 'approval_submitted', id: 7 });
  assert.equal(f.app.openRequestId, 8);
  const count = dialog.closeCount;
  response.resolve({ ok: true, json: async () => ({ result: { accepted: true } }) });
  await pending;
  assert.equal(f.app.openRequestId, 8); assert.equal(dialog.open, true); assert.equal(dialog.closeCount, count);
  assert.equal(typeof f.calls[0].payload.id, 'number');
});

test('turn completion before turn/start HTTP response does not leave composer busy', async () => {
  const f = fixture(), response = deferred();
  f.app.thread = { id: 'fixture-thread' }; f.app.resumed.add('fixture-thread'); f.app.connected = true;
  f.document.getElementById('prompt').value = 'fixture input';
  f.setFetch((_url, payload) => payload?.method === 'turn/start' ? response.promise : { ok: true, json: async () => ({ result: { data: [] } }) });
  const pending = f.sendMessage();
  f.notification({ type: 'notification', method: 'turn/completed', params: { threadId: 'fixture-thread', turn: { id: 'fixture-turn', status: 'completed' } } });
  response.resolve({ ok: true, json: async () => ({ result: { turn: { id: 'fixture-turn', status: 'inProgress', items: [] } } }) });
  await pending;
  assert.equal(f.app.busy, false); assert.equal(f.app.turn, null);
  assert.equal(f.document.getElementById('stop-turn').hidden, true);
});

test('Markdown HTML and unsafe links remain text', () => {
  const f = fixture(), target = new Node();
  f.markdown(target, '<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[attack](javascript:alert)\n\n[safe](https://example.com/page)');
  const nodes = allNodes(target);
  assert.equal(nodes.some(node => ['SCRIPT', 'IMG', 'SVG', 'IFRAME'].includes(node.tagName)), false);
  assert.ok(target.textContent.includes('<script>alert(1)</script>'));
  assert.ok(target.textContent.includes('[attack](javascript:alert)'));
  const anchors = nodes.filter(node => node.tagName === 'A');
  assert.equal(anchors.length, 1); assert.equal(anchors[0].href, 'https://example.com/page');
  assert.equal(anchors[0].rel, 'noopener noreferrer');
});

test('modern approval buttons follow offered decisions exactly', async () => {
  const f = fixture();
  f.setFetch(() => ({ ok: true, json: async () => ({ result: { accepted: true } }) }));
  f.notification(request('cancel-only', ['cancel']));
  let buttons = f.document.getElementById('request-actions').children;
  assert.equal(buttons.length, 1); assert.equal(buttons[0].textContent, '停止本次回复');
  await buttons[0].trigger('click');
  assert.equal(f.calls[0].payload.result.decision, 'cancel');
  f.notification(request('persistent', [{ acceptWithExecpolicyAmendment: { execpolicy_amendment: ['fixture'] } }]));
  buttons = f.document.getElementById('request-actions').children;
  assert.equal(buttons.length, 0);
  assert.ok(f.document.getElementById('request-description').textContent.includes('永久规则'));
});

test('new backend instance resets replay sequence and resumed-thread cache', () => {
  const f = fixture(); f.app.thread = { id: 'fixture-thread' };
  f.notification({ type: 'connection', instanceId: 'first', connected: false, pendingRequests: [] });
  f.app.lastEvent = 40; f.app.resumed.add('fixture-thread');
  f.connectEvents();
  f.sources[0].emit({ type: 'connection', instanceId: 'second', connected: false, pendingRequests: [] }, 0);
  assert.equal(f.app.lastEvent, 0); assert.equal(f.app.resumed.size, 0);
  f.sources[0].emit({ type: 'notification', method: 'item/agentMessage/delta', params: { threadId: 'fixture-thread', itemId: 'after-restart', delta: 'fresh' } }, 1);
  assert.equal(f.app.items.get('after-restart').text, 'fresh');
});

test('reconnect hydration reads current history without claiming its writer', async () => {
  const f = fixture(); f.app.thread = { id: 'fixture-thread' }; f.app.connected = true;
  await f.hydrate();
  const methods = f.calls.map(call => call.payload?.method);
  assert.ok(methods.includes('thread/read')); assert.equal(methods.includes('thread/resume'), false);
  assert.equal(f.app.resumed.has('fixture-thread'), false);
});

test('reading an externally active thread neither resumes nor exposes its interrupt control', async () => {
  const f = fixture();
  f.setFetch((_url, payload) => ({ ok: true, json: async () => ({ result: payload.method==='thread/turns/list'?{data:[{id:'external-turn',status:'inProgress',items:[]}]}:{ thread: { id: payload.params.threadId, turns: [] } } }) }));
  await f.openThread('external-thread');
  assert.deepEqual(f.calls.map(call => call.payload.method), ['thread/read','thread/turns/list']);
  assert.equal(f.app.thread.id, 'external-thread'); assert.equal(f.app.busy, false); assert.equal(f.app.turn, null);
  assert.equal(f.document.getElementById('stop-turn').hidden, true);
  f.notification({ type: 'notification', method: 'turn/started', params: { threadId: 'external-thread', turn: { id: 'external-turn' } } });
  assert.equal(f.app.busy, false); assert.equal(f.document.getElementById('stop-turn').hidden, true);
  await f.stopTurn();
  assert.equal(f.calls.length, 2);
});

test('structured writer conflict keeps native code and shows no raw thread error', async () => {
  const f = fixture();
  f.setFetch(() => ({ ok: false, json: async () => ({ error: { code: -32600, kind: 'thread_in_use', message: '中文说明' } }) }));
  await assert.rejects(f.rpc('thread/resume', { threadId: 'external-thread' }), error => {
    assert.equal(error.code, -32600); assert.equal(error.kind, 'thread_in_use');
    assert.ok(error.message.includes('另一个 Codex 窗口')); return true;
  });
});

test('legacy active-writer rejection preserves draft and attachments with no retry or new thread', async () => {
  const f = fixture(), prompt = f.document.getElementById('prompt');
  f.app.connected = true; f.app.thread = { id: 'external-thread' };
  prompt.value = 'my unsent draft'; f.app.attachments.push({ name: 'draft.png', url: 'data:image/png;base64,fixture' });
  f.setFetch(() => ({ ok: false, json: async () => ({ error: { code: -32600, message: 'thread external-thread already has an active writer' } }) }));
  await f.sendMessage();
  assert.equal(f.app.thread.id, 'external-thread'); assert.equal(f.app.writerConflict, 'external-thread');
  assert.equal(f.app.resumed.has('external-thread'), false); assert.equal(f.app.loading, false); assert.equal(f.app.busy, false);
  assert.equal(prompt.value, 'my unsent draft'); assert.equal(f.app.attachments.length, 1);
  const caption = f.document.getElementById('composer-caption');
  assert.ok(caption.textContent.includes('另一个 Codex 窗口'));
  assert.equal(caption.textContent.includes('active writer'), false); assert.equal(caption.textContent.includes('external-thread'), false);
  assert.deepEqual(caption.children.map(button => button.textContent), ['另开新会话', '重试连接']);
  await f.sendMessage();
  assert.deepEqual(f.calls.map(call => call.payload.method), ['thread/resume']);
});

test('new-thread conflict action only changes selection and retains the unsent input', async () => {
  const f = fixture(), prompt = f.document.getElementById('prompt');
  f.app.connected = true; f.app.thread = { id: 'external-thread' }; prompt.value = 'draft';
  f.app.attachments.push({ name: 'draft.png', url: 'data:image/png;base64,fixture' });
  f.setFetch(() => ({ ok: false, json: async () => ({ error: { code: -32600, kind: 'thread_in_use', message: 'occupied' } }) }));
  await f.sendMessage();
  await f.document.getElementById('composer-caption').children[0].trigger('click');
  assert.equal(f.app.thread, null); assert.equal(f.app.writerConflict, null);
  assert.equal(prompt.value, 'draft'); assert.equal(f.app.attachments.length, 1);
  assert.equal(f.storage.has('dragon-atelier-last-thread'), false);
  assert.deepEqual(f.calls.map(call => call.payload.method), ['thread/resume']);
});

test('explicit reconnect after writer conflict never resends the preserved draft', async () => {
  const f = fixture(), prompt = f.document.getElementById('prompt');
  f.app.connected = true; f.app.thread = { id: 'external-thread' }; prompt.value = 'draft';
  f.app.attachments.push({ name: 'draft.png', url: 'data:image/png;base64,fixture' });
  f.setFetch(() => ({ ok: false, json: async () => ({ error: { code: -32600, kind: 'thread_in_use', message: 'occupied' } }) }));
  await f.sendMessage();
  f.setFetch((_url, payload) => ({ ok: true, json: async () => ({ result: payload.method === 'thread/resume' ? { thread: { id: 'external-thread', turns: [] } } : payload.method === 'turn/start' ? { turn: { id: 'draft-turn', status: 'inProgress', items: [] } } : { data: [] } }) }));
  await f.document.getElementById('composer-caption').children[1].trigger('click');
  assert.equal(f.app.writerConflict, null); assert.ok(f.app.resumed.has('external-thread'));
  assert.equal(prompt.value, 'draft'); assert.equal(f.app.attachments.length, 1);
  assert.ok(f.document.getElementById('composer-caption').textContent.includes('尚未发送'));
  assert.deepEqual(f.calls.map(call => call.payload.method), ['thread/resume', 'thread/resume']);
  await f.sendMessage();
  assert.equal(f.calls.filter(call => call.payload.method === 'turn/start').length, 1);
  assert.equal(prompt.value, ''); assert.equal(f.app.attachments.length, 0);
});

test('owned-thread snapshot restores running controls without a second resume', async () => {
  const f = fixture();
  f.app.connected = true;
  f.notification({ type: 'connection', instanceId: 'bridge', connected: true, pendingRequests: [], ownedThreadIds: ['owned-thread'] });
  f.setFetch((_url, payload) => ({ ok: true, json: async () => ({ result: payload.method==='thread/turns/list'?{data:[{id:'owned-turn',status:'inProgress',items:[]}]}:{ thread: { id: payload.params.threadId, turns: [] } } }) }));
  await f.openThread('owned-thread');
  assert.equal(f.app.busy, true); assert.equal(f.app.turn, 'owned-turn');
  assert.equal(f.document.getElementById('stop-turn').hidden, false);
  assert.deepEqual(f.calls.map(call => call.payload.method), ['thread/read','thread/turns/list']);
  await f.stopTurn();
  assert.deepEqual(f.calls.map(call => call.payload.method), ['thread/read','thread/turns/list','turn/interrupt']);
});

test('sending an owned history does not resume and preserves explicit model selection', async () => {
  const f = fixture(); f.app.connected = true; f.app.thread = { id: 'owned-thread', turns: [] }; f.app.resumed.add('owned-thread');
  f.document.getElementById('prompt').value = 'draft'; f.document.getElementById('model').value = 'chosen-model'; f.document.getElementById('effort').value = 'high';
  f.setFetch((_url, payload) => ({ ok: true, json: async () => ({ result: payload.method === 'turn/start' ? { turn: { id: 'new-turn', status: 'inProgress', items: [] } } : { data: [] } }) }));
  await f.sendMessage();
  assert.equal(f.calls.some(call => call.payload.method === 'thread/resume'), false);
  const send = f.calls.find(call => call.payload.method === 'turn/start');
  assert.equal(send.payload.params.model, 'chosen-model'); assert.equal(send.payload.params.effort, 'high');
});

test('a resumed existing turn keeps the draft instead of starting a second turn', async () => {
  const f = fixture(); f.app.connected = true; f.app.thread = { id: 'owned-thread', turns: [] };
  f.document.getElementById('prompt').value = 'draft';
  f.setFetch(() => ({ ok: true, json: async () => ({ result: { thread: { id: 'owned-thread', turns: [{ id: 'ongoing-turn', status: 'inProgress', items: [] }] } } }) }));
  await f.sendMessage();
  assert.deepEqual(f.calls.map(call => call.payload.method), ['thread/resume']);
  assert.equal(f.app.busy, true); assert.equal(f.app.turn, 'ongoing-turn');
  assert.equal(f.document.getElementById('prompt').value, 'draft');
});

test('closed and archived notifications revoke writer controls for selected and background threads', async () => {
  const f = fixture(); f.app.thread = { id: 'selected-thread' }; f.app.resumed.add('selected-thread'); f.app.resumed.add('background-thread');
  f.document.getElementById('prompt').value = 'draft';
  f.notification({ type: 'notification', method: 'turn/started', params: { threadId: 'selected-thread', turn: { id: 'selected-turn' } } });
  f.notification({ type: 'notification', method: 'thread/closed', params: { threadId: 'background-thread' } });
  assert.equal(f.app.resumed.has('background-thread'), false); assert.equal(f.app.busy, true);
  f.notification({ type: 'notification', method: 'thread/archived', params: { threadId: 'selected-thread' } });
  assert.equal(f.app.resumed.has('selected-thread'), false); assert.equal(f.app.busy, false); assert.equal(f.app.turn, null);
  assert.equal(f.document.getElementById('stop-turn').hidden, true); assert.equal(f.document.getElementById('prompt').value, 'draft');
  await f.stopTurn(); assert.equal(f.calls.length, 0);
});

function attachDraft(f) {
  f.document.getElementById('prompt').value = 'unsent draft';
  f.app.attachments.push({ name: 'draft.png', url: 'data:image/png;base64,fixture' });
}
function assertDraft(f) {
  assert.equal(f.document.getElementById('prompt').value, 'unsent draft');
  assert.equal(f.app.attachments.length, 1);
  assert.equal(f.app.attachments[0].name, 'draft.png');
}
function status(connected, overrides = {}) {
  return { appId: 'codex-dragon-client', instanceId: 'bridge', connected, ownedThreadIds: [], pendingRequests: [], ...overrides };
}

test('a fresh clone uses the backend workspace on another drive without overwriting a later preference',async()=>{
  const f=fixture(),clone='E:\\Projects\\codex-dragon-decorate';
  assert.equal(f.prefs.cwd,'');
  f.notification({type:'connection',...status(false,{defaultCwd:clone})});
  assert.equal(f.prefs.cwd,clone);assert.equal(f.document.getElementById('workspace-label').textContent,clone);
  assert.equal(JSON.parse(f.storage.get('dragon-atelier-settings')).cwd,clone);
  f.app.connected=true;f.document.getElementById('prompt').value='new clone';
  f.setFetch((_url,payload)=>resultResponse(payload.method==='thread/start'?{thread:{id:'clone-thread'}}:payload.method==='turn/start'?{turn:{id:'clone-turn',status:'completed',items:[]}}:{data:[]}));
  await f.sendMessage();assert.equal(f.calls.find(call=>call.payload?.method==='thread/start').payload.params.cwd,clone);
  f.prefs.cwd='F:\\MyWorkspace';f.notification({type:'connection',...status(false,{defaultCwd:'E:\\MovedClone'})});
  assert.equal(f.prefs.cwd,'F:\\MyWorkspace');
});

test('saved workspace is preserved and only the legacy automatic default migrates',()=>{
  const chosen=fixture({savedSettings:{cwd:'F:\\Chosen',dark:true}});
  chosen.notification({type:'connection',...status(false,{defaultCwd:'E:\\Clone'})});
  assert.equal(chosen.prefs.cwd,'F:\\Chosen');assert.equal(chosen.prefs.dark,true);
  const legacy=fixture({savedSettings:{cwd:'D:\\codexwork\\c',pet:false}});
  legacy.notification({type:'connection',...status(false,{defaultCwd:'E:\\Clone'})});
  assert.equal(legacy.prefs.cwd,'E:\\Clone');assert.equal(legacy.prefs.pet,false);
  const invalid=fixture();invalid.notification({type:'connection',...status(false,{defaultCwd:'relative/directory'})});
  assert.equal(invalid.prefs.cwd,'');
  invalid.notification({type:'connection',...status(false,{appId:'other-app',defaultCwd:'E:\\Other'})});
  assert.equal(invalid.prefs.cwd,'');
});

test('a backend without a default workspace receives no empty cwd override',async()=>{
  const f=fixture();f.app.connected=true;f.document.getElementById('prompt').value='native default';
  f.setFetch((_url,payload)=>resultResponse(payload.method==='thread/start'?{thread:{id:'native-thread'}}:payload.method==='turn/start'?{turn:{id:'native-turn',status:'completed',items:[]}}:{data:[]}));
  await f.sendMessage();assert.equal(Object.hasOwn(f.calls.find(call=>call.payload?.method==='thread/start').payload.params,'cwd'),false);
});
function readOnlyResponse(payload) {
  const result = ['thread/list','model/list','thread/turns/list'].includes(payload?.method) ? { data: [] }
    : payload?.method === 'thread/read' ? { thread: { id: payload.params.threadId, turns: [] } }
    : { account: { type: 'chatgpt', planType: 'fixture' } };
  return { ok: true, json: async () => ({ result }) };
}

test('missing HTTP backend gives the desktop shortcut instruction and preserves draft and image', async () => {
  const f = fixture(); attachDraft(f);
  f.app.connected = true; f.app.resumed.add('previous-thread');
  f.setFetch(() => { throw Error('fixture transport unavailable'); });
  await f.reconnect();
  assert.equal(f.app.connected, false); assert.equal(f.app.resumed.size, 0);
  assertDraft(f);
  assert.ok(f.document.getElementById('composer-caption').textContent.includes('后台未运行'));
  assert.ok(f.document.getElementById('composer-caption').textContent.includes('Codex 龙娘工坊'));
  assert.equal(f.document.getElementById('reconnect').hidden, false);
  assert.equal(f.document.getElementById('reconnect').disabled, false);
  assert.deepEqual(f.calls.map(call => call.url), ['/api/status']);
});

test('explicit dead-child reconnect posts once and reads history without resume or send', async () => {
  const f = fixture(), response = deferred(); attachDraft(f);
  f.app.thread = { id: 'previous-thread', turns: [] };
  f.setFetch((url, payload) => url === '/api/status' ? { ok: true, json: async () => status(false) }
    : url === '/api/reconnect' ? response.promise : readOnlyResponse(payload));
  const pending = f.reconnect();
  await Promise.resolve(); await Promise.resolve();
  await f.reconnect();
  response.resolve({ ok: true, json: async () => ({ result: status(true) }) });
  await pending;
  assert.equal(f.calls.filter(call => call.url === '/api/reconnect').length, 1);
  assert.equal(f.calls.find(call => call.url === '/api/reconnect').method, 'POST');
  assert.deepEqual(f.calls.find(call => call.url === '/api/reconnect').payload, {});
  assert.ok(f.calls.some(call => call.payload?.method === 'thread/read'));
  assert.equal(f.calls.some(call => ['thread/resume', 'thread/start', 'turn/start', 'turn/interrupt'].includes(call.payload?.method)), false);
  assert.equal(f.app.connected, true); assert.equal(f.app.resumed.size, 0);
  assertDraft(f);
  assert.ok(f.document.getElementById('composer-caption').textContent.includes('尚未发送'));
});

test('reconnecting a healthy backend refreshes read-only state without restarting it', async () => {
  const f = fixture(); attachDraft(f); f.app.thread = { id: 'previous-thread', turns: [] };
  f.setFetch((url, payload) => url === '/api/status' ? { ok: true, json: async () => status(true) } : readOnlyResponse(payload));
  await f.reconnect();
  assert.equal(f.calls.some(call => call.url === '/api/reconnect'), false);
  assert.ok(f.calls.some(call => call.payload?.method === 'thread/read'));
  assert.equal(f.calls.some(call => ['thread/resume', 'turn/start'].includes(call.payload?.method)), false);
  assertDraft(f);
});

test('connecting then ready events clear stale ownership and approval controls while preserving draft', async () => {
  const f = fixture(); attachDraft(f);
  f.app.connected = true; f.app.instanceId = 'bridge'; f.app.thread = { id: 'previous-thread', turns: [] };
  f.app.resumed.add('previous-thread');
  f.notification({ type: 'notification', method: 'turn/started', params: { threadId: 'previous-thread', turn: { id: 'old-turn' } } });
  f.notification(request('old-request'));
  assert.equal(f.document.getElementById('request-dialog').open, true);
  f.notification({ type: 'notification', method: 'bridge/connection', params: { ...status(false), connection: 'connecting' } });
  assert.equal(f.app.connected, false); assert.equal(f.app.busy, false); assert.equal(f.app.turn, null);
  assert.equal(f.app.resumed.size, 0); assert.equal(f.app.requests.length, 0);
  assert.equal(f.document.getElementById('request-dialog').open, false);
  assert.equal(f.document.getElementById('stop-turn').hidden, true);
  assert.ok(f.document.getElementById('composer-caption').textContent.includes('正在连接'));
  assertDraft(f);
  f.notification({ type: 'notification', method: 'bridge/connection', params: { ...status(true), connection: 'ready' } });
  await f.hydrate();
  assert.equal(f.app.connected, true); assert.equal(f.app.resumed.size, 0); assertDraft(f);
  assert.equal(f.calls.some(call => ['thread/resume', 'turn/start'].includes(call.payload?.method)), false);
});

test('late turn response after disconnect cannot clear draft or restore turn controls', async () => {
  const f = fixture(), response = deferred(); attachDraft(f);
  f.app.connected = true; f.app.instanceId = 'bridge'; f.app.thread = { id: 'owned-thread', turns: [] }; f.app.resumed.add('owned-thread');
  f.setFetch((_url, payload) => payload?.method === 'turn/start' ? response.promise : readOnlyResponse(payload));
  const pending = f.sendMessage();
  f.notification({ type: 'connection', ...status(false) });
  response.resolve({ ok: true, json: async () => ({ result: { turn: { id: 'late-turn', status: 'inProgress', items: [] } } }) });
  await pending;
  assert.equal(f.app.connected, false); assert.equal(f.app.resumed.size, 0); assert.equal(f.app.busy, false); assert.equal(f.app.turn, null);
  assert.equal(f.document.getElementById('stop-turn').hidden, true); assertDraft(f);
});

test('late resume response after disconnect and ready cannot reacquire ownership or send', async () => {
  const f = fixture(), response = deferred(); attachDraft(f);
  f.app.connected = true; f.app.instanceId = 'bridge'; f.app.thread = { id: 'previous-thread', turns: [] };
  f.setFetch((_url, payload) => payload?.method === 'thread/resume' ? response.promise : readOnlyResponse(payload));
  const pending = f.sendMessage(), previousEpoch = f.app.connectionEpoch;
  f.notification({ type: 'connection', ...status(false) });
  f.notification({ type: 'connection', ...status(true) });
  response.resolve({ ok: true, json: async () => ({ result: { thread: { id: 'previous-thread', turns: [] } } }) });
  await pending; await f.hydrate();
  assert.ok(f.app.connectionEpoch > previousEpoch); assert.equal(f.app.resumed.size, 0);
  assert.equal(f.calls.filter(call => call.payload?.method === 'thread/resume').length, 1);
  assert.equal(f.calls.some(call => call.payload?.method === 'turn/start'), false);
  assert.equal(f.app.busy, false); assertDraft(f);
});

test('late explicit writer retry cannot restore ownership after disconnection', async () => {
  const f = fixture(), response = deferred(); attachDraft(f);
  f.app.connected = true; f.app.instanceId = 'bridge'; f.app.thread = { id: 'previous-thread', turns: [] }; f.app.writerConflict = 'previous-thread';
  f.setFetch(() => response.promise);
  const pending = f.retryThreadWriter();
  f.notification({ type: 'connection', ...status(false) });
  response.resolve({ ok: true, json: async () => ({ result: { thread: { id: 'previous-thread', turns: [] } } }) });
  await pending;
  assert.equal(f.app.resumed.size, 0); assert.equal(f.app.writerConflict, null); assert.equal(f.app.busy, false); assertDraft(f);
  assert.equal(f.calls.some(call => call.payload?.method === 'turn/start'), false);
});

test('backend identity mismatch refuses reconnect POST and preserves draft', async () => {
  const f = fixture(); attachDraft(f);
  f.setFetch(() => ({ ok: true, json: async () => status(false, { appId: 'other-app' }) }));
  await f.reconnect();
  assert.equal(f.app.connected, false); assertDraft(f);
  assert.deepEqual(f.calls.map(call => call.url), ['/api/status']);
  assert.ok(f.document.getElementById('composer-caption').textContent.includes('不是龙娘工坊'));
});

const resultResponse=result=>({ok:true,json:async()=>({result})});
const historyTurn=(id,text)=>({id,status:'completed',items:[{id:'item-'+id,type:'agentMessage',text}]});

test('send feedback is immediate, repeated sends are blocked and early SSE replaces the pending bubble once',async()=>{
  const f=fixture(),response=deferred();f.app.connected=true;f.app.thread={id:'owned'};f.app.resumed.add('owned');
  f.document.getElementById('prompt').value='draft';
  f.setFetch((_url,payload)=>payload.method==='turn/start'?response.promise:resultResponse({data:[]}));
  const pending=f.sendMessage();
  assert.equal(f.document.getElementById('send').disabled,true);
  assert.ok(f.document.getElementById('composer-caption').textContent.includes('正在发送'));
  assert.ok(f.document.getElementById('conversation').textContent.includes('draft'));
  assert.equal(f.document.getElementById('prompt').value,'draft');
  await f.sendMessage();assert.equal(f.calls.length,1);
  const item={id:'user',type:'userMessage',content:[{type:'text',text:'draft'}]};
  f.notification({type:'notification',method:'item/started',params:{threadId:'owned',item}});
  response.resolve(resultResponse({turn:{id:'turn',status:'inProgress',items:[item]}}));await pending;
  assert.equal(f.app.pendingMessage,null);assert.equal(f.app.items.size,1);
  assert.equal(f.document.getElementById('conversation').children.length,1);
  assert.equal(f.document.getElementById('prompt').value,'');
  assert.equal(f.document.getElementById('send').disabled,false);
});

test('failed send removes only the pending bubble and keeps the draft and image',async()=>{
  const f=fixture();attachDraft(f);f.app.connected=true;f.app.thread={id:'owned'};f.app.resumed.add('owned');
  f.setFetch(()=>({ok:false,json:async()=>({error:{message:'fixture failure'}})}));
  await f.sendMessage();assertDraft(f);assert.equal(f.app.pendingMessage,null);
  assert.equal(f.document.getElementById('conversation').children.length,0);
  assert.equal(f.document.getElementById('send').disabled,false);
});

test('send acknowledgement preserves a next draft and images added while waiting',async()=>{
  const f=fixture(),response=deferred();attachDraft(f);f.app.connected=true;f.app.thread={id:'owned'};f.app.resumed.add('owned');
  f.setFetch((_url,payload)=>payload.method==='turn/start'?response.promise:resultResponse({data:[]}));
  const pending=f.sendMessage(),nextImage={name:'next.png',url:'data:image/png;base64,next'};
  f.document.getElementById('prompt').value='next draft';f.app.attachments.push(nextImage);
  response.resolve(resultResponse({turn:{id:'turn',status:'inProgress',items:[]}}));await pending;
  assert.equal(f.document.getElementById('prompt').value,'next draft');
  assert.equal(f.app.attachments.length,1);assert.equal(f.app.attachments[0],nextImage);
  assert.equal(f.app.pendingMessage.label.textContent,'已发送');
  f.notification({type:'notification',method:'item/started',params:{threadId:'owned',item:{id:'accepted-user',type:'userMessage',content:[{type:'text',text:'unsent draft'}]}}});
  assert.equal(f.app.pendingMessage,null);assert.equal(f.app.items.size,1);
});

test('resume for send excludes history, preserves the loaded page and trusts live idle state',async()=>{
  const f=fixture();f.app.connected=true;const loaded=[{id:'stale-active',status:'inProgress',items:[]}];
  f.app.thread={id:'history',turns:loaded};f.document.getElementById('prompt').value='draft';
  f.setFetch((_url,payload)=>resultResponse(payload.method==='thread/resume'?{thread:{id:'history',turns:[],status:{type:'idle'}}}:payload.method==='turn/start'?{turn:{id:'new',status:'inProgress',items:[]}}:{data:[]}));
  await f.sendMessage();assert.equal(f.calls[0].payload.params.excludeTurns,true);
  assert.equal(f.app.thread.turns,loaded);assert.equal(f.app.turn,'new');
  assert.equal(f.calls.some(call=>call.payload.method==='thread/read'),false);
  assert.equal(f.calls.some(call=>call.payload.method==='thread/turns/list'),false);
});

test('active metadata resume reads one summary turn and preserves the unsent draft',async()=>{
  const f=fixture();f.app.connected=true;f.app.thread={id:'history',turns:[]};f.document.getElementById('prompt').value='draft';
  f.setFetch((_url,payload)=>resultResponse(payload.method==='thread/resume'?{thread:{id:'history',turns:[],status:{type:'active',activeFlags:[]}}}:{data:[{id:'running',status:'inProgress',items:[]}]}));
  await f.sendMessage();assert.deepEqual(f.calls.map(call=>call.payload.method),['thread/resume','thread/turns/list']);
  assert.equal(f.calls[1].payload.params.itemsView,'summary');assert.equal(f.calls[1].payload.params.limit,1);
  assert.equal(f.app.turn,'running');assert.equal(f.document.getElementById('prompt').value,'draft');
  assert.equal(f.app.pendingMessage,null);
});

test('automatic history uses the index and explicit refresh can repair imported history',async()=>{
  const f=fixture();await f.loadThreads();await f.loadThreads(false,true);
  assert.equal(f.calls[0].payload.params.useStateDbOnly,true);assert.equal(f.calls[1].payload.params.useStateDbOnly,false);
});

test('pet hover plays a wave, survives pointer motion and restores the latest work state',()=>{
  const f=fixture({reducedMotion:false});f.app.petReady=true;f.setPetState('idle');
  f.petPointerEnter({pointerType:'mouse'});const dwell=[...f.timers.values()].find(timer=>timer.delay===220);assert.ok(dwell);dwell.callback();
  assert.equal(f.app.petState,'waving');assert.equal(f.app.petHover,true);
  const animation=[...f.intervals.values()].find(timer=>timer.delay===170);assert.ok(animation);animation.callback();
  const position=f.document.getElementById('pet-sprite').style.backgroundPosition;
  f.petPointerMove({clientX:50,clientY:50});assert.equal(f.document.getElementById('pet-sprite').style.backgroundPosition,position);
  f.setPetState('running');assert.equal(f.app.petState,'waving');assert.equal(f.intervals.size,1);
  f.petPointerLeave();assert.equal(f.app.petState,'running');assert.equal(f.app.petHover,false);assert.equal(f.intervals.size,1);
});

test('brief pet crossing and touch do not trigger a wave and reduced motion stays static',()=>{
  const f=fixture();f.app.petReady=true;f.setPetState('idle');
  f.petPointerEnter({pointerType:'mouse'});f.petPointerLeave();assert.equal(f.timers.size,0);assert.equal(f.app.petHover,false);
  f.petPointerEnter({pointerType:'touch'});assert.equal(f.app.petPointerInside,false);assert.equal(f.timers.size,0);
  f.petPointerEnter({pointerType:'mouse'});[...f.timers.values()].find(timer=>timer.delay===220).callback();
  assert.equal(f.app.petState,'waving');assert.equal(f.intervals.size,0);
});

test('history list includes desktop sources, searches beyond loaded titles and never lowercases the query',async()=>{
  const f=fixture();f.document.getElementById('chat-search').value='MiXeD 龙娘';
  f.setFetch((_url,payload)=>resultResponse({data:[{id:'old-match',name:'MiXeD 龙娘 elsewhere'}],nextCursor:'search-page-2'}));
  await f.loadThreads();
  const params=f.calls[0].payload.params;
  assert.deepEqual(Array.from(params.sourceKinds),['cli','vscode','appServer','exec','unknown']);
  assert.equal(params.searchTerm,'MiXeD 龙娘');assert.equal(params.archived,false);assert.equal('cwd' in params,false);
  assert.equal(f.app.threads[0].id,'old-match');assert.equal(f.app.nextCursor,'search-page-2');
  assert.ok(f.document.getElementById('history-status').textContent.includes('会话名称'));
});

test('search debounces and invalidates an older list even before the next search request starts',async()=>{
  const f=fixture(),pending=deferred();f.setFetch(()=>pending.promise);
  const old=f.loadThreads();
  f.document.getElementById('chat-search').value='first';f.searchThreads();
  f.document.getElementById('chat-search').value='new';f.searchThreads();
  pending.resolve(resultResponse({data:[{id:'old'}],nextCursor:'old-cursor'}));await old;
  assert.equal(f.app.threads.length,0);assert.equal(f.app.nextCursor,null);
  const scheduled=[...f.timers.values()].filter(timer=>timer.delay===250);assert.equal(scheduled.length,1);
  f.setFetch(()=>resultResponse({data:[{id:'new'}],nextCursor:null}));await scheduled[0].callback();
  assert.equal(f.calls.at(-1).payload.params.searchTerm,'new');assert.equal(f.app.threads[0].id,'new');
});

test('new search and refresh reject stale results and list pagination suppresses duplicate cursor requests',async()=>{
  const f=fixture(),page=deferred();
  f.setFetch(()=>resultResponse({data:[{id:'a',name:'original'}],nextCursor:'cursor-1'}));await f.loadThreads();
  f.setFetch(()=>page.promise);const first=f.loadThreads(true);await f.loadThreads(true);
  assert.equal(f.calls.filter(call=>call.payload.params.cursor==='cursor-1').length,1);
  page.resolve(resultResponse({data:[{id:'a',name:'updated'},{id:'b'}],nextCursor:'cursor-2'}));await first;
  assert.deepEqual(Array.from(f.app.threads,thread=>thread.id),['a','b']);assert.equal(f.app.threads[0].name,'updated');assert.equal(f.app.nextCursor,'cursor-2');
  const stale=deferred();f.setFetch(()=>stale.promise);const refresh=f.loadThreads();
  f.document.getElementById('chat-search').value='current';f.setFetch(()=>resultResponse({data:[{id:'current'}],nextCursor:null}));await f.loadThreads();
  stale.resolve(resultResponse({data:[{id:'stale'}],nextCursor:'stale'}));await refresh;
  assert.deepEqual(Array.from(f.app.threads,thread=>thread.id),['current']);assert.equal(f.app.nextCursor,null);
});

test('history empty and list failure are visible without touching the draft',async()=>{
  const f=fixture();attachDraft(f);await f.loadThreads();
  assert.ok(f.document.getElementById('history-status').textContent.includes('还没有'));
  f.setFetch(()=>({ok:false,json:async()=>({error:{code:-32000,message:'fixture failure'}})}));
  await assert.rejects(f.loadThreads());assert.ok(f.document.getElementById('history-status').textContent.includes('加载失败'));
  assert.equal(f.app.threadsLoading,false);assertDraft(f);
});

test('history reads full pages newest first and renders chronological messages without claiming a writer',async()=>{
  const f=fixture();attachDraft(f);f.setFetch((_url,payload)=>resultResponse(payload.method==='thread/read'?{thread:{id:'history',cwd:'D:\\actual-history',turns:[]}}:{data:[historyTurn('new','new message'),historyTurn('old','old message')],nextCursor:'older-page'}));
  await f.openThread('history');
  assert.deepEqual(f.calls.map(call=>call.payload.method),['thread/read','thread/turns/list']);
  assert.equal(f.calls[0].payload.params.includeTurns,false);
  assert.equal(f.calls[1].payload.params.itemsView,'full');assert.equal(f.calls[1].payload.params.sortDirection,'desc');assert.equal(f.calls[1].payload.params.limit,5);
  assert.deepEqual(Array.from(f.app.thread.turns,turn=>turn.id),['old','new']);
  const text=f.document.getElementById('conversation').textContent;assert.ok(text.indexOf('old message')<text.indexOf('new message'));
  assert.equal(f.document.getElementById('workspace-label').textContent,'D:\\actual-history');assert.equal(f.app.historyCursor,'older-page');assert.equal(f.app.resumed.size,0);assertDraft(f);
});

test('earlier pages prepend chronologically, deduplicate turns and preserve existing tool card and scroll',async()=>{
  const f=fixture(),tool={id:'tool-new',type:'commandExecution',command:'fixture',aggregatedOutput:'done',status:'completed'};
  f.setFetch((_url,payload)=>resultResponse(payload.method==='thread/read'?{thread:{id:'history'}}:payload.params.cursor?{data:[historyTurn('new','duplicate'),historyTurn('mid','middle'),historyTurn('old','earliest')],nextCursor:null}:{data:[{id:'new',status:'completed',items:[tool]}],nextCursor:'older-page'}));
  await f.openThread('history');attachDraft(f);const node=f.app.items.get('tool-new').node;node.open=true;
  const conversation=f.document.getElementById('conversation');Object.defineProperty(conversation,'scrollHeight',{get:()=>600+100*conversation.children.length,configurable:true});conversation.scrollTop=40;
  await f.loadEarlier();
  assert.deepEqual(Array.from(f.app.thread.turns,turn=>turn.id),['old','mid','new']);assert.equal(f.app.items.get('tool-new').node,node);assert.equal(node.open,true);
  assert.equal(conversation.children.at(-1),node);assert.equal(conversation.scrollTop,240);assert.equal(f.app.historyCursor,null);assert.equal(f.document.getElementById('load-earlier').hidden,true);assertDraft(f);
  assert.equal(f.calls.some(call=>call.payload.method==='thread/resume'),false);
});

test('superseded history, earlier-page and disconnected reads never overwrite the selected conversation',async()=>{
  const f=fixture(),slow=deferred();f.app.connected=true;
  f.setFetch((_url,payload)=>payload.params.threadId==='slow'?slow.promise:resultResponse(payload.method==='thread/read'?{thread:{id:payload.params.threadId}}:{data:[historyTurn('fast-turn','current')],nextCursor:'older'}));
  const pending=f.openThread('slow');await f.openThread('fast');slow.resolve(resultResponse({thread:{id:'slow'}}));await pending;
  assert.equal(f.app.thread.id,'fast');assert.equal(f.storage.get('dragon-atelier-last-thread'),'fast');
  const older=deferred();f.setFetch(()=>older.promise);const pendingOlder=f.loadEarlier();f.newChat();
  older.resolve(resultResponse({data:[historyTurn('stale-old','old')]}));await pendingOlder;assert.equal(f.app.thread,null);assert.equal(f.app.items.size,0);
  const disconnected=deferred();f.setFetch(()=>disconnected.promise);const read=f.openThread('after-disconnect');f.notification({type:'connection',...status(false)});
  disconnected.resolve(resultResponse({thread:{id:'after-disconnect'}}));await read;assert.equal(f.app.thread,null);assert.equal(f.app.historyLoading,false);
});

test('only an unsupported history method falls back to the legacy full read',async()=>{
  const f=fixture();f.setFetch((_url,payload)=>payload.method==='thread/turns/list'?{ok:false,json:async()=>({error:{code:-32601,message:'method not found'}})}:resultResponse({thread:{id:'legacy',turns:payload.params.includeTurns?[historyTurn('old','legacy message')]:[]}}));
  await f.openThread('legacy');assert.deepEqual(f.calls.map(call=>call.payload.method),['thread/read','thread/turns/list','thread/read']);assert.equal(f.calls.at(-1).payload.params.includeTurns,true);assert.ok(f.document.getElementById('conversation').textContent.includes('legacy message'));
  const other=fixture();other.setFetch((_url,payload)=>payload.method==='thread/turns/list'?{ok:false,json:async()=>({error:{code:-32000,message:'storage temporarily unavailable'}})}:resultResponse({thread:{id:'error'}}));
  await assert.rejects(other.openThread('error'));assert.equal(other.calls.length,2);assert.equal(other.app.thread,null);assert.ok(other.document.getElementById('history-message-status').textContent.includes('加载失败'));
});

test('historical tools show command output and search query, keep reasoning hidden and sanitize result keys',()=>{
  const f=fixture();f.renderItem({id:'command',type:'commandExecution',command:'fixture command',aggregatedOutput:'fixture output',status:'completed'});
  assert.ok(f.app.items.get('command').body.textContent.includes('fixture command'));assert.ok(f.app.items.get('command').body.textContent.includes('fixture output'));
  f.renderItem({id:'search',type:'webSearch',query:'historical query'});assert.equal(f.app.items.get('search').body.textContent,'historical query');
  f.renderItem({id:'mcp',type:'mcpToolCall',server:'fixture',tool:'read',result:{content:[{type:'text',text:'<script>plain text</script>'}],structuredContent:{access_token:'private-value',nested:{apiKey:'private-key',answer:'safe result'}}}});
  const card=f.app.items.get('mcp');assert.equal(card.node.tagName,'DETAILS');assert.ok(card.text.includes('safe result'));assert.ok(card.text.includes('已隐藏'));assert.equal(card.text.includes('private-value'),false);assert.equal(card.text.includes('private-key'),false);
  f.renderItem({id:'reasoning',type:'reasoning',text:'hidden'});assert.equal(f.app.items.has('reasoning'),false);assert.equal(allNodes(card.node).some(node=>node.tagName==='SCRIPT'),false);
});

test('live command output retains the command and a readable output boundary until completion',()=>{
  const f=fixture();f.app.thread={id:'active'};
  f.renderItem({id:'command',type:'commandExecution',command:'fixture command',status:'inProgress'});
  f.notification({type:'notification',method:'item/commandExecution/outputDelta',params:{threadId:'active',itemId:'command',delta:'first\n'}});
  f.notification({type:'notification',method:'item/commandExecution/outputDelta',params:{threadId:'active',itemId:'command',delta:'second'}});
  assert.equal(f.app.items.get('command').body.textContent,'fixture command\n\nfirst\nsecond');
});
