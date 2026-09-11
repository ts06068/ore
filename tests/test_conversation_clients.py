"""CLI and MCP contract checks against HTTP transport fixtures, without a model."""
import json

import httpx
from typer.testing import CliRunner

from ore import cli
from ore.mcp import MCPAdapter, TOOLS


def fixture_cli(monkeypatch, responder=None):
    calls = []
    original = httpx.Client
    state = {'id': 'chat/a', 'title': 'Reports', 'status': 'draft', 'messages': [], 'plans': [], 'runs': [],
             'events': [], 'active_plan_id': None}

    def transport(request):
        calls.append(request)
        if responder:
            return responder(request)
        return httpx.Response(200, json=state)

    monkeypatch.setenv('ORE_AUTH_TOKEN', 'fixture-operator-token')
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    return calls, state


def test_cli_conversation_list_and_status_are_read_only(monkeypatch):
    calls, state = fixture_cli(monkeypatch)
    runner = CliRunner()
    listing = runner.invoke(cli.app, ['chat', '--server', 'https://ore.test', 'list'])
    assert listing.exit_code == 0, listing.output
    status = runner.invoke(cli.app, ['chat', '--server', 'https://ore.test', 'status', 'chat/a'])
    assert status.exit_code == 0, status.output
    assert [(request.method, request.url.raw_path) for request in calls] == [
        ('GET', b'/v1/conversations'), ('GET', b'/v1/conversations/chat%2Fa')]
    assert calls[0].headers['authorization'] == 'Bearer fixture-operator-token'
    assert 'fixture-operator-token' not in status.output


def test_cli_execute_message_does_not_approve_and_controls_are_explicit(monkeypatch):
    calls, state = fixture_cli(monkeypatch)
    runner = CliRunner()
    sent = runner.invoke(cli.app, ['chat', '--server', 'https://ore.test', '--mode', 'execute',
                                  'send', 'chat/a', 'Collect the report'])
    assert sent.exit_code == 0, sent.output
    assert json.loads(calls[0].content) == {'content': 'Collect the report', 'mode': 'execute',
                                         'change_and_continue': False}
    assert calls[0].url.raw_path == b'/v1/conversations/chat%2Fa/messages'
    for command, extra, suffix in [('approve', ['plan/b'], '/plans/plan%2Fb/approve'),
                                    ('stop', [], '/interrupt'), ('resume', [], '/resume')]:
        result = runner.invoke(cli.app, ['chat', '--server', 'https://ore.test', command, 'chat/a', *extra])
        assert result.exit_code == 0, result.output
        assert calls[-1].url.raw_path == ('/v1/conversations/chat%2Fa' + suffix).encode()
        assert calls[-1].method == 'POST'


