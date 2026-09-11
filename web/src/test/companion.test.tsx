import {render,screen,fireEvent,waitFor} from '@testing-library/react';
import {describe,it,expect,vi} from 'vitest';
import ChromeCompanion from '../components/ChromeCompanion';
import type {OreClient,Handoff} from '@ore/sdk';
const handoff={id:'h',job_id:'j',kind:'browser',status:'claimed',reason:'Fixture',state_version:1,href:'/handoffs/h'} as Handoff;
describe('Chrome pairing',()=>{
 it('pairs only after a user action, uses a scoped code and refreshes the handoff version before attachment',async()=>{
  const pair={pair_id:'a'.repeat(32),token:'fixture-scoped-token',expires_at:Date.now()/1000+300,origins:['https://journal.example']};
  const request=vi.fn(async(path:string,_init?:RequestInit)=>path==='/v1/companion/pair'?pair:path.endsWith('/attach')?{}:{connected:true});
  const client={request,handoff:vi.fn(async()=>({...handoff,state_version:7}))} as unknown as OreClient;
  const onAttached=vi.fn(async()=>{});
  render(<ChromeCompanion client={client} handoff={handoff} onAttached={onAttached}/>);
  expect(request).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Create pairing code'));
  await screen.findByText('Chrome connected');
  const code=JSON.parse((screen.getByLabelText('Chrome pairing code') as HTMLTextAreaElement).value);
  expect(code.server).toBe(window.location.origin);expect(code.token).toBe(pair.token);
  expect(localStorage.getItem('ore-companion-token')).toBeNull();
  fireEvent.click(screen.getByText('Use connected Chrome for this request'));
  await waitFor(()=>expect(onAttached).toHaveBeenCalledOnce());
  const body=JSON.parse(String(request.mock.calls.find(([path])=>path.endsWith('/attach'))![1]?.body));
  expect(body.expected_version).toBe(7);
 });
});
