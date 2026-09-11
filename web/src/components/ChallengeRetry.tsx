import {RefreshCw} from 'lucide-react';
import type {Handoff} from '@ore/sdk';
import {record,text} from '../lib/format';
import {isHistoricalHandoff} from '../lib/conversation';

export function ChallengeRetry({handoff,busy,onRetry}:{handoff:Handoff;busy:boolean;onRetry:()=>void}){
 const retry=record(handoff.automatic_retry);const context=record(handoff.conversation_context);
 const attempts=retry.max_attempts,seconds=retry.max_seconds;
 const live=handoff.session_state==='live'&&Boolean(handoff.session_id);
 const restore=handoff.session_state==='lost'&&handoff.browser_transport==='desktop_chrome';
 if(retry.eligible!==true||isHistoricalHandoff(handoff)||context.phase!=='execution'||(typeof context.active_execution_job_id==='string'&&context.active_execution_job_id!==handoff.job_id)||!handoff.challenge_id||(!live&&!restore)
    ||typeof attempts!=='number'||!Number.isInteger(attempts)||attempts<1||typeof seconds!=='number'||!Number.isFinite(seconds)||seconds<=0)return null;
 return <section className="info-callout handoff-access-note" aria-label="Automatic verification retry"><strong>Automatic verification retry</strong>
  <p>{text(retry.reason,'The previous automatic verification window ended.')}</p>
  <p>{restore?'Restore this collection’s Chrome browser and authorize':'Authorize'} up to {attempts} automatic verification {attempts===1?'attempt':'attempts'} within {seconds} seconds. Previous attempts and elapsed time remain recorded.</p>
  <p>ORE must verify that the requested journal page has loaded before access is marked complete.</p>
  <button className="button outline" disabled={busy} onClick={onRetry}><RefreshCw size={15}/>{restore?'Restore Chrome and retry once':'Retry automatic verification'}</button>
 </section>;
}
