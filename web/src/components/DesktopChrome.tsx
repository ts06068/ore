import {useEffect,useRef,useState} from 'react';
import {Monitor} from 'lucide-react';
import type {Handoff,JsonObject,OreClient} from '@ore/sdk';
import {ErrorNotice,Spinner} from './Common';
import {safeExternalCheckpoint} from './BrowserEnvironmentIssue';
import {errorMessage} from '../lib/format';

type Props={client:OreClient;handoff:Handoff;onAttached:()=>Promise<void>};
export default function DesktopChrome({client,handoff,onAttached}:Props){
 const [preview,setPreview]=useState<JsonObject|null>(null),[previewError,setPreviewError]=useState('');
 const [busy,setBusy]=useState(false),[error,setError]=useState('');
 const pending=useRef(false),attempt=useRef<{id:string;version:number;action:'attach'|'close';key:string}|null>(null);
 const historical=Boolean(handoff.superseded)||['resolved','cancelled'].includes(handoff.status);
 useEffect(()=>{let alive=true;setPreview(null);setPreviewError('');if(historical||!safeExternalCheckpoint(handoff.checkpoint_url)||handoff.browser_transport==='desktop_chrome'&&handoff.session_state==='live')return;void client.request<JsonObject>('/v1/desktop/preview?handoff_id='+encodeURIComponent(handoff.id)).then(value=>{if(alive)setPreview(value);}).catch(cause=>{if(alive)setPreviewError(errorMessage(cause));});return()=>{alive=false;};},[client,handoff.id,handoff.state_version,handoff.checkpoint_url,historical,handoff.browser_transport,handoff.session_state]);
 if(historical)return null;
 const attached=handoff.browser_transport==='desktop_chrome'&&handoff.session_state==='live';
 async function change(action:'attach'|'close'){
  if(pending.current||(action==='attach'&&(!preview?.eligible||preview.expected_version!==handoff.state_version)))return;
  pending.current=true;setBusy(true);setError('');
  try{
   if(!attempt.current||attempt.current.id!==handoff.id||attempt.current.version!==handoff.state_version||attempt.current.action!==action)
    attempt.current={id:handoff.id,version:handoff.state_version,action,key:crypto.randomUUID()};
   await client.request<Handoff>(`/v1/desktop/${action}`,{method:'POST',body:JSON.stringify({
    handoff_id:handoff.id,expected_version:handoff.state_version,idempotency_key:attempt.current.key})});
   await onAttached();
  }catch(cause){
   setError(errorMessage(cause));
   // A concurrent change may have made the displayed version stale.
   try{await onAttached();}catch{/* Preserve the original actionable error. */}
  }finally{pending.current=false;setBusy(false);}
 }
 return <section className="panel" aria-label="ORE Chrome desktop">
  <h3>Chrome in ORE</h3>
  <p>Open this request’s original checkpoint in ordinary Chrome on an ORE-owned virtual desktop. No browser extension is needed. You keep control after it opens; opening the desktop does not resume the mission.</p>
  {!attached&&preview?.eligible===true&&<div className="desktop-access-preview"><strong>Access for this browser session</strong><p>The session can visit these approved origins:</p><ul>{(Array.isArray(preview.origins)?preview.origins:[]).map(origin=><li key={String(origin)}><code>{String(origin)}</code></li>)}</ul><small>The mission’s collection scope and challenge limits are preserved.</small></div>}
  {!attached&&previewError&&<ErrorNotice message={previewError}/>}
  {attached?<><p role="status">ORE Chrome desktop is attached. Complete any access checks in the browser below, then use Verify &amp; resume.</p><button className="button outline" disabled={busy} onClick={()=>void change('close')}>{busy&&<Spinner/>}Close virtual Chrome</button></>:<button className="button primary" disabled={busy||!safeExternalCheckpoint(handoff.checkpoint_url)||preview?.eligible!==true||preview.expected_version!==handoff.state_version} onClick={()=>void change('attach')}>{busy?<Spinner/>:<Monitor size={16}/>}Use ORE Chrome desktop</button>}
  {error&&<ErrorNotice message={error}/>}
 </section>;
}
