"""Exact executor capability boundaries; no network, real credentials or models."""
from types import SimpleNamespace
import pytest
from ore.execution import ExecutionManager
from ore.policy import AccessDenied


def test_executor_capability_is_scoped_to_method_path_and_worker():
    manager=object.__new__(ExecutionManager)
    manager.enrollment_token='synthetic-enrollment'
    def authenticate(worker, token):
        if worker!='worker-a' or token!='synthetic-worker-a':raise AccessDenied('Invalid capability')
    manager.authenticate=authenticate
    def allowed(path, method='POST', token='synthetic-worker-a'):
        return manager.authorize_request(SimpleNamespace(url=SimpleNamespace(path=path),method=method,headers={'authorization':'Bearer '+token}))
    assert allowed('/v1/execution/workers/worker-a/heartbeat')
    assert allowed('/v1/execution/register',token='synthetic-enrollment')
    assert not allowed('/v1/execution/register')
    assert not allowed('/v1/execution/register','GET',token='synthetic-enrollment')
    assert not allowed('/v1/execution/workers/worker-b/heartbeat')
    assert not allowed('/v1/execution/workers/worker-a/heartbeat','GET')
    assert not allowed('/v1/execution/workers/worker-a/anything')
    for path in ('/v1/jobs','/v1/secrets/secret','/v1/access-profiles','/v1/browser/sessions','/v1/execution/admin','/v1/execution/workers/worker-a/rpc/extra'):
        assert not allowed(path)
    assert not allowed('/v1/execution/workers/worker-a/heartbeat',token='synthetic-enrollment')
