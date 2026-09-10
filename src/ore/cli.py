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
                try:await engine.browser.start();result['checks']['browser']='ok'
                except Exception as exc:result['checks']['browser']=type(exc).__name__
            return result
        finally:await engine.stop()
    output(asyncio.run(check()))

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
def worker(server:str='http://127.0.0.1:8765',parallel:int=typer.Option(1,min=1,max=32),once:bool=False):
    """Run independent Codex agent threads on this authenticated host."""
    from .remote import worker as run_worker
    token=os.environ.get('ORE_AUTH_TOKEN')
    if not token:
        path=Settings().state_dir/'operator.token'
        if path.exists():token=path.read_text().strip()
    if not token:raise typer.BadParameter('Set ORE_AUTH_TOKEN or use the local operator.token file')
    asyncio.run(run_worker(server,token,parallel=parallel,once=once))

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
    settings=Settings().prepare();upgrade(settings.database_url);output({'schema':'0001','status':'upgraded'})

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

if __name__=='__main__':app()
