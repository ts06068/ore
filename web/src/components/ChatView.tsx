import {useCallback, useEffect, useRef, useState} from 'react';
import {ArrowUp, Check, ChevronDown, Download, FileText, MessageCircle, Play, Square, GitBranch, Link2, PanelRight, X, Search} from 'lucide-react';
import type {AccessProfile, Artifact, ConnectionCard, Conversation, ConversationEvent, JsonObject, Model, OreClient, OreEvent, PlanRevision, SchedulerSnapshot, WorkflowRun} from '@ore/sdk';
import {ErrorNotice, Logo, Spinner, Status} from './Common';
import {date, download, errorMessage, label, record, safeUrl, text} from '../lib/format';

import {applyPublicMessageEvent,conversationTurns} from '../lib/conversation';
import {CollectionProgress} from './CollectionProgress';
import {Markdown} from './Markdown';
import {ConnectionCards} from './ConnectionCards';
import {isConnectionSetupMessage} from '../lib/connections';

interface Props {client:OreClient;id:string;models:Model[];onNavigate:(path:string)=>void;onChanged:()=>void;}
const activeStates=new Set(['planning','running','executing','queued','resuming']);
const pausedStates=new Set(['paused','interrupted','stopped']);
const stoppingStates=new Set(['interrupting','stopping','pause_requested']);

export function conversationEvent(event:OreEvent):ConversationEvent {
 const envelope=record(event.data);
 if(typeof envelope.type==='string'&&'data' in envelope)return {...envelope,id:event.id??text(envelope.id,''),type:envelope.type,data:envelope.data,created_at:typeof envelope.created_at==='string'?envelope.created_at:undefined};
 return {id:event.id??'',type:event.event,data:event.data};
}
export function mergeConversationEvents(current:ConversationEvent[],incoming:ConversationEvent[]):ConversationEvent[] {
 const result=new Map(current.map(item=>[String(item.id),item]));
 for(const item of incoming)if(String(item.id)&&!['message.delta','message.complete'].includes(item.type))result.set(String(item.id),item);
 return [...result.values()].sort((a,b)=>Number(a.id)-Number(b.id));
}
function publicFailure(value:unknown):string{const data=record(value);const fields=data.fields??record(data.last_validation).fields;const issues=Array.isArray(fields)?fields.map(field=>{const item=record(field);return [Array.isArray(item.path)?item.path.join('.'):undefined,item.message].filter(Boolean).join(': ');}):[];return [text(data.message??data.error,'ORE could not complete this step.'),...issues,typeof data.action==='string'?data.action:''].filter(Boolean).join(' ');}
function laterCursor(current:string|undefined,next:string|number|undefined){if(next===undefined)return current;const value=String(next);return current&&Number(current)>Number(value)?current:value;}
function modelEfforts(model:Model|undefined):string[]{const entries=model?.supportedReasoningEfforts??model?.supported_reasoning_efforts??[];return (Array.isArray(entries)?entries:[]).map(value=>typeof value==='string'?value:text(record(value).reasoningEffort??record(value).reasoning_effort??record(value).effort,'')).filter(Boolean);}
function values(value:unknown):string[]{if(value===undefined||value===null)return [];if(Array.isArray(value))return value.flatMap(values);if(typeof value==='object')return Object.entries(record(value)).map(([key,item])=>`${label(key)}: ${values(item).join(', ')}`);return [String(value)];}

function reviewChecks(value:unknown):string[]{
 if(Array.isArray(value))return value.flatMap(reviewChecks);
 if(typeof value!=='object'||value===null)return values(value);
 const item=record(value);const description=item.description??item.summary??item.label;
 if(typeof description==='string')return [description];
 const op=text(item.op??item.type,'');
 const expected=item.right??item.expected;
 const explanations:Record<string,string>={exists:'The required output must be present.',truthy:'The completion condition must hold.',eq:'The output must match the expected value'+(expected!==undefined?': '+values(expected).join(', '):'.'),ne:'The output must differ from the excluded value.',contains:'The output must include the required content.',in:'The output must be one of the accepted values.',schema:'The output must match the required structure.',all:'All specified completion conditions must hold.',any:'At least one specified completion condition must hold.'};
 return explanations[op]?[explanations[op]]:values(value);
}

