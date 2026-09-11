import {it,expect,vi} from 'vitest';
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {OreClient} from '@ore/sdk';
import type {Handoff} from '@ore/sdk';
import DesktopChrome from '../components/DesktopChrome';
import HandoffsView from '../components/HandoffsView';

vi.mock('../components/BrowserView',()=>({default:()=>null}));
const handoff:Handoff={id:'request-1',job_id:'mission-1',kind:'challenge',status:'needs_user',
 reason:'Open the original journal checkpoint',state_version:7,href:'/handoffs/request-1',
 checkpoint_url:'https://journal.test/issue/2024/1',session_state:'lost',session_id:'old-browser'};

const preview={handoff_id:handoff.id,expected_version:7,eligible:true,scope:'operator_session_only',origins:['https://journal.test'],checkpoint_url:handoff.checkpoint_url};

it('attaches one native desktop using authenticated versioned API and reloads this handoff',async()=>{
 const calls:{url:string;method:string;headers:Headers;body:unknown}[]=[];
 let current=handoff;
 const client=new OreClient({baseUrl:'https://ore.test',token:'fixture-operator-token',fetch:async(url,init)=>{
  calls.push({url:String(url),method:init?.method??'GET',headers:new Headers(init?.headers),body:init?.body?JSON.parse(String(init.body)):null});
  if(String(url).includes('/desktop/preview'))return Response.json(preview);
  if(init?.method==='POST')current={...handoff,state_version:8,session_state:'live',session_id:'native-browser',browser_transport:'desktop_chrome'};
  return new Response(JSON.stringify(current));
 }});
 const user=userEvent.setup();
 render(<HandoffsView client={client} profiles={[]} id={handoff.id} onNavigate={()=>undefined}/>);
 expect(await screen.findByText(/original checkpoint in ordinary Chrome/)).toBeInTheDocument();
 expect(screen.getByText(/No browser extension is needed/)).toBeInTheDocument();
 expect(screen.getByText('Use Chrome on this PC')).toBeInTheDocument();
 await waitFor(()=>expect(screen.getByRole('button',{name:'Use ORE Chrome desktop'})).toBeEnabled());
 await user.click(screen.getByRole('button',{name:'Use ORE Chrome desktop'}));
 expect(await screen.findByText(/ORE Chrome desktop is attached/)).toBeInTheDocument();
 const posts=calls.filter(call=>call.method==='POST');
 expect(posts).toHaveLength(1);
 expect(posts[0].url).toBe('https://ore.test/v1/desktop/attach');
 expect(posts[0].headers.get('Authorization')).toBe('Bearer fixture-operator-token');
 expect(posts[0].body).toEqual({handoff_id:handoff.id,expected_version:7,idempotency_key:expect.any(String)});
 expect(calls.filter(call=>call.method==='GET'&&call.url.endsWith('/v1/handoffs/request-1')).length).toBeGreaterThan(1);
 expect(calls.every(call=>!call.url.includes('fixture-operator-token'))).toBe(true);
 expect(document.body.textContent).not.toContain('fixture-operator-token');
 expect(screen.getByText(/opening the desktop does not resume the mission/)).toBeInTheDocument();
});

