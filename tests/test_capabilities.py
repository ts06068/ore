import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from ore.capabilities import CapabilityRegistry, ToolSpec, object_schema
from ore.config import Settings
from ore.coverage import CoverageLedger
from ore.models import Mission
from ore.policy import AccessDenied
from ore.store import Store
from ore.tools import ToolRuntime
from ore.vault import Vault


@pytest.fixture
def runtime(tmp_path):
    store = Store('sqlite:///:memory:'); store.initialize()
    job = store.create_job(Mission(goal='Transform generic content', artifact_roles=[]).model_dump(mode='json'))
    engine = SimpleNamespace(store=store, settings=SimpleNamespace(state_dir=tmp_path), vault=Vault(tmp_path/'vault'),
        event=store.append_event, profile=lambda mission: {'id':'public'}, coverage=CoverageLedger(store,tmp_path))
    engine.capabilities = CapabilityRegistry(engine, plugins=False)
    yield ToolRuntime(engine,job['id'])
    store.close()


@pytest.mark.asyncio
async def test_extensible_contracts_and_deterministic_content(runtime):
    registry = runtime.engine.capabilities
    async def twice(args, ctx): return {'n': args['n']*2}
    registry.register(ToolSpec('custom.twice','A new domain without core edits',object_schema({'n':{'type':'integer'}},['n']),read_only=True),twice)
    assert (await registry.execute('custom.twice',{'n':3},runtime))['n']==6
    with pytest.raises(ValueError): await registry.execute('custom.twice',{'n':'bad'},runtime)
    selected=await registry.execute('content.select',{'html':'<main><p>A <b>specific</b> paragraph.</p><p>Second.</p></main>', 'selector':'main p'},runtime)
    assert selected['items'][0]['text']=='A specific paragraph.' and selected['matched']==2
    saved=await registry.execute('content.write',{'data':selected['items'],'format':'json','filename':'paragraphs.json'},runtime)
    read=await registry.execute('content.read',{'artifact_id':saved['artifact']['id']},runtime)
    assert read['data']==selected['items']
    assert saved['artifact']['original_download'] is False


@pytest.mark.asyncio
async def test_readonly_and_artifact_integrity_boundaries(runtime):
    registry=runtime.engine.capabilities
    runtime.read_only=True
    with pytest.raises(AccessDenied): await registry.execute('content.write',{'data':'a','format':'text'},runtime)
    runtime.read_only=False
    saved=await registry.execute('content.write',{'data':'a','format':'text'},runtime)
    record=runtime.engine.store.artifacts(runtime.job_id)[0]
    from pathlib import Path
    Path(record['path']).write_bytes(b'changed')
    with pytest.raises(AccessDenied,match='hash'):await registry.execute('content.read',{'artifact_id':saved['artifact']['id']},runtime)


def test_remote_schema_references_and_replacement_are_rejected(runtime):
    registry=runtime.engine.capabilities
    with pytest.raises(ValueError):registry.register(ToolSpec('evil.schema','invalid',{'$ref':'https://example.test/schema'}),lambda a,r:{})
    with pytest.raises(ValueError):registry.register(registry.spec('state'),lambda a,r:{})


@pytest.mark.asyncio
async def test_generated_program_digest_and_output_schema(runtime,monkeypatch):
    from ore import sandbox
    async def image(language):return 'sha256:'+'a'*64
    async def execute(program,value,**kwargs):return {'output':{'sum':sum(value)},'image':program['image']}
    monkeypatch.setattr(sandbox,'resolve_image',image);monkeypatch.setattr(sandbox,'run_program',execute)
    registry=runtime.engine.capabilities
    registered=await registry.execute('code.register',{'language':'python','source':'source',
        'input_schema':{'type':'array','items':{'type':'integer'}},'output_schema':object_schema({'sum':{'type':'integer'}},['sum'])},runtime)
    result=await registry.execute('code.run',{'program_id':registered['program_id'],'input':[1,2]},runtime)
    assert result['output']=={'sum':3} and result['output_verified']
    with pytest.raises(ValueError):await registry.execute('code.run',{'program_id':registered['program_id'],'input':['bad']},runtime)
    other=runtime.engine.store.create_job({'goal':'other'})
    with pytest.raises(AccessDenied):await registry.execute('code.run',{'program_id':registered['program_id'],'input':[1]},ToolRuntime(runtime.engine,other['id']))

@pytest.mark.asyncio
async def test_raw_paragraph_preserves_source_whitespace(runtime):
    result=await runtime.engine.capabilities.execute('content.select',{
        'html':'<p>First  sentence.\n<b>Second</b> sentence.</p>', 'selector':'p','text_mode':'raw'},runtime)
    assert result['items'][0]['text']=='First  sentence.\nSecond sentence.'
    assert result['text_mode']=='raw'


def test_sync_plugin_cannot_outlive_a_cancelled_task(runtime):
    # A to_thread handler survives cancellation of its awaiter. Require an async
    # lifecycle contract; blocking generated programs belong in the sandbox.
    with pytest.raises(ValueError, match='must be async'):
        runtime.engine.capabilities.register(
            ToolSpec('custom.blocking', 'Unsafe lifecycle', object_schema()), lambda args, ctx: {})
