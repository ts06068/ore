import { AlertCircle, ArrowUpRight, LoaderCircle, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { label, safeUrl } from '../lib/format';
export function Logo({symbol=false,className=''}:{symbol?:boolean;className?:string}){const asset=symbol?'ore-symbol':'ore-original';return <span className={`brand ${symbol?'brand-symbol':''} ${className}`}><img className="brand-light" src={`/brand/${asset}.svg`} alt="ORE"/><img className="brand-dark" src={`/brand/${symbol?'ore-symbol-inverse':'ore-inverse'}.svg`} alt="" aria-hidden="true"/></span>;}
export function Status({value='unknown'}:{value?:string}){return <span className={`status status-${value}`}><i/>{label(value)}</span>;}
export function Spinner(){return <LoaderCircle size={16} className="spin" aria-label="Loading"/>;}
export function Empty({icon,title,children,action}:{icon?:ReactNode;title:string;children?:ReactNode;action?:ReactNode}){return <div className="empty">{icon&&<div className="empty-icon">{icon}</div>}<h3>{title}</h3><p>{children}</p>{action}</div>;}
export function ErrorNotice({message,onClose}:{message:string;onClose?:()=>void}){return <div className="error-notice" role="alert"><AlertCircle size={18}/><span>{message}</span>{onClose&&<button aria-label="Dismiss error" className="icon-button" onClick={onClose}><X size={16}/></button>}</div>;}
export function ExternalLink({href,children}:{href:unknown;children:ReactNode}){const url=safeUrl(href);return url?<a href={url} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={13}/></a>:<>{children}</>;}
export function JsonView({value}:{value:unknown}){return <pre className="json-view">{JSON.stringify(value,null,2)}</pre>;}
export function Field({label:caption,hint,children}:{label:string;hint?:string;children:ReactNode}){return <label className="field"><span>{caption}</span>{children}{hint&&<small>{hint}</small>}</label>;}
