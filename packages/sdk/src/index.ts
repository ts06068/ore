/** ORE's transport client. It does not invoke a model or own credentials. */
export type JsonObject = Record<string, unknown>;
export interface Job extends JsonObject {
  id: string; name?: string; instructions?: string; status?: string;
  created_at?: string; updated_at?: string; revision?: number;
  mission?: JsonObject; coverage?: JsonObject; report?: JsonObject;
}
export interface Resource extends JsonObject {
  id: string; title?: string; doi?: string; source?: string;
  status?: string; eligibility?: string; year?: number; url?: string;
}
export interface Artifact extends JsonObject {
  id: string; resource_id?: string; role?: string; filename?: string;
  status?: string; bytes?: number; sha256?: string; url?: string;
}
export interface Source extends JsonObject { id: string; name?: string; enabled?: boolean; status?: string; }
export interface Model extends JsonObject { id: string; model?: string; displayName?: string; name?: string; supportedReasoningEfforts?: unknown[]; }
export interface BrowserSession extends JsonObject { id: string; job_id?: string; url?: string; control?: string; epoch?: number; status?: string; }
export interface Rune extends JsonObject { id: string; name?: string; content?: string; version?: string; }
export interface AccessProfile extends JsonObject { id: string; name?: string; network?: string; origins?: string[]; secret_refs?: Record<string,string>; }
export interface OreEvent { id?: string; event: string; data: unknown; retry?: number; }
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
  createJob(mission:JsonObject):Promise<Job> {return this.request('/v1/jobs',{method:'POST',body:JSON.stringify(mission)});}
  getJob(id:string):Promise<Job> {return this.request(`/v1/jobs/${segment(id)}`);}
  reviseJob(id:string,patch:JsonObject):Promise<Job> {return this.request(`/v1/jobs/${segment(id)}`,{method:'PATCH',body:JSON.stringify(patch)});}
  action(id:string,action:'run'|'pause'|'resume'|'refresh'|'audit'):Promise<JsonObject> {return this.request(`/v1/jobs/${segment(id)}/${action}`,{method:'POST',body:'{}'});}
  async resources(id:string):Promise<Resource[]> {return asItems(await this.request(`/v1/jobs/${segment(id)}/resources`),'resources');}
  async artifacts(id:string):Promise<Artifact[]> {return asItems(await this.request(`/v1/jobs/${segment(id)}/artifacts`),'artifacts');}
  async artifactFile(jobId:string,artifactId:string):Promise<Blob> {return (await this.response(`/v1/jobs/${segment(jobId)}/artifacts/${segment(artifactId)}/file`)).blob();}
  async exportJob(id:string,format='jsonl'):Promise<Blob> {return (await this.response(`/v1/jobs/${segment(id)}/export?format=${encodeURIComponent(format)}`)).blob();}
  async models():Promise<Model[]> {return asItems(await this.request('/v1/models'),'models');}
  async sources():Promise<Source[]> {return asItems(await this.request('/v1/sources'),'sources');}
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
