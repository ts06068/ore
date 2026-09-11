import type {Handoff} from '@ore/sdk';
import {record} from '../lib/format';

export function safeExternalCheckpoint(value:unknown):string|undefined {
 if(typeof value!=='string')return;
 try {
  const url=new URL(value);
  if(!['https:','http:'].includes(url.protocol)||url.username||url.password)return;
  url.search='';url.hash='';
  return url.href;
 } catch {return;}
}

const messages:Record<string,{heading:string;message:string}>={
 automation_signal_observed:{heading:'Browser automation signal observed',message:'ORE’s browser reports that it is controlled by automation. This signal does not confirm a compatibility failure or a publisher’s reason for challenging the session.'},
 debug_automation_detected:{heading:'Compatibility check detected automation',message:'Cloudflare’s official compatibility page reported an automated browser. This diagnostic does not establish why a publisher challenged or rejected the session.'},
 unverified_automation_message:{heading:'Browser diagnostic needs review',message:'An automation-related message was recorded without confirmed official diagnostic-page context. A publisher’s rejection reason has not been established.'},
 automation_rejected:{heading:'Recorded browser diagnostic',message:'An earlier diagnostic detected browser automation. Review its recorded evidence; this does not establish a publisher’s rejection reason.'},
};

export default function BrowserEnvironmentIssue({handoff}:{handoff:Handoff}) {
 const issue=record(handoff.browser_environment??handoff.browser_environment_issue);
 const code=typeof issue.code==='string'?issue.code:'';
 const evidence=record(issue.evidence);
 const confirmed=code==='debug_automation_detected'&&evidence.official_debug_page===true&&evidence.explicit_automation_message===true;
 const content=messages[code==='debug_automation_detected'&&!confirmed?'unverified_automation_message':code];
 if(!content)return null;
 const checkpoint=safeExternalCheckpoint(handoff.checkpoint_url);
 const observedAt=typeof issue.observed_at==='string'&&/^\d{4}-\d{2}-\d{2}T[0-9:.+Z-]+$/.test(issue.observed_at)?issue.observed_at:'';
 const evidenceRef=typeof issue.evidence_ref==='string'&&/^(report|observation):[A-Za-z0-9_.:/-]{1,160}$/.test(issue.evidence_ref)?issue.evidence_ref:'';
 const source=issue.source_reference==='https://debug.challenges.cloudflare.com/'||issue.source_reference==='https://developers.cloudflare.com/cloudflare-challenges/troubleshooting/challenge-solve-issues/'?issue.source_reference:undefined;
 return <section className="browser-environment-issue" aria-label="Browser environment issue" role="status">
  <h3>{content.heading}</h3>
  <p>{content.message}</p>
  <p>This is the last recorded browser diagnostic. If this session keeps failing, compare access in your regular browser or use an available API or open-access route.</p>
  <p>Your browser opens a separate session. Its login and verification session do not transfer to ORE automatically. ORE must pass its own access check before resuming this browser task.</p>
  {checkpoint&&<a className="button outline" href={checkpoint} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">Open in your browser ↗</a>}
  {(observedAt||evidenceRef||source)&&<details><summary>Recorded diagnostic</summary>{observedAt&&<p>Observed: {observedAt}</p>}{evidenceRef&&<p>Evidence: {evidenceRef}</p>}{source&&<p><a href={source} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">Diagnostic source ↗</a></p>}</details>}
 </section>;
}
