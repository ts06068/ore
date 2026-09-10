import {afterEach,describe,it,expect,vi} from 'vitest';
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../App';
afterEach(()=>{cleanup();vi.unstubAllGlobals();localStorage.clear();});
describe('operator console',()=>{
 it('connects with a token and keeps the secret out of persisted preferences',async()=>{let loggedIn=false;const user=userEvent.setup();const fetcher=vi.fn(async(url:string,init?:RequestInit)=>{if(url.endsWith('/v1/auth/login')){expect(JSON.parse(String(init?.body))).toEqual({token:'ore-test-secret'});loggedIn=true;return new Response('{}');}if(!loggedIn)return new Response(JSON.stringify({detail:'Unauthorized'}),{status:401});if(url.endsWith('/v1/sources'))return new Response(JSON.stringify([{id:'crossref',name:'Crossref'}]));return new Response('[]');});vi.stubGlobal('fetch',fetcher);render(<App/>);await user.type(await screen.findByLabelText('Operator token'),'ore-test-secret');await user.click(screen.getByRole('button',{name:'Connect workspace'}));await waitFor(()=>expect(screen.getByRole('heading',{name:'Missions'})).toBeInTheDocument());expect(Object.values(localStorage)).not.toContain('ore-test-secret');await user.click(screen.getByRole('button',{name:/New mission/}));expect(await screen.findByRole('dialog')).toBeInTheDocument();await waitFor(()=>expect(screen.getByRole('button',{name:'Crossref'})).toBeInTheDocument());});
});
