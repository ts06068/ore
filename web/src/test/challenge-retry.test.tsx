import {expect,it,vi} from 'vitest';
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {OreClient,type Handoff} from '@ore/sdk';
import {ChallengeRetry} from '../components/ChallengeRetry';
import HandoffsView from '../components/HandoffsView';

const handoff:Handoff={id:'retry-handoff',job_id:'collection-job',kind:'challenge',status:'needs_user',reason:'Automatic verification window expired.',state_version:7,control_epoch:4,href:'/handoffs/retry-handoff',session_id:'native-session',session_state:'live',browser_transport:'desktop_chrome',challenge_id:'prior-episode',conversation_context:{phase:'execution'},automatic_retry:{eligible:true,reason:'The earlier planning verification window expired before collection began.',max_attempts:1,max_seconds:120,previous_attempts:0,previous_elapsed_seconds:3600}};

it('shows the exact operator grant and preserves the previous verification history',async()=>{
 const action=vi.fn();render(<ChallengeRetry handoff={handoff} busy={false} onRetry={action}/>);
 expect(screen.getByLabelText('Automatic verification retry')).toHaveTextContent('1 automatic verification attempt within 120 seconds');
 expect(screen.getByLabelText('Automatic verification retry')).toHaveTextContent('Previous attempts and elapsed time remain recorded.');
 expect(screen.getByText('The earlier planning verification window expired before collection began.')).toBeInTheDocument();expect(action).not.toHaveBeenCalled();
 await userEvent.setup().click(screen.getByRole('button',{name:'Retry automatic verification'}));expect(action).toHaveBeenCalledTimes(1);
});
it('explains restoration before retrying an eligible lost native browser',()=>{
 render(<ChallengeRetry handoff={{...handoff,session_state:'lost'}} busy={false} onRetry={()=>undefined}/>);
 expect(screen.getByRole('button',{name:'Restore Chrome and retry once'})).toBeInTheDocument();
 expect(screen.getByLabelText('Automatic verification retry')).toHaveTextContent('Restore this collection’s Chrome browser and authorize up to 1');
});
it.each([
 {conversation_context:{phase:'planning'}},
 {conversation_context:{phase:'execution',historical:true}},
 {conversation_context:{phase:'execution',active_execution_job_id:'different-collection'}},
 {superseded:true}, {status:'resolved'}, {session_state:'unavailable'},
 {session_state:'lost',browser_transport:'playwright'},
 {automatic_retry:{eligible:false,max_attempts:1,max_seconds:120}},
 {automatic_retry:{eligible:true,max_attempts:1,max_seconds:0}},
 {automatic_retry:{eligible:true,max_attempts:'1',max_seconds:120}},
 {automatic_retry:undefined}, {challenge_id:null},
])('does not offer a new grant for historical, unverified, or ineligible requests: %j',patch=>{
 render(<ChallengeRetry handoff={{...handoff,...patch}} busy={false} onRetry={()=>undefined}/>);
 expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
it('disables additional clicks while a grant is pending',()=>{
 render(<ChallengeRetry handoff={handoff} busy onRetry={()=>undefined}/>);
 expect(screen.getByRole('button',{name:'Retry automatic verification'})).toBeDisabled();
});
it('posts a versioned operator-only action and reuses its key after a failed response without automatic replay',async()=>{
 const calls:{path:string;method:string;body:Record<string,unknown>}[]=[];let attempts=0;
 const client=new OreClient({baseUrl:'https://ore.test',fetch:async(input,init={})=>{
  const path=new URL(String(input)).pathname;const body=init.body?JSON.parse(String(init.body)):{};calls.push({path,method:init.method??'GET',body});
  if(path.endsWith('/actions')){attempts++;if(attempts===1)throw new Error('Connection dropped after the request.');return Response.json({...handoff,status:'resolved',automatic_retry:{eligible:false}});}
  if(path.includes('/v1/browser/'))return Response.json({sessions:[]});
  if(path==='/v1/desktop/preview')return Response.json({eligible:false});
  return Response.json({...handoff,session_state:'lost'});
 }});
 const user=userEvent.setup();const initial=render(<HandoffsView client={client} profiles={[]} id={handoff.id} onNavigate={()=>undefined}/>);
 const button=await screen.findByRole('button',{name:'Restore Chrome and retry once'});expect(attempts).toBe(0);
 await user.click(button);expect(await screen.findByRole('alert')).toHaveTextContent('Connection dropped');expect(attempts).toBe(1);
 await user.click(screen.getByRole('button',{name:'Restore Chrome and retry once'}));await waitFor(()=>expect(attempts).toBe(2));
 const mutations=calls.filter(call=>call.method==='POST');expect(mutations).toHaveLength(2);
 expect(mutations[0]?.body).toMatchObject({action:'retry_verification',expected_version:7,expected_epoch:4});
 expect(mutations[1]?.body.idempotency_key).toBe(mutations[0]?.body.idempotency_key);
 expect(mutations[0]?.body).not.toHaveProperty('max_attempts');expect(mutations[0]?.body).not.toHaveProperty('max_seconds');
 initial.unmount();render(<HandoffsView client={client} profiles={[]} id={handoff.id} onNavigate={()=>undefined}/>);await screen.findByRole('button',{name:'Restore Chrome and retry once'});expect(attempts).toBe(2);
});
