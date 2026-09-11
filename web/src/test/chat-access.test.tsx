import {it,expect,vi} from 'vitest';
import {render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {OreClient,type Conversation,type Handoff} from '@ore/sdk';
import {ChatAccessRequests} from '../components/ChatAccessRequests';
import ChatView from '../components/ChatView';
import HandoffsView from '../components/HandoffsView';
import {conversationHandoffs} from '../lib/conversation';

const planning:Handoff={id:'planning-handoff',job_id:'recon-job',kind:'challenge',status:'needs_user',state_version:2,href:'/handoffs/planning-handoff',reason:'Verify planning access',session_state:'live',session_id:'recon-browser',checkpoint_url:'https://academic.oup.com/eurheartj',conversation_context:{phase:'planning',historical:true,conversation_id:'chat-1',active_execution_job_id:'collection-job'}};
const collection:Handoff={...planning,id:'collection-handoff',job_id:'collection-job',href:'/handoffs/collection-handoff',session_id:'collection-browser',reason:'Verify collection access',conversation_context:{phase:'execution',conversation_id:'chat-1'}};
const state:Conversation={id:'chat-1',title:'EHJ collection',status:'awaiting_auth',messages:[{id:'u',role:'user',content:"Download all articles in EHJ's June 2024 issues"}],plans:[],runs:[{id:'run-1',job_id:'collection-job',status:'awaiting_auth',nodes:[{id:'collect',status:'awaiting_auth',error:{code:'http_error',status:403}}]}],planning:{job_id:'recon-job',active:false,handoffs:[planning]}};

it('keeps planning and previous execution requests separate from the active collection',()=>{
 const earlier={...collection,id:'earlier',job_id:'old-collection'};const resolved={...collection,id:'resolved',status:'resolved'};
 const result=conversationHandoffs(state,[planning,earlier,collection,resolved]);
 expect(result.active.map(item=>item.id)).toEqual(['collection-handoff']);expect(result.history.map(item=>item.id)).toEqual(['planning-handoff','earlier','resolved']);
});
it('allows a current planning request before execution and after an explicit new planning turn',()=>{
 const request={...planning,conversation_context:{phase:'planning'}};
 expect(conversationHandoffs({...state,runs:[],planning:{job_id:'recon-job',active:false}},[request]).active).toHaveLength(1);
 expect(conversationHandoffs({...state,planning:{job_id:'recon-job',active:true}},[request]).active).toHaveLength(1);
});
it('prefers a resolved request at the newer version over a stale snapshot',()=>{
 const result=conversationHandoffs(state,[{...collection,status:'resolved',state_version:3},collection]);
 expect(result.active).toHaveLength(0);expect(result.history[0]?.status).toBe('resolved');
});
it('explains a collection HTTP failure when the only visible browser belongs to planning',async()=>{
 const navigate=vi.fn();render(<ChatAccessRequests conversation={state} currentRun={state.runs[0]} requests={[planning]} requestsLoaded onNavigate={navigate}/>);
 const current=screen.getByRole('status');expect(current).toHaveTextContent('HTTP 403');expect(current).toHaveTextContent('No browser request is attached to this collection yet.');
 expect(screen.queryByRole('button',{name:'Open collection request'})).not.toBeInTheDocument();
 await userEvent.setup().click(screen.getByRole('button',{name:'Inspect current mission'}));expect(navigate).toHaveBeenCalledWith('/jobs/collection-job');
 expect(screen.getByText(/These requests belong to earlier planning/)).toBeInTheDocument();
});
it('links only the current collection request as the action to continue collection',async()=>{
 const navigate=vi.fn();render(<ChatAccessRequests conversation={state} currentRun={state.runs[0]} requests={[planning,collection]} requestsLoaded onNavigate={navigate}/>);
 const card=screen.getByRole('region',{name:'Collection access request'});await userEvent.setup().click(within(card).getByRole('button',{name:'Open collection request'}));
 expect(navigate).toHaveBeenCalledWith('/handoffs/collection-handoff');expect(screen.queryByText('No browser request is attached to this collection yet.')).not.toBeInTheDocument();
});
it('renders an earlier planning handoff read-only without attaching or controlling its browser',async()=>{
 const calls:{path:string;method:string}[]=[];const navigate=vi.fn();const client=new OreClient({baseUrl:'https://ore.test',fetch:async(input,init)=>{calls.push({path:new URL(String(input)).pathname,method:init?.method??'GET'});return Response.json(planning);}});
 render(<HandoffsView client={client} profiles={[]} id={planning.id} onNavigate={navigate}/>);
 expect(await screen.findByRole('region',{name:'Planning request context'})).toHaveTextContent('It cannot resume the current collection.');
 expect(screen.queryByRole('button',{name:'Take control'})).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'Verify planning access'})).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'Use ORE Chrome desktop'})).not.toBeInTheDocument();
 await userEvent.setup().click(screen.getByRole('button',{name:'Inspect current collection'}));expect(navigate).toHaveBeenCalledWith('/jobs/collection-job');
 expect(calls).toEqual([{path:'/v1/handoffs/planning-handoff',method:'GET'}]);
});
it('clears a stale run handoff after an authoritative empty poll even when artifact loading fails',async()=>{
 const client=new OreClient({baseUrl:'https://ore.test',fetch:async(input)=>{const path=new URL(String(input)).pathname;if(path.endsWith('/events'))return new Response(new ReadableStream());if(path.includes('/artifacts'))return Response.json({detail:'Unavailable'},{status:503});if(path==='/v1/handoffs')return Response.json([]);if(path==='/v1/access-profiles')return Response.json([]);if(path==='/v1/connections')return Response.json({connections:[]});if(path==='/v1/providers/status')return Response.json({providers:[]});if(path==='/v1/scheduler')return Response.json({});return Response.json({...state,runs:[{...state.runs[0],handoffs:[collection]}]});}});
 render(<ChatView client={client} id={state.id} models={[]} onNavigate={()=>undefined} onChanged={()=>undefined}/>);
 await waitFor(()=>expect(screen.getByRole('status',{name:'Current collection access'})).toHaveTextContent('No browser request is attached to this collection yet.'));
 expect(screen.queryByRole('button',{name:'Open collection request'})).not.toBeInTheDocument();
});