export default function ChatView({client,id,models,onNavigate,onChanged}:Props){
 const [conversation,setConversation]=useState<Conversation|null>(null);
 const [events,setEvents]=useState<ConversationEvent[]>([]);
 const [draft,setDraft]=useState('');
 const [mode,setMode]=useState<'plan'|'execute'>(id?'plan':'execute');
 const [policy,setPolicy]=useState<'auto'|'fixed'>('auto');
 const [backendKind,setBackendKind]=useState('codex');
 const [providerModel,setProviderModel]=useState('sonnet');
 const [providerCustom,setProviderCustom]=useState('');
 const [connectionCards,setConnectionCards]=useState<ConnectionCard[]>([]);
 const [apiConnectionId,setApiConnectionId]=useState('');
 const [accessProfileRef,setAccessProfileRef]=useState('public');
 const [accessProfiles,setAccessProfiles]=useState<AccessProfile[]>([]);
 const [model,setModel]=useState('');
 const [effort,setEffort]=useState('');
 const [connectionsOpen,setConnectionsOpen]=useState(false);
 const [branchBusy,setBranchBusy]=useState('');
 const [sending,setSending]=useState(false);
 const [continueChanges,setContinueChanges]=useState(false);
 const [controlBusy,setControlBusy]=useState(false);
 const [controlAction,setControlAction]=useState<string>('');
 const [loading,setLoading]=useState(Boolean(id));
 const [error,setError]=useState('');
 const [stream,setStream]=useState('connecting');
 const [artifacts,setArtifacts]=useState<(Artifact&{job_id?:string})[]>([]);
 const [handoffs,setHandoffs]=useState<JsonObject[]>([]);
 const [resultsOpen,setResultsOpen]=useState(false);
 const [fileSearch,setFileSearch]=useState('');
 const [scheduler,setScheduler]=useState<SchedulerSnapshot|null>(null);
 const [historyMore,setHistoryMore]=useState(false);
 const [historyBusy,setHistoryBusy]=useState(false);
 const historyCursor=useRef<number|undefined>(undefined);
 const idRef=useRef(id);idRef.current=id;
 const changedRef=useRef(onChanged);changedRef.current=onChanged;
 const bottomRef=useRef<HTMLDivElement>(null);
 const transcriptRef=useRef<HTMLDivElement>(null);
 const followTail=useRef(true);
 const [showLatest,setShowLatest]=useState(false);
 const composerRef=useRef<HTMLTextAreaElement>(null);
 const fixedModel=models.find(item=>(item.model??item.id)===model);
 const efforts=modelEfforts(fixedModel);
 const apiBackend=['openai','anthropic'].includes(backendKind);
 const providerModelId=backendKind==='claude_code'&&providerModel!=='custom'?providerModel:providerCustom.trim();
 const profileOptions=[...new Map([{id:'public',name:'Public'},...accessProfiles,...connectionCards.filter(card=>card.kind==='source'&&card.access_profile_ref).map(card=>({id:card.access_profile_ref!,name:accessProfiles.find(profile=>profile.id===card.access_profile_ref)?.name??card.access_profile_ref!})),{id:accessProfileRef,name:accessProfiles.find(profile=>profile.id===accessProfileRef)?.name??(accessProfileRef==='public'?'Public':accessProfileRef)}].map(profile=>[profile.id,profile])).values()];
 const apiConnections=connectionCards.filter(card=>card.provider===backendKind&&typeof (record(card.backend).api_key_ref??card.api_key_ref)==='string');
 const selectedApiConnection=apiConnections.find(card=>card.id===apiConnectionId)??apiConnections[0];
 const savedBackend=record(record(conversation?.settings).backend);
 const apiKeyRef=selectedApiConnection?(record(selectedApiConnection.backend).api_key_ref??selectedApiConnection.api_key_ref):savedBackend.kind===backendKind?savedBackend.api_key_ref:undefined;
 const invalidProvider=backendKind!=='codex'&&(!providerModelId||(apiBackend&&!apiKeyRef));
 const setupMessage=isConnectionSetupMessage(draft);
 const invalidMessageSettings=!setupMessage&&(invalidProvider||(backendKind==='codex'&&policy==='fixed'&&!model));
 const status=conversation?.status??'draft';
 const stopping=stoppingStates.has(status)||(controlBusy&&controlAction==='stop');
 const paused=pausedStates.has(status);
 const active=!paused&&!stopping&&(Boolean(conversation?.planner_running)||activeStates.has(status)||(conversation?.runs??[]).some(run=>activeStates.has(run.status)));
 const currentPlan=conversation?.plans?.find(plan=>plan.id===conversation.active_plan_id)??conversation?.plans?.at(-1);
 const hasPendingPlan=Boolean(currentPlan&&(!currentPlan.status||['proposed','draft','awaiting_approval'].includes(currentPlan.status)));
 const currentRun=conversation?.runs?.find(run=>run.id===conversation.active_run_id)??conversation?.runs?.at(-1);

 const acceptSnapshot=useCallback((fresh:Conversation)=>{setConversation(current=>current?.id===fresh.id&&Number(current.events_cursor)>Number(fresh.events_cursor)?current:fresh);setEvents(current=>mergeConversationEvents(current,fresh.events??[]));},[]);
 useEffect(()=>{let alive=true;void client.accessProfiles().then(profiles=>{if(alive)setAccessProfiles(profiles);}).catch(()=>undefined);return()=>{alive=false;};},[client]);
 useEffect(()=>{if(!model&&models.length){setModel(models[0]!.model??models[0]!.id);setEffort(modelEfforts(models[0])[0]??'');}},[models,model]);
 useEffect(()=>{
  setConversation(null);setEvents([]);setArtifacts([]);setHandoffs([]);setResultsOpen(false);setFileSearch('');setError('');setContinueChanges(false);setLoading(Boolean(id));setHistoryMore(false);historyCursor.current=undefined;followTail.current=true;setShowLatest(false);
  if(!id){setMode('execute');setStream('idle');return;}
  let disposed=false;let cursor:string|undefined;let reconnect:ReturnType<typeof setTimeout>|undefined;let refreshTimer:ReturnType<typeof setTimeout>|undefined;
  const controller=new AbortController();
  const snapshot=async()=>{try{const fresh=await client.conversation(id);if(disposed)return;acceptSnapshot(fresh);if(historyCursor.current===undefined&&fresh.events?.length){historyCursor.current=Number(fresh.events[0]!.id);setHistoryMore(historyCursor.current>1);}cursor=laterCursor(cursor,fresh.events_cursor);setLoading(false);}catch(cause){if(!disposed){setLoading(false);setError(errorMessage(cause));}}};
  const listen=async()=>{if(disposed)return;try{setStream('connecting');for await(const event of client.conversationEvents(id,{after:cursor,signal:controller.signal,onOpen:()=>{if(!disposed)setStream('live');}})){if(disposed)return;setStream('live');const normalized=conversationEvent(event);setConversation(current=>applyPublicMessageEvent(current,normalized));cursor=laterCursor(cursor,normalized.id);if(!['message.delta','message.complete'].includes(normalized.type))setEvents(current=>mergeConversationEvents(current,[normalized]));if(normalized.type==='error')setError(publicFailure(normalized.data));if(normalized.type!=='message.delta'&&!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=undefined;void snapshot();changedRef.current();},180);}if(!disposed){setStream('reconnecting');reconnect=setTimeout(()=>void listen(),2000);}}catch(cause){if(disposed)return;setStream('reconnecting');if((cause as {status?:number}).status===401){setError('Your operator session expired. Connect again to continue.');return;}reconnect=setTimeout(()=>void listen(),2000);}};
  void snapshot().then(()=>listen());const poll=setInterval(()=>void snapshot(),5000);
  return()=>{disposed=true;controller.abort();clearInterval(poll);if(reconnect)clearTimeout(reconnect);if(refreshTimer)clearTimeout(refreshTimer);};
 },[id,client,acceptSnapshot]);
 useEffect(()=>{const node=composerRef.current;if(node){node.style.height='auto';node.style.height=Math.min(node.scrollHeight||70,220)+'px';}},[draft]);
 useEffect(()=>{if(!resultsOpen)return;const panel=document.querySelector<HTMLElement>('.chat-results-panel');panel?.querySelector<HTMLButtonElement>('button')?.focus();const close=(event:KeyboardEvent)=>{if(event.key==='Escape')setResultsOpen(false);if(event.key==='Tab'&&window.innerWidth<1440&&panel){const nodes=[...panel.querySelectorAll<HTMLElement>('button,input,summary,a')].filter(node=>node.getClientRects().length>0&&!node.hasAttribute('disabled'));const first=nodes[0],last=nodes.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}};document.addEventListener('keydown',close);return()=>{document.removeEventListener('keydown',close);document.querySelector<HTMLButtonElement>('[aria-label="Open collected results"]')?.focus();};},[resultsOpen]);
 useEffect(()=>{const settings=record(conversation?.settings);setAccessProfileRef(text(record(settings.constraints).access_profile_ref??settings.access_profile_ref??settings.access_profile,'public'));if(settings.mode==='plan'||settings.mode==='execute')setMode(settings.mode);if(settings.model_policy==='auto'||settings.model_policy==='fixed')setPolicy(settings.model_policy);if(typeof settings.model==='string')setModel(settings.model);if(typeof settings.reasoning_effort==='string')setEffort(settings.reasoning_effort);const backend=record(settings.backend);setBackendKind(text(backend.kind,'codex'));if(backend.kind&&backend.kind!=='codex'){const selected=text(backend.model??settings.model,backend.kind==='claude_code'?'sonnet':'');setProviderModel(['sonnet','opus'].includes(selected)?selected:'custom');setProviderCustom(selected);}},[conversation?.id]);
 const runJobs=(conversation?.runs??[]).map(run=>run.job_id).filter((value):value is string=>Boolean(value)).join(',');
 useEffect(()=>{if(!runJobs){setScheduler(null);return;}let alive=true;const update=async()=>{try{const state=await client.scheduler();if(alive)setScheduler(state);}catch{/* Optional older-server capability stays undisplayed. */}};void update();const timer=setInterval(()=>void update(),5000);return()=>{alive=false;clearInterval(timer);};},[client,runJobs]);
 useEffect(()=>{let alive=true;if(!runJobs)return;const load=async()=>{const results=await Promise.allSettled(runJobs.split(',').map(async jobId=>{const [files,requests]=await Promise.all([client.artifacts(jobId),client.handoffs(jobId)]);return {files:files.map(file=>({...file,job_id:jobId})),requests};}));if(alive){setArtifacts(results.flatMap(item=>item.status==='fulfilled'?item.value.files:[]));setHandoffs(results.flatMap(item=>item.status==='fulfilled'?item.value.requests:[]));}};void load();const timer=setInterval(()=>void load(),5000);return()=>{alive=false;clearInterval(timer);};},[client,runJobs]);
 useEffect(()=>{if(followTail.current){bottomRef.current?.scrollIntoView?.({block:'end',behavior:'auto'});}else setShowLatest(true);},[conversation?.messages?.length,conversation?.messages?.at(-1)?.content,events.length,currentPlan?.id,status,artifacts.length,handoffs.length]);
 function goLatest(){followTail.current=true;setShowLatest(false);bottomRef.current?.scrollIntoView?.({block:'end',behavior:'smooth'});}
 async function send(){const content=draft.trim();if(!content||sending||invalidMessageSettings)return;setSending(true);setError('');try{const settings=setupMessage?(['codex','claude_code'].includes(backendKind)?{backend:{kind:backendKind},...((backendKind==='codex'?model:providerModelId)?{model:backendKind==='codex'?model:providerModelId}:{})}:{}):backendKind!=='codex'?{mode,model_policy:'fixed' as const,model:providerModelId,backend:{kind:backendKind,model:providerModelId,...(apiBackend?{api_key_ref:apiKeyRef}:{})}}:{mode,model_policy:policy,...(record(record(conversation?.settings).backend).kind&&record(record(conversation?.settings).backend).kind!=='codex'?{backend:{kind:'codex'}}:{}),...(policy==='fixed'?{model,reasoning_effort:effort||undefined}:{})};const target=id||((await client.createConversation({mode,...settings})).id);if(!id)onNavigate('/chat/'+encodeURIComponent(target));const fresh=await client.sendMessage(target,{content,...settings,access_profile_ref:accessProfileRef,...(!setupMessage&&continueChanges&&mode==='execute'&&conversation?.approved_plan_id&&!paused&&!stopping?{change_and_continue:true}:{})});if(!id||idRef.current===target){acceptSnapshot(fresh);setDraft('');setContinueChanges(false);followTail.current=true;}changedRef.current();}catch(cause){setError(errorMessage(cause));}finally{setSending(false);}}
 async function branch(messageId:string){if(!id||branchBusy)return;setBranchBusy(messageId);setError('');try{const fresh=await client.branchConversation(id,{message_id:messageId});changedRef.current();onNavigate('/chat/'+encodeURIComponent(fresh.id));}catch(cause){setError(errorMessage(cause));}finally{setBranchBusy('');}}
 async function control(action:'stop'|'resume'|'approve',planId?:string){if(!id||controlBusy)return;setControlBusy(true);setControlAction(action);setError('');try{const fresh=action==='stop'?await client.interruptConversation(id):action==='resume'?await client.resumeConversation(id):await client.approvePlan(id,planId!);if(idRef.current===id)acceptSnapshot(fresh);changedRef.current();}catch(cause){setError(errorMessage(cause));}finally{setControlBusy(false);setControlAction('');}}
 async function earlierHistory(){if(!id||historyBusy)return;setHistoryBusy(true);try{const page=await client.conversationEventHistory(id,{before:historyCursor.current,limit:100});followTail.current=false;setEvents(current=>mergeConversationEvents(current,page.events));historyCursor.current=page.before_cursor??historyCursor.current;setHistoryMore(page.has_more);}catch(cause){setError(errorMessage(cause));}finally{setHistoryBusy(false);}}
 async function getArtifact(artifact:Artifact&{job_id?:string}){try{if(artifact.job_id){download(await client.artifactFile(artifact.job_id,artifact.id),text(artifact.filename??artifact.name,artifact.id));}else{const href=safeUrl(artifact.url);if(href)window.open(href,'_blank','noopener,noreferrer');}}catch(cause){setError(errorMessage(cause));}}
 const allArtifacts:(Artifact&{job_id?:string})[]=[...artifacts,...(conversation?.runs??[]).flatMap(run=>(run.artifacts??[]).map(artifact=>({...artifact,job_id:run.job_id})))].filter((item,index,rows)=>rows.findIndex(other=>other.id===item.id)===index);
 const allHandoffs=[...handoffs,...(conversation?.runs??[]).flatMap(run=>run.handoffs??[])].filter((item,index,rows)=>rows.findIndex(other=>other.id===item.id)===index);
 const turns=conversationTurns(conversation,events);
 const activeCollection=currentRun?.collection_progress??record(currentRun?.progress).collection_progress;
 const filteredArtifacts=allArtifacts.filter(artifact=>[artifact.filename,artifact.name,artifact.role,artifact.resource_id].some(value=>typeof value==='string'&&value.toLowerCase().includes(fileSearch.toLowerCase())));
 return <section className={`chat-workspace ${resultsOpen?'chat-with-results':''}`} aria-label="ORE conversation">
  <header className="chat-heading"><div><h1>{id?(conversation?.title??'Conversation'):'New conversation'}</h1>{id&&<span className={`chat-stream chat-stream-${stream}`}>{stream==='live'?'Live updates':stream==='reconnecting'?'Reconnecting · history is saved':stream==='connecting'?'Connecting to updates':'History loaded'}</span>}</div><div className="chat-heading-actions"><button className="icon-button" aria-label="Connect accounts and sources" aria-expanded={connectionsOpen} onClick={()=>setConnectionsOpen(!connectionsOpen)}><Link2 size={19}/></button>{id&&<Status value={status}/>}<button className="icon-button" aria-label="Open collected results" aria-expanded={resultsOpen} onClick={()=>setResultsOpen(!resultsOpen)}><PanelRight size={19}/></button>{paused&&!hasPendingPlan&&<button className="button outline compact" disabled={controlBusy} onClick={()=>void control('resume')}><Play size={14}/>Resume</button>}</div></header>
  <div className="chat-body"><div className="chat-main">
  <div className="chat-transcript" ref={transcriptRef} onScroll={()=>{const element=transcriptRef.current;if(element){followTail.current=element.scrollHeight-element.scrollTop-element.clientHeight<100;if(followTail.current)setShowLatest(false);}}}>
   <div className="chat-column">
    {loading&&<div className="chat-loading"><Spinner/>Loading your conversation…</div>}
    {!id&&<div className="chat-welcome"><Logo className="chat-welcome-logo"/><h1>What would you like to collect?</h1><p>Define the scope. Follow the work.<br/>Keep the evidence.</p><div className="chat-examples">{['Find the original articles in JACC’s June 2024 issues.','Extract selected paragraphs from a set of websites.','Collect current guidance and compare it with last year.'].map(example=><button key={example} onClick={()=>{setDraft(example);composerRef.current?.focus();}}><MessageCircle size={16}/><span>{example}</span></button>)}</div></div>}
    <ConnectionCards client={client} conversationId={id||undefined} open={connectionsOpen} onClose={()=>setConnectionsOpen(false)} onNavigate={onNavigate} refreshKey={conversation?.events_cursor} onConnectionsChange={setConnectionCards} selectedProfile={accessProfileRef} onSelectProfile={setAccessProfileRef} agentBackend={backendKind} agentModel={backendKind==='codex'?model:providerModelId}/>
    {conversation?.branched_from&&<p className="chat-branch-origin"><GitBranch size={14}/>Branched conversation · history through the selected message was copied.</p>}
    {historyMore&&<button className="history-load button outline compact" disabled={historyBusy} onClick={()=>void earlierHistory()}>{historyBusy?'Loading earlier activity…':'Load earlier activity'}</button>}
    {turns.map((turn,index)=><section className="chat-turn" key={turn.id} data-turn-id={turn.id} aria-label={turn.user?'Conversation turn '+(index+1):'Earlier conversation activity'}>
     {turn.legacy&&<p className="chat-history-label">Earlier activity</p>}
     {turn.user&&<article className="chat-message chat-message-user"><div className="chat-message-author">You{turn.user.created_at&&<time>{date(turn.user.created_at)}</time>}</div><div className="chat-message-content">{turn.user.content}</div><button className="message-branch" aria-label={'Branch from message '+turn.user.id} disabled={Boolean(branchBusy)} onClick={()=>void branch(turn.user!.id)}><GitBranch size={13}/>Branch from here</button></article>}
     <div className="assistant-turn"><div className="assistant-identity"><Logo symbol/><span>ORE</span></div>
      {turn.events.length>0&&<details className="chat-activity" open={(active||stopping)&&turn.id===(conversation?.messages??[]).filter(message=>message.role==='user').at(-1)?.id}><summary><span>{(active||stopping)&&index===turns.length-1?'Working on your request':'Progress and decisions'}</span><small>{turn.events.length} updates</small><ChevronDown size={15}/></summary><div className="chat-event-list">{turn.events.map(event=><PublicEvent key={String(event.id)} event={event}/>)}</div></details>}
      {turn.messages.filter(message=>message.role==='assistant').map(message=><article key={message.id} className="chat-message chat-message-assistant" data-message-id={message.id}><div className="chat-message-content"><Markdown>{message.content}</Markdown></div>{message.status!=='streaming'&&<button className="message-branch" aria-label={'Branch from message '+message.id} disabled={Boolean(branchBusy)} onClick={()=>void branch(message.id)}><GitBranch size={13}/>Branch from here</button>}{message.status==='streaming'&&<span className="message-stream-state" role="status">Receiving response…</span>}{message.status==='interrupted'&&<span className="message-stream-state">Response interrupted · received text saved</span>}{message.status==='failed'&&<span className="message-stream-state">Response could not finish · received text saved</span>}</article>)}
      {turn.plans.map(plan=><PlanCard key={plan.id} plan={plan} busy={controlBusy} stopped={stopping} onApprove={()=>void control('approve',plan.id)} onRevise={()=>{setMode('plan');composerRef.current?.focus();}}/>)}
      {turn.runs.map(run=><div className="chat-run" key={run.id}><RunProgress run={run} onNavigate={onNavigate} scheduler={scheduler}/><CollectionProgress value={run.collection_progress??record(run.progress).collection_progress}/>{allArtifacts.some(artifact=>artifact.job_id===run.job_id)&&<button className="turn-results-link" onClick={()=>setResultsOpen(true)}><FileText size={17}/>{allArtifacts.filter(artifact=>artifact.job_id===run.job_id).length} collected files<ChevronDown size={15}/></button>}</div>)}
     </div>
    </section>)}
    {conversation?.usage&&(conversation.planner_running||!currentRun)&&<RunDiagnostics run={{usage:conversation.usage}}/>}
    {(conversation?.questions??[]).length>0&&<div className="chat-questions"><span className="chat-card-caption">Waiting for your answer</span>{(conversation?.questions??[]).map((question,index)=>{const item=record(question);const prompt=typeof question==='string'?question:text(item.question??item.prompt??item.text??item.content,'');return prompt?<div key={text(item.id,String(index))}><p>{prompt}</p>{Array.isArray(item.options)&&<div className="chat-question-options">{item.options.map((option,optionIndex)=>{const value=typeof option==='string'?option:text(record(option).label??record(option).value,'');return <button className="button outline compact" key={optionIndex} onClick={()=>{setDraft(value);composerRef.current?.focus();}}>{value}</button>;})}</div>}</div>:null;})}</div>}
    {allHandoffs.map(handoff=><div key={text(handoff.id)} className="chat-handoff"><strong>Your input is needed</strong><p>{text(handoff.reason,'Open this request to continue.')}</p><button className="button outline compact" onClick={()=>onNavigate('/handoffs/'+encodeURIComponent(text(handoff.id,'')))}>Open request</button></div>)}
    {allArtifacts.length>0&&<section className="chat-results" aria-label="Collected files"><header><h2>Collected files <span>{allArtifacts.length}</span></h2>{allArtifacts.length>4&&<button className="text-button" onClick={()=>setResultsOpen(true)}>View all results</button>}</header>{allArtifacts.slice(0,4).map(artifact=><div key={artifact.id}><FileText size={17}/><span><strong>{text(artifact.filename??artifact.name,artifact.id)}</strong><small>{text(artifact.role,'File')}{artifact.status?' · '+label(artifact.status):''}</small></span><button className="icon-button" aria-label={'Download '+text(artifact.filename??artifact.name,artifact.id)} disabled={!artifact.job_id&&!safeUrl(artifact.url)} onClick={()=>void getArtifact(artifact)}><Download size={17}/></button></div>)}</section>}
    {(active||stopping)&&<div className="chat-working" role="status"><Spinner/>{stopping?'Stopping work. Waiting for workers to confirm.':conversation?.planner_running?'ORE is reviewing your request…':'ORE is working…'}</div>}
    {paused&&<p className="chat-paused">Work is paused. Completed results are saved. You can discuss changes before resuming.</p>}
    <div ref={bottomRef}/>
   </div>
  </div>
  <div className="chat-composer-wrap"><div className="chat-column">{(active||stopping)&&<CollectionProgress value={activeCollection} compact/>}
   {showLatest&&<button className="chat-latest button outline compact" onClick={goLatest}><ChevronDown size={14}/>Latest update</button>}
   {error&&<ErrorNotice message={error} onClose={()=>setError('')}/>}
   <form className="chat-composer" onSubmit={event=>{event.preventDefault();void send();}}>
    <textarea ref={composerRef} aria-label="Message ORE" rows={2} placeholder={paused?'Discuss changes, or resume when you’re ready…':active?'Ask about progress or change the instructions…':'Ask ORE to find, collect or extract…'} value={draft} onChange={event=>setDraft(event.target.value)} onKeyDown={event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.nativeEvent.isComposing){event.preventDefault();void send();}}}/>
    <div className="chat-composer-controls"><div className="chat-settings"><label><span className="sr-only">Chat access profile</span><select aria-label="Chat access profile" value={accessProfileRef} onChange={event=>setAccessProfileRef(event.target.value)}>{profileOptions.map(profile=><option key={profile.id} value={profile.id}>{profile.name??profile.id}</option>)}</select></label><label><span className="sr-only">Conversation mode</span><select aria-label="Conversation mode" value={mode} onChange={event=>setMode(event.target.value as typeof mode)}><option value="plan">Plan</option><option value="execute">Execute</option></select></label><label><span className="sr-only">Agent account</span><select aria-label="Agent account" value={backendKind} onChange={event=>setBackendKind(event.target.value)}><option value="codex">Codex</option><option value="claude_code">Claude Code</option><option value="openai">OpenAI API</option><option value="anthropic">Anthropic API</option></select></label>{backendKind!=='codex'?<>{backendKind==='claude_code'&&<select aria-label="Claude model" value={providerModel} onChange={event=>setProviderModel(event.target.value)}><option value="sonnet">Sonnet</option><option value="opus">Opus</option><option value="custom">Custom model</option></select>}{(apiBackend||providerModel==='custom')&&<input className="chat-custom-model" aria-label="Provider model ID" placeholder="Provider model ID" value={providerCustom} onChange={event=>setProviderCustom(event.target.value)}/>} {apiBackend&&<select aria-label="API connection" value={selectedApiConnection?.id??''} onChange={event=>setApiConnectionId(event.target.value)}><option value="">{apiKeyRef?'Saved conversation connection':'Connect an API key first'}</option>{apiConnections.map(card=><option key={card.id} value={card.id}>{label(card.provider)} · {card.access_profile_ref??card.id}</option>)}</select>}</>:<><label><span className="sr-only">Model policy</span><select aria-label="Model policy" value={policy} onChange={event=>setPolicy(event.target.value as typeof policy)}><option value="auto">Auto · quality first</option><option value="fixed">Fixed model</option></select></label>{policy==='fixed'&&<><select aria-label="Model" value={model} onChange={event=>{setModel(event.target.value);setEffort(modelEfforts(models.find(item=>(item.model??item.id)===event.target.value))[0]??'');}}><option value="" disabled>Select model</option>{models.map(item=><option key={item.id} value={item.model??item.id}>{item.displayName??item.name??item.id}</option>)}</select>{efforts.length>0&&<select aria-label="Reasoning effort" value={effort} onChange={event=>setEffort(event.target.value)}>{efforts.map(value=><option key={value} value={value}>{label(value)}</option>)}</select>}</>}</>}</div><div className="chat-send-actions">{(active||stopping)&&<button className="chat-stop" type="button" aria-label="Stop ORE" title="Stop planning and execution" disabled={controlBusy||stopping} onClick={()=>void control('stop')}><Square size={14} fill="currentColor"/>{stopping?'Stopping':'Stop'}</button>}<button type="submit" className="chat-send" aria-label="Send message" disabled={sending||!draft.trim()||invalidMessageSettings}>{sending?<Spinner/>:<ArrowUp size={20}/>}</button></div></div>{mode==='execute'&&Boolean(conversation?.approved_plan_id)&&!paused&&!stopping&&<label className="chat-continue"><input type="checkbox" checked={continueChanges} onChange={event=>setContinueChanges(event.target.checked)}/>Apply changes and continue</label>}
   </form><p className="chat-composer-note">{mode==='plan'?'Plan together. Collection starts after you approve a plan.':(!id||conversation?.execution_policy==='auto_within_scope'?'ORE carries out your request within the agreed scope. You can interrupt anytime.':'Direct the work. This conversation still requires approval for a new plan.')} <span>Enter to send · Shift + Enter for a new line</span></p>
  </div></div>
 </div>{resultsOpen&&<><button className="results-scrim" aria-label="Close results panel" onClick={()=>setResultsOpen(false)}/><aside className="chat-results-panel" aria-label="Collection results"><header><div><span className="chat-card-caption">Your collection</span><h2>Results</h2></div><button className="icon-button" aria-label="Close results" onClick={()=>setResultsOpen(false)}><X size={19}/></button></header><CollectionProgress value={activeCollection}/><div className="results-search"><Search size={16}/><input aria-label="Search collected files" placeholder="Search files or roles" value={fileSearch} onChange={event=>setFileSearch(event.target.value)}/></div><p className="results-count">{filteredArtifacts.length} files</p>{filteredArtifacts.map(artifact=><div className="result-file" key={artifact.id}><FileText size={18}/><div><strong>{text(artifact.filename??artifact.name,artifact.id)}</strong><small>{label(text(artifact.role,'file'))} · {label(text(artifact.status,'pending'))}</small>{artifact.sha256&&<details><summary>Verification</summary><code>{artifact.sha256}</code></details>}</div><button className="icon-button" aria-label={'Download result '+text(artifact.filename??artifact.name,artifact.id)} disabled={!artifact.job_id&&!safeUrl(artifact.url)} onClick={()=>void getArtifact(artifact)}><Download size={17}/></button></div>)}{!allArtifacts.length&&<p className="results-empty">Verified outputs will appear here as the work progresses.</p>}</aside></>}</div>
 </section>;
}

