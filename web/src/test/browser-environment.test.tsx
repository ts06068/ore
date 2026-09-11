import {it,expect,vi} from 'vitest';
import {render,screen,fireEvent,waitFor} from '@testing-library/react';
import {OreClient} from '@ore/sdk';
import type {Handoff} from '@ore/sdk';
import BrowserEnvironmentIssue,{safeExternalCheckpoint} from '../components/BrowserEnvironmentIssue';
import HandoffsView from '../components/HandoffsView';

const handoff:Handoff={id:'h',job_id:'j',kind:'challenge',status:'needs_user',reason:'Access check is waiting',state_version:3,href:'/handoffs/h',session_state:'lost',checkpoint_url:'https://journal.test/article?token=private-token#private-fragment',browser_environment_issue:{code:'automation_rejected',message:'The recorded diagnostic detected browser automation.',evidence_ref:'report:browser-check',observed_at:'2026-09-10T00:00:00Z',cookie:'private-cookie'}};

it('shows only an explicitly recorded automation issue and a safe separate-browser link',()=>{
 render(<BrowserEnvironmentIssue handoff={handoff}/>);
 expect(screen.getByRole('status')).toHaveTextContent('detected browser automation');
 expect(screen.getByRole('status')).toHaveTextContent('do not transfer to ORE automatically');
 expect(screen.getByRole('status')).toHaveTextContent('ORE must pass its own access check');
 const link=screen.getByRole('link',{name:'Open in your browser ↗'});
 expect(link).toHaveAttribute('href','https://journal.test/article');
 expect(link).toHaveAttribute('target','_blank');
 expect(link).toHaveAttribute('rel','noopener noreferrer');
 expect(link).toHaveAttribute('referrerpolicy','no-referrer');
 expect(screen.getByRole('status')).not.toHaveTextContent('private-token');
 expect(screen.getByRole('status')).not.toHaveTextContent('private-cookie');
});

it('does not infer an environment rejection from an ordinary challenge',()=>{
 render(<BrowserEnvironmentIssue handoff={{...handoff,browser_environment_issue:undefined}}/>);
 expect(screen.queryByRole('status')).not.toBeInTheDocument();
 expect(screen.queryByRole('link')).not.toBeInTheDocument();
});

it('rejects script, data and credential-bearing external links',()=>{
 for(const url of ['javascript:alert(1)','data:text/html,x','https://user:password@journal.test/article','//journal.test/article'])expect(safeExternalCheckpoint(url)).toBeUndefined();
});

it('external link does not resume ORE, copy cookies, or send a mutation',async()=>{
 const fetcher=vi.fn(async(_input:RequestInfo|URL,_init?:RequestInit)=>new Response(JSON.stringify(handoff)));
 const client=new OreClient({baseUrl:'https://ore.test',fetch:fetcher});
 document.cookie='ore-fixture-cookie=unchanged';
 const cookie=document.cookie;
 render(<HandoffsView client={client} profiles={[]} id="h" onNavigate={()=>undefined}/>);
 const link=await screen.findByRole('link',{name:'Open in your browser ↗'});
 link.addEventListener('click',event=>event.preventDefault());
 fireEvent.click(link);
 await waitFor(()=>expect(fetcher.mock.calls.some(([url])=>String(url).includes('/desktop/preview'))).toBe(true));
 expect(fetcher.mock.calls.every(([,init])=>!init?.method||init.method==='GET')).toBe(true);
 expect(document.cookie).toBe(cookie);
 expect(screen.getByRole('button',{name:'Verify & resume'})).toBeInTheDocument();
 expect(document.body).not.toHaveTextContent('private-token');
 expect(document.body).not.toHaveTextContent('private-cookie');
 document.cookie='ore-fixture-cookie=;max-age=0';
});


it('shows a runtime signal without claiming an official debug or publisher rejection',()=>{
 render(<BrowserEnvironmentIssue handoff={{...handoff,browser_environment:{code:'automation_signal_observed',explanation:'private-page-content',evidence:{navigator_webdriver:true,publisher_block_confirmed:false},source_reference:'https://developers.cloudflare.com/cloudflare-challenges/troubleshooting/challenge-solve-issues/'}}}/>);
 expect(screen.getByRole('heading',{name:'Browser automation signal observed'})).toBeInTheDocument();
 expect(screen.getByRole('status')).toHaveTextContent('does not confirm a compatibility failure');
 expect(screen.getByRole('status')).toHaveTextContent('last recorded browser diagnostic');
 expect(screen.getByRole('status')).not.toHaveTextContent('private-page-content');
 expect(screen.queryByRole('heading',{name:'Compatibility check detected automation'})).not.toBeInTheDocument();
});

it('shows explicit official debug evidence with a limited conclusion',()=>{
 render(<BrowserEnvironmentIssue handoff={{...handoff,browser_environment:{code:'debug_automation_detected',evidence:{official_debug_page:true,explicit_automation_message:true,publisher_block_confirmed:false},source_reference:'https://debug.challenges.cloudflare.com/'}}}/>);
 expect(screen.getByRole('heading',{name:'Compatibility check detected automation'})).toBeInTheDocument();
 expect(screen.getByRole('status')).toHaveTextContent('does not establish why a publisher');
 expect(screen.getByRole('link',{name:'Diagnostic source ↗'})).toHaveAttribute('href','https://debug.challenges.cloudflare.com/');
});

it('requires explicit evidence before presenting a debug finding',()=>{
 render(<BrowserEnvironmentIssue handoff={{...handoff,browser_environment:{code:'debug_automation_detected',evidence:{navigator_webdriver:true},source_reference:'javascript:alert(1)'}}}/>);
 expect(screen.getByRole('heading',{name:'Browser diagnostic needs review'})).toBeInTheDocument();
 expect(screen.queryByRole('heading',{name:'Compatibility check detected automation'})).not.toBeInTheDocument();
 expect(screen.queryByRole('link',{name:'Diagnostic source ↗'})).not.toBeInTheDocument();
});

it('does not show a resolved compatibility claim from an unconfirmed runtime',()=>{
 render(<BrowserEnvironmentIssue handoff={{...handoff,browser_environment:{code:'runtime_support_unconfirmed',evidence:{navigator_webdriver:false}}}}/>);
 expect(screen.queryByRole('status')).not.toBeInTheDocument();
});
