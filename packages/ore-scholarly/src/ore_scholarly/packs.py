"""Versioned, installable Rune contexts; no hard-coded selectors claimed live-tested."""
from importlib.resources import files
import hashlib
import json
from .common import ScholarlyError


def list_runes() -> list[dict]:
    result = []
    for path in sorted(files("ore_scholarly").joinpath("runes").iterdir(), key=lambda p: p.name):
        if path.name.endswith(".json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append({"id": data["protocol_id"], "name": path.name[:-5],
                           "version": data["protocol_version"], "title": data["context"].get("journal_title", "General web content"),
                           "live_verified": False})
    return result


def load_rune(name: str) -> dict:
    if not isinstance(name, str) or "/" in name or "\\" in name or name.startswith("."):
        raise ScholarlyError("invalid_rune", "Use a packaged Rune name from list_runes().")
    name = name.removeprefix("journal.").removesuffix(".json")
    aliases = {"plos_medicine": "plos-medicine", "jama_cardiology": "jama-cardiology", "general.web": "general-web", "general_web": "general-web", "kiss.search": "kiss", "riss.search": "riss"}
    name = aliases.get(name, name)
    path = files("ore_scholarly").joinpath("runes", name + ".json")
    if not path.is_file():
        raise ScholarlyError("unknown_rune", f"No packaged Rune named {name}.")
    raw = path.read_bytes()
    data = json.loads(raw)
    data["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    return data