function PlanCard({plan,busy,stopped,onApprove,onRevise}:{plan:PlanRevision;busy:boolean;stopped:boolean;onApprove:()=>void;onRevise:()=>void}){
 const proposed=!plan.status||['proposed','draft','awaiting_approval'].includes(plan.status);const workflow=record(plan.workflow);const nodes=Array.isArray(plan.workflow)?plan.workflow:Array.isArray(workflow.nodes)?workflow.nodes:[];
 return <section className="chat-plan" aria-label={`Plan revision ${plan.revision}`}><header><span className="chat-card-caption">Plan · revision {plan.revision}</span><Status value={plan.status??'proposed'}/></header><h2>{plan.goal}</h2>{plan.summary&&<p>{plan.summary}</p>}{[['Scope',plan.scope],['Outputs',plan.outputs],['Conditions',plan.constraints],['Budget',plan.budget],['Completion checks',plan.acceptance]].map(([caption,value])=>{const entries=caption==='Completion checks'?reviewChecks(value):values(value);return entries.length>0?<div className="chat-plan-section" key={String(caption)}><h3>{String(caption)}</h3><ul>{entries.map((entry,index)=><li key={index}>{entry}</li>)}</ul></div>:null;})}{nodes.length>0&&<details><summary>Planned steps</summary><ol>{nodes.map((node,index)=>{const item=record(node);return <li key={text(item.id,String(index))}>{text(item.goal??item.title??item.description??item.tool??item.id,'Step '+(index+1))}</li>;})}</ol></details>}<footer>{proposed?<><button className="button primary" disabled={busy||stopped} onClick={onApprove}><Check size={15}/>Approve and run</button><button className="button subtle" onClick={onRevise}>Revise in chat</button>{stopped&&<small>Wait for workers to stop before approving this plan.</small>}</>:<span><Check size={15}/> {label(plan.status??'approved')}</span>}</footer></section>;
}
function PublicEvent({event}:{event:ConversationEvent}){
 const data=record(event.data);const snapshots=Array.isArray(data.runs)?data.runs:[];const nodes=snapshots.flatMap(run=>Array.isArray(record(run).nodes)?record(run).nodes as unknown[]:[]);const completed=nodes.filter(node=>['succeeded','skipped'].includes(text(record(node).status,''))).length;const summary=text(data.routing_reason??data.summary??data.message??data.reason??data.description,typeof event.data==='string'?event.data:nodes.length?completed+' of '+nodes.length+' known tasks complete.':'');
 const title=label(text(data.event_type,event.type).replaceAll('.',' '));const details=Object.fromEntries(['tool','capability','phase','model','reasoning_effort','effort','mode','status','stage','task_id','node_id','children','error_code','validation','elapsed_seconds','tokens','cost','quality'].filter(key=>data[key]!==undefined).map(key=>[key,data[key]]));
 return <div className="chat-event"><span className="chat-event-dot"/><div><strong>{title}</strong>{event.created_at&&<time>{date(event.created_at)}</time>}{summary&&<p>{summary}</p>}{Array.isArray(data.fields)&&data.fields.length>0&&<ul className="planning-field-errors">{data.fields.map((field,index)=>{const item=record(field);return <li key={index}>{Array.isArray(item.path)&&<strong>{item.path.join('.')} · </strong>}{text(item.message)}</li>;})}</ul>}{typeof data.action==='string'&&<p>{data.action}</p>}{Object.keys(details).length>0&&<details><summary>Execution details</summary><dl>{Object.entries(details).map(([key,value])=><div key={key}><dt>{label(key)}</dt><dd>{values(value).join(', ')}</dd></div>)}</dl></details>}</div></div>;
}
function RunProgress({run,onNavigate,scheduler}:{run:WorkflowRun;onNavigate:(path:string)=>void;scheduler:SchedulerSnapshot|null}){
 const progress=record(run.progress);const nodes=Array.isArray(run.nodes)?(run.nodes as JsonObject[]).filter(node=>!node.superseded&&node.status!=='superseded'):[];const complete=nodes.filter(node=>['succeeded','skipped'].includes(text(node.status??node.state,''))).length;const eta=record(run.eta??progress.eta);const range=record(eta.remaining_active_seconds);const low=Number(range.low),high=Number(range.high);const estimated=typeof range.low==='number'&&typeof range.high==='number'&&Number.isFinite(low)&&Number.isFinite(high);const quotaWaits=nodes.filter(node=>node.status==='retry_wait'&&record(node.error).code==='source_quota_wait');const etaText=quotaWaits.length?'Collection is waiting for a source quota reset. Completion time will be updated when retrieval resumes.':run.status==='completed'?'Execution completed.':estimated?`${Math.ceil(low/60)}–${Math.ceil(high/60)} min remaining`:text(eta.reason,'An estimate will appear once enough work has completed.');
 const capacity=scheduler?.jobs?.find(job=>job.job_id===run.job_id);
 return <div className="chat-run-progress"><div><Status value={run.status}/><span>{text(progress.summary??(progress.stage!==run.status?progress.stage:undefined),'Workflow')}</span>{run.job_id&&<button className="text-button" onClick={()=>onNavigate('/jobs/'+encodeURIComponent(run.job_id!))}>Inspect mission</button>}</div><p>{nodes.length>0?complete+' of '+nodes.length+' known tasks complete. ':''}{etaText}</p>{quotaWaits.length>0&&<div className="chat-source-wait" role="status">{quotaWaits.map(node=>{const issue=record(node.error);const next=typeof node.retry_at==='string'?node.retry_at:typeof issue.resets_at==='number'?new Date(issue.resets_at*1000).toISOString():undefined;return <p key={text(node.id)}>{label(text(issue.source,'Source'))} · {label(text(issue.operation,'retrieval'))} is waiting.{next?' Next check: '+date(next)+'.':' Reset time has not been reported.'}</p>;})}</div>}{nodes.length>0&&<details className="chat-run-nodes"><summary>Execution steps</summary><ol>{nodes.map(node=><li key={text(node.id)}><span>{text(node.goal??record(node.spec).goal??record(node.spec).tool??node.id)}</span><Status value={text(node.status??node.state,'pending')}/></li>)}</ol></details>}<RunDiagnostics run={run}/>{capacity&&scheduler?.global&&<details className="execution-capacity"><summary>Worker capacity</summary><p>Browser and network executor pool</p><dl>{[['Pool requested',scheduler.global.desired_executors],['Pool ready',scheduler.global.ready_executors],['Mission running',capacity.running],['Mission target',capacity.target],['Approved mission cap',capacity.approved_max],['Global task cap',scheduler.global.global_limit]].map(([title,value])=><div key={String(title)}><dt>{title}</dt><dd>{value}</dd></div>)}</dl>{capacity.reason&&<p>{capacity.reason}</p>}</details>}</div>;
}


