import test from 'node:test';
import assert from 'node:assert/strict';
import {OreClient,OreApiError,parseEventStream,asItems} from '../dist/index.js';
const bytes=new TextEncoder();
function stream(parts){return new ReadableStream({start(controller){for(const part of parts)controller.enqueue(typeof part==='string'?bytes.encode(part):part);controller.close();}});}
test('SSE preserves IDs, split UTF-8, CRLF boundaries and multiline data',async()=>{
 const raw=bytes.encode('id: 14\r\nevent: resource\r\ndata: {"title":"논문"}\r\n\r\n:data heartbeat\n\nevent: note\ndata: first\ndata: second\n\nid: 99\ndata: incomplete');
 const chunks=[];for(let i=0;i<raw.length;i+=3)chunks.push(raw.slice(i,i+3));
 const events=[];for await(const event of parseEventStream(stream(chunks)))events.push(event);
 assert.deepEqual(events,[{id:'14',event:'resource',data:{title:'논문'}},{id:'14',event:'note',data:'first\nsecond'}]);
});
test('SSE abort cancels a pending reader',async()=>{let cancelled=false;const control=new AbortController();const pending=new ReadableStream({cancel(){cancelled=true;}});const iterator=parseEventStream(pending,control.signal);const next=iterator.next();control.abort();assert.equal((await next).done,true);assert.equal(cancelled,true);});
test('requests carry bearer auth and safely encode object identifiers',async()=>{
 const calls=[];const client=new OreClient({baseUrl:'https://ore.example/',token:()=> 'operator-secret',fetch:async(url,init)=>{calls.push({url,init});return new Response(JSON.stringify({id:'job/a',status:'paused'}),{status:200});}});
 await client.action('job/a','pause');assert.equal(calls[0].url,'https://ore.example/v1/jobs/job%2Fa/pause');assert.equal(calls[0].init.headers.get('Authorization'),'Bearer operator-secret');assert.equal(calls[0].init.method,'POST');assert.equal(calls[0].url.includes('operator-secret'),false);
});
test('API errors preserve structured validation detail and request ID',async()=>{const client=new OreClient({baseUrl:'https://ore.example',fetch:async()=>new Response(JSON.stringify({detail:[{msg:'Invalid mission'}]}),{status:422,headers:{'X-Request-Id':'req-2'}})});await assert.rejects(client.createJob({}),error=>error instanceof OreApiError&&error.status===422&&error.requestId==='req-2'&&Array.isArray(error.detail));});
test('WebSocket authentication is a subprotocol, never a query token',()=>{const client=new OreClient({baseUrl:'https://ore.example/base',token:'secret + /'});const connection=client.browserSocket('browser/a');assert.equal(connection.url,'wss://ore.example/base/v1/browser/browser%2Fa/stream');assert.equal(connection.protocols[0],'ore.v1');assert.equal(Buffer.from(connection.protocols[1].slice('ore.token.'.length),'base64url').toString(),'secret + /');assert.equal(new URL(connection.url).search,'');});
test('events resume with cursor while retaining bearer headers',async()=>{let request;const client=new OreClient({baseUrl:'http://localhost:8788',token:'secret',fetch:async(url,init)=>{request={url,init};return new Response(stream(['id: 7\ndata: {"type":"done"}\n\n']));}});const result=[];for await(const event of client.events('j',{after:'5'}))result.push(event);assert.match(request.url,/after=5$/);assert.equal(request.init.headers.get('Accept'),'text/event-stream');assert.equal(request.init.headers.get('Authorization'),'Bearer secret');assert.equal(result[0].id,'7');});
test('list envelopes and plain arrays remain interoperable',()=>{assert.deepEqual(asItems({resources:[{id:1}]},'resources'),[{id:1}]);assert.deepEqual(asItems([{id:2}]),[{id:2}]);assert.deepEqual(asItems({unknown:true}),[]);});
test('handoff actions carry durable state and control epochs, not tokens in URLs',async()=>{let call;const client=new OreClient({baseUrl:'https://ore.test',token:'private',fetch:async(url,init)=>{call={url,init};return new Response(JSON.stringify({id:'h'}));}});await client.handoffAction({id:'h',state_version:4,control_epoch:9},'resume',{idempotency_key:'stable-key'});assert.deepEqual(JSON.parse(call.init.body),{action:'resume',expected_version:4,expected_epoch:9,idempotency_key:'stable-key'});assert.equal(call.url,'https://ore.test/v1/handoffs/h/actions');});
test('source readiness is queried for the selected access profile',async()=>{let url;const client=new OreClient({baseUrl:'https://ore.test',fetch:async(value)=>{url=value;return new Response('[]');}});await client.sources('institution/a');assert.equal(url,'https://ore.test/v1/sources?access_profile_id=institution%2Fa');});
test('conversation controls encode IDs and keep plan approval explicit',async()=>{
 const calls=[];const client=new OreClient({baseUrl:'https://ore.test',token:'operator',fetch:async(url,init)=>{calls.push({url,init});return Response.json({id:'chat/a',title:'Report',status:'draft',messages:[],plans:[],runs:[]});}});
 await client.createConversation({mode:'plan'});await client.sendMessage('chat/a',{content:'Collect the report',mode:'execute',model_policy:'auto'});
 assert.equal(calls[1].url,'https://ore.test/v1/conversations/chat%2Fa/messages');assert.equal(calls.some(call=>call.url.endsWith('/approve')),false);
 await client.approvePlan('chat/a','plan/b');await client.interruptConversation('chat/a');await client.resumeConversation('chat/a');
 assert.equal(calls[2].url,'https://ore.test/v1/conversations/chat%2Fa/plans/plan%2Fb/approve');assert.equal(calls[3].url,'https://ore.test/v1/conversations/chat%2Fa/interrupt');assert.equal(calls[4].url,'https://ore.test/v1/conversations/chat%2Fa/resume');
 assert.equal(calls.every(call=>call.init.headers.get('Authorization')==='Bearer operator'),true);
});
test('conversation events restore the durable cursor and preserve public event envelopes',async()=>{
 let call;const client=new OreClient({baseUrl:'https://ore.test',fetch:async(url,init)=>{call={url,init};return new Response(stream(['id: 15\nevent: progress\ndata: {"id":15,"type":"progress","data":{"summary":"Inventory verified"}}\n\n']));}});
 const events=[];for await(const event of client.conversationEvents('chat/a',{after:'14'}))events.push(event);
 assert.equal(call.url,'https://ore.test/v1/conversations/chat%2Fa/events?after=14');assert.equal(call.init.headers.get('Last-Event-ID'),'14');assert.equal(events[0].data.data.summary,'Inventory verified');
});
test('conversation history pages preserve cursor parameters and authenticated access',async()=>{let request;const client=new OreClient({baseUrl:'https://ore.test',token:'private-token',fetch:async(url,init)=>{request={url,init};return Response.json({events:[],has_more:false,before_cursor:null,events_cursor:101});}});const page=await client.conversationEventHistory('chat/a',{before:91,limit:50});assert.equal(request.url,'https://ore.test/v1/conversations/chat%2Fa/event-history?before=91&limit=50');assert.equal(request.init.headers.get('Authorization'),'Bearer private-token');assert.equal(page.has_more,false);});
test('scheduler exposes requested and ready pool capacity separately',async()=>{const value={global:{desired_executors:4,ready_executors:2},jobs:[]};let path;const client=new OreClient({baseUrl:'https://ore.test',fetch:async(url)=>{path=url;return Response.json(value);}});assert.deepEqual(await client.scheduler(),value);assert.equal(path,'https://ore.test/v1/scheduler');});

