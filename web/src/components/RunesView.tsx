import { useCallback, useEffect, useState } from 'react';
import { BookOpen, CheckCircle2, FileCode2, Plus, Save, ShieldCheck } from 'lucide-react';
import { OreClient } from '@ore/sdk';
import type { JsonObject, Rune } from '@ore/sdk';
import { Empty, ErrorNotice, JsonView, Spinner } from './Common';
import { errorMessage, record, text } from '../lib/format';
const template=`schema_version: ore.rune/v0
protocol_id: my-journal
protocol_version: 0.1.0
instructions: |
  Discover original research articles within the mission scope.
  Retrieve the published PDF and all supplementary files.
  Record evidence for unavailable or uncertain material.
scope:
  origins: []
  artifact_roles: [main_pdf, supplement]
checks:
  - included_resources_have_main_pdf_status
  - included_resources_have_supplement_discovery_status
`;
export default function RunesView({client}:{client:OreClient}){
 const [runes,setRunes]=useState<Rune[]>([]);const [id,setId]=useState('');const [content,setContent]=useState('');const [error,setError]=useState('');const [busy,setBusy]=useState(false);const [result,setResult]=useState<JsonObject|null>(null);const [saved,setSaved]=useState(false);
 const load=useCallback(async()=>{try{setRunes(await client.runes());}catch(cause){setError(errorMessage(cause));}},[client]);useEffect(()=>{void load();},[load]);
 async function select(rune:Rune){setBusy(true);setError('');setResult(null);setSaved(false);try{const data=await client.getRune(rune.id);setId(rune.id);setContent(typeof data.content==='string'?data.content:JSON.stringify(data.content??data,null,2));}catch(cause){setError(errorMessage(cause));}finally{setBusy(false);}}
 async function validate(save=false){setBusy(true);setError('');setSaved(false);try{const data=await client.validateRune(content);setResult(data);const valid=data.valid===true||data.ok===true||data.status==='valid';if(save){if(!valid){setError('Resolve validation errors before saving this Rune.');return;}await client.saveRune(id,content);setSaved(true);await load();}}catch(cause){setError(errorMessage(cause));}finally{setBusy(false);}}
 return <section className="page"><header className="page-heading"><div><div className="eyebrow">REUSABLE CONTEXT</div><h1>Rune library</h1><p>Give agents a repeatable procedure and a verifiable definition of done.</p></div><button className="button primary" onClick={()=>{setId('my-journal');setContent(template);setResult(null);setSaved(false);}}><Plus size={16}/>New Rune</button></header>{error&&<ErrorNotice message={error} onClose={()=>setError('')}/>}
 <div className="rune-layout"><aside className="panel"><div className="panel-heading"><h3>Protocols <span>{runes.length}</span></h3><BookOpen size={16}/></div>{runes.map(rune=><button className={`rune-item ${rune.id===id?'active':''}`} key={rune.id} onClick={()=>void select(rune)}><FileCode2 size={18}/><span><strong>{rune.name||rune.id}</strong><small>{text(rune.version??record(rune.metadata).protocol_version,'Versioned protocol')}</small></span></button>)}{!runes.length&&<p className="session-empty">Create a Rune to capture a site's procedure.</p>}</aside>
 <div className="panel rune-editor">{!content?<Empty icon={<FileCode2 size={28}/>} title="Context that travels with the mission">Select a protocol to inspect its instructions, scope and checks.</Empty>:<><div className="editor-toolbar"><label>Protocol ID<input aria-label="Protocol ID" value={id} onChange={e=>{setId(e.target.value);setSaved(false);}} pattern="[A-Za-z0-9._-]+"/></label><div><button className="button subtle compact" disabled={busy} onClick={()=>void validate()}>{busy?<Spinner/>:<ShieldCheck size={16}/>}Validate</button><button className="button primary compact" disabled={busy||!id.trim()||!/^[A-Za-z0-9._-]+$/.test(id)} onClick={()=>void validate(true)}><Save size={15}/>Save version</button></div></div><textarea className="code-editor" aria-label="Rune contents" value={content} spellCheck={false} onChange={e=>{setContent(e.target.value);setResult(null);setSaved(false);}}/><div className="editor-footer"><span>YAML or JSON · validated before saving</span>{saved&&<span className="success-text"><CheckCircle2 size={14}/>Rune saved</span>}</div>{result&&<div className="validation-result"><h4>Validation result</h4><JsonView value={result}/></div>}</>}
 </div></div></section>;
}
