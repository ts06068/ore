import {useCallback,useEffect,useState} from 'react';
import {ArrowUpRight,Hand,RefreshCw} from 'lucide-react';
import {OreClient} from '@ore/sdk';
import type {AccessProfile,Handoff} from '@ore/sdk';
import {ErrorNotice,Spinner,Status} from './Common';
import BrowserView from './BrowserView';
import ChromeCompanion from './ChromeCompanion';
import DesktopChrome from './DesktopChrome';
import BrowserEnvironmentIssue,{safeExternalCheckpoint} from './BrowserEnvironmentIssue';
import {errorMessage,label,text} from '../lib/format';
export default function HandoffsView({client,profiles,id,onNavigate}:{client:OreClient;profiles:AccessProfile[];id?:string;onNavigate:(path:string)=>void}){
 const [items,setItems]=useState<Handoff[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const load=useCallback(async()=>{setItems(id?[await client.handoff(id)]:await client.handoffs());},[client,id]);
 useEffect(()=>{void load().catch(e=>setError(errorMessage(e)));const timer=setInterval(()=>void load().catch(e=>setError(errorMessage(e))),5000);return()=>clearInterval(timer);},[load]);
 async function act(item:Handoff,action:string){setBusy(true);setError('');try{await client.handoffAction(item,action);await load();}catch(e){setError(errorMessage(e));await load();}finally{setBusy(false);}}
 return <section className="page handoffs-page"><header className="page-heading"><div><div className="eyebrow">DURABLE USER REQUESTS</div><h1>Needs your input</h1><p>Requests remain here after a disconnect or server restart. Open the exact browser, verify access, then resume.</p></div><button className="button outline" onClick={()=>void load()}><RefreshCw size={16}/>Refresh</button></header>{error&&<ErrorNotice message={error} onClose={()=>setError('')}/>}
 {!items.length&&<div className="panel empty-state"><Hand/><h3>No active requests</h3><p>ORE will show account, challenge and provider requests here. Codex can retrieve these links using the ORE MCP status tools.</p></div>}
 {items.map(item=><article className="panel handoff-card" key={item.id}><div className="panel-heading"><h3>{label(item.kind)} · {item.source?label(item.source):'Browser access'}</h3><Status value={item.status}/></div><p>{item.reason}</p><div className="key-values"><div><span>Browser</span><strong>{label(item.session_state??'unavailable')}</strong></div><div><span>Operation</span><strong>{label(item.operation??'browser')}</strong></div><div><span>Access profile</span><strong>{item.access_profile_ref}</strong></div></div>{Array.isArray(item.setup_steps)&&<ol>{item.setup_steps.map((step,index)=><li key={index}>{String(step)}</li>)}</ol>}
 <BrowserEnvironmentIssue handoff={item}/>
 {id&&!item.superseded&&!['resolved','cancelled'].includes(item.status)&&<DesktopChrome client={client} handoff={item} onAttached={load}/>}
 {id&&!item.superseded&&!['resolved','cancelled'].includes(item.status)&&<ChromeCompanion client={client} handoff={item} onAttached={load}/>}
 {safeExternalCheckpoint(item.checkpoint_url)&&<p className="muted small">Starting page: {safeExternalCheckpoint(item.checkpoint_url)}</p>}<div className="handoff-actions"><button className="button subtle" onClick={()=>onNavigate(`/jobs/${item.job_id}`)}>Inspect mission<ArrowUpRight size={14}/></button>{!id&&<button className="button primary" onClick={()=>onNavigate(item.href)}>Open request<ArrowUpRight size={14}/></button>}
 {id&&!['resolved','cancelled'].includes(item.status)&&<>{item.session_state==='live'?<button className="button outline" disabled={busy} onClick={()=>void act(item,'claim')}>Take control</button>:<button className="button primary" disabled={busy||!item.checkpoint_url} onClick={()=>void act(item,'recreate_session')}>{busy?<Spinner/>:<Hand size={15}/>} {item.session_id?'Restore browser session':'Open provider browser'}</button>}
 {item.source&&<><button className="button outline" onClick={()=>onNavigate('/connections?profile='+encodeURIComponent(item.access_profile_ref??'public')+'&source='+encodeURIComponent(item.source??'')+'&operation='+encodeURIComponent(item.operation??'search'))}>Store key / verify API</button><button className="button outline" disabled={busy} onClick={()=>void act(item,'mark_pending')}>Mark approval pending</button><button className="button outline" disabled={busy} onClick={()=>void act(item,'exclude_operation')}>Exclude {text(item.operation,'search')} for this mission</button></>}
 <button className="button primary" disabled={busy} onClick={()=>void act(item,'resume')}>Verify & resume</button><button className="button subtle" disabled={busy} onClick={()=>void act(item,'cancel')}>Dismiss request</button></>}</div>
 {id&&!['resolved','cancelled'].includes(item.status)&&!item.superseded&&item.session_state==='live'&&item.session_id&&<BrowserView key={item.session_id} client={client} profiles={profiles} initialSession={item.session_id} embedded/>}</article>)}
 </section>;
}
