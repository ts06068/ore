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
