import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {safeUrl} from '../lib/format';
export function Markdown({children}:{children:string}){return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{a:({href,children})=>{const url=safeUrl(href);return url?<a href={url} target="_blank" rel="noopener noreferrer">{children}</a>:<span>{children}</span>;},img:({alt})=><span className="markdown-image-label">{alt||'Referenced image'}</span>,table:({children})=><div className="markdown-table"><table>{children}</table></div>}}>{children}</ReactMarkdown></div>;}