function finiteMetric(value:unknown):value is number{return typeof value==='number'&&Number.isFinite(value)&&value>=0;}
function metricNumber(value:number):string{return new Intl.NumberFormat('en-US',{maximumFractionDigits:0}).format(value);}
function metricDuration(seconds:number):string{const value=Math.ceil(seconds);const hours=Math.floor(value/3600);const minutes=Math.floor(value%3600/60);return (hours?hours+'h ':'')+(minutes?minutes+'m ':'')+(value%60+'s');}

/** Public execution facts only. Missing telemetry is not a zero or a success. */
export function RunDiagnostics({run}:{run:Pick<WorkflowRun,'usage'|'execution_runtime'|'runtime_details'|'reuse'|'validation_strength'>}){
 const {usage,runtime_details:details,reuse,validation_strength:proof}=run;
 const runtime=run.execution_runtime;
 const modes:Record<string,string>={native:'Native agent',structured:'Structured agent',deterministic:'Deterministic tools'};
 const mode=runtime?modes[runtime]:undefined;
 const title=runtime?'Execution and budget':'Request budget';
 const rows:{name:string;value:string;warning?:boolean}[]=[];
 if(mode)rows.push({name:'Execution mode',value:mode});
 if(details?.model)rows.push({name:'Selected model',value:details.model+(details.effort?' · '+details.effort:'')});
 if(usage){
  if(finiteMetric(usage.elapsed_seconds))rows.push({name:'Active budget time',value:metricDuration(usage.elapsed_seconds)});
  if('remaining_seconds' in usage&&(usage.remaining_seconds===null||finiteMetric(usage.remaining_seconds)))rows.push({name:'Time budget remaining',value:usage.remaining_seconds===null?'Not limited':metricDuration(usage.remaining_seconds)});
  if(finiteMetric(usage.tokens?.totalTokens))rows.push({name:'Observed tokens',value:metricNumber(usage.tokens.totalTokens)+(usage.usage_complete===false?' · partial':'')});
  if('remaining_tokens' in usage&&(usage.remaining_tokens===null||finiteMetric(usage.remaining_tokens)))rows.push({name:'Token budget remaining',value:usage.remaining_tokens===null?'Not limited':metricNumber(usage.remaining_tokens)});
  if(finiteMetric(usage.provider_turns))rows.push({name:'Provider turns',value:metricNumber(usage.provider_turns)});
  if(finiteMetric(usage.token_overshoot)&&usage.token_overshoot>0)rows.push({name:'Token budget exceeded by',value:metricNumber(usage.token_overshoot),warning:true});
 }
 if(reuse){
  if(finiteMetric(reuse.verified_reuses))rows.push({name:'Verified recipe reuses',value:metricNumber(reuse.verified_reuses)});
  if(finiteMetric(reuse.candidate_recipes))rows.push({name:'Recipe candidates',value:metricNumber(reuse.candidate_recipes)});
  if(finiteMetric(reuse.promoted_recipes))rows.push({name:'Promoted recipes',value:metricNumber(reuse.promoted_recipes)});
 }
 const checks=proof?[...(proof.schema?['Output shape']:[]),...(proof.receipt?['Saved output receipts']:[]),...(proof.independent_goal?['Independent goal checks']:[]),...(proof.corpus?['Collection coverage']:[])]:[];
 if(checks.length)rows.push({name:'Completed checks',value:checks.join(' · ')});
 if(!rows.length&&!usage&&!details?.adaptation_status)return null;
 const adaptation=details?.adaptation_status;
 const adaptationText=adaptation==='awaiting_native_outcome_validation'?'Quality baseline · cheaper routes are not yet validated.':adaptation==='fixed'?'The selected model and effort are fixed.':undefined;
 const noModel=usage?.zero_model_calls===true&&usage.usage_complete!==false;
 return <details className="runtime-details" aria-label={title}><summary><span>{title}</span>{mode&&<small>{mode}</small>}</summary>
  <dl>{rows.map(row=><div key={row.name} className={row.warning?'runtime-warning':undefined}><dt>{row.name}</dt><dd>{row.value}</dd></div>)}</dl>
  {usage?.paused&&<p>Budget clock paused.</p>}
  {usage?.usage_complete===false&&<p className="runtime-warning" role="status">Usage is incomplete. Token totals may be partial.</p>}
  {noModel&&<p>No model calls in this budget scope.</p>}
  {adaptationText&&<p>{adaptationText}</p>}
  {reuse&&finiteMetric(reuse.candidate_recipes)&&reuse.candidate_recipes>0&&<p>Recipe candidates still need validation before automatic reuse.</p>}
  {proof?.schema&&!proof.independent_goal&&!proof.corpus&&<p>Output shape checks do not establish collection completeness.</p>}
  {usage&&<p className="runtime-footnote">Budget time is a limit, separate from the completion estimate. A provider turn may contain multiple model steps.</p>}
 </details>;
}
