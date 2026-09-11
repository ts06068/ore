import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from ore.desktop import DesktopError, DesktopRuntime, DesktopSession, managed_policy, normalized_origins, read_completed_downloads


def make_session(tmp_path):
    profile=tmp_path/'profile'; downloads=tmp_path/'downloads'
    (profile/'Default').mkdir(parents=True);downloads.mkdir()
    return DesktopSession('fixture','ore-desktop-test',tmp_path,profile,downloads,['https://example.test'],'fixture')


def test_managed_policy_exact_origins_and_resource_controls(tmp_path):
    policy=managed_policy(['https://example.test','http://example.test:8080'])
    assert policy['URLBlocklist']==['*','chrome://*','file://*','devtools://*']
    assert policy['URLAllowlist']==['http://.example.test:8080','https://.example.test:443','about:blank']
    assert policy['DeveloperToolsAvailability']==2
    runtime=DesktopRuntime(tmp_path)
    args=runtime._run_args(make_session(tmp_path),tmp_path/'policy.json')
    assert '--cap-drop=ALL' in args and '--security-opt=no-new-privileges:true' in args
    assert args[args.index('--memory')+1]=='2g'
    assert args[args.index('--pids-limit')+1]=='256'
    assert '--read-only' in args and '--pull=never' in args
    joined=' '.join(args)
    for forbidden in ['--privileged','docker.sock','.codex','--no-sandbox','--remote-debugging','--network host']:
        assert forbidden not in joined


@pytest.mark.parametrize('value', ['https://*.example.test','https://user:pass@example.test','https://example.test/path',
    'https://example.test/?token=secret','file:///tmp/doc','https://example.test/#fragment','https://example.test,other'])
def test_origin_policy_rejects_ambiguous_scope(value):
    with pytest.raises(ValueError):normalized_origins([value])


@pytest.mark.asyncio
async def test_lifecycle_and_typed_text_stays_off_argv(tmp_path,monkeypatch):
    runtime=DesktopRuntime(tmp_path)
    calls=[]
    async def command(args,**kwargs):
        calls.append((args,kwargs))
        if args[0]=='exec':
            request=json.loads(kwargs['data'])
            return json.dumps({'ready':True} if request['action']=='ready' else {'ok':True}).encode()
        return b'container'
    monkeypatch.setattr(runtime,'_command',command)
    session=await runtime.create('fixture',allowed_origins=['https://example.test'])
    await runtime.type_text(session.id,'private fixture text')
    assert 'private fixture text' not in ' '.join(calls[-1][0])
    assert json.loads(calls[-1][1]['data'])['args']['text']=='private fixture text'
    with pytest.raises(DesktopError):await runtime.navigate(session.id,'https://other.test/')
    await runtime.close_session(session.id);await runtime.close_session(session.id)
    assert len([c for c in calls if c[0][0]=='stop'])==1
    assert not runtime.sessions


@pytest.mark.asyncio
async def test_native_observation_is_png_ocr_not_html(tmp_path,monkeypatch):
    runtime=DesktopRuntime(tmp_path);session=make_session(tmp_path);runtime.sessions[session.id]=session
    png=b'\x89PNG\r\n\x1a\nfixture'
    async def rpc(*args,**kwargs):return {'png_base64':base64.b64encode(png).decode(),'url':'https://example.test/',
        'url_observed':True,'title':'Example','text':'Visible example','width':1280,'height':800,'text_method':'screenshot_ocr'}
    monkeypatch.setattr(runtime,'_rpc',rpc)
    result=await runtime.observe(session.id)
    assert result['png']==png and result['text_method']=='screenshot_ocr'
    assert 'html' not in result and 'status' not in result


def seed_history(session, rows):
    db=sqlite3.connect(session.profile_dir/'Default'/'History')
    db.execute('CREATE TABLE downloads(id INTEGER,guid TEXT,target_path TEXT,received_bytes INTEGER,total_bytes INTEGER,state INTEGER,interrupt_reason INTEGER)')
    db.execute('CREATE TABLE downloads_url_chains(id INTEGER,chain_index INTEGER,url TEXT)')
    # A trap table proves the code does not need or export arbitrary history fields.
    db.execute('CREATE TABLE private_fixture(secret TEXT)');db.execute("INSERT INTO private_fixture VALUES ('not-for-output')")
    for row in rows:db.execute('INSERT INTO downloads VALUES (?,?,?,?,?,?,?)',row)
    db.executemany('INSERT INTO downloads_url_chains VALUES (?,?,?)',[(1,0,'https://example.test/start'),
        (1,1,'https://cdn.example.test/file.pdf'),(2,0,'https://example.test/partial'),
        (3,0,'https://example.test/escape'),(4,0,'https://example.test/symlink'),(5,0,'https://example.test/wrong')])
    db.commit();db.close()