def test_cli_interactive_review_stop_resume_and_followup(monkeypatch):
    calls, state = fixture_cli(monkeypatch)
    state.update(active_plan_id='plan-1', plans=[{'id': 'plan-1', 'revision': 1, 'goal': 'Collect reports',
                                               'status': 'proposed', 'outputs': ['Report text']}])
    answers = iter(['/approve', '/stop', '/resume', 'What remains?', '/exit'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    result = CliRunner().invoke(cli.app, ['chat', '--server', 'https://ore.test', '--conversation', 'chat/a'])
    assert result.exit_code == 0, result.output
    assert 'Plan for review: plan-1' in result.output
    assert 'Report text' in result.output
    paths = [request.url.raw_path for request in calls if request.method == 'POST']
    assert paths == [b'/v1/conversations/chat%2Fa/plans/plan-1/approve',
                     b'/v1/conversations/chat%2Fa/interrupt', b'/v1/conversations/chat%2Fa/resume',
                     b'/v1/conversations/chat%2Fa/messages']


def test_cli_ctrl_c_requests_stop_but_does_not_claim_confirmed_pause(monkeypatch):
    calls, state = fixture_cli(monkeypatch)
    state['status'] = 'interrupting'
    def interrupted(prompt):
        raise KeyboardInterrupt()
    monkeypatch.setattr('builtins.input', interrupted)
    result = CliRunner().invoke(cli.app, ['chat', '--server', 'https://ore.test', '--conversation', 'chat/a'])
    assert result.exit_code == 0, result.output
    assert calls[-1].url.raw_path == b'/v1/conversations/chat%2Fa/interrupt'
    assert 'State: interrupting' in result.output
    assert 'State: paused' not in result.output


def test_cli_http_errors_do_not_echo_credentials_or_provider_body(monkeypatch):
    fixture_cli(monkeypatch, lambda request: httpx.Response(403, text='provider-private-token'))
    result = CliRunner().invoke(cli.app, ['chat', '--server', 'https://ore.test', 'status', 'chat/a'])
    assert result.exit_code != 0
    assert '403' in result.output
    assert 'provider-private-token' not in result.output
    assert 'fixture-operator-token' not in result.output


async def test_mcp_conversation_tools_use_same_api_and_preserve_plan_gate():
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'chat/a', 'href': '/chat/chat%2Fa', 'status': 'planning'})
    async with httpx.AsyncClient(base_url='https://ore.test', transport=httpx.MockTransport(transport)) as client:
        adapter = MCPAdapter('https://ore.test', 'fixture', client=client)
        await adapter.call('ore_conversation_create', {'mode': 'plan', 'model_policy': 'auto'})
        response = await adapter.call('ore_conversation_message', {
            'conversation_id': 'chat/a', 'content': 'Collect the report', 'mode': 'execute'})
        assert response['href'] == 'https://ore.test/chat/chat%2Fa'
        assert all(not request.url.path.endswith('/approve') for request in calls)
        await adapter.call('ore_conversation_approve', {'conversation_id': 'chat/a', 'plan_id': 'plan/b'})
        await adapter.call('ore_conversation_interrupt', {'conversation_id': 'chat/a'})
        await adapter.call('ore_conversation_resume', {'conversation_id': 'chat/a'})
        assert [request.url.raw_path for request in calls] == [
            b'/v1/conversations', b'/v1/conversations/chat%2Fa/messages',
            b'/v1/conversations/chat%2Fa/plans/plan%2Fb/approve',
            b'/v1/conversations/chat%2Fa/interrupt', b'/v1/conversations/chat%2Fa/resume']
        assert json.loads(calls[1].content) == {'content': 'Collect the report', 'mode': 'execute'}


async def test_mcp_conversation_rejects_credential_arguments_without_http():
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(base_url='https://ore.test', transport=httpx.MockTransport(transport)) as client:
        adapter = MCPAdapter('https://ore.test', 'fixture', client=client)
        response = await adapter.handle({'id': 7, 'method': 'tools/call', 'params': {
            'name': 'ore_conversation_message', 'arguments': {
                'conversation_id': 'chat', 'content': 'Find reports', 'token': 'private-value'}}})
        assert response['result']['isError']
        assert not calls
        assert 'private-value' not in json.dumps(response)
    names = {tool['name'] for tool in TOOLS}
    assert {'ore_status', 'ore_list_jobs', 'ore_conversation_list', 'ore_conversation_get'} <= names


def test_cli_reopen_preserves_runtime_settings_without_forwarding_create_only_fields(monkeypatch):
    calls, state = fixture_cli(monkeypatch)
    state['settings'] = {'mode': 'execute', 'model_policy': 'fixed', 'model': 'configured-model',
                         'reasoning_effort': 'high', 'constraints': {'allowed_origins': ['https://example.org']},
                         'backend': {'provider': 'codex'}, 'budget': {'turns': 30}}
    answers = iter(['What remains?', '/exit'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    result = CliRunner().invoke(cli.app, ['chat', '--server', 'https://ore.test', '--conversation', 'chat/a'])
    assert result.exit_code == 0, result.output
    body = json.loads(next(request.content for request in calls if request.method == 'POST'))
    assert body == {'content': 'What remains?', 'mode': 'execute', 'model_policy': 'fixed',
                    'model': 'configured-model', 'reasoning_effort': 'high'}
