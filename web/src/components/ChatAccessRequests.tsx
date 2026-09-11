import type {Conversation,JsonObject,WorkflowRun} from '@ore/sdk';
import {conversationHandoffs} from '../lib/conversation';
import {record,text} from '../lib/format';

export function ChatAccessRequests({conversation,requests,currentRun,requestsLoaded,onNavigate}:{conversation:Conversation|null;requests:JsonObject[];currentRun?:WorkflowRun;requestsLoaded:boolean;onNavigate:(path:string)=>void}){
 const {active,history}=conversationHandoffs(conversation,requests);
 const currentRequests=active.filter(item=>item.job_id===currentRun?.job_id);
 const nodes=Array.isArray(currentRun?.nodes)?currentRun.nodes.map(record):[];
 const accessNode=nodes.find(node=>['awaiting_auth','awaiting_user'].includes(text(node.status,'')));
 const blocked=Boolean(currentRun&&(['awaiting_auth','awaiting_user'].includes(currentRun.status)||accessNode));
 const problem=record(accessNode?.error);const httpStatus=typeof problem.status==='number'&&Number.isInteger(problem.status)&&problem.status>=400&&problem.status<600?problem.status:undefined;
 return <>
  {blocked&&<section className="chat-handoff" aria-label="Current collection access" role="status"><strong>Collection is waiting for access</strong><p>{httpStatus?`The current collection received HTTP ${httpStatus}.`:'The current collection requires access verification before it can continue.'} {currentRequests.length?'Use the collection request below to verify its own browser session.':requestsLoaded?'No browser request is attached to this collection yet.':'Checking for a browser request for this collection…'}</p>{requestsLoaded&&!currentRequests.length&&<p>Inspect the current mission or ask ORE in this chat to try another permitted retrieval route. A browser request from an earlier planning step does not resume this collection.</p>}{currentRun?.job_id&&<button className="button outline compact" onClick={()=>onNavigate('/jobs/'+encodeURIComponent(currentRun.job_id!))}>Inspect current mission</button>}</section>}
  {active.map(item=>{const planning=record(item.conversation_context).phase==='planning'||item.job_id===record(conversation?.planning).job_id;return <section key={text(item.id)} className="chat-handoff" aria-label={planning?'Planning access request':'Collection access request'}><strong>{planning?'Planning needs browser access':'Your input is needed for this collection'}</strong><p>{text(item.reason,'Open this request to continue.')}</p>{planning&&<p>This request belongs to planning. Verifying it does not start the collection.</p>}<button className="button outline compact" onClick={()=>onNavigate('/handoffs/'+encodeURIComponent(text(item.id,'')))}>{planning?'Open planning request':'Open collection request'}</button></section>;})}
  {history.length>0&&<details className="chat-handoff"><summary>Earlier browser requests · {history.length}</summary><p>These requests belong to earlier planning or execution steps. They do not resume the current collection.</p>{history.map(item=><div key={text(item.id)}><p>{text(item.reason,'Earlier browser request')}</p><button className="text-button" onClick={()=>onNavigate('/handoffs/'+encodeURIComponent(text(item.id,'')))}>Inspect earlier request</button></div>)}</details>}
 </>;
}