def test_download_receipt_requires_completed_history_confined_regular_file_and_size(tmp_path):
    session=make_session(tmp_path)
    (session.download_dir/'actual.pdf').write_bytes(b'%PDF-fixture')
    (session.download_dir/'partial.crdownload').write_bytes(b'partial')
    outside=tmp_path/'outside';outside.write_bytes(b'secret')
    (session.download_dir/'link.pdf').symlink_to(outside)
    (session.download_dir/'wrong.pdf').write_bytes(b'wrong')
    seed_history(session,[(1,'actual','/state/downloads/actual.pdf',12,12,1,0),
        (2,'partial','/state/downloads/partial.crdownload',7,100,0,0),
        (3,'escape','/state/downloads/../outside',6,6,1,0),
        (4,'link','/state/downloads/link.pdf',6,6,1,0),
        (5,'wrong','/state/downloads/wrong.pdf',100,100,1,0)])
    receipts=read_completed_downloads(session)
    assert len(receipts)==1
    receipt=receipts[0]
    assert receipt['url_chain']==['https://example.test/start','https://cdn.example.test/file.pdf']
    assert receipt['bytes']==12 and receipt['complete'] and len(receipt['sha256'])==64
    assert receipt['provenance']=='chrome_download_history'
    assert 'not-for-output' not in json.dumps(receipts)


