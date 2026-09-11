import {useCallback,useEffect,useRef,useState} from 'react';
import {ArrowUpRight,Hand,RefreshCw} from 'lucide-react';
import {OreClient} from '@ore/sdk';
import type {AccessProfile,Handoff} from '@ore/sdk';
import {ErrorNotice,Spinner,Status} from './Common';
import BrowserView from './BrowserView';
import ChromeCompanion from './ChromeCompanion';
import DesktopChrome from './DesktopChrome';
import BrowserEnvironmentIssue,{safeExternalCheckpoint} from './BrowserEnvironmentIssue';
import {ChallengeRetry} from './ChallengeRetry';
import {errorMessage,label,record,text} from '../lib/format';
import {isHistoricalHandoff} from '../lib/conversation';
export default function HandoffsView({client,profiles,id,onNavigate}:{client:OreClient;profiles:AccessProfile[];id?:string;onNavigate:(path:string)=>void}){
 const [items,setItems]=useState<Handoff[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const actionPending=useRef(false);
 const retryAttempt=useRef<{id:string;version:number;epoch?:number;key:string}|null>(null);
 const load=useCallback(async()=>{setItems(id?[await client.handoff(id)]:await client.handoffs());},[client,id]);
 useEffect(()=>{void load().catch(e=>setError(errorMessage(e)));const timer=setInterval(()=>void load().catch(e=>setError(errorMessage(e))),5000);return()=>clearInterval(timer);},[load]);
 async function act(item:Handoff,action:string){
  if(actionPending.current)return;actionPending.current=true;setBusy(true);setError('');
  try{
   if(action==='retry_verification'&&(!retryAttempt.current||retryAttempt.current.id!==item.id||retryAttempt.current.version!==item.state_version||retryAttempt.current.epoch!==item.control_epoch))retryAttempt.current={id:item.id,version:item.state_version,epoch:item.control_epoch,key:crypto.randomUUID()};
   await client.handoffAction(item,action,action==='retry_verification'?{idempotency_key:retryAttempt.current!.key}:{});await load();
  }catch(e){setError(errorMessage(e));try{await load();}catch{/* Keep the original action error; never replay a mutation during refresh. */}}
  finally{actionPending.current=false;setBusy(false);}
 }
 return <section className="page handoffs-page"><header className="page-heading"><div><div className="eyebrow">DURABLE USER REQUESTS</div><h1>Needs your input</h1><p>Requests remain here after a disconnect or server restart. Each request applies only to its own planning or collection step.</p></div><button className="button outline" onClick={()=>void load()}><RefreshCw size={16}/>Refresh</button></header>{error&&<ErrorNotice message={error} onClose={()=>setError('')}/>}
 {!items.length&&<div className="panel empty-state"><Hand/><h3>No active requests</h3><p>ORE will show account, challenge and provider requests here. Codex can retrieve these links using the ORE MCP status tools.</p></div>}
 {items.map(item=>{const context=record(item.conversation_context);const historical=isHistoricalHandoff(item);const planning=context.phase==='planning';return <article className="panel handoff-card" key={item.id}><div className="panel-heading"><h3>{label(item.kind)} · {item.source?label(item.source):'Browser access'}</h3><Status value={item.status}/></div><p>{item.reason}</p>{planning&&<section className="info-callout handoff-access-note" aria-label="Planning request context"><strong>{historical?'Earlier planning request':'Planning browser request'}</strong><p>{historical?'This request belongs to an earlier planning step. It cannot resume the current collection.':'Access verification applies to this planning session. It does not start the collection.'}</p>{typeof context.active_execution_job_id==='string'&&<button className="text-button" onClick={()=>onNavigate('/jobs/'+encodeURIComponent(context.active_execution_job_id as string))}>Inspect current collection</button>}{typeof context.conversation_id==='string'&&<button className="text-button" onClick={()=>onNavigate('/chat/'+encodeURIComponent(context.conversation_id as string))}>Return to conversation</button>}</section>}<div className="key-values"><div><span>Browser</span><strong>{label(item.session_state??'unavailable')}</strong></div><div><span>Operation</span><strong>{label(item.operation??'browser')}</strong></div><div><span>Access profile</span><strong>{item.access_profile_ref}</strong></div></div>{Array.isArray(item.setup_steps)&&<ol>{item.setup_steps.map((step,index)=><li key={index}>{String(step)}</li>)}</ol>}
 <BrowserEnvironmentIssue handoff={item}/>
 {id&&!historical&&<ChallengeRetry handoff={item} busy={busy} onRetry={()=>void act(item,'retry_verification')}/>}
 {id&&!historical&&<DesktopChrome client={client} handoff={item} onAttached={load}/>}
 {id&&!historical&&<ChromeCompanion client={client} handoff={item} onAttached={load}/>}
 {safeExternalCheckpoint(item.checkpoint_url)&&<p className="muted small">Starting page: {safeExternalCheckpoint(item.checkpoint_url)}</p>}<div className="handoff-actions"><button className="button subtle" onClick={()=>onNavigate(`/jobs/${item.job_id}`)}>Inspect mission<ArrowUpRight size={14}/></button>{!id&&<button className="button primary" onClick={()=>onNavigate(item.href)}>{historical?'Inspect request':'Open request'}<ArrowUpRight size={14}/></button>}
 {id&&!historical&&<>{item.session_state==='live'?<button className="button outline" disabled={busy} onClick={()=>void act(item,'claim')}>Take control</button>:<button className="button primary" disabled={busy||!item.checkpoint_url} onClick={()=>void act(item,'recreate_session')}>{busy?<Spinner/>:<Hand size={15}/>} {item.session_id?'Restore browser session':'Open provider browser'}</button>}
 {item.source&&<><button className="button outline" onClick={()=>onNavigate('/connections?profile='+encodeURIComponent(item.access_profile_ref??'public')+'&source='+encodeURIComponent(item.source??'')+'&operation='+encodeURIComponent(item.operation??'search'))}>Store key / verify API</button><button className="button outline" disabled={busy} onClick={()=>void act(item,'mark_pending')}>Mark approval pending</button><button className="button outline" disabled={busy} onClick={()=>void act(item,'exclude_operation')}>Exclude {text(item.operation,'search')} for this mission</button></>}
 <button className="button primary" disabled={busy} onClick={()=>void act(item,'resume')}>{planning?'Verify planning access':'Verify & resume'}</button><button className="button subtle" disabled={busy} onClick={()=>void act(item,'cancel')}>Dismiss request</button></>}</div>
 {id&&!historical&&item.session_state==='live'&&item.session_id&&<BrowserView key={item.session_id} client={client} profiles={profiles} initialSession={item.session_id} embedded/>}</article>;})}
 </section>;
}
