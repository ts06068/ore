import { AlertCircle, ArrowUpRight, LoaderCircle, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { label, safeUrl } from '../lib/format';
export function Logo(){return <div className="brand"><svg viewBox="0 0 40 40" aria-hidden="true"><path d="M20 2 37 12v16L20 38 3 28V12zm0 8-10 6v8l10 6 10-6v-8z" fill="currentColor"/></svg><span>ORE<span className="brand-dot">.</span></span></div>;}
export function Status({value='unknown'}:{value?:string}){return <span className={`status status-${value}`}><i/>{label(value)}</span>;}
export function Spinner(){return <LoaderCircle size={16} className="spin" aria-label="Loading"/>;}
export function Empty({icon,title,children,action}:{icon?:ReactNode;title:string;children?:ReactNode;action?:ReactNode}){return <div className="empty">{icon&&<div className="empty-icon">{icon}</div>}<h3>{title}</h3><p>{children}</p>{action}</div>;}
export function ErrorNotice({message,onClose}:{message:string;onClose?:()=>void}){return <div className="error-notice" role="alert"><AlertCircle size={18}/><span>{message}</span>{onClose&&<button aria-label="Dismiss error" className="icon-button" onClick={onClose}><X size={16}/></button>}</div>;}
export function ExternalLink({href,children}:{href:unknown;children:ReactNode}){const url=safeUrl(href);return url?<a href={url} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={13}/></a>:<>{children}</>;}
export function JsonView({value}:{value:unknown}){return <pre className="json-view">{JSON.stringify(value,null,2)}</pre>;}
export function Field({label:caption,hint,children}:{label:string;hint?:string;children:ReactNode}){return <label className="field"><span>{caption}</span>{children}{hint&&<small>{hint}</small>}</label>;}
