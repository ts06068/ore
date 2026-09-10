"""Transport, provenance, and continuation contracts shared by source adapters."""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree as ET


class ScholarlyError(Exception):
    """Provider failure suitable for an audited job; never includes secret URLs/bodies."""

    def __init__(self, code: str, message: str, *, source: str = "", status: int | None = None,
                 retry_after: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code, self.source, self.status = code, source, status
        self.retry_after, self.details = retry_after, details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "source": self.source,
                "http_status": self.status, "retry_after": self.retry_after, "details": self.details}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def endpoint(url: str) -> str:
    parts = urlsplit(str(url))
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


def credential(config: dict, name: str, default_env: str, source: str, required: bool = True) -> str | None:
    if name in config:
        raise ScholarlyError("literal_credential", f"Use {name}_env or {name}_ref to reference a credential.", source=source)
    if name + "_ref" in config:
        ref = config[name + "_ref"]
        resolver = config.get("secret_resolver")
        if not isinstance(ref, str) or not ref or not callable(resolver):
            raise ScholarlyError("invalid_config", f"{name}_ref requires a secret_resolver callable.", source=source)
        try:
            value = resolver(ref)
        except Exception:
            raise ScholarlyError("credentials_missing", "Credential reference could not be resolved.", source=source) from None
        if value is not None and not isinstance(value, str):
            raise ScholarlyError("invalid_config", "Credential resolver must return a string or None.", source=source)
        if required and not value:
            raise ScholarlyError("credentials_missing", "Credential reference is empty.", source=source)
        return value or None
    ref = config.get(name + "_env", default_env)
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref):
        raise ScholarlyError("invalid_config", f"Invalid {name}_env reference.", source=source)
    value = os.environ.get(ref)
    if required and not value:
        raise ScholarlyError("credentials_missing", f"Set the environment variable referenced by {name}_env.",
                             source=source, details={"environment_variable": ref})
    return value or None


@asynccontextmanager
async def session(config: dict):
    existing = config.get("client")
    if existing is not None:
        yield existing
    else:
        async with httpx.AsyncClient(transport=config.get("transport"),
                                     timeout=config.get("timeout", 30), follow_redirects=False,
                                     headers={"User-Agent": "ORE-Scholarly/0.1 (+programmable-metadata-retrieval)"}) as client:
            yield client


async def get(client: httpx.AsyncClient, url: str, source: str, *, params=None, headers=None,
              kind: str = "json", allow_not_found: bool = False) -> Any:
    """No implicit redirect, retries or credential-bearing exception text. Metadata only."""
    try:
        async with client.stream("GET", url, params=params, headers=headers, follow_redirects=False) as response:
            status = response.status_code
            if status == 404 and allow_not_found:
                return None
            if status >= 300:
                code = ("rate_limited" if status == 429 else "access_required" if status in (401, 403)
                        else "redirect_requires_review" if status < 400 else "provider_http_error")
                raise ScholarlyError(code, f"{source} returned HTTP {status}.", source=source,
                                     status=status, retry_after=response.headers.get("Retry-After"))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 16 * 1024 * 1024:
                    raise ScholarlyError("response_too_large", "Metadata response exceeds 16 MiB.", source=source)
    except ScholarlyError:
        raise
    except httpx.HTTPError:
        raise ScholarlyError("transport_error", f"{source} metadata request failed.", source=source) from None
    try:
        if kind == "json":
            return json.loads(body)
        if kind == "xml":
            root = ET.fromstring(body)
            for element in root.iter():
                element.tag = element.tag.rsplit("}", 1)[-1]
            return root
        return bytes(body)
    except Exception as exc:
        if isinstance(exc, ScholarlyError):
            raise
        raise ScholarlyError("invalid_response", f"{source} returned invalid {kind} metadata.", source=source) from None


def text(node, path: str = ".", default: str = "") -> str:
    found = node.find(path) if node is not None and path != "." else node
    return "".join(found.itertext()).strip() if found is not None else default


def as_list(value) -> list:
    return value if isinstance(value, list) else [] if value is None else [value]


def integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def year(value):
    match = re.search(r"\b(1[5-9]\d{2}|20\d{2}|2100)\b", str(value or ""))
    return int(match.group()) if match else None


def doi(value) -> str | None:
    value = str(value or "").strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I)
    return value.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def record(source: str, identifier: str, *, url: str, **fields) -> dict:
    result = {"id": str(identifier), "source": source, "doi": None, "pmid": None, "pmcid": None,
              "title": "", "journal": "", "issns": [], "volume": None, "issue": None,
              "year": None, "authors": [], "article_type": None,
              "eligibility": "unclassified", "urls": [], "version": "metadata",
              "provenance": {"source": source, "record_id": str(identifier),
                             "retrieved_at": now(), "endpoint": endpoint(url)}}
    result.update(fields)
    result["url"] = fields.get("url") or next(iter(result.get("urls") or []), None)
    return result


def fingerprint(source, query, year_from, year_to, journals, limit, config) -> str:
    signature = [source, query, year_from, year_to, journals, limit,
                 {key: config.get(key) for key in ("database", "view", "query_mode", "oai_from", "oai_until")}]
    return hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]


def encode_cursor(source: str, signature: str, **state) -> str:
    value = {"v": 1, "source": source, "signature": signature, "state": state}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, source: str, signature: str) -> dict:
    if not cursor:
        return {}
    try:
        if len(cursor) > 100000:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if value["v"] != 1 or value["source"] != source or value["signature"] != signature or not isinstance(value["state"], dict):
            raise ValueError
        return value["state"]
    except (ValueError, KeyError, TypeError):
        raise ScholarlyError("invalid_cursor", "Cursor does not match this source, query, filters and page size.", source=source) from None


def search_result(source, query, records, total=None, next_cursor=None, truncated=False, **extras) -> dict:
    return {"records": records, "next_cursor": next_cursor, "total": total, "truncated": truncated,
            "has_more": next_cursor is not None, "source": source, "query": query, **extras}


def validate(query, year_from, year_to, journals, limit):
    if not isinstance(query, str) or not query.strip():
        raise ScholarlyError("invalid_query", "A nonempty search query is required.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ScholarlyError("invalid_limit", "limit must be between 1 and 1000.")
    for value in (year_from, year_to):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1500 <= value <= 2100):
            raise ScholarlyError("invalid_year", "Publication years must be integers from 1500 to 2100.")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ScholarlyError("invalid_year", "year_from exceeds year_to.")
    if journals is not None and (not isinstance(journals, (list, tuple)) or any(not isinstance(j, str) or not j.strip() for j in journals)):
        raise ScholarlyError("invalid_journals", "journals must be a list of nonempty journal names or ISSNs.")


def local_match(item: dict, query: str | None, year_from, year_to, journals) -> bool:
    if query and query.casefold() not in (item.get("title", "") + " " + item.get("abstract", "")).casefold():
        return False
    if (year_from is not None or year_to is not None) and item.get("year") is None:
        return False
    if year_from is not None and item["year"] < year_from or year_to is not None and item["year"] > year_to:
        return False
    if journals and not any(j.casefold() == item.get("journal", "").casefold() or j in item.get("issns", []) for j in journals):
        return False
    return True
