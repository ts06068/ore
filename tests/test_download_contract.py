"""Invalid sealed ID pairs fail before runtime state, transport, or file writes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ore.contracts import validate_action
from ore.policy import AccessDenied
from ore.tools import ToolRuntime


def arguments(name):
    base = {"resource_id": "fixture-resource", "role": "main_pdf"}
    return {**base, **({"url": "https://journal.example/main.pdf"} if name == "download" else
                      {"session_id": "fixture-session", "download_index": 0})}


@pytest.mark.parametrize("name", ["download", "artifact_commit"])
@pytest.mark.parametrize("ids", [
    {"candidate_id": "unpaywall:10.1234/example:main_pdf:fixture"},
    {"requirement_id": "sealed-requirement"},
    {"candidate_id": "sealed-candidate", "requirement_id": ""},
    {"candidate_id": " ", "requirement_id": "sealed-requirement"},
])
@pytest.mark.asyncio
async def test_unpaired_or_empty_ids_fail_before_runtime_io(name, ids):
    engine = Mock()
    runtime = ToolRuntime(engine, "fixture-job")
    runtime._execute = AsyncMock()
    with pytest.raises(ValueError, match="IDs returned by resolve are not sealed IDs"):
        await runtime.execute(name, {**arguments(name), **ids})
    assert engine.mock_calls == []
    runtime._execute.assert_not_awaited()


@pytest.mark.parametrize("name", ["download", "artifact_commit"])
def test_contract_keeps_both_omitted_or_complete_sealed_pair(name):
    plain = validate_action(name, arguments(name))
    assert "candidate_id" not in plain and "requirement_id" not in plain
    sealed = validate_action(name, {**arguments(name), "candidate_id": "sealed-candidate", "requirement_id": "sealed-requirement"})
    assert sealed["candidate_id"] == "sealed-candidate"
    assert sealed["requirement_id"] == "sealed-requirement"


@pytest.mark.parametrize("mode", ["bounded", "systematic", "inventory"])
@pytest.mark.asyncio
async def test_url_only_bounded_path_does_not_weaken_systematic_gate(monkeypatch, mode):
    # Source authorization is independently covered; this isolates ID admission.
    monkeypatch.setattr("ore.tools.require_operation", lambda *a, **kw: None)
    engine = SimpleNamespace(store=SimpleNamespace(get_job=Mock(return_value={
        "id": "fixture-job", "status": "running", "mission": {"completeness": mode}})),
        event=Mock(), profile=Mock(return_value={}), execution=SimpleNamespace(enabled=False))
    runtime = ToolRuntime(engine, "fixture-job")
    runtime._execute = AsyncMock(return_value={"fixture": True})
    if mode == "bounded":
        assert await runtime.execute("download", arguments("download")) == {"fixture": True}
        runtime._execute.assert_awaited_once()
    else:
        with pytest.raises(AccessDenied, match="sealed candidate_id and requirement_id are required"):
            await runtime.execute("download", arguments("download"))
        runtime._execute.assert_not_awaited()
