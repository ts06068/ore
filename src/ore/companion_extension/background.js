import {parsePair,allowed,publicError} from './protocol.js';
let socket=null,tabId=null,origins=[],timer=null,busy=Promise.resolve(),status='Disconnected',files=new Map(),worldId=null,mainFrame=null,lastDocument=null;
const command=(method,params={})=>chrome.debugger.sendCommand({tabId},method,params);
async function evaluate(fn,...args){if(worldId===null){const tree=await command('Page.getFrameTree');worldId=(await command('Page.createIsolatedWorld',{frameId:tree.frameTree.frame.id,worldName:'ore-companion'})).executionContextId;}const r=await command('Runtime.evaluate',{contextId:worldId,expression:`(${fn.toString()})(...${JSON.stringify(args)})`,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error('The page operation failed.');return r.result?.value;}
function pageRead(op,a){
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const nodes=selector=>[...document.querySelectorAll(selector.replaceAll(':visible',''))].filter(e=>!selector.includes(':visible')||visible(e));
 const list=a.selector?nodes(a.selector):[];const selected=a.index==null?list[0]:list[a.index];
 if(op==='title')return document.title;
 if(op==='webdriver')return navigator.webdriver===true;
 if(op==='query'){
  if(a.kind==='count')return list.length;
  if(a.kind==='texts')return list.map(e=>(e.innerText||'').slice(0,40000));
  return (selected?.innerText||'').slice(0,40000);
 }
 if(op==='elements')return [...document.querySelectorAll('a,button,input,textarea,select,[role="button"]')].map((e,index)=>({index,tag:e.tagName.toLowerCase(),text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').slice(0,200),type:e.getAttribute('type'),name:e.getAttribute('name'),href:e.href||null,visible:visible(e)})).filter(e=>e.visible).slice(0,180);
 if(op==='content'){if([...document.querySelectorAll('input[type="password"]')].some(visible))throw Error('A visible login form cannot be captured as article evidence.');const copy=document.documentElement.cloneNode(true);copy.querySelectorAll('input[type="password"]').forEach(e=>e.remove());copy.querySelectorAll('input,textarea').forEach(e=>{e.removeAttribute('value');e.textContent='';});copy.querySelectorAll('script:not([type="application/ld+json"])').forEach(e=>e.remove());const html=copy.outerHTML;if(html.length>2000000)throw Error('Page exceeds companion snapshot limit.');return html;}
 if(op==='locate'){
  if(!selected||!visible(selected))throw Error('Target is not visible.');
  selected.scrollIntoView({block:'center',inline:'center'});const r=selected.getBoundingClientRect();
  return {x:r.x+r.width/2,y:r.y+r.height/2,href:selected.closest('a')?.href||null};
 }
 if(op==='focus'){if(!selected||!visible(selected))throw Error('Target is not visible.');selected.focus();if(selected.select)selected.select();return true;}
 throw Error('Unsupported page operation.');
}
async function check(){const url=await evaluate(()=>location.href);if(url!=='about:blank'&&!allowed(url,origins))throw Error('Return the dedicated tab to an allowed mission origin.');return url;}
async function execute(op,a={}){
 if(op==='detach'){setTimeout(()=>void disconnect(),0);return {url:'about:blank',value:true};}
 await check();let value;
 if(op==='navigate'){
  if(!allowed(a.url,origins))throw Error('Navigation outside mission origins.');
  worldId=null;await command('Page.navigate',{url:a.url});await new Promise(r=>setTimeout(r,700));
 }else if(op==='back'){
  const h=await command('Page.getNavigationHistory'),entry=h.entries[h.currentIndex-1];
  if(!entry||!allowed(entry.url,origins))throw Error('No permitted history entry.');
  worldId=null;await command('Page.navigateToHistoryEntry',{entryId:entry.id});await new Promise(r=>setTimeout(r,300));
 }else if(['title','webdriver','query','elements','content'].includes(op))value=await evaluate(pageRead,op,a);
 else if(op==='screenshot'){
  const metrics=await command('Page.getLayoutMetrics'),v=metrics.cssVisualViewport||metrics.visualViewport;
  const shot=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false,clip:{x:v.pageX,y:v.pageY,width:Math.min(v.clientWidth,1280),height:Math.min(v.clientHeight,800),scale:1/(await evaluate(()=>devicePixelRatio||1))}});value=shot.data;
 }else if(op==='click'){
  const p=a.selector?await evaluate(pageRead,'locate',a):a;
  if(p.href&&!allowed(p.href,origins))throw Error('Link outside mission origins.');
  if(!Number.isFinite(p.x)||!Number.isFinite(p.y)||p.x<0||p.y<0||p.x>1280||p.y>800)throw Error('Target outside supported viewport.');
  await command('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,x:p.x,y:p.y});
  await command('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,x:p.x,y:p.y});
 }else if(op==='fill'||op==='type'){
  if(typeof a.text!=='string'||a.text.length>10000)throw Error('Input too long.');
  if(op==='fill')await evaluate(pageRead,'focus',a);
  await command('Input.insertText',{text:a.text});
 }else if(op==='key'){
  const keys={Enter:13,Tab:9,Escape:27,Backspace:8,ArrowUp:38,ArrowDown:40,ArrowLeft:37,ArrowRight:39,PageDown:34,PageUp:33,Home:36,End:35};
  if(!keys[a.key])throw Error('Unsupported page key.');
  await command('Input.dispatchKeyEvent',{type:'keyDown',key:a.key,windowsVirtualKeyCode:keys[a.key]});await command('Input.dispatchKeyEvent',{type:'keyUp',key:a.key,windowsVirtualKeyCode:keys[a.key]});
 }else if(op==='scroll')await command('Input.dispatchMouseEvent',{type:'mouseWheel',x:100,y:100,deltaX:Math.max(-10000,Math.min(a.deltaX||0,10000)),deltaY:Math.max(-10000,Math.min(a.deltaY||0,10000))});
 else if(op==='fetch_begin'){
  if(!allowed(a.url,origins)||!Number.isInteger(a.limit)||a.limit<0||a.limit>67108864)throw Error('File request exceeds paired scope.');
  if(files.size)throw Error('Complete the current file transfer first.');
  // Fetch in the selected tab: its existing browser session stays on this PC.
  // Redirects and cross-origin CORS failures are explicit gaps, never bypassed.
  const result=await evaluate(async(url,limit)=>{
   const response=await fetch(url,{credentials:'include',redirect:'error'});
   if(!response.ok)throw Error('File HTTP error');
   const reader=response.body.getReader();let total=0;const chunks=[];
   try{while(true){const r=await reader.read();if(r.done)break;total+=r.value.length;if(total>limit)throw Error('File byte limit exceeded');chunks.push(r.value);}}finally{await reader.cancel();}
   const bytes=new Uint8Array(total);let offset=0;for(const part of chunks){bytes.set(part,offset);offset+=part.length;}
   // Keep bytes inside this tab, not in the ORE model context.
   const id=crypto.randomUUID();globalThis.__oreTransfers??=new Map();globalThis.__oreTransfers.set(id,bytes);
   return {id,bytes:total,status:response.status,content_type:response.headers.get('content-type'),url:response.url};
  },a.url,a.limit);files.set(result.id,true);value=result;
 }else if(op==='fetch_part'){
  if(!files.has(a.id)||!Number.isInteger(a.offset)||a.offset<0)throw Error('Unknown file transfer.');
  value=await evaluate((id,offset)=>{const part=globalThis.__oreTransfers?.get(id)?.slice(offset,offset+262144);if(!part)throw Error('File transfer expired');let str='';for(let i=0;i<part.length;i+=8192)str+=String.fromCharCode(...part.subarray(i,i+8192));return btoa(str);},a.id,a.offset);
 }else if(op==='fetch_end'){
  if(!files.has(a.id))throw Error('Unknown file transfer.');
  await evaluate(id=>globalThis.__oreTransfers?.delete(id),a.id);files.delete(a.id);value=true;
 }else throw Error('Unsupported companion operation.');
 const url=await check();return {url,value:value??null,http_status:lastDocument&&lastDocument.url.split('#')[0]===url.split('#')[0]?lastDocument.status:null};
}
async function disconnect(){
 if(timer)clearInterval(timer);timer=null;
 const old=socket;socket=null;if(old)old.close();
 if(tabId!==null){try{await chrome.debugger.detach({tabId});}catch{}tabId=null;}
 files.clear();origins=[];worldId=null;mainFrame=null;lastDocument=null;status='Disconnected';
}
async function connect(text){
 if(socket)throw Error('Disconnect the current mission first.');
 const pair=parsePair(text);origins=pair.origins;
 const tab=await chrome.tabs.create({url:'about:blank',active:true});tabId=tab.id;
 try{
  await chrome.debugger.attach({tabId},'1.3');await command('Page.enable');await command('Runtime.enable');mainFrame=(await command('Page.getFrameTree')).frameTree.frame.id;await command('Network.enable');
  const ws=new WebSocket(pair.websocket);socket=ws;status='Connecting';
  ws.onopen=()=>ws.send(JSON.stringify({pair_id:pair.pair_id,token:pair.token}));
  ws.onmessage=event=>{
   let data;try{data=JSON.parse(event.data);}catch{return;}
   if(data.type==='connected'){status='Connected — only the dedicated tab is controlled';timer=setInterval(()=>{if(ws.readyState===1)ws.send(JSON.stringify({type:'ping'}));},20000);return;}
   if(!data.id)return;
   busy=busy.then(async()=>{if(socket!==ws)return;try{const result=await execute(data.operation,data.arguments);if(ws.readyState===1)ws.send(JSON.stringify({id:data.id,result}));}catch(error){if(ws.readyState===1)ws.send(JSON.stringify({id:data.id,error:publicError(error)}));}});
  };
  ws.onclose=()=>{if(socket===ws)void disconnect();};ws.onerror=()=>{status='Connection failed. Check the server address or SSH tunnel.';};
 }catch(error){await disconnect();throw error;}
}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
 if(sender.id!==chrome.runtime.id)return;
 (async()=>{if(message.action==='connect')await connect(message.code);else if(message.action==='disconnect')await disconnect();respond({status});})().catch(error=>respond({error:publicError(error)}));return true;
});
chrome.debugger.onDetach.addListener(source=>{if(source.tabId===tabId)void disconnect();});

chrome.debugger.onEvent.addListener((source,method,params)=>{
 if(source.tabId!==tabId)return;
 if(method==='Runtime.executionContextsCleared'){worldId=null;files.clear();}
 if(method==='Page.frameStartedLoading'&&params.frameId===mainFrame)lastDocument=null;
 if(method==='Page.frameNavigated'&&!params.frame.parentId)mainFrame=params.frame.id;
 if(method==='Network.responseReceived'&&params.type==='Document'&&params.frameId===mainFrame)lastDocument={url:params.response.url,status:params.response.status};
});
