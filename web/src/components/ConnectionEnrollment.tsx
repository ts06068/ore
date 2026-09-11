import {useCallback,useEffect,useRef,useState} from 'react';
import {Check,RefreshCw,ShieldCheck,X} from 'lucide-react';
import type {ConnectionCard,OreClient} from '@ore/sdk';
import {ErrorNotice,Field,Spinner,Status} from './Common';
import {date,record,safeUrl} from '../lib/format';

type FormAction={id:string;state_version:number;status:string;kind:string;url:string;handoff_href?:string;fields?:{label:string;protected:boolean}[];target?:{label:string;tag:string;type?:string};created_at?:string;message_code?:string};
const detailFields={given_name:'Given name',family_name:'Family name',affiliation:'Institution or affiliation',application_name:'Application name',website:'Application website'};
const titles:Record<string,string>={fill:'Fill the selected form fields',click:'Use the selected page control',capture_key:'Save API key from this field'};
function reviewBrowser(href:string|undefined):string|undefined{return href&&/^\/handoffs\/[A-Za-z0-9_-]+$/.test(href)?href:undefined;}
const states:Record<string,string>={stale:'The page has changed. Ask ORE for a fresh action before continuing.',uncertain:'The action result could not be confirmed. Check the provider page before continuing.',rejected:'You declined this action.',completed:'This action is complete.'};

/** Proposals contain review labels, never credentials or provider page contents. */
export function ConnectionEnrollment({client,card}:{client:OreClient;card:ConnectionCard}){
 const base='/v1/connections/'+encodeURIComponent(card.id);
 const [actions,setActions]=useState<FormAction[]>([]),[stale,setStale]=useState(false),[busy,setBusy]=useState(''),[error,setError]=useState('');
 const decisions=useRef<Record<string,string>>({});
 const refresh=useCallback(async()=>{
  try{const response=await client.request(base+'/form-actions');const rows=record(response).actions;if(Array.isArray(rows)){setActions(previous=>rows.map(item=>{const incoming=item as FormAction;const known=previous.find(action=>action.id===incoming.id);return known&&known.state_version>incoming.state_version?known:incoming;}));setStale(false);}}
  catch{setStale(true);}
 },[client,base]);
 useEffect(()=>{if(!card.job_id)return;void refresh();const timer=setInterval(()=>void refresh(),5000);return()=>clearInterval(timer);},[card.job_id,refresh]);
 async function decide(action:FormAction,decision:'approve'|'reject'){
  if(busy||stale||action.status!=='pending')return;setBusy(action.id);setError('');
  const key=action.id+':'+action.state_version+':'+decision;
  const idempotencyKey=decisions.current[key]??(decisions.current[key]=crypto.randomUUID());
  try{await client.request(base+'/form-actions/'+encodeURIComponent(action.id)+'/'+decision,{method:'POST',body:JSON.stringify({expected_version:action.state_version,idempotency_key:idempotencyKey})});await refresh();}
  catch{setStale(true);setError('The form action could not be confirmed. Refresh its status before trying again.');}
  finally{setBusy('');}
 }
 return <div className="connection-enrollment">
  {card.actions?.includes('request_agent')&&<EnrollmentDetails client={client} base={base}/>}
  {card.job_id&&<VerificationCode client={client} base={base}/>}
  {actions.length>0&&<section className="connection-form-actions" aria-label="Provider form actions">
   <h3>Provider setup</h3>
   {stale&&<p role="status">Form updates paused. Last known actions are shown.</p>}
   {error&&<ErrorNotice message={error}/>}
   {actions.map(action=><article key={action.id} className="connection-form-action" aria-label={titles[action.kind]??'Provider action'}>
    <header><strong>{titles[action.kind]??'Provider action'}</strong><Status value={action.status}/></header>
    {safeUrl(action.url)?<a href={safeUrl(action.url)} target="_blank" rel="noopener noreferrer">{action.url}</a>:<p>{action.url}</p>}
    {reviewBrowser(action.handoff_href)&&<div className="connection-browser-review"><a className="button outline compact" href={reviewBrowser(action.handoff_href)} target="_blank" rel="noopener noreferrer">Review this ORE browser</a><p>If you take control, ask ORE for a fresh action before approving.</p></div>}
    {action.target?.label&&<p>Selected control: <strong>{action.target.label}</strong>{action.target.type?' · '+action.target.type:''}</p>}
    {!!action.fields?.length&&<ul>{action.fields.map((field,index)=><li key={index}>{field.label} · {field.protected?'Use a saved credential':'Use saved registration details'}</li>)}</ul>}
    {action.kind==='capture_key'&&<p><ShieldCheck size={14}/>The key will go directly to protected storage and will not appear in chat.</p>}
    {action.status==='pending'?<><p>Review the page and selected action, then choose whether ORE should continue.</p><div className="connection-actions"><button className="button primary compact" disabled={Boolean(busy)||stale} onClick={()=>void decide(action,'approve')}>{busy===action.id?<Spinner/>:<Check size={14}/>}Approve action</button><button className="button outline compact" disabled={Boolean(busy)||stale} onClick={()=>void decide(action,'reject')}><X size={14}/>Decline</button></div></>:states[action.status]&&<p>{states[action.status]}</p>}
    {action.created_at&&<small>{date(action.created_at)}</small>}
   </article>)}
   <button className="button subtle compact" disabled={Boolean(busy)} onClick={()=>void refresh()}><RefreshCw size={14}/>Refresh form actions</button>
  </section>}
 </div>;
}

