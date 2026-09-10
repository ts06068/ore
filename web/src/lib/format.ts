import type { JsonObject, Job } from '@ore/sdk';
export function record(value:unknown):JsonObject {return value && typeof value==='object'&&!Array.isArray(value)?value as JsonObject:{};}
export function text(value:unknown,fallback='—'):string {return typeof value==='string'&&value ? value : typeof value==='number'?String(value):fallback;}
export function number(value:unknown):number {return typeof value==='number'&&Number.isFinite(value)?value:0;}
export function jobName(job:Job):string {return text(job.name ?? record(job.mission).name,`Mission ${job.id.slice(0,8)}`);}
export function instructions(job:Job):string {return text(job.instructions ?? record(job.mission).instructions ?? record(job.mission).goal,'No instructions recorded.');}
export function date(value:unknown):string {if(typeof value!=='string')return '—';const stamp=new Date(value);return Number.isNaN(stamp.getTime())?value:stamp.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
export function bytes(value:unknown):string {if(typeof value!=='number')return '—';if(value<1024)return `${value} B`;const scale=Math.min(Math.floor(Math.log(value)/Math.log(1024)),4);return `${(value/1024**scale).toFixed(1)} ${['B','KB','MB','GB','TB'][scale]}`;}
export function label(value:string):string {return value.replaceAll('_',' ').replace(/\b\w/g,word=>word.toUpperCase());}
export function safeUrl(value:unknown):string|undefined {if(typeof value!=='string')return;try{const url=new URL(value);if(['https:','http:'].includes(url.protocol))return url.href;}catch{/* not an external URL */}}
export function errorMessage(error:unknown):string {return error instanceof Error?error.message:String(error);}
export function download(blob:Blob,filename:string){const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=filename;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
export function listText(value:unknown):string {return Array.isArray(value)?value.map(v=>typeof v==='string'?v:text(record(v).id)).join(', '):text(value);}