test('conversation snapshots preserve optional runtime evidence, exact zero limits and incomplete usage',async()=>{
 const usage={tokens:{totalTokens:501200},provider_turns:8,remaining_seconds:0,remaining_tokens:0,usage_complete:false,token_overshoot:1200,paused:true};
 const value={id:'chat',title:'Collect',status:'paused_budget',messages:[],plans:[],usage,runs:[{id:'run',status:'paused_budget',execution_runtime:'native',runtime_details:{model:'available-model',effort:'high',adaptation_status:'awaiting_native_outcome_validation'},usage,reuse:{verified_reuses:2,candidate_recipes:4,promoted_recipes:1},validation_strength:{schema:true,receipt:true,independent_goal:false,corpus:false}}]};
 const client=new OreClient({baseUrl:'https://ore.test',fetch:async()=>Response.json(value)});
 const conversation=await client.conversation('chat');assert.deepEqual(conversation,value);
 assert.equal(conversation.runs[0].usage.remaining_tokens,0);assert.equal(conversation.runs[0].reuse.candidate_recipes,4);
});

test('folder, branch, connection and secret methods preserve scoped paths and versioned mutations',async()=>{
 const calls=[];const client=new OreClient({baseUrl:'https://ore.test',fetch:async(url,init)=>{calls.push({url,init,body:init.body?JSON.parse(init.body):null});return Response.json({id:'new',connections:[],folders:[],providers:[]});}});
 await client.conversationFolders();await client.createConversationFolder('Research');await client.updateConversationFolder('f/a','Review');await client.updateConversation('c/a',{folder_id:null});await client.branchConversation('c/a',{message_id:'m1'});await client.connections('c/a');
 const card={id:'k/a',state_version:7};await client.connectionAction(card,'refresh','action-key');await client.connectionSecret(card,'api_key','secret-value','secret-key');await client.providerStatuses();await client.health();
 assert.equal(calls[2].url,'https://ore.test/v1/conversation-folders/f%2Fa');assert.deepEqual(calls[3].body,{folder_id:null});assert.equal(calls[4].url,'https://ore.test/v1/conversations/c%2Fa/branch');assert.deepEqual(calls[4].body,{message_id:'m1'});assert.equal(calls[5].url,'https://ore.test/v1/connections?conversation_id=c%2Fa');
 assert.deepEqual(calls[6].body,{action:'refresh',expected_version:7,idempotency_key:'action-key'});assert.deepEqual(calls[7].body,{field:'api_key',value:'secret-value',expected_version:7,idempotency_key:'secret-key'});assert.equal(calls[7].url.includes('secret-value'),false);assert.equal(calls[9].url,'https://ore.test/healthz');
});

test('documented list aliases and branch preserve the established transport paths',async()=>{
 const urls=[];const client=new OreClient({baseUrl:'https://ore.test',fetch:async(url)=>{urls.push(url);return Response.json({connections:[],folders:[],id:'branch'});}});
 assert.deepEqual(await client.listConnections('c/a'),[]);assert.deepEqual(await client.listConversationFolders(),[]);assert.equal((await client.branchConversation('c/a',{message_id:'m1'})).id,'branch');
 assert.deepEqual(urls,['https://ore.test/v1/connections?conversation_id=c%2Fa','https://ore.test/v1/conversation-folders','https://ore.test/v1/conversations/c%2Fa/branch']);
});
