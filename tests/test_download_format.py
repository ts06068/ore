"""Download format expectations survive opaque staging names and redirects."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ore.tools import ToolRuntime
from ore.vault import Vault


@pytest.mark.parametrize("declared_filename,requested_url", [
    ("supplement.docx", "https://journal.example/download/opaque"),
    (None, "https://journal.example/supplement%2Edocx"),
])
def test_commit_preserves_declared_format_before_validating_redirect_body(tmp_path, declared_filename, requested_url):
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "opaque-id"
    source.write_text("<html><title>Preparing to download ...</title></html>")
    saved = []
    store = SimpleNamespace(resources=lambda _: [{"id": "article"}],
        add_artifact=lambda job, data, **kw: saved.append(data) or data)
    engine = SimpleNamespace(store=store, settings=SimpleNamespace(state_dir=tmp_path), vault=Vault(tmp_path / "vault"))
    args = {"resource_id": "article", "role": "supplement", "url": requested_url,
            "_requested_source_url": requested_url, "_redirect_chain": [requested_url, "https://journal.example/preparing"]}
    if declared_filename:
        args["filename"] = declared_filename
    result = ToolRuntime(engine, "fixture-job").commit(source, args, "https://journal.example/preparing")
    assert result["filename"] == "supplement.docx"
    assert result["original_name"] == "supplement.docx"
    assert result["status"] == "invalid"
    assert "expected_docx_received_text/html" in result["issues"]
    assert saved == [result]
    assert source.read_text().startswith("<html>")
