/** ORE's transport client. It does not invoke a model or own credentials. */
export type JsonObject = Record<string, unknown>;
/** Optional route ordering; evidence and completeness requirements remain independent. */
export interface RetrievalPolicy {mode:'api_open_access_first'|'official_first';browser_fallback:boolean;}
export interface Mission extends JsonObject {retrieval_policy?:RetrievalPolicy;}
export interface Job extends JsonObject {
  id: string; name?: string; instructions?: string; status?: string;
  created_at?: string; updated_at?: string; revision?: number;
  mission?: Mission; coverage?: JsonObject; report?: JsonObject;
}
export interface Resource extends JsonObject {
  id: string; title?: string; doi?: string; source?: string;
  status?: string; eligibility?: string; year?: number; url?: string;
}
export interface Artifact extends JsonObject {
  id: string; resource_id?: string; role?: string; filename?: string;
  status?: string; bytes?: number; sha256?: string; url?: string;
}
export interface Handoff extends JsonObject {id:string;job_id:string;kind:string;status:string;reason:string;state_version:number;href:string;session_id?:string;session_state?:string;control_epoch?:number;source?:string;operation?:string;access_profile_ref?:string;checkpoint_url?:string;}
export interface SchedulerSnapshot extends JsonObject {global:{global_limit:number;executor_limit:number;desired_executors:number;ready_executors:number;pinned_executors:number;resource_pressure?:unknown;observed_at?:string}|null;jobs:{job_id:string;target:number;approved_max:number;initial:number;running:number;runnable:number;ready_executor_capacity:number;reason:string;stable_windows:number;control_epoch:number}[];pool?:JsonObject|null;}
export interface Progress extends JsonObject {stage:string;needs_user:boolean;eta:{state:string;reason:string;sample_count:number;remaining_active_seconds?:{low:number;high:number}|null;};}
export interface Source extends JsonObject { id: string; name?: string; enabled?: boolean; status?: string; }
export interface Model extends JsonObject { id: string; model?: string; displayName?: string; name?: string; supportedReasoningEfforts?: unknown[]; }
export interface BrowserSession extends JsonObject { id: string; job_id?: string; url?: string; control?: string; epoch?: number; status?: string; }
export interface Rune extends JsonObject { id: string; name?: string; content?: string; version?: string; }
export interface AccessProfile extends JsonObject { id: string; name?: string; network?: string; origins?: string[]; secret_refs?: Record<string,string>; }
export interface OreEvent { id?: string; event: string; data: unknown; retry?: number; }
/** Durable chat content. Public progress summaries are separate from model internals. */
export interface ConversationMessage extends JsonObject {id:string;role:'user'|'assistant'|'system'|'tool';content:string;created_at?:string;operator_message_id?:string;sequence?:number;status?:'streaming'|'complete'|'interrupted'|'failed';}
export interface ConversationEvent extends JsonObject {id:string|number;type:string;data:unknown;created_at?:string;operator_message_id?:string;}
export interface PlanRevision extends JsonObject {id:string;revision:number;goal:string;status?:string;summary?:string;scope?:unknown;outputs?:unknown;constraints?:unknown;budget?:unknown;acceptance?:unknown;workflow?:unknown;created_at?:string;operator_message_id?:string;}
export interface CollectionProgress extends JsonObject {
 denominator:{status:'unknown'|'provisional'|'sealed';basis:string;total:number|null};
 resources:{discovered:number;included:number;complete:number;unresolved:number;excluded?:number;classification_unresolved?:number};
 roles:{role:string;expected:number|null;verified:number;missing:number;unknown:number;declared?:number}[];
 issues:{discovered:number;complete:number;total:number|null};artifacts?:{known:number;verified:number};supplements?:{none_confirmed:number;inventory_unknown:number;present:number};kind?:'scholarly'|'resources'|'artifacts';gaps:{count:number;by_kind:Record<string,number>};
}
/** Observed shared planning/execution budget; tokens are not monetary charges. */
export interface RunUsage extends JsonObject {
 tokens?:Record<string,number>;provider_turns?:number;elapsed_seconds?:number;
 remaining_seconds?:number|null;remaining_tokens?:number|null;usage_complete?:boolean;
 zero_model_calls?:boolean;token_overshoot?:number;paused?:boolean;
}
export type ExecutionRuntime='native'|'structured'|'deterministic';
export interface RuntimeDetails extends JsonObject {adapter_version?:string;adaptation_status?:string;model?:string;effort?:string;}
export interface RecipeReuse extends JsonObject {verified_reuses?:number;candidate_recipes?:number;promoted_recipes?:number;}
/** These dimensions are distinct; shape checks alone do not prove the goal. */
export interface ValidationStrength extends JsonObject {schema?:boolean;receipt?:boolean;independent_goal?:boolean;corpus?:boolean;}
export interface WorkflowRun extends JsonObject {
 id:string;status:string;job_id?:string;progress?:JsonObject;collection_progress?:CollectionProgress;
 operator_message_id?:string;plan_id?:string;eta?:JsonObject;artifacts?:Artifact[];handoffs?:Handoff[];
 execution_runtime?:ExecutionRuntime;runtime_details?:RuntimeDetails;usage?:RunUsage;
 reuse?:RecipeReuse;validation_strength?:ValidationStrength;
}
export interface ConversationFolder extends JsonObject {id:string;title:string;created_at?:string;updated_at?:string;}
export interface ConversationSummary extends JsonObject {id:string;title:string;status:string;folder_id?:string|null;branched_from?:{conversation_id:string;message_id:string}|null;execution_policy?:'auto_within_scope'|'explicit';planner_running?:boolean;active_plan_id?:string;created_at?:string;updated_at?:string;}
export interface Conversation extends ConversationSummary {id:string;title:string;status:string;messages:ConversationMessage[];questions?:unknown[];plans:PlanRevision[];active_plan_id?:string;active_run_id?:string;runs:WorkflowRun[];usage?:RunUsage;events?:ConversationEvent[];events_cursor?:string|number;planner_running?:boolean;created_at?:string;updated_at?:string;}
export interface ConversationSettings extends JsonObject {title?:string;folder_id?:string|null;execution_policy?:'auto_within_scope'|'explicit';mode?:'plan'|'execute';model_policy?:'auto'|'fixed';model?:string;reasoning_effort?:string;routing?:JsonObject;backend?:JsonObject;constraints?:JsonObject;budget?:JsonObject;}
export interface ConversationInput extends JsonObject {content:string;backend?:JsonObject;mode?:'plan'|'execute';model_policy?:'auto'|'fixed';model?:string;reasoning_effort?:string;change_and_continue?:boolean;}
/** Persistent public connection state. Credential values never appear in cards. */
export interface ConnectionCard extends JsonObject {id:string;provider:string;kind:'model'|'source';conversation_id?:string|null;access_profile_ref?:string;operation:string;status:string;state_version:number;actions:string[];handoff_id?:string;handoff_href?:string;job_id?:string;verification_url?:string;user_code?:string;expires_at?:string|number|null;credential_fields:string[];configured_fields:string[];login_code_required?:boolean;message_code?:string;backend?:JsonObject;message?:string;observed_at?:string;}
export interface ProviderQuota extends JsonObject {status:'available'|'unknown'|'unsupported'|'stale';windows:{id:string;used_percent?:number;remaining_percent?:number;window_minutes?:number;resets_at?:string|number|null}[];ordinary_usage_allowed:boolean|null;observed_at?:string;stale:boolean;}
export interface ProviderStatus extends JsonObject {provider:string;auth:{status:string;mode?:string;plan_type?:string;observed_at?:string|number};quota:ProviderQuota;usage?:JsonObject;}
export interface SourceQuota extends JsonObject {source?:string;credential_id?:string|null;quota_group?:string;status:string;remaining?:number|null;limit?:number|null;resets_at?:string|number|null;observed_at?:string|number|null;scope?:string;access_profile_ref?:string;}
export interface ProviderStatusSnapshot {providers:ProviderStatus[];source_quotas?:SourceQuota[];}
export interface BrowserFrame { type: 'frame'; data: string; width: number; height: number; frame_id: string; epoch: number; control: string; }
export interface BrowserInput { type: 'input'; action: 'click'|'scroll'|'key'|'text'; epoch: number; frame_id: string; x?: number; y?: number; deltaY?: number; key?: string; text?: string; }
export interface OreClientOptions { baseUrl: string; token?: string | (()=>string); fetch?: typeof fetch; }
export class OreApiError extends Error {
  constructor(public status: number, public detail: unknown, public requestId?: string) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail)); this.name = 'OreApiError';
  }
}
export function asItems<T>(value: unknown, ...keys: string[]): T[] {
  if (Array.isArray(value)) return value as T[];
  if (value && typeof value === 'object') {
    const record = value as JsonObject;
    for (const key of [...keys, 'items', 'data', 'results']) if (Array.isArray(record[key])) return record[key] as T[];
  }
  return [];
}
/** Incremental SSE parser: preserves multiline data, event IDs and split UTF-8. */
export async function* parseEventStream(body: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<OreEvent> {
  const reader = body.getReader(); const decoder = new TextDecoder();
  let buffer = ''; let event = 'message'; let id: string|undefined; let retry: number|undefined; let data: string[]=[];
  const line = (value: string): OreEvent|undefined => {
    if (value === '') {
      if (!data.length) { event='message'; retry=undefined; return; }
      const raw=data.join('\n'); let parsed: unknown=raw;
      try { parsed=JSON.parse(raw); } catch { /* plain text events remain text */ }
      const output: OreEvent={event,data:parsed}; if(id !== undefined) output.id=id; if(retry !== undefined) output.retry=retry;
      data=[]; event='message'; retry=undefined; return output;
    }
    if(value.startsWith(':')) return;
    const colon=value.indexOf(':'); const key=colon < 0 ? value : value.slice(0,colon);
    let text=colon < 0 ? '' : value.slice(colon+1); if(text.startsWith(' ')) text=text.slice(1);
    if(key==='data') data.push(text);
    else if(key==='event') event=text || 'message';
    else if(key==='id' && !text.includes('\0')) id=text;
    else if(key==='retry' && /^\d+$/.test(text)) retry=Number(text);
  };
  const cancel=()=>{void reader.cancel().catch(()=>undefined);};
  signal?.addEventListener('abort',cancel,{once:true});
  try {
    while(!signal?.aborted) {
      const chunk=await reader.read();
      buffer+=decoder.decode(chunk.value,{stream:!chunk.done});
      // Wait for a possible LF when CR is the final character in a network chunk.
      let match: RegExpExecArray|null;
      while((match=/\r\n|\n|\r/.exec(buffer))) {
        if(match[0]==='\r' && match.index===buffer.length-1 && !chunk.done) break;
        const value=buffer.slice(0,match.index); buffer=buffer.slice(match.index+match[0].length);
        const item=line(value); if(item) yield item;
      }
      if(chunk.done) break;
    }
    // SSE requires a blank line to dispatch: incomplete trailing messages are not committed.
  } finally { signal?.removeEventListener('abort',cancel); await reader.cancel().catch(()=>undefined); reader.releaseLock(); }
}
const segment=(value:string)=>encodeURIComponent(value);
export class OreClient {
  readonly baseUrl: string; private readonly fetcher: typeof fetch; private readonly token: string|(()=>string);
  constructor(options: OreClientOptions) {
    this.baseUrl=options.baseUrl.replace(/\/+$/,''); this.fetcher=options.fetch ?? globalThis.fetch.bind(globalThis); this.token=options.token ?? '';
  }
  private headers(extra?: HeadersInit): Headers {
    const headers=new Headers(extra); const token=typeof this.token==='function' ? this.token() : this.token;
    if(token) headers.set('Authorization',`Bearer ${token}`); return headers;
  }
  url(path:string):string { return `${this.baseUrl}${path.startsWith('/')?'':'/'}${path}`; }
  async response(path:string, init:RequestInit={}):Promise<Response> {
    const headers=this.headers(init.headers);
    if(init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type','application/json');
    const response=await this.fetcher(this.url(path), {...init,headers,credentials:'same-origin'});
    if(!response.ok) {
      const text=await response.text(); let detail:unknown=text || response.statusText;
      try { const body=JSON.parse(text); detail=body.detail ?? body.error ?? body; } catch { /* preserve server text */ }
      throw new OreApiError(response.status,detail,response.headers.get('X-Request-Id') ?? undefined);
    }
    return response;
  }
  async request<T=JsonObject>(path:string,init:RequestInit={}):Promise<T> {
    const response=await this.response(path,init); if(response.status===204) return undefined as T;
    return await response.json() as T;
  }
  async listJobs():Promise<Job[]> {return asItems(await this.request('/v1/jobs'),'jobs');}
  health():Promise<{status:string;version:string}> {return this.request('/healthz');}
  async conversationFolders():Promise<ConversationFolder[]> {return asItems(await this.request('/v1/conversation-folders'),'folders');}
  listConversationFolders():Promise<ConversationFolder[]> {return this.conversationFolders();}
  createConversationFolder(title:string):Promise<ConversationFolder> {return this.request('/v1/conversation-folders',{method:'POST',body:JSON.stringify({title})});}
  updateConversationFolder(id:string,title:string):Promise<ConversationFolder> {return this.request(`/v1/conversation-folders/${segment(id)}`,{method:'PATCH',body:JSON.stringify({title})});}
  deleteConversationFolder(id:string):Promise<void> {return this.request(`/v1/conversation-folders/${segment(id)}`,{method:'DELETE'});}
  updateConversation(id:string,input:{title?:string;folder_id?:string|null}):Promise<Conversation> {return this.request(`/v1/conversations/${segment(id)}`,{method:'PATCH',body:JSON.stringify(input)});}
  branchConversation(id:string,input:{message_id:string;title?:string;folder_id?:string|null}):Promise<Conversation> {return this.request(`/v1/conversations/${segment(id)}/branch`,{method:'POST',body:JSON.stringify(input)});}
  async connections(conversationId?:string):Promise<ConnectionCard[]> {return asItems(await this.request('/v1/connections'+(conversationId?'?conversation_id='+segment(conversationId):'')),'connections');}
  listConnections(conversationId?:string):Promise<ConnectionCard[]> {return this.connections(conversationId);}
  createConnection(input:{provider:string;conversation_id?:string;access_profile_ref?:string;operation?:string}):Promise<ConnectionCard> {return this.request('/v1/connections',{method:'POST',body:JSON.stringify(input)});}
  connectionAction(card:ConnectionCard,action:string,idempotencyKey=crypto.randomUUID(),extra:{agent_backend?:string;agent_model?:string}={}):Promise<ConnectionCard> {return this.request(`/v1/connections/${segment(card.id)}/actions`,{method:'POST',body:JSON.stringify({action,expected_version:card.state_version,idempotency_key:idempotencyKey,...extra})});}
  connectionSecret(card:ConnectionCard,field:string,value:string,idempotencyKey=crypto.randomUUID()):Promise<ConnectionCard> {return this.request(`/v1/connections/${segment(card.id)}/secret`,{method:'POST',body:JSON.stringify({field,value,expected_version:card.state_version,idempotency_key:idempotencyKey})});}
  connectionLoginCode(card:ConnectionCard,code:string,idempotencyKey=crypto.randomUUID()):Promise<ConnectionCard> {return this.request(`/v1/connections/${segment(card.id)}/login-code`,{method:'POST',body:JSON.stringify({code,expected_version:card.state_version,idempotency_key:idempotencyKey})});}
  providerStatusSnapshot():Promise<ProviderStatusSnapshot> {return this.request('/v1/providers/status');}
  async providerStatuses():Promise<ProviderStatus[]> {return asItems(await this.request('/v1/providers/status'),'providers');}
  async conversations():Promise<ConversationSummary[]> {return asItems(await this.request('/v1/conversations'),'conversations');}
  createConversation(input:ConversationSettings={}):Promise<Conversation> {return this.request('/v1/conversations',{method:'POST',body:JSON.stringify(input)});}
  conversationEventHistory(id:string,options:{before?:number;limit?:number}={}):Promise<{events:ConversationEvent[];has_more:boolean;before_cursor:number|null;events_cursor:number}> {const query=new URLSearchParams();if(options.before!==undefined)query.set('before',String(options.before));if(options.limit!==undefined)query.set('limit',String(options.limit));return this.request(`/v1/conversations/${segment(id)}/event-history${query.size?'?'+query:''}`);}
  conversation(id:string):Promise<Conversation> {return this.request(`/v1/conversations/${segment(id)}`);}
  sendMessage(id:string,input:ConversationInput):Promise<Conversation> {return this.request(`/v1/conversations/${segment(id)}/messages`,{method:'POST',body:JSON.stringify(input)});}
  approvePlan(id:string,planId:string):Promise<Conversation> {return this.request(`/v1/conversations/${segment(id)}/plans/${segment(planId)}/approve`,{method:'POST',body:'{}'});}
  interruptConversation(id:string):Promise<Conversation> {return this.request(`/v1/conversations/${segment(id)}/interrupt`,{method:'POST',body:'{}'});}
  resumeConversation(id:string):Promise<Conversation> {return this.request(`/v1/conversations/${segment(id)}/resume`,{method:'POST',body:'{}'});}
  async *conversationEvents(id:string,options:{after?:string;signal?:AbortSignal;onOpen?:()=>void}={}):AsyncGenerator<OreEvent> {
    const query=options.after?`?after=${encodeURIComponent(options.after)}`:'';
    const response=await this.response(`/v1/conversations/${segment(id)}/events${query}`,{headers:{Accept:'text/event-stream',...(options.after?{'Last-Event-ID':options.after}:{})},signal:options.signal});
    if(!response.body)throw new Error('The conversation event stream did not return a response body.');
    options.onOpen?.();
    yield* parseEventStream(response.body,options.signal);
  }
  createJob(mission:JsonObject):Promise<Job> {return this.request('/v1/jobs',{method:'POST',body:JSON.stringify(mission)});}
  getJob(id:string):Promise<Job> {return this.request(`/v1/jobs/${segment(id)}`);}
  reviseJob(id:string,patch:JsonObject):Promise<Job> {return this.request(`/v1/jobs/${segment(id)}`,{method:'PATCH',body:JSON.stringify(patch)});}
  action(id:string,action:'run'|'pause'|'resume'|'refresh'|'audit'):Promise<JsonObject> {return this.request(`/v1/jobs/${segment(id)}/${action}`,{method:'POST',body:'{}'});}
  async resources(id:string):Promise<Resource[]> {return asItems(await this.request(`/v1/jobs/${segment(id)}/resources`),'resources');}
  async artifacts(id:string):Promise<Artifact[]> {return asItems(await this.request(`/v1/jobs/${segment(id)}/artifacts`),'artifacts');}
  async artifactFile(jobId:string,artifactId:string):Promise<Blob> {return (await this.response(`/v1/jobs/${segment(jobId)}/artifacts/${segment(artifactId)}/file`)).blob();}
  async exportJob(id:string,format='jsonl'):Promise<Blob> {return (await this.response(`/v1/jobs/${segment(id)}/export?format=${encodeURIComponent(format)}`)).blob();}
  async models():Promise<Model[]> {return asItems(await this.request('/v1/models'),'models');}
  async sources(profile='public'):Promise<Source[]> {return asItems(await this.request('/v1/sources?access_profile_id='+encodeURIComponent(profile)),'sources');}
  sourceSetup(source:string,body:JsonObject):Promise<Handoff> {return this.request(`/v1/sources/${segment(source)}/setup`,{method:'POST',body:JSON.stringify(body)});}
  sourceCheck(source:string,body:JsonObject):Promise<JsonObject> {return this.request(`/v1/sources/${segment(source)}/check`,{method:'POST',body:JSON.stringify(body)});}
  async handoffs(jobId?:string):Promise<Handoff[]> {return asItems(await this.request('/v1/handoffs?status=active'+(jobId?'&job_id='+segment(jobId):'')));}
  handoff(id:string):Promise<Handoff> {return this.request(`/v1/handoffs/${segment(id)}`);}
  handoffAction(item:Handoff,action:string,extra:JsonObject={}):Promise<Handoff> {return this.request(`/v1/handoffs/${segment(item.id)}/actions`,{method:'POST',body:JSON.stringify({action,expected_version:item.state_version,expected_epoch:item.control_epoch,idempotency_key:crypto.randomUUID(),...extra})});}
  scheduler():Promise<SchedulerSnapshot> {return this.request('/v1/scheduler');}
  progress(id:string):Promise<Progress> {return this.request(`/v1/jobs/${segment(id)}/progress`);}
  coverage(id:string):Promise<JsonObject> {return this.request(`/v1/jobs/${segment(id)}/coverage`);}
  async workers():Promise<JsonObject[]> {return asItems(await this.request('/v1/workers'),'workers');}
  async runes():Promise<Rune[]> {return asItems(await this.request('/v1/runes'),'runes');}
  getRune(id:string):Promise<Rune> {return this.request(`/v1/runes/${segment(id)}`);}
  saveRune(id:string,content:string):Promise<Rune> {return this.request(`/v1/runes/${segment(id)}`,{method:'PUT',body:JSON.stringify({content})});}
  validateRune(content:string):Promise<JsonObject> {return this.request('/v1/runes/validate',{method:'POST',body:JSON.stringify({content})});}
  async accessProfiles():Promise<AccessProfile[]> {return asItems(await this.request('/v1/access-profiles'),'profiles','access_profiles');}
  saveAccessProfile(profile:JsonObject):Promise<AccessProfile> {return this.request('/v1/access-profiles',{method:'POST',body:JSON.stringify(profile)});}
  authStatus():Promise<JsonObject> {return this.request('/v1/auth/status');}
  async browserSessions():Promise<BrowserSession[]> {return asItems(await this.request('/v1/browser/sessions'),'sessions');}
  browserControl(id:string,action:'takeover'|'resume'):Promise<BrowserSession> {return this.request(`/v1/browser/${segment(id)}/${action}`,{method:'POST',body:'{}'});}
  browserSocket(id:string):{url:string;protocols:string[]} {
    const url=new URL(this.url(`/v1/browser/${segment(id)}/stream`)); url.protocol=url.protocol==='https:'?'wss:':'ws:';
    const token=typeof this.token==='function' ? this.token() : this.token;
    const bytes=new TextEncoder().encode(token); let text=''; for(const byte of bytes) text+=String.fromCharCode(byte);
    const encoded=btoa(text).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
    return {url:url.href, protocols:token?['ore.v1',`ore.token.${encoded}`]:['ore.v1']};
  }
  async *events(id:string,options:{after?:string;signal?:AbortSignal}={}):AsyncGenerator<OreEvent> {
    const query=options.after?`?after=${encodeURIComponent(options.after)}`:'';
    const response=await this.response(`/v1/jobs/${segment(id)}/events${query}`,{headers:{Accept:'text/event-stream'},signal:options.signal});
    if(!response.body) throw new Error('The event stream did not return a response body.');
    yield* parseEventStream(response.body,options.signal);
  }
}