it('shows a slot error and reuses the idempotency key when retrying the same version',async()=>{
 const requests:Record<string,unknown>[]=[];
 const client=new OreClient({baseUrl:'https://ore.test',fetch:async(_url,init)=>{
  if(String(_url).includes('/desktop/preview'))return Response.json(preview);
  requests.push(JSON.parse(String(init?.body)));
  return new Response(JSON.stringify({detail:'The native desktop slot is already in use.'}),{status:409});
 }});
 const refreshed=vi.fn(async()=>undefined),user=userEvent.setup();
 render(<DesktopChrome client={client} handoff={handoff} onAttached={refreshed}/>);
 await waitFor(()=>expect(screen.getByRole('button',{name:'Use ORE Chrome desktop'})).toBeEnabled());
 await user.click(screen.getByRole('button',{name:'Use ORE Chrome desktop'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('The native desktop slot is already in use.');
 await waitFor(()=>expect(screen.getByRole('button',{name:'Use ORE Chrome desktop'})).toBeEnabled());
 await user.click(screen.getByRole('button',{name:'Use ORE Chrome desktop'}));
 await waitFor(()=>expect(refreshed).toHaveBeenCalledTimes(2));
 expect(requests).toHaveLength(2);
 expect(requests[1]).toEqual(requests[0]);
});

it('disables repeat attachment while the native desktop is starting',async()=>{
 let complete!:(value:Response)=>void;
 const fetcher=vi.fn(async(input:RequestInfo|URL)=>String(input).includes('/desktop/preview')?Response.json(preview):new Promise<Response>(resolve=>{complete=resolve;}));
 const client=new OreClient({baseUrl:'https://ore.test',fetch:fetcher});
 const refreshed=vi.fn(async()=>undefined),user=userEvent.setup();
 render(<DesktopChrome client={client} handoff={handoff} onAttached={refreshed}/>);
 const button=screen.getByRole('button',{name:'Use ORE Chrome desktop'});
 await waitFor(()=>expect(button).toBeEnabled());
 await user.click(button);
 expect(button).toBeDisabled();
 await user.click(button);
 expect(fetcher).toHaveBeenCalledTimes(2);
 complete(new Response(JSON.stringify({...handoff,state_version:8})));
 await waitFor(()=>expect(refreshed).toHaveBeenCalledOnce());
});

it('does not attach historical requests or requests without a safe checkpoint',()=>{
 const client=new OreClient({baseUrl:'https://ore.test',fetch:vi.fn()});
 const {rerender}=render(<DesktopChrome client={client} handoff={{...handoff,superseded:true}} onAttached={async()=>undefined}/>);
 expect(screen.queryByRole('button',{name:'Use ORE Chrome desktop'})).not.toBeInTheDocument();
 rerender(<DesktopChrome client={client} handoff={{...handoff,checkpoint_url:'javascript:alert(1)'}} onAttached={async()=>undefined}/>);
 expect(screen.getByRole('button',{name:'Use ORE Chrome desktop'})).toBeDisabled();
});

it('closes only the attached virtual Chrome and refreshes the same pending request',async()=>{
 const calls:{url:string;body:unknown}[]=[];
 const attached={...handoff,state_version:12,browser_transport:'desktop_chrome',session_state:'live'};
 const client=new OreClient({baseUrl:'https://ore.test',fetch:async(url,init)=>{
  calls.push({url:String(url),body:JSON.parse(String(init?.body))});
  return new Response(JSON.stringify({...attached,state_version:13,session_state:'lost'}));
 }});
 const refreshed=vi.fn(async()=>undefined),user=userEvent.setup();
 render(<DesktopChrome client={client} handoff={attached} onAttached={refreshed}/>);
 expect(screen.queryByRole('button',{name:'Use ORE Chrome desktop'})).not.toBeInTheDocument();
 await user.click(screen.getByRole('button',{name:'Close virtual Chrome'}));
 await waitFor(()=>expect(refreshed).toHaveBeenCalledOnce());
 expect(calls).toEqual([{url:'https://ore.test/v1/desktop/close',body:{handoff_id:handoff.id,expected_version:12,idempotency_key:expect.any(String)}}]);
});

it('shows the exact previewed origins and prevents attachment when scope preview fails',async()=>{
 const post=vi.fn();const client=new OreClient({baseUrl:'https://ore.test',fetch:async(_input,init)=>{if(init?.method==='POST')post();return Response.json({detail:'The stored checkpoint has no approved finite scope.'},{status:403});}});
 render(<DesktopChrome client={client} handoff={handoff} onAttached={async()=>{}}/>);
 expect(await screen.findByRole('alert')).toHaveTextContent('no approved finite scope');expect(screen.getByRole('button',{name:'Use ORE Chrome desktop'})).toBeDisabled();expect(post).not.toHaveBeenCalled();
});
