"""Do not confuse stopped local clients with stopped external execution."""
import asyncio

import pytest

from ore.codex import BackendError, CodexBackend
from ore import sandbox
from ore.capabilities import ToolSpec, object_schema


async def test_codex_interrupt_needs_completed_event_and_preserves_receipt():
    backend = CodexBackend(binary='fixture-codex')
    backend.turn_ids['thread'] = 'turn'
    requests = []
    async def request(method, params, **kwargs):
        requests.append(method)
        backend.turn_completions[('thread', 'turn')].set()
        return {}
    backend.request = request
    first = await backend.interrupt('thread')
    assert first['requested'] is True and first['acknowledged'] is True
    second = await backend.interrupt('thread')
    assert second['requested'] is False and second['acknowledged'] is True
    assert requests == ['turn/interrupt']


async def test_codex_transport_failure_is_unconfirmed_not_success():
    backend = CodexBackend(binary='fixture-codex')
    backend.turn_ids['thread'] = 'turn'
    async def request(*args, **kwargs):
        raise BackendError('fixture transport disconnected')
    backend.request = request
    assert (await backend.interrupt('thread'))['acknowledged'] is False
    assert (await backend.interrupt('unknown'))['acknowledged'] is None


@pytest.mark.parametrize('docker_state,expected', [(b'ore-code-abc running\n', False), (b'', True), (b'ore-code-abc exited\n', True)])
async def test_failed_remove_requires_independent_container_state(monkeypatch, docker_state, expected):
    monkeypatch.setattr(sandbox.shutil, 'which', lambda _: '/fixture/docker')
    async def command(*args, **kwargs):
        if 'rm' in args:
            raise RuntimeError('remove connection lost')
        return docker_state
    monkeypatch.setattr(sandbox, '_command', command)
    assert await sandbox.ensure_container_stopped('ore-code-abc') is expected


async def test_unreachable_docker_cannot_confirm_termination(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, 'which', lambda _: '/fixture/docker')
    async def command(*args, **kwargs):
        raise RuntimeError('daemon offline')
    monkeypatch.setattr(sandbox, '_command', command)
    assert await sandbox.ensure_container_stopped('ore-code-abc') is False
