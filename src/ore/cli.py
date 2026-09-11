"""ORE command line. No API key is required for the Codex subscription backend."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import typer
import yaml
from rich import print
from . import __version__
from .config import Settings

app=typer.Typer(no_args_is_help=True,help='ORE — programmable retrieval with LLM agents.')
rune_app=typer.Typer(help='Validate and inspect versioned Rune protocols.');app.add_typer(rune_app,name='rune')

def load(path):
    value=yaml.safe_load(Path(path).read_text())
    if not isinstance(value,dict):raise typer.BadParameter('Expected a YAML/JSON object')
    return value

def output(value):typer.echo(json.dumps(value,ensure_ascii=False,indent=2,default=str))

@app.command()
def version():typer.echo(__version__)

@app.command()
def serve(host:str='127.0.0.1',port:int=8765,workers:int|None=None):
    """Serve API, web UI and browser sessions. Auth token lives in .ore/operator.token."""
    import uvicorn
    from .engine import Engine
    from .server import create_app
    settings=Settings(host=host,port=port)
    if workers is not None:settings.max_workers=workers
    engine=Engine(settings)
    print(f'ORE: http://{host}:{port} — operator token file: {engine.settings.state_dir / "operator.token"}')
    uvicorn.run(create_app(engine),host=host,port=port,access_log=False)

@app.command()
def doctor(browser:bool=False):
    """Check local runtime availability without printing authentication secrets."""
    from .engine import Engine
    async def check():
        engine=Engine(Settings(max_workers=0));result={'version':__version__,'state_dir':str(engine.settings.state_dir),'checks':{}}
        try:
            result['checks']['database']='ok'
            try:result['models']=await engine.models();result['checks']['codex']='ok'
            except Exception as exc:result['checks']['codex']=type(exc).__name__
            try:
                from ore_scholarly import list_sources,list_runes
                result['sources']=list_sources();result['runes']=list_runes()
            except ImportError:result['checks']['scholarly']='not installed'
            if browser:
                try:
                    if engine.settings.browser_backend == 'desktop_chrome':
                        mission = {'goal': 'Native desktop startup check', 'allowed_origins': ['https://example.org']}
                        job = engine.store.create_job(mission)
                        session = await engine.browser.create(job['id'], mission, {'id': 'public'})
                        result['browser_transport'] = 'desktop_chrome'
                        result['browser_check_scope'] = 'native_window_startup_only'
                        await engine.browser.close_session(session.id)
                    else:
                        await engine.browser.start()
                    result['checks']['browser'] = 'ok'
                except Exception as exc:result['checks']['browser']=type(exc).__name__
            return result
        finally:await engine.stop()
    output(asyncio.run(check()))

@app.command('desktop-build')
def desktop_build(image: str = 'ore-desktop:0.2.0rc1'):
    """Build the installed-Chrome desktop image from bundled sources."""
    import re
    import shutil
    import subprocess
    import tempfile
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*', image):
        raise typer.BadParameter('Invalid desktop image tag')
    package = Path(__file__).parent
    assets = package / 'desktop_assets'
    if not assets.is_dir():
        assets = package.parents[1] / 'deploy' / 'desktop'
    if not (assets / 'Dockerfile').is_file():
        raise typer.BadParameter('Desktop image sources are missing from this installation')
    with tempfile.TemporaryDirectory(prefix='ore-desktop-build-') as folder:
        context = Path(folder)
        shutil.copytree(assets, context / 'deploy' / 'desktop')
        config = context / 'docker-config'; config.mkdir(mode=0o700)
        subprocess.run(['docker', '--config', str(config), 'build', '-f',
            str(context / 'deploy' / 'desktop' / 'Dockerfile'), '-t', image, str(context)], check=True)
    output({'image': image, 'built': True, 'browser_access_verified': False})


@app.command()
def run(mission:Path,rune:Path|None=None,wait:bool=True):
    """Create and execute a local mission. Ctrl-C preserves resumable job state."""
    from .engine import Engine
    async def execute():
        engine=Engine();job=engine.create(load(mission),load(rune) if rune else None)
        output({'job_id':job['id']})
        try:
            if not wait:return job
            await engine.run(job['id'])
            if wait:
                while engine.store.get_job(job['id'])['status'] in ('queued','running'):
                    await asyncio.sleep(1)
                return {**engine.store.get_job(job['id']),'audit':engine.audit(job['id'])}
            return job
        finally:await engine.stop()
    output(asyncio.run(execute()))

@app.command()
def resume(job_id:str):
    """Resume a local job without changing its mission revision or inventory generation."""
    from .engine import Engine
    async def execute():
        engine=Engine()
        try:
            await engine.run(job_id)
            while engine.store.get_job(job_id)['status'] in ('queued','running'):await asyncio.sleep(1)
            return {**engine.store.get_job(job_id),'audit':engine.audit(job_id)}
        finally:await engine.stop()
    output(asyncio.run(execute()))

@app.command()
def audit(job_id:str):
    from .engine import Engine
    engine=Engine();output(engine.audit(job_id));engine.store.close()

@app.command()
def jobs():
    from .engine import Engine
    engine=Engine();output(engine.store.list_jobs());engine.store.close()

@app.command()
def worker(server:str='http://127.0.0.1:8765',parallel:int=typer.Option(1,min=1,max=32),once:bool=False,job_id:list[str]|None=typer.Option(None,'--job-id')):
    """Run independent Codex agent threads on this authenticated host."""
    from .remote import worker as run_worker
    token=os.environ.get('ORE_AUTH_TOKEN')
    if not token:
        path=Settings().state_dir/'operator.token'
        if path.exists():token=path.read_text().strip()
    if not token:raise typer.BadParameter('Set ORE_AUTH_TOKEN or use the local operator.token file')
    asyncio.run(run_worker(server,token,parallel=parallel,once=once,job_ids=job_id))

@app.command()
def mcp(server:str='http://127.0.0.1:8765'):
    """Serve ORE tools over MCP stdio for a local Codex client."""
    from .mcp import run_stdio
    token=os.environ.get('ORE_AUTH_TOKEN')
    path=Settings().state_dir/'operator.token'
    if not token and path.exists():token=path.read_text().strip()
    if not token:raise typer.BadParameter('Set ORE_AUTH_TOKEN or configure a local operator token')
    run_stdio(server,token)

@app.command()
def executor(server:str='http://127.0.0.1:8765'):
    """Run an isolated execution worker; requires a scoped enrollment token."""
    from .executor import run_executor
    asyncio.run(run_executor(server))

@app.command('secret-set')
def secret_set(ref:str):
    """Store a credential using a hidden terminal prompt; never pass it as a CLI argument."""
    from .config import SecretStore
    settings=Settings().prepare();value=typer.prompt('Secret value',hide_input=True,confirmation_prompt=False)
    SecretStore(settings.state_dir).set(ref,value);output({'ref':ref,'stored':True})

@rune_app.command('validate')
def validate_rune(path:Path):
    from .models import Rune
    value=Rune.model_validate(load(path));output({'valid':True,'digest':value.digest})

@rune_app.command('list')
def list_runes():
    from ore_scholarly import list_runes as entries
    output(entries())

@app.command()
def benchmark(suite:Path,report_name:str='paired-evaluation.json'):
    """Run paired fixed-Astra/auto cases against independent expected artifacts."""
    from .evaluation import run_paired_evaluation
    value=load(suite)
    output(asyncio.run(run_paired_evaluation(Settings().prepare(),value['cases'],report_name=report_name,candidate_profile=value.get('candidate_profile'),access_profile=value.get('access_profile'))))

@app.command('db-upgrade')
def db_upgrade():
    """Apply additive schema migrations without exposing connection credentials."""
    from .migrate import upgrade
    settings=Settings().prepare();upgrade(settings.database_url);output({'schema':'0002','status':'upgraded'})

@app.command('vault-sync')
def vault_sync(job_id:str,bucket:str,prefix:str='ore',endpoint:str|None=None):
    """Replicate verified job files to the explicitly chosen S3 destination."""
    from .engine import Engine
    from .storage import S3Mirror
    engine=Engine();mirror=S3Mirror(bucket,prefix=prefix,endpoint_url=endpoint)
    try:
        results=[]
        for artifact in engine.store.artifacts(job_id):
            if artifact.get('status')!='verified':continue
            result=mirror.copy(artifact);engine.store.record_observation(job_id,{'kind':'artifact_replication','artifact_id':artifact['id'],**result});results.append(result)
        output(results)
    finally:engine.store.close()


chat_app=typer.Typer(invoke_without_command=True,help='Plan and run through a durable ORE conversation.')
app.add_typer(chat_app,name='chat')


def _chat_token():
    token=os.environ.get('ORE_AUTH_TOKEN')
    path=Settings().state_dir/'operator.token'
    if not token and path.exists():token=path.read_text().strip()
    if not token:raise typer.BadParameter('Set ORE_AUTH_TOKEN or configure a local operator token')
    return token


def _chat_request(server,method,path,body=None):
    """Use the running server; never start a second coordinator or print credentials."""
    import httpx
    from .policy import redact
    try:
        with httpx.Client(base_url=server.rstrip('/'),headers={'Authorization':'Bearer '+_chat_token()},timeout=30) as client:
            response=client.request(method,path,json=body)
            response.raise_for_status()
            return redact(response.json())
    except httpx.HTTPStatusError as exc:
        raise typer.BadParameter(f'ORE server returned HTTP {exc.response.status_code}') from None
    except httpx.RequestError:
        raise typer.BadParameter('Could not reach the ORE server') from None


def _chat_path(conversation_id):
    from urllib.parse import quote
    if not conversation_id.strip():raise typer.BadParameter('conversation_id must not be empty')
    return '/v1/conversations/'+quote(conversation_id,safe='')


@chat_app.callback()
def chat(ctx:typer.Context,server:str=typer.Option('http://127.0.0.1:8765',envvar='ORE_SERVER'),
         conversation_id:str|None=typer.Option(None,'--conversation','-c'),
         mode:str|None=None,model_policy:str|None=None,model:str|None=None,reasoning_effort:str|None=None):
    """Open interactive chat. /approve, /stop, /resume and /status act on this conversation."""
    if mode is not None and mode not in ('plan','execute'):raise typer.BadParameter('mode must be plan or execute')
    if model_policy is not None and model_policy not in ('auto','fixed'):raise typer.BadParameter('model-policy must be auto or fixed')
    settings={}
    if mode:settings['mode']=mode
    if model_policy:settings['model_policy']=model_policy
    if model:settings['model']=model
    if reasoning_effort:settings['reasoning_effort']=reasoning_effort
    ctx.ensure_object(dict);ctx.obj.update(server=server,settings=settings)
    if ctx.invoked_subcommand is not None:return
    if conversation_id:snapshot=_chat_request(server,'GET',_chat_path(conversation_id))
    else:snapshot=_chat_request(server,'POST','/v1/conversations',settings)
    _chat_session(server,snapshot,{**{key:value for key,value in snapshot.get('settings',{}).items()
        if key in ('mode','model_policy','model','reasoning_effort','routing')},**settings})


def _chat_session(server,snapshot,settings):
    """Keep terminal input available while polling saved public messages and progress."""
    import threading
    from urllib.parse import quote
    path=_chat_path(snapshot['id']);stop=threading.Event();lock=threading.Lock()
    seen_messages=set();seen_events=set();seen_plans=set();last_status=[None]
    typer.echo(f"Conversation: {snapshot['id']}\n{server.rstrip('/')}/chat/{quote(snapshot['id'],safe='')}\n"
        'Commands: /approve [plan-id], /stop, /resume, /status, /plan, /execute, /continue <change>, /exit.\n'
        'Ctrl-C requests Stop and exits; /exit leaves server work unchanged.')
    def display(value):
        with lock:
            for message in value.get('messages',[]):
                if message.get('id') in seen_messages:continue
                seen_messages.add(message.get('id'))
                if message.get('role')=='assistant':typer.echo('\nORE: '+str(message.get('content','')))
            for event in value.get('events',[]):
                ident=str(event.get('id',''))
                if ident in seen_events:continue
                seen_events.add(ident);data=event.get('data') or {}
                if event.get('type') not in ('message','clarification','plan.proposed','plan.approved') and isinstance(data,dict):
                    summary=data.get('summary') or data.get('message') or data.get('routing_reason') or data.get('reason')
                    if summary:typer.echo(f"[{data.get('event_type',event.get('type','progress'))}] {summary}")
            for plan in value.get('plans',[]):
                if plan.get('id') in seen_plans:continue
                seen_plans.add(plan.get('id'))
                typer.echo('\nPlan for review: '+str(plan.get('id','')))
                output({key:plan[key] for key in ('revision','goal','summary','scope','outputs','constraints','budget','acceptance','status') if key in plan})
            if value.get('status')!=last_status[0]:
                last_status[0]=value.get('status');typer.echo('State: '+str(value.get('status','unknown')))
    display(snapshot)
    def poll():
        failed=False
        while not stop.wait(1):
            try:
                value=_chat_request(server,'GET',path)
                if not stop.is_set():display(value)
                failed=False
            except Exception:
                if not failed:typer.echo('\nUpdates disconnected; saved conversation will be retried.',err=True)
                failed=True
    thread=threading.Thread(target=poll,name='ore-chat-updates',daemon=True);thread.start()
    try:
        while True:
            try:content=input('You: ').strip()
            except EOFError:break
            if not content:continue
            if content in ('/exit','/quit'):break
            try:
                if content=='/status':output(_chat_request(server,'GET',path));continue
                if content in ('/plan','/execute'):
                    settings={**settings,'mode':content[1:]};typer.echo('Mode: '+settings['mode']);continue
                if content=='/stop':value=_chat_request(server,'POST',path+'/interrupt',{})
                elif content=='/resume':value=_chat_request(server,'POST',path+'/resume',{})
                elif content=='/approve' or content.startswith('/approve '):
                    parts=content.split(maxsplit=1)
                    value=_chat_request(server,'GET',path)
                    plan_id=parts[1] if len(parts)>1 else value.get('active_plan_id')
                    if not plan_id:typer.echo('No plan is available to approve.');continue
                    plan=next((plan for plan in value.get('plans',[]) if plan.get('id')==plan_id),None)
                    if not plan or plan_id not in seen_plans:
                        display(value);typer.echo('Review the plan above, then enter /approve again.');continue
                    value=_chat_request(server,'POST',path+'/plans/'+quote(plan_id,safe='')+'/approve',{})
                elif content.startswith('/continue '):
                    value=_chat_request(server,'POST',path+'/messages',{'content':content[len('/continue '):],**settings,'mode':'execute','change_and_continue':True})
                elif content.startswith('/'):
                    typer.echo('Unknown command. Use /approve, /stop, /resume, /status, /plan, /execute or /exit.');continue
                else:value=_chat_request(server,'POST',path+'/messages',{'content':content,**settings})
                display(value)
            except typer.BadParameter as exc:typer.echo(str(exc),err=True)
    except KeyboardInterrupt:
        try:display(_chat_request(server,'POST',path+'/interrupt',{}))
        except typer.BadParameter as exc:typer.echo(str(exc),err=True)
    finally:stop.set();thread.join(timeout=1)


@chat_app.command('list')
def chat_list(ctx:typer.Context):
    """List conversations without opening interactive input."""
    output(_chat_request(ctx.obj['server'],'GET','/v1/conversations'))


@chat_app.command('status')
def chat_status(ctx:typer.Context,conversation_id:str):
    """Read saved messages, plans, public progress and interruption state."""
    output(_chat_request(ctx.obj['server'],'GET',_chat_path(conversation_id)))


@chat_app.command('send')
def chat_send(ctx:typer.Context,conversation_id:str,message:str=typer.Argument(...),change_and_continue:bool=False):
    """Send one message and print its immediate durable snapshot; never implicitly approve a new plan."""
    output(_chat_request(ctx.obj['server'],'POST',_chat_path(conversation_id)+'/messages',
        {'content':message,**ctx.obj['settings'],'change_and_continue':change_and_continue}))


@chat_app.command('approve')
def chat_approve(ctx:typer.Context,conversation_id:str,plan_id:str):
    """Approve a plan already reviewed by the operator."""
    from urllib.parse import quote
    output(_chat_request(ctx.obj['server'],'POST',_chat_path(conversation_id)+'/plans/'+quote(plan_id,safe='')+'/approve',{}))


@chat_app.command('stop')
def chat_stop(ctx:typer.Context,conversation_id:str):
    """Request interruption; the returned state distinguishes stopping from paused."""
    output(_chat_request(ctx.obj['server'],'POST',_chat_path(conversation_id)+'/interrupt',{}))


@chat_app.command('resume')
def chat_resume(ctx:typer.Context,conversation_id:str):
    """Resume a saved conversation explicitly."""
    output(_chat_request(ctx.obj['server'],'POST',_chat_path(conversation_id)+'/resume',{}))


if __name__=='__main__':app()
