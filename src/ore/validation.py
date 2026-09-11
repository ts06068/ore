"""Document parsing service with no provider credentials or persistent corpus mount.

Compose attaches this service only to an internal validation network. Callers send
bytes, then verify returned input/output hashes themselves before storing results.
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sys
import tempfile
import httpx
from fastapi import FastAPI, HTTPException, Request
from .vault import Vault, VaultError, sha256_file, ZipLimits

MAX_INPUT = 256 * 1024 * 1024
MAX_DERIVED = 64 * 1024 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
PARSER_TIMEOUT_SECONDS = 120


def parse_operation(path, expected, operation, root):
    vault = Vault(root)
    verified = vault.commit_file(path, expected)
    if operation == 'verify':return {'source_sha256': verified['sha256'], 'verification': {k:v for k,v in verified.items() if k != 'path'}}
    if verified['status'] != 'verified':raise VaultError('Parsing requires a verified original')
    total = 0
    def encode(item):
        nonlocal total
        data = Path(item['path']).read_bytes(); total += len(data)
        if total > MAX_DERIVED:raise VaultError('Derived output exceeds isolated parser transfer limit')
        return {**{k:v for k,v in item.items() if k != 'path'}, 'content_base64': base64.b64encode(data).decode()}
    if operation == 'expand':
        members = vault.extract_zip(verified['path'], parent_sha256=verified['sha256'], limits=ZipLimits(max_total_bytes=MAX_DERIVED, max_member_bytes=MAX_DERIVED))
        return {'source_sha256': verified['sha256'], 'members': [encode(item) for item in members]}
    if operation == 'extract':
        from .forge import Forge
        result = Forge(vault).extract(verified)
        for key in ('artifact', 'table_artifact'):
            if result.get(key):result[key] = encode(result[key])
        return {'source_sha256': verified['sha256'], 'result': result}
    raise VaultError('Unknown isolated parser operation')


class ParserTimeout(VaultError):
    pass


def _parser_command(contract, output):
    return [sys.executable, '-m', 'ore.validation', '--parse-child', str(contract), str(output)]


async def _reap_parser(process):
    """Stop the parser and descendants, then reap before releasing its slot."""
    if os.name == 'posix':
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    elif process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 0.5)
    except asyncio.TimeoutError:
        if os.name == 'posix':
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
        await process.wait()
    finally:
        # A parser may have spawned an OCR process which outlived its direct parent.
        if os.name == 'posix':
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)


async def run_parser(path, expected, operation, root, *, timeout_seconds=PARSER_TIMEOUT_SECONDS):
    root = Path(root)
    directory = root.parent
    contract, output = directory / 'contract.json', directory / 'result.json'
    contract.write_text(json.dumps({'path': str(path), 'expected': expected, 'operation': operation, 'root': str(root)}))
    contract.chmod(0o600)
    # No role tokens, proxy settings, publisher credentials, or host HOME are inherited.
    environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1',
        'PYTHONPATH': str(Path(__file__).resolve().parent.parent), 'TMPDIR': str(directory)}
    process = await asyncio.create_subprocess_exec(*_parser_command(contract, output),
        cwd=directory, env=environment, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=os.name == 'posix')
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ParserTimeout('Document parser exceeded its wall-clock limit') from exc
        if process.returncode != 0 or not output.is_file() or output.is_symlink():
            raise VaultError('Isolated parser failed')
        if output.stat().st_size > MAX_RESPONSE:
            raise VaultError('Parser response exceeds transfer limit')
        result = json.loads(output.read_bytes())
        if not isinstance(result, dict) or result.get('error'):
            raise VaultError('Isolated parser rejected the document')
        return result
    finally:
        cleanup = asyncio.create_task(_reap_parser(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


def _parse_child(contract_path, output_path):
    # This entry point has no HTTP, credential-resolution or arbitrary command API.
    if os.name == 'posix':
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESPONSE, MAX_RESPONSE))
    contract = json.loads(Path(contract_path).read_bytes())
    try:
        result = parse_operation(contract['path'], contract['expected'], contract['operation'], Path(contract['root']))
        count = 0
        with Path(output_path).open('xb') as output:
            for part in json.JSONEncoder(ensure_ascii=True).iterencode(result):
                data = part.encode('utf-8')
                count += len(data)
                if count > MAX_RESPONSE:
                    raise VaultError('Parser response exceeds transfer limit')
                output.write(data)
    except Exception:
        Path(output_path).unlink(missing_ok=True)
        return 1
    return 0


def create_validator_app(token=None, *, parser_timeout_seconds=PARSER_TIMEOUT_SECONDS):
    token = token or os.environ.get('ORE_VALIDATOR_TOKEN')
    if not token:raise ValueError('A dedicated validator token is required')
    if parser_timeout_seconds <= 0:raise ValueError('Parser deadline must be positive')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.parser_timeout_seconds = parser_timeout_seconds
    slots = asyncio.Semaphore(1)

    @app.get('/healthz')
    async def health():return {'status':'ok','provider_credentials':False}

    @app.post('/v1/validate')
    async def validate(request: Request, operation: str = 'verify'):
        supplied=request.headers.get('authorization','').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied,token):raise HTTPException(401,'Invalid validator credential')
        if operation not in ('verify','extract','expand'):raise HTTPException(400,'Unsupported parser operation')
        try:
            raw_expected=request.headers.get('x-ore-expected','{}')
            if len(raw_expected)>16384:raise ValueError()
            expected=json.loads(raw_expected)
            if not isinstance(expected,dict):raise ValueError()
            expected={k:v for k,v in expected.items() if k in ('role','doi','title','version','filename','sha256','media_type','require_identity','expected_bytes')}
        except (ValueError,TypeError):raise HTTPException(400,'Invalid verification contract')
        async with slots:
            with tempfile.TemporaryDirectory(prefix='ore-validator-') as directory:
                root=Path(directory); path=root/'input'; count=0
                with path.open('xb') as stream:
                    async for chunk in request.stream():
                        count+=len(chunk)
                        if count>MAX_INPUT:raise HTTPException(413,'Document exceeds parser input limit')
                        stream.write(chunk)
                try:return await run_parser(path,expected,operation,root/'vault',timeout_seconds=app.state.parser_timeout_seconds)
                except ParserTimeout as exc:raise HTTPException(504,'Document parser deadline exceeded') from exc
                except (VaultError,ValueError,RuntimeError,OSError) as exc:raise HTTPException(422,type(exc).__name__) from exc
    return app


class IsolatedVault(Vault):
    def __init__(self,root,url,token):
        super().__init__(root)
        self.validator_url=url.rstrip('/')
        self.validator_token=token

    def _call(self,path,expected,operation):
        path=Path(path)
        if path.is_symlink() or not path.is_file():raise VaultError('Parser input must be a regular file')
        if path.stat().st_size>MAX_INPUT:raise VaultError('Document exceeds parser input limit')
        digest=sha256_file(path)
        expected={**expected,'sha256':digest,'expected_bytes':path.stat().st_size}
        # Provider credentials/cookies are never sent; the validator has its own role token.
        headers={'Authorization':'Bearer '+self.validator_token,'x-ore-expected':json.dumps(expected,ensure_ascii=True),'Content-Type':'application/octet-stream'}
        with path.open('rb') as stream, httpx.Client(timeout=180,trust_env=False,follow_redirects=False) as client:
            response=client.post(self.validator_url+'/v1/validate',params={'operation':operation},headers=headers,content=stream)
        response.raise_for_status();result=response.json()
        if result.get('source_sha256')!=digest or sha256_file(path)!=digest:raise VaultError('Parser input receipt mismatch')
        return result

    def _adopt(self,path,receipt):
        path=Path(path);digest=sha256_file(path)
        if digest!=receipt.get('sha256') or path.stat().st_size!=receipt.get('bytes'):raise VaultError('Parser output receipt mismatch')
        destination=self.blob_path(digest);destination.parent.mkdir(parents=True,exist_ok=True)
        descriptor,temporary=tempfile.mkstemp(prefix='validated-',dir=self.staging)
        try:
            with os.fdopen(descriptor,'wb') as output,path.open('rb') as incoming:shutil.copyfileobj(incoming,output)
            if sha256_file(temporary)!=digest:raise VaultError('Parser output changed during storage')
            try:os.link(temporary,destination)
            except FileExistsError:
                if destination.is_symlink() or sha256_file(destination)!=digest:raise VaultError('Stored object is corrupt')
        finally:Path(temporary).unlink(missing_ok=True)
        return {**receipt,'path':str(destination),'validator':'isolated_service'}

    def commit_file(self,path,expected=None):
        expected=dict(expected or {})
        result=self._call(path,expected,'verify')
        return self._adopt(path,result['verification'])

    def _decode(self,item):
        item=dict(item);data=base64.b64decode(item.pop('content_base64'),validate=True)
        if len(data)>MAX_DERIVED:raise VaultError('Derived output exceeds transfer limit')
        descriptor,temporary=tempfile.mkstemp(prefix='derived-',dir=self.staging)
        try:
            with os.fdopen(descriptor,'wb') as stream:stream.write(data)
            return self._adopt(temporary,item)
        finally:Path(temporary).unlink(missing_ok=True)

    def extract_zip(self,path,*,parent_sha256=None,limits=None):
        result=self._call(path,{'role':'supplement'},'expand')
        if parent_sha256 and result['source_sha256']!=parent_sha256:raise VaultError('Archive parent mismatch')
        return [self._decode(item) for item in result['members']]

    def extract_document(self,artifact):
        expected={key:artifact[key] for key in ('role','version','media_type','filename') if artifact.get(key)}
        # The original has already passed identity verification at admission.
        expected['role']='attachment'
        result=self._call(artifact['path'],expected,'extract')['result']
        for key in ('artifact','table_artifact'):
            if result.get(key):result[key]=self._decode(result[key])
        return result


def make_vault(root):
    url=os.environ.get('ORE_VALIDATOR_URL');token=os.environ.get('ORE_VALIDATOR_TOKEN')
    if url:
        if not token:raise ValueError('Configured validator requires its dedicated token')
        return IsolatedVault(root,url,token)
    return Vault(root)


if __name__=='__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--parse-child':
        raise SystemExit(_parse_child(sys.argv[2], sys.argv[3]))
    import uvicorn
    uvicorn.run(create_validator_app(),host='0.0.0.0',port=8770,access_log=False)