function EnrollmentDetails({client,base}:{client:OreClient;base:string}){
 const [values,setValues]=useState<Record<string,string>>({}),[loaded,setLoaded]=useState(false),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false),[error,setError]=useState('');
 async function load(){if(loaded)return;try{const response=record(await client.request(base+'/enrollment/details'));const stored=record(response.values);setValues(Object.fromEntries(Object.keys(detailFields).filter(key=>typeof stored[key]==='string').map(key=>[key,String(stored[key])])));setLoaded(true);}catch{setError('Registration details could not be loaded. Reopen this section to try again.');}}
 async function save(){if(!loaded||busy)return;setBusy(true);setError('');setSaved(false);try{await client.request(base+'/enrollment/details',{method:'POST',body:JSON.stringify({values})});setSaved(true);}catch{setError('Registration details could not be saved. Your entries are kept here.');}finally{setBusy(false);}}
 return <details className="connection-registration" onToggle={event=>{if(event.currentTarget.open)void load();}}>
  <summary>Registration details</summary><p>Provide only the details needed for your account. ORE can use saved details to fill provider forms.</p>
  <form onSubmit={event=>{event.preventDefault();void save();}}>
   {Object.entries(detailFields).map(([key,label])=><Field key={key} label={label}><input type={key==='website'?'url':'text'} maxLength={500} disabled={!loaded||busy} value={values[key]??''} autoComplete="off" onChange={event=>{setSaved(false);setValues(current=>({...current,[key]:event.target.value}));}}/></Field>)}
   <button className="button outline compact" disabled={!loaded||busy}>{busy?<Spinner/>:null}Save registration details</button>
  </form>{saved&&<p role="status">Registration details saved.</p>}{error&&<ErrorNotice message={error}/>}
 </details>;
}


function VerificationCode({client,base}:{client:OreClient;base:string}){
 const [value,setValue]=useState(''),[busy,setBusy]=useState(false),[saved,setSaved]=useState(false),[error,setError]=useState('');
 async function save(){if(!value||busy)return;const code=value;setValue('');setBusy(true);setSaved(false);setError('');try{await client.request(base+'/enrollment/secret',{method:'POST',body:JSON.stringify({field:'mfa_code',value:code})});setSaved(true);}catch{setError('The verification code could not be saved. Enter a fresh code when the provider requests it.');}finally{setBusy(false);}}
 return <details className="connection-registration"><summary>One-time verification code</summary><p>If the provider requests a code, enter it here. It goes directly to protected storage and is not added to chat.</p><form onSubmit={event=>{event.preventDefault();void save();}}><Field label="Provider verification code"><input type="password" autoComplete="off" spellCheck={false} maxLength={100} value={value} onChange={event=>{setSaved(false);setValue(event.target.value);}}/></Field><button className="button outline compact" disabled={!value||busy}>{busy?<Spinner/>:<ShieldCheck size={14}/>}Save verification code</button></form>{saved&&<p role="status">Verification code saved for this setup step.</p>}{error&&<ErrorNotice message={error}/>}</details>;
}
