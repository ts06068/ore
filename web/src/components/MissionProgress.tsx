import {useEffect,useState} from 'react';
import {OreClient} from '@ore/sdk';
import type {Job,JsonObject,Source} from '@ore/sdk';
import {ErrorNotice,Status} from './Common';
import {errorMessage,label,record,text} from '../lib/format';
export function MissionProgress({job,onNavigate}:{job:Job;onNavigate:(path:string)=>void}){
 const progress=record(job.progress),eta=record(progress.eta),range=record(eta.remaining_active_seconds),requests=Array.isArray(job.handoffs)?job.handoffs as JsonObject[]:[];
 const duration=(seconds:unknown)=>typeof seconds==='number'?`${Math.ceil(seconds/60)} min`:'—';
 return <div className="panel progress-panel"><div><strong>Estimated processing time</strong> <Status value={text(eta.state,'estimating')}/><h3>{eta.state==='available'?`${duration(range.low)} – ${duration(range.high)}`:eta.state==='complete'?'Complete':label(text(eta.state,'estimating'))}</h3><p>{text(eta.reason,'Waiting for inventory and observations.')}</p><small>{Number(eta.sample_count??0)} completed samples · Inventory {progress.inventory_known?'verified':'still open'} · User/provider waiting time is excluded.</small></div>{requests.length>0&&<div className="request-links"><strong>{requests.length} request{requests.length===1?'':'s'} need your input</strong>{requests.map(h=><button className="button primary" key={text(h.id)} onClick={()=>onNavigate(text(h.href))}>{text(h.reason,'Open browser request').slice(0,100)} →</button>)}</div>}</div>;
}
const operations=['search','resolve','browser','download','import'];
export function SourcePolicyEditor({client,job,sources,onChanged}:{client:OreClient;job:Job;sources:Source[];onChanged:()=>void}){
 const [excluded,setExcluded]=useState<Record<string,string>>({}),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{const values=record(record(record(job.mission).source_policy).exclude);setExcluded(Object.fromEntries(operations.map(op=>[op,Array.isArray(values[op])?(values[op] as string[]).join(', '):''])));},[job.id,job.revision]);
 async function save(){setBusy(true);setError('');try{const mission=record(job.mission),policy=record(mission.source_policy);const exclude=Object.fromEntries(operations.map(op=>[op,(excluded[op]??'').split(',').map(s=>s.trim()).filter(Boolean)]));await client.reviseJob(job.id,{mission:{source_policy:{schema_version:'ore.source-policy/v1',...policy,exclude}}});onChanged();}catch(e){setError(errorMessage(e));}finally{setBusy(false);}}
 return <details className="panel policy-editor"><summary>Source permissions & pending API exclusions</summary><p>Exclude specific source IDs per operation. Excluding WoS search keeps its browser route available unless you also exclude browser.</p><p className="muted small">Available IDs: {sources.map(s=>s.id).join(', ')}</p>{operations.map(op=><label key={op}>{label(op)} exclusions<input value={excluded[op]??''} onChange={e=>setExcluded({...excluded,[op]:e.target.value})} placeholder={op==='search'?'wos, scopus':'Comma-separated source IDs'}/></label>)}{error&&<ErrorNotice message={error}/>}<button className="button outline" disabled={busy} onClick={()=>void save()}>Save a mission revision</button></details>;
}
