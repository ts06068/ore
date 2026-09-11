import {describe,it,expect,vi} from 'vitest';
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MissionForm from '../components/MissionForm';
describe('mission authoring',()=>{
 it('submits instructions, source scope, artifact requirements and worker budget',async()=>{const user=userEvent.setup();const create=vi.fn(async(_mission:Record<string,unknown>,_start:boolean)=>undefined);render(<MissionForm sources={[{id:'crossref',name:'Crossref'}]} models={[]} profiles={[]} onClose={()=>undefined} onCreate={create}/>);await user.type(screen.getByLabelText('Mission name'),'Evidence review');await user.type(screen.getByLabelText(/^Instructions/),'Collect original articles and all supplements.');await user.type(screen.getByLabelText(/^Search query/),'transplantation');await user.click(screen.getByRole('button',{name:'Create mission'}));await waitFor(()=>expect(create).toHaveBeenCalledTimes(1));expect(create.mock.calls[0]?.[0]).toMatchObject({name:'Evidence review',instructions:'Collect original articles and all supplements.',sources:['crossref'],artifact_roles:['main_pdf','supplement'],query:'transplantation',limits:{max_agent_workers:2},routing:{mode:'auto'}});expect(create.mock.calls[0]?.[1]).toBe(false);expect(create.mock.calls[0]?.[0]).not.toHaveProperty('retrieval_policy');});
 it('rejects inverted publication years before calling the server',async()=>{const user=userEvent.setup();const create=vi.fn(async(_mission:Record<string,unknown>,_start:boolean)=>undefined);render(<MissionForm sources={[{id:'crossref'}]} models={[]} profiles={[]} onClose={()=>undefined} onCreate={create}/>);await user.type(screen.getByLabelText(/^Instructions/),'Collect articles.');await user.clear(screen.getByLabelText('From year'));await user.type(screen.getByLabelText('From year'),'2025');await user.clear(screen.getByLabelText('Through year'));await user.type(screen.getByLabelText('Through year'),'2020');await user.click(screen.getByRole('button',{name:'Create mission'}));expect(await screen.findByRole('alert')).toHaveTextContent('start year');expect(create).not.toHaveBeenCalled();});
});
it.each([
 ['api_open_access_first',false],
 ['official_first',true],
] as const)('submits explicit retrieval order %s with browser fallback %s',async(mode,browserFallback)=>{
 const user=userEvent.setup();
 const create=vi.fn(async(_mission:Record<string,unknown>,_start:boolean)=>undefined);
 render(<MissionForm sources={[{id:'crossref'}]} models={[]} profiles={[]} onClose={()=>undefined} onCreate={create}/>);
 await user.type(screen.getByLabelText(/^Instructions/),'Retrieve the published main PDF and supplements.');
 await user.selectOptions(screen.getByLabelText(/^Retrieval order/),mode);
 if(!browserFallback)await user.click(screen.getByRole('checkbox',{name:'Allow browser fallback'}));
 await user.click(screen.getByRole('button',{name:'Create mission'}));
 await waitFor(()=>expect(create).toHaveBeenCalledTimes(1));
 expect(create.mock.calls[0]?.[0]).toMatchObject({retrieval_policy:{mode,browser_fallback:browserFallback},artifact_roles:['main_pdf','supplement'],sources:['crossref']});
});
it('omits retrieval policy when returning to the selected access profile default',async()=>{
 const user=userEvent.setup();
 const create=vi.fn(async(_mission:Record<string,unknown>,_start:boolean)=>undefined);
 render(<MissionForm sources={[{id:'crossref'}]} models={[]} profiles={[{id:'institution',name:'Institution access'}]} onClose={()=>undefined} onCreate={create}/>);
 await user.type(screen.getByLabelText(/^Instructions/),'Collect original articles.');
 await user.selectOptions(screen.getByLabelText('Access profile'),'institution');
 await user.selectOptions(screen.getByLabelText(/^Retrieval order/),'api_open_access_first');
 await user.click(screen.getByRole('checkbox',{name:'Allow browser fallback'}));
 await user.selectOptions(screen.getByLabelText(/^Retrieval order/),'');
 expect(screen.queryByRole('checkbox',{name:'Allow browser fallback'})).not.toBeInTheDocument();
 await user.click(screen.getByRole('button',{name:'Create mission'}));
 await waitFor(()=>expect(create).toHaveBeenCalledTimes(1));
 expect(create.mock.calls[0]?.[0]).toHaveProperty('access_profile_ref','institution');
 expect(create.mock.calls[0]?.[0]).not.toHaveProperty('retrieval_policy');
});
