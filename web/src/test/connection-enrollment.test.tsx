import {afterEach,describe,expect,it,vi} from 'vitest';
import {act,cleanup,render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {OreClient} from '@ore/sdk';
import type {ConnectionCard} from '@ore/sdk';
import {ConnectionEnrollment} from '../components/ConnectionEnrollment';

afterEach(()=>{cleanup();vi.useRealTimers();});
const card:ConnectionCard={id:'source1',provider:'scopus',kind:'source',operation:'search',status:'connecting',state_version:2,job_id:'setup-job',actions:['request_agent'],credential_fields:['api_key'],configured_fields:[]};
function harness(){
 let state={id:'action1',state_version:1,status:'pending',kind:'capture_key',url:'https://dev.elsevier.com/api-key',target:{index:4,label:'API key',tag:'input'}};
 let details={given_name:'Ada',family_name:'Lovelace'};let offline=false;const calls:{path:string;method:string;body:Record<string,unknown>}[]=[];
 const client=new OreClient({baseUrl:'https://ore.test',fetch:async(input,init={})=>{
  const path=new URL(String(input)).pathname;const method=init.method??'GET';const body=init.body?JSON.parse(String(init.body)):{};calls.push({path,method,body});
  if(path.endsWith('/enrollment/details')){if(method==='POST')details=body.values;return Response.json({values:details,configured_fields:Object.keys(details)});}
  if(offline)throw new Error('offline');
  if(path.endsWith('/approve')||path.endsWith('/reject')){expect(body.expected_version).toBe(state.state_version);state={...state,state_version:state.state_version+1,status:path.endsWith('/approve')?'completed':'rejected'};return Response.json(state);}
  return Response.json({actions:[state]});
 }});
 return {client,calls,offline:()=>{offline=true;},online:()=>{offline=false;}};
}
describe('reviewed provider setup actions in chat',()=>{
 it('requires explicit approval for saving a provider key and restores the terminal state',async()=>{
  const h=harness();const user=userEvent.setup();const mounted=render(<ConnectionEnrollment client={h.client} card={card}/>);
  const action=await screen.findByRole('article',{name:'Save API key from this field'});
  expect(within(action).getByRole('link')).toHaveAttribute('href','https://dev.elsevier.com/api-key');expect(within(action).getByText('API key')).toBeInTheDocument();expect(h.calls.some(call=>call.method==='POST')).toBe(false);
  await user.click(within(action).getByRole('button',{name:'Approve action'}));expect(await screen.findByText('This action is complete.')).toBeInTheDocument();
  const body=h.calls.find(call=>call.path.endsWith('/approve'))!.body;expect(Object.keys(body).sort()).toEqual(['expected_version','idempotency_key']);expect(body.idempotency_key).toEqual(expect.any(String));expect(screen.queryByRole('button',{name:'Approve action'})).not.toBeInTheDocument();
  mounted.unmount();render(<ConnectionEnrollment client={h.client} card={card}/>);expect(await screen.findByText('This action is complete.')).toBeInTheDocument();expect(h.calls.filter(call=>call.path.endsWith('/approve'))).toHaveLength(1);
 });
 it('loads and saves only named registration details through their own endpoint',async()=>{
  const h=harness();const user=userEvent.setup();render(<ConnectionEnrollment client={h.client} card={card}/>);await user.click(screen.getByText('Registration details'));
  await waitFor(()=>expect(screen.getByLabelText('Given name')).toHaveValue('Ada'));await user.type(screen.getByLabelText('Institution or affiliation'),'Example University');await user.click(screen.getByRole('button',{name:'Save registration details'}));
  expect(await screen.findByText('Registration details saved.')).toBeInTheDocument();expect(h.calls.find(call=>call.path.endsWith('/enrollment/details')&&call.method==='POST')?.body).toEqual({values:{given_name:'Ada',family_name:'Lovelace',affiliation:'Example University'}});expect(h.calls.some(call=>call.path.endsWith('/messages'))).toBe(false);
 });
 it('disables cached form actions during a disconnection instead of replaying them',async()=>{
  vi.useFakeTimers();const h=harness();render(<ConnectionEnrollment client={h.client} card={card}/>);await act(async()=>{await Promise.resolve();});expect(screen.getByRole('button',{name:'Approve action'})).toBeEnabled();
  h.offline();await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});expect(screen.getByRole('button',{name:'Approve action'})).toBeDisabled();expect(screen.getByText('Form updates paused. Last known actions are shown.')).toBeInTheDocument();
  h.online();await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});expect(screen.getByRole('button',{name:'Approve action'})).toBeEnabled();expect(h.calls.some(call=>call.method==='POST')).toBe(false);
 });
 it('allows a pending setup action to be declined without executing it',async()=>{
  const h=harness();const user=userEvent.setup();render(<ConnectionEnrollment client={h.client} card={card}/>);await user.click(await screen.findByRole('button',{name:'Decline'}));expect(await screen.findByText('You declined this action.')).toBeInTheDocument();expect(h.calls.some(call=>call.path.endsWith('/approve'))).toBe(false);
 });
});


it('sends a verification code only to protected setup storage and clears it immediately',async()=>{
 const h=harness();const user=userEvent.setup();render(<ConnectionEnrollment client={h.client} card={card}/>);await user.click(screen.getByText('One-time verification code'));
 await user.type(screen.getByLabelText('Provider verification code'),'654321');await user.click(screen.getByRole('button',{name:'Save verification code'}));expect(await screen.findByText('Verification code saved for this setup step.')).toBeInTheDocument();
 expect(screen.getByLabelText('Provider verification code')).toHaveValue('');expect(document.body.textContent).not.toContain('654321');expect(h.calls.find(call=>call.path.endsWith('/enrollment/secret'))?.body).toEqual({field:'mfa_code',value:'654321'});expect(h.calls.some(call=>call.path.endsWith('/messages'))).toBe(false);
});


it.each([['/handoffs/action-page',true],['https://other.test/handoffs/action-page',false],['/handoffs/action-page?token=private',false]])('links only the action-specific ORE browser route: %s',async(href,allowed)=>{
 const client=new OreClient({baseUrl:'https://ore.test',fetch:async()=>Response.json({actions:[{id:'review1',state_version:1,status:'pending',kind:'click',url:'https://dev.elsevier.com/api-key',handoff_href:href,target:{label:'Create API key'}}]})});
 render(<ConnectionEnrollment client={client} card={card}/>);await screen.findByRole('button',{name:'Approve action'});const link=screen.queryByRole('link',{name:'Review this ORE browser'});if(allowed)expect(link).toHaveAttribute('href',href);else expect(link).not.toBeInTheDocument();
});
