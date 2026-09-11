"""Parser service admission and hash-bound results, without provider credentials."""
import hashlib
import io
import json
import zipfile
from fastapi.testclient import TestClient
from ore.validation import create_validator_app, parse_operation


def test_validator_requires_role_token_and_rejects_html_main_pdf(tmp_path):
    with TestClient(create_validator_app('validation-fixture')) as client:
        assert client.post('/v1/validate',content=b'x').status_code==401
        response=client.post('/v1/validate',headers={'Authorization':'Bearer validation-fixture','x-ore-expected':json.dumps({'role':'main_pdf'})},content=b'<html>Sign in</html>')
        assert response.status_code==200
        result=response.json()
        assert result['verification']['status']=='invalid'
        assert 'path' not in result['verification']
        assert result['source_sha256']==hashlib.sha256(b'<html>Sign in</html>').hexdigest()
        assert client.post('/v1/validate?operation=shell',headers={'Authorization':'Bearer validation-fixture'},content=b'x').status_code==400


def test_isolated_parser_detects_archive_escape_and_returns_original_hash(tmp_path):
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w') as archive:archive.writestr('../escape.txt','bad')
    path=tmp_path/'archive';path.write_bytes(payload.getvalue())
    result=parse_operation(path,{'role':'supplement'},'verify',tmp_path/'vault')
    assert result['verification']['status']=='invalid'
    assert not (tmp_path/'escape.txt').exists()
    assert result['source_sha256']==hashlib.sha256(payload.getvalue()).hexdigest()


def test_derived_results_are_bytes_and_receipts_without_server_paths(tmp_path):
    path=tmp_path/'input';path.write_text('A verified excerpt.')
    result=parse_operation(path,{'role':'attachment'},'extract',tmp_path/'vault')
    artifact=result['result']['artifact']
    assert artifact['content_base64']
    assert 'path' not in artifact
    assert result['source_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()



def test_parser_timeout_kills_reaps_and_next_document_succeeds(tmp_path, monkeypatch):
    import os
    import sys
    import time
    import pytest
    import ore.validation as validation
    pidfile = tmp_path / 'hung-parser.pid'
    original = validation._parser_command
    def pathological_command(contract, output):
        data = json.loads(contract.read_text())
        if data['expected'].get('filename') != 'hang-fixture':
            return original(contract, output)
        program = "import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(pidfile)
        return [sys.executable, '-c', program]
    monkeypatch.setattr(validation, '_parser_command', pathological_command)
    app = create_validator_app('fixture-validator', parser_timeout_seconds=0.5)
    headers = {'Authorization': 'Bearer fixture-validator', 'x-ore-expected': json.dumps({'filename': 'hang-fixture'})}
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.post('/v1/validate', headers=headers, content=b'hang')
        assert response.status_code == 504 and time.monotonic() - started < 5
        pid = int(pidfile.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        app.state.parser_timeout_seconds = 10
        response = client.post('/v1/validate', headers={'Authorization': 'Bearer fixture-validator'}, content=b'Next real document')
        assert response.status_code == 200
        assert response.json()['source_sha256'] == hashlib.sha256(b'Next real document').hexdigest()


def test_parser_child_has_no_provider_or_role_credentials(tmp_path, monkeypatch):
    import sys
    import ore.validation as validation
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-provider-secret')
    monkeypatch.setenv('ORE_VALIDATOR_TOKEN', 'synthetic-role-secret')
    original = validation._parser_command
    def checked_command(contract, output):
        actual = original(contract, output)
        program = "import os; assert 'OPENAI_API_KEY' not in os.environ; assert 'ORE_VALIDATOR_TOKEN' not in os.environ; os.execv(%r,%r)" % (sys.executable, actual)
        return [sys.executable, '-c', program]
    monkeypatch.setattr(validation, '_parser_command', checked_command)
    with TestClient(create_validator_app('fixture-validator')) as client:
        response = client.post('/v1/validate', headers={'Authorization': 'Bearer fixture-validator'}, content=b'Credentialless child')
        assert response.status_code == 200