def control_module():
    path=Path(__file__).parents[1]/'deploy'/'desktop'/'control.py'
    spec=importlib.util.spec_from_file_location('ore_desktop_control_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('key',['F12','Control+Shift+i','Super+Return','Alt+F2','ctrl+l;rm -rf /','ctrl+shift+j'])
def test_native_input_does_not_enable_debugger_or_shell_shortcuts(key):
    with pytest.raises(ValueError):control_module().key(key)


def test_image_keeps_normal_chrome_sandbox_and_no_browser_debugger():
    entry=(Path(__file__).parents[1]/'deploy'/'desktop'/'entrypoint.py').read_text()
    assert "'google-chrome-stable'" in entry
    for flag in ['--no-sandbox','--disable-setuid-sandbox','--remote-debugging-port','--remote-debugging-pipe','--disable-blink-features','AutomationControlled']:
        assert flag not in entry


def test_watchdog_mode_is_explicit_with_hard_pid_limit_and_default_unchanged(tmp_path):
    session=make_session(tmp_path)
    hard=DesktopRuntime(tmp_path)
    diagnostic=DesktopRuntime(tmp_path,resource_mode='watchdog')
    hard_args=hard._run_args(session,tmp_path/'policy.json')
    soft_args=diagnostic._run_args(session,tmp_path/'policy.json')
    assert '--memory' in hard_args and '--cpus' in hard_args
    assert '--memory' not in soft_args and '--cpus' not in soft_args
    assert soft_args[soft_args.index('--pids-limit')+1]=='256'
    assert soft_args[soft_args.index('--cgroup-parent')+1]=='/'
    assert '--cap-drop=ALL' in soft_args and '--security-opt=no-new-privileges:true' in soft_args
    with pytest.raises(ValueError):DesktopRuntime(tmp_path,resource_mode='automatic')
    with pytest.raises(ValueError):DesktopRuntime(tmp_path,resource_mode='watchdog',max_lifetime_seconds=301)


@pytest.mark.asyncio
@pytest.mark.parametrize('rss,cpu,reason',[(3*1024**3,1,'rss_budget'),(1,601,'cpu_time_budget')])
async def test_watchdog_kills_budget_exhaustion_before_input_lock(tmp_path,monkeypatch,rss,cpu,reason):
    runtime=DesktopRuntime(tmp_path,resource_mode='watchdog');session=make_session(tmp_path)
    calls=[]
    async def sample(_):return rss,cpu
    async def command(args,**kwargs):calls.append(args);return b''
    async def close(sid):calls.append(['closed',sid]);session.closed=True
    monkeypatch.setattr(runtime,'_sample_usage',sample)
    monkeypatch.setattr(runtime,'_command',command)
    monkeypatch.setattr(runtime,'close_session',close)
    await runtime._watchdog(session)
    assert calls[0]==['kill',session.container_name]
    assert calls[1]==['closed',session.id]
    assert session.resource_usage['stop_reason']==reason
    assert session.resource_usage['hard_memory_cpu_limits'] is False


def test_seccomp_adds_exact_namespace_calls_without_granting_host_capabilities():
    path=Path(__file__).parents[1]/'deploy'/'desktop'/'seccomp-chrome.json'
    profile=json.loads(path.read_text())
    assert profile['defaultAction']=='SCMP_ACT_ERRNO'
    additions=profile['syscalls'][-5:]
    assert additions[0]['names']==['unshare']
    assert additions[0]['args']==[{'index':0,'value':0x10000000,'op':'SCMP_CMP_EQ'}]
    assert [rule['args'][0]['value'] for rule in additions[1:3]]==[0x10000011,0x70000011]
    assert additions[3]=={'names':['chroot'],'action':'SCMP_ACT_ALLOW'}

    assert additions[4]['args']==[{'index':0,'value':0x20000011,'op':'SCMP_CMP_EQ'}]


@pytest.mark.asyncio
async def test_resource_sampling_deduplicates_docker_thread_rows(tmp_path,monkeypatch):
    runtime=DesktopRuntime(tmp_path);session=make_session(tmp_path)
    async def command(args,**kwargs):
        if args[0]=='top':return b'PID RSS\n100 200\n100 201\n100 200\n101 300\n'
        return b'usage_usec 1500000\npids_current 4\n'
    monkeypatch.setattr(runtime,'_command',command)
    rss,cpu=await runtime._sample_usage(session)
    assert rss==(201+300)*1024 and cpu==1.5

@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
def test_download_snapshot_reads_committed_records_under_exclusive_lock(tmp_path, journal_mode):
    session = make_session(tmp_path)
    (session.download_dir/'actual.pdf').write_bytes(b'%PDF-fixture')
    seed_history(session, [(1,'actual','/state/downloads/actual.pdf',12,12,1,0)])
    history = session.profile_dir/'Default'/'History'
    writer = sqlite3.connect(history)
    writer.execute('PRAGMA journal_mode=' + journal_mode)
    writer.execute('PRAGMA locking_mode=EXCLUSIVE')
    writer.execute('BEGIN EXCLUSIVE')
    writer.execute('UPDATE downloads SET state=0 WHERE id=1')
    writer.commit()
    # The writer keeps the exclusive lock. An uncommitted update must not turn
    # this pending download into a completed ORE receipt.
    writer.execute('BEGIN EXCLUSIVE')
    writer.execute('UPDATE downloads SET state=1 WHERE id=1')
    assert read_completed_downloads(session) == []
    writer.commit()
    records = read_completed_downloads(session)
    assert len(records) == 1 and records[0]['complete']
    assert writer.execute('SELECT state FROM downloads').fetchone() == (1,)
    writer.close()


def test_native_runtime_reaps_helpers_and_bounds_ocr_threads(tmp_path):
    runtime = DesktopRuntime(tmp_path)
    args = runtime._run_args(make_session(tmp_path), tmp_path/'policy.json')
    assert '--init' in args
    assert 'OMP_THREAD_LIMIT=1' in args and 'MAGICK_THREAD_LIMIT=1' in args


def test_url_observation_waits_for_clipboard_without_repeating_input_or_cancelling_load(monkeypatch):
    module = control_module()
    keys = []; commands = []; values = iter([b'', b'https://example.test/issue'])
    monkeypatch.setattr(module, 'key', keys.append)
    monkeypatch.setattr(module, 'clear_clipboard', lambda: None)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    def command(args, **kwargs):
        commands.append(args)
        return next(values) if args[0] == 'xclip' else b''
    monkeypatch.setattr(module, 'run', command)
    assert module.current_url() == 'https://example.test/issue'
    assert keys == ['ctrl+l', 'ctrl+c']
    assert len([c for c in commands if c[0] == 'xclip']) == 2
    assert commands[-1][-1] == 'ctrl+F6'


def test_ownership_lease_preserves_challenge_time_without_resetting_resource_budgets(tmp_path,monkeypatch):
    from ore import desktop
    now=[100.]
    monkeypatch.setattr(desktop.time,'monotonic',lambda:now[0])
    runtime=DesktopRuntime(tmp_path,resource_mode='watchdog')
    session=make_session(tmp_path);session.lease_deadline=400.;session.ownership_deadline=700.
    session.resource_usage={'observed_cpu_seconds':99.}
    runtime.sessions[session.id]=session
    now[0]=350.
    assert runtime.renew_lease(session.id)
    assert session.lease_deadline==650.
    assert session.resource_usage['observed_cpu_seconds']==99.
    now[0]=640.
    assert runtime.renew_lease(session.id) and session.lease_deadline==700.
    now[0]=701.
    assert not runtime.renew_lease(session.id)
    assert session.lease_deadline==700.


@pytest.mark.asyncio
async def test_unconfirmed_desktop_termination_retains_session_and_capacity(tmp_path,monkeypatch):
    from ore.desktop import DesktopNotFound
    runtime=DesktopRuntime(tmp_path,resource_mode='watchdog');session=make_session(tmp_path)
    runtime.sessions[session.id]=session
    released=[]
    monkeypatch.setattr(runtime,'_release_watchdog_lock',lambda:released.append(True))
    async def unavailable(args,**kwargs):raise DesktopError('daemon unavailable')
    monkeypatch.setattr(runtime,'_command',unavailable)
    with pytest.raises(DesktopError,match='unconfirmed'):await runtime.close_session(session.id)
    assert not session.closed and session.id in runtime.sessions and not released
    assert session.resource_usage['termination_unconfirmed']
    async def absent(args,**kwargs):raise DesktopNotFound('absent')
    monkeypatch.setattr(runtime,'_command',absent)
    await runtime.close_session(session.id)
    assert session.closed and session.id not in runtime.sessions and released
