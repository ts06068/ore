"""Isolated installed-Chrome desktop controlled through a fixed X11 protocol.

This backend has no DOM/CDP/Playwright interface. Managed URL policies constrain
navigation; they are not a substitute for an egress firewall or API rate policy.
The host owns Docker lifecycle. Containers receive only their profile/download
state and a read-only policy, never the Docker socket or coordinator credentials.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
import uuid


class DesktopError(RuntimeError):
    """Native desktop unavailable or its bounded control command failed."""


class DesktopNotFound(DesktopError):
    """Docker explicitly confirmed that this named container is absent."""


def normalized_origins(values):
    if not isinstance(values, (list, tuple, set)) or not values or len(values) > 512:
        raise ValueError('Desktop requires a bounded explicit origin allowlist')
    result = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError('Invalid desktop origin')
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                or '*' in parsed.netloc or not re.fullmatch(r'[A-Za-z0-9.\-:\[\]]+', parsed.netloc)):
            raise ValueError('Desktop allowlist entries must be exact HTTP(S) origins')
        host = parsed.hostname.lower()
        host = f'[{host}]' if ':' in host else host
        port = parsed.port
        suffix = f':{port}' if port and port != (443 if parsed.scheme == 'https' else 80) else ''
        result.add(f'{parsed.scheme}://{host}{suffix}')
    return sorted(result)


def managed_policy(origins):
    normalized = normalized_origins(origins)
    # A dot before the host is Chrome's exact-host syntax, excluding subdomains.
    allowed = []
    for origin in normalized:
        p = urlsplit(origin)
        host = p.hostname
        authority = f'[{host}]' if ':' in host else '.' + host
        authority += ':' + str(p.port or (443 if p.scheme == 'https' else 80))
        allowed.append(f'{p.scheme}://{authority}')
    return {'URLBlocklist': ['*', 'chrome://*', 'file://*', 'devtools://*'], 'URLAllowlist': allowed + ['about:blank'],
            'DownloadDirectory': '/state/downloads', 'PromptForDownloadLocation': False, 'AlwaysOpenPdfExternally': True,
            'DeveloperToolsAvailability': 2, 'ExtensionInstallBlocklist': ['*'],
            'BrowserSignin': 0, 'SyncDisabled': True, 'DefaultBrowserSettingEnabled': False,
            'BrowserGuestModeEnabled': False, 'IncognitoModeAvailability': 1}


@dataclass
class DesktopSession:
    id: str
    container_name: str
    state_dir: Path
    profile_dir: Path
    download_dir: Path
    allowed_origins: list[str]
    profile_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    watchdog: asyncio.Task | None = None
    resource_usage: dict = field(default_factory=dict)
    lease_deadline: float | None = None
    ownership_deadline: float | None = None


class DesktopRuntime:
    def __init__(self, state_dir: Path, *, image='ore-desktop:0.2.0rc1', docker_binary='docker',
                 host_state_dir: Path | None = None, width=1280, height=800, network='bridge',
                 memory='2g', cpus=2, pids_limit=256, startup_timeout=40, command_timeout=40,
                 resource_mode='cgroup', seccomp_profile: Path | None = None,
                 max_lifetime_seconds=300, cpu_budget_seconds=600):
        self.state_dir = Path(state_dir).resolve()
        self.host_state_dir = Path(host_state_dir).resolve() if host_state_dir else self.state_dir
        if not 640 <= width <= 3840 or not 480 <= height <= 2160:
            raise ValueError('Unsupported desktop dimensions')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]*', image):
            raise ValueError('Invalid desktop image')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', network) or network in {'host', 'container'}:
            raise ValueError('Desktop requires a private bridge or isolated network')
        if not re.fullmatch(r'[1-9][0-9]*[mg]', memory) or not 0.25 <= cpus <= 16 or not 64 <= pids_limit <= 2048:
            raise ValueError('Invalid desktop resource limits')
        if resource_mode not in {'cgroup','watchdog'}:
            raise ValueError('Unknown desktop resource mode')
        if not 1 <= max_lifetime_seconds <= 300 or not 1 <= cpu_budget_seconds <= 600:
            raise ValueError('Diagnostic desktop budget exceeds its fixed maximum')
        asset=Path(__file__).resolve().parents[2]/'deploy'/'desktop'/'seccomp-chrome.json'
        if not asset.is_file():asset=Path(__file__).with_name('desktop_assets')/'seccomp-chrome.json'
        self.seccomp_profile=Path(seccomp_profile) if seccomp_profile else asset
        if not self.seccomp_profile.is_file():raise DesktopError('Native Chrome seccomp profile is missing')
        self.resource_mode=resource_mode
        self.max_lifetime_seconds=max_lifetime_seconds
        self.cpu_budget_seconds=cpu_budget_seconds
        self._watchdog_fd=None
        self.resource_reports={}
        self.image, self.docker_binary, self.network = image, docker_binary, network
        self.width, self.height, self.memory, self.cpus, self.pids_limit = width, height, memory, cpus, pids_limit
        self.startup_timeout, self.command_timeout = startup_timeout, command_timeout
        self.sessions: dict[str, DesktopSession] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._owner = uuid.uuid4().hex[:12]

    async def _command(self, args, *, data=None, timeout=None, max_output=24*1024*1024):
        process = await asyncio.create_subprocess_exec(self.docker_binary, *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def read_bound(stream):
            chunks=[];size=0
            while part := await stream.read(65536):
                size+=len(part)
                if size > max_output:
                    raise DesktopError('Desktop command exceeded output limit')
                chunks.append(part)
            return b''.join(chunks)
        async def communicate():
            if process.stdin:
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
            out, err = await asyncio.gather(read_bound(process.stdout), read_bound(process.stderr))
            await process.wait()
            return out, err
        try:
            out, err = await asyncio.wait_for(communicate(), timeout or self.command_timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            # Never reflect arbitrary Docker/helper stdout that may include URL/text arguments.
            safe = err.decode('utf-8', 'replace').lower()
            if 'no such container' in safe or 'no such object' in safe:
                raise DesktopNotFound('Desktop container is absent')
            if 'threaded' in safe and 'cgroup' in safe:
                reason='host cgroup does not support requested resource limits'
            elif 'operation not permitted' in safe or 'permission denied' in safe:
                reason='host denied the required container operation'
            elif 'no such image' in safe or 'pull access denied' in safe:
                reason='desktop image is not installed'
            else:
                reason=f'command exited with status {process.returncode}'
            raise DesktopError(reason)
        return out+err if args and args[0]=='logs' else out

    def _host_path(self, path):
        return self.host_state_dir / path.relative_to(self.state_dir)

    def _run_args(self, session, policy_path):
        limits=(['--memory',self.memory,'--memory-swap',self.memory,'--cpus',str(self.cpus)]
                if self.resource_mode=='cgroup' else ['--cgroup-parent','/'])
        return ['run', '--detach', '--init', '--pull=never', '--name', session.container_name,
            '--label', 'ore.role=desktop', '--label', 'ore.desktop.owner='+self._owner,
            '--user', f'{os.getuid()}:{os.getgid()}', '--network', self.network,
            '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--security-opt','seccomp='+str(self.seccomp_profile),*limits,
            '--pids-limit', str(self.pids_limit), '--shm-size', '512m',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m,mode=1777',
            '--env', 'DISPLAY=:99', '--env', 'OMP_THREAD_LIMIT=1', '--env', 'MAGICK_THREAD_LIMIT=1',
            '--env', 'XDG_CACHE_HOME=/tmp/ore-cache',
            '--env', 'XDG_CONFIG_HOME=/tmp/ore-config', '--env', 'XDG_RUNTIME_DIR=/tmp/ore-runtime',
            '--env','XDG_DATA_HOME=/tmp/ore-data',
            '--env', f'ORE_DESKTOP_WIDTH={self.width}',
            '--env', f'ORE_DESKTOP_HEIGHT={self.height}',
            '--mount', f'type=bind,source={self._host_path(session.state_dir)},target=/state',
            '--mount', f'type=bind,source={self._host_path(policy_path)},target=/etc/opt/chrome/policies/managed/ore.json,readonly',
            self.image]

    async def create(self, session_id=None, *, allowed_origins, profile_id=None, task_budget_seconds=None):
        origins = normalized_origins(allowed_origins)
        sid = session_id or str(uuid.uuid4())
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', sid):
            raise ValueError('Invalid desktop session id')
        # Each session gets a complete native Chrome profile. Never share an active user-data directory.
        if profile_id is not None and (not isinstance(profile_id,str) or len(profile_id)>512):
            raise ValueError('Invalid desktop profile identity')
        if task_budget_seconds is not None and (isinstance(task_budget_seconds, bool) or not isinstance(task_budget_seconds, (int,float)) or not math.isfinite(task_budget_seconds) or task_budget_seconds <= 0):
            raise ValueError('Desktop ownership requires a finite positive task budget')
        async with self._lifecycle_lock:
            if sid in self.sessions:
                raise DesktopError('Desktop session is already active')
            if self.resource_mode=='watchdog':
                if self.sessions:raise DesktopError('Diagnostic watchdog mode permits one desktop only')
            digest=hashlib.sha256(sid.encode()).hexdigest()
            directory=self.state_dir/'sessions'/digest
            for path in [self.state_dir,self.state_dir/'sessions',directory,directory/'profile',directory/'downloads']:
                if path.is_symlink():raise DesktopError('Desktop state paths cannot be symlinks')
                path.mkdir(parents=True,exist_ok=True,mode=0o700)
                path.chmod(0o700)
            policy_dir=self.state_dir/'policies'
            policy_dir.mkdir(mode=0o700,exist_ok=True)
            policy_path=policy_dir/(digest+'.json')
            if policy_path.is_symlink():raise DesktopError('Desktop policy path cannot be a symlink')
            policy_path.write_text(json.dumps(managed_policy(origins)))
            policy_path.chmod(0o600)
            session=DesktopSession(sid,'ore-desktop-'+self._owner+'-'+digest[:16],directory,
                directory/'profile',directory/'downloads',origins,profile_id or sid)
            now=time.monotonic()
            session.ownership_deadline=now+min(float(task_budget_seconds or self.max_lifetime_seconds),3600)
            session.lease_deadline=min(now+self.max_lifetime_seconds,session.ownership_deadline)
            if self.resource_mode=='watchdog':self._acquire_watchdog_lock()
            try:
                await self._command(self._run_args(session,policy_path),timeout=30,max_output=65536)
                self.sessions[sid]=session
                if self.resource_mode=='watchdog':
                    session.watchdog=asyncio.create_task(self._watchdog(session))
                else:
                    session.watchdog=asyncio.create_task(self._lease_watchdog(session))
                deadline=time.monotonic()+self.startup_timeout
                while time.monotonic()<deadline:
                    try:
                        result=await self._rpc(session,'ready',{},timeout=5)
                        if result.get('ready'):
                            self.sessions[sid]=session
                            return session
                    except DesktopError:
                        pass
                    await asyncio.sleep(.25)
                raise DesktopError('Chrome did not expose a native window; inspect scoped container logs for sandbox/host failure')
            except BaseException:
                try:
                    log=await self._command(['logs','--tail','100',session.container_name],timeout=5,max_output=65536)
                    fd=os.open(directory/'startup.log',os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as stream:stream.write(log)
                except (DesktopError,OSError):pass
                # A launch timeout may still have created the named container.
                # Keep its ownership reserved until cleanup has a real receipt.
                self.sessions.setdefault(sid,session)
                try:await self.close_session(sid)
                except DesktopError:
                    raise DesktopError('Desktop startup failed and termination remains unconfirmed') from None
                raise

    async def create_session(self, session_id=None, allowed_origins=None, **kwargs):
        return await self.create(session_id,allowed_origins=allowed_origins,**kwargs)

    def get(self, sid):
        session=self.sessions.get(sid)
        if session is None or session.closed:
            raise DesktopError('Desktop session is unavailable')
        return session

    async def _rpc(self, session, action, args, *, timeout=None):
        raw=await self._command(['exec','--interactive',session.container_name,'python3','/opt/ore-desktop/control.py'],
            data=json.dumps({'action':action,'args':args}).encode(),timeout=timeout)
        if action=='screenshot':
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise DesktopError('Desktop returned an invalid PNG')
            return raw
        try:return json.loads(raw)
        except ValueError as exc:raise DesktopError('Desktop returned an invalid control response') from exc

    async def screenshot(self,sid):
        session=self.get(sid)
        async with session.lock:return await self._rpc(session,'screenshot',{})

    async def observe(self,sid,*,include_text=True,read_url=True):
        session=self.get(sid)
        async with session.lock:
            result=await self._rpc(session,'observe',{'include_text':include_text,'read_url':read_url})
            try:png=base64.b64decode(result.pop('png_base64'),validate=True)
            except (KeyError,ValueError) as exc:raise DesktopError('Desktop returned an invalid screenshot') from exc
            if not png.startswith(b'\x89PNG\r\n\x1a\n'):
                raise DesktopError('Desktop returned an invalid screenshot')
            result['png']=png
            return result

    async def current_url(self,sid):
        session=self.get(sid)
        async with session.lock:return (await self._rpc(session,'current_url',{})).get('url')

    async def action(self,sid,action,args=None):
        session=self.get(sid);args=args or {}
        if action not in {'navigate','click','type','fill','press','key','scroll'}:
            raise ValueError('Unsupported desktop action')
        if action=='navigate':
            parsed=urlsplit(args.get('url',''))
            origin=f'{parsed.scheme}://{parsed.netloc}'
            if normalized_origins([origin])[0] not in session.allowed_origins:
                raise DesktopError('Navigation origin is outside this desktop allowlist')
        async with session.lock:return await self._rpc(session,action,args)

    async def navigate(self,sid,url):return await self.action(sid,'navigate',{'url':url})
    async def click(self,sid,x,y,button='left'):return await self.action(sid,'click',{'x':x,'y':y,'button':button})
    async def type_text(self,sid,text):return await self.action(sid,'type',{'text':text})
    async def key(self,sid,key):return await self.action(sid,'press',{'key':key})
    async def scroll(self,sid,delta_y,delta_x=0):return await self.action(sid,'scroll',{'delta_y':delta_y,'delta_x':delta_x})

    async def downloads(self,sid):
        session=self.get(sid)
        async with session.lock:return await asyncio.to_thread(read_completed_downloads,session)

    async def close_session(self,sid):
        session=self.sessions.get(sid)
        if session is None:return
        async with session.lock:
            if session.closed:return
            # Stop/rm success or an explicit not-found receipt confirms that
            # execution ended. A daemon/transport error is not that receipt.
            confirmed=False
            try:
                await self._command(['stop','--time','10',session.container_name],timeout=15,max_output=65536)
                confirmed=True
            except DesktopNotFound:confirmed=True
            except DesktopError:pass
            try:
                await self._command(['rm','--force',session.container_name],timeout=10,max_output=65536)
                confirmed=True
            except DesktopNotFound:confirmed=True
            except DesktopError:pass
            if not confirmed:
                session.resource_usage['termination_unconfirmed']=True
                raise DesktopError('Desktop termination is unconfirmed; ownership and capacity remain reserved')
            session.resource_usage['termination_unconfirmed']=False
            session.closed=True
            if session.watchdog and session.watchdog is not asyncio.current_task():
                session.watchdog.cancel()
                try:await session.watchdog
                except asyncio.CancelledError:pass
            self.sessions.pop(sid,None)
            self._release_watchdog_lock()

    def _acquire_watchdog_lock(self):
        path=Path('/tmp')/f'ore-desktop-watchdog-{os.getuid()}.lock'
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            if os.fstat(fd).st_uid!=os.getuid():raise DesktopError('Unexpected watchdog lock owner')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,DesktopError) as exc:
            os.close(fd)
            raise DesktopError('Another diagnostic desktop is already running') from exc
        self._watchdog_fd=fd

    def _release_watchdog_lock(self):
        if self._watchdog_fd is not None:
            fcntl.flock(self._watchdog_fd,fcntl.LOCK_UN)
            os.close(self._watchdog_fd)
            self._watchdog_fd=None

    def renew_lease(self, sid):
        """Trusted browser-owner heartbeat; never resets CPU or challenge budgets."""
        session=self.get(sid);now=time.monotonic()
        if session.lease_deadline is None or session.ownership_deadline is None:return False
        if now >= session.lease_deadline or now >= session.ownership_deadline:return False
        session.lease_deadline=min(now+self.max_lifetime_seconds,session.ownership_deadline)
        return True

    async def _sample_usage(self,session):
        # Numeric-only process listing; no browser command lines or document contents.
        rows=await self._command(['top',session.container_name,'-eo','pid,rss'],timeout=5,max_output=65536)
        process_rss={}
        for line in rows.decode().splitlines():
            parts=line.split()
            if len(parts)==2 and all(value.isdigit() for value in parts):
                pid,value=map(int,parts)
                process_rss[pid]=max(process_rss.get(pid,0),value)
        rss=sum(process_rss.values())*1024
        code='from pathlib import Path; print(Path("/sys/fs/cgroup/cpu.stat").read_text()); print("pids_current",Path("/sys/fs/cgroup/pids.current").read_text())'
        raw=await self._command(['exec',session.container_name,'python3','-c',code],timeout=5,max_output=65536)
        fields=dict(line.split() for line in raw.decode().splitlines() if len(line.split())==2)
        if 'usage_usec' not in fields or not rss:raise DesktopError('Resource accounting unavailable')
        session.resource_usage['observed_pids']=int(fields.get('pids_current',0))
        return rss,int(fields['usage_usec'])/1_000_000

    async def _lease_watchdog(self,session):
        """Cgroup-backed sessions still require an active owner and finite lifetime."""
        while not session.closed:
            remaining=(session.lease_deadline or 0)-time.monotonic()
            if remaining<=0:
                session.resource_usage['stop_reason']='ownership_lease_expired'
                try:await self._command(['kill',session.container_name],timeout=5,max_output=65536)
                except DesktopError:pass
                await self.close_session(session.id)
                return
            await asyncio.sleep(min(1,remaining))

    async def _watchdog(self,session):
        started=time.monotonic();baseline=0
        memory_limit=int(self.memory[:-1])*(1024**(3 if self.memory[-1]=='g' else 2))
        report={'mode':'watchdog','hard_memory_cpu_limits':False,'hard_pid_limit':self.pids_limit,
                'rss_budget_bytes':memory_limit,'cpu_budget_seconds':self.cpu_budget_seconds,
                'lifetime_seconds':self.max_lifetime_seconds,'peak_observed_rss_bytes':0,
                'observed_cpu_seconds':0,'samples':0}
        session.resource_usage=report;self.resource_reports[session.id]=report
        try:
            while not session.closed:
                lease=session.lease_deadline if session.lease_deadline is not None else started+self.max_lifetime_seconds
                report['lease_remaining_seconds']=max(0,lease-time.monotonic())
                report['ownership_remaining_seconds']=max(0,(session.ownership_deadline or lease)-time.monotonic())
                if time.monotonic()>=lease:
                    report['stop_reason']='lifetime_budget';break
                rss,cpu=await self._sample_usage(session)
                report['samples']+=1
                report['peak_observed_rss_bytes']=max(report['peak_observed_rss_bytes'],rss)
                report['observed_cpu_seconds']=max(report['observed_cpu_seconds'],cpu-baseline)
                if rss>memory_limit:report['stop_reason']='rss_budget';break
                if cpu-baseline>self.cpu_budget_seconds:report['stop_reason']='cpu_time_budget';break
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            report['stop_reason']='supervisor_unavailable'
        finally:
            report['elapsed_seconds']=round(time.monotonic()-started,3)
            try:
                fd=os.open(session.state_dir/'resource-report.json',os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as stream:json.dump(report,stream,indent=2)
            except OSError:pass
        if not session.closed:
            # Kill before waiting on an input/observation lock, including hung OCR.
            try:await self._command(['kill',session.container_name],timeout=5,max_output=65536)
            except DesktopError:pass
            await self.close_session(session.id)

    async def close(self):
        outcomes=await asyncio.gather(*(self.close_session(sid) for sid in list(self.sessions)),return_exceptions=True)
        if not self.sessions:self._release_watchdog_lock()
        failures=[value for value in outcomes if isinstance(value,BaseException)]
        if failures:raise failures[0]


def _copy_history_snapshot(history, destination):
    # Chrome retains an exclusive SQLite lock. Copy only History and its recovery
    # files during one stable filesystem generation; never disable source locking.
    # SQLite recovers the private copy, including uncommitted rollback/WAL data.
    names = ('History', 'History-journal', 'History-wal')
    def signature(path):
        try:
            value = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode) or value.st_size > 64 * 1024 * 1024:
            raise sqlite3.OperationalError('History snapshot file is unavailable or too large')
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    before = {name: signature(history.parent / name) for name in names}
    if before['History'] is None:
        raise sqlite3.OperationalError('History is unavailable')
    for name, expected in before.items():
        if expected is None:
            continue
        fd = os.open(history.parent / name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            value = os.fstat(source.fileno())
            if (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns) != expected:
                raise sqlite3.OperationalError('History changed during snapshot')
            data = source.read(expected[2] + 1)
        if len(data) != expected[2]:
            raise sqlite3.OperationalError('History changed during snapshot')
        (destination / name).write_bytes(data)
    if before != {name: signature(history.parent / name) for name in names}:
        raise sqlite3.OperationalError('History changed during snapshot')
    return destination / 'History'


def read_completed_downloads(session: DesktopSession):
    """Read only Chrome download history; cookie/login tables are never queried.

    A completed History record plus exact file size and a confined regular file
    provides observed browser provenance, not format/content/identity validation.
    """
    history=session.profile_dir/'Default'/'History'
    if not history.is_file() or history.is_symlink():return []
    receipts=[]
    connection=None
    snapshot=None
    try:
        snapshot=tempfile.TemporaryDirectory(prefix='ore-download-history-')
        copied=_copy_history_snapshot(history, Path(snapshot.name))
        connection=sqlite3.connect(copied,timeout=1)
        # Recovery may write only to this disposable copy, never the live profile.
        if connection.execute('PRAGMA quick_check').fetchone() != ('ok',):return []
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        rows=connection.execute('SELECT id,guid,target_path,received_bytes,total_bytes,state,interrupt_reason FROM downloads ORDER BY id DESC LIMIT 1000').fetchall()
        for did,guid,target,received,total,state,interrupt in rows:
            if state!=1 or interrupt!=0 or received<=0 or total not in (-1,received):continue
            p=Path(target)
            try:relative=p.relative_to('/state/downloads')
            except ValueError:continue
            if len(relative.parts)!=1 or relative.name in {'','.','..'} or relative.name.endswith(('.crdownload','.tmp')):continue
            path=session.download_dir/relative
            try:
                before=path.lstat()
                if not stat.S_ISREG(before.st_mode) or path.resolve().parent!=session.download_dir.resolve() or before.st_size!=received:continue
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as stream:
                    opened=os.fstat(stream.fileno())
                    if opened.st_ino!=before.st_ino or opened.st_dev!=before.st_dev:continue
                    digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    after=os.fstat(stream.fileno())
                    if (opened.st_size,opened.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):continue
                chain=[row[0] for row in connection.execute('SELECT url FROM downloads_url_chains WHERE id=? ORDER BY chain_index',(did,)).fetchall()]
                if not chain or any(urlsplit(url).scheme not in {'http','https'} or not urlsplit(url).hostname or urlsplit(url).username for url in chain):continue
            except (OSError,ValueError):continue
            receipts.append({'id':str(guid or did),'filename':relative.name,'path':str(path),
                'bytes':received,'received_bytes':received,'total_bytes':total,'state':state,'complete':True,
                'sha256':digest,'url':chain[0],'final_url':chain[-1],'url_chain':chain,
                'provenance':'chrome_download_history'})
    except (sqlite3.Error, OSError):
        return []
    finally:
        if connection is not None:connection.close()
        if snapshot is not None:snapshot.cleanup()
    return receipts
