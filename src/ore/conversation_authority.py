"""Host-created authority for ordinary collection requests, never model self-approval."""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from .models import Budget, canonical_digest
from .source_policy import HOSTS, normalize_source_policy


# Collection may navigate and interact with its approved source browser, including
# reserved verification attempts; browser policy and ownership checks still apply.
# Provider forms, account/connection changes, and purchases require separate authority.
COLLECTION_TOOLS = {"download", "resource", "inventory", "artifact_commit", "extract", "archive_expand",
                    "page_extract", "seal_issue", "seal_article", "content.write", "code.register", "code.run",
                    "browser_action", "challenge", "handoff", "scholarly.archive_inventory"}
CONSEQUENTIAL = re.compile(r"\b(sign\s*up|register\s+(?:an?\s+)?account|create\s+(?:an?\s+)?account|purchase|pay\s+for|change\s+(?:my\s+)?password)\b|회원가입|계정\s*생성|결제|비밀번호\s*변경", re.I)


def request_envelope(record, request, message_id, catalog, profile):
    settings = record["settings"]
    if settings.get("execution_policy", "explicit") != "auto_within_scope" or settings.get("mode") != "execute":
        return None
    # User message history is the trusted task specification. Assistant summaries,
    # retrieved documents and provider proposals cannot add authority to it.
    messages = [{"id": row["id"], "content": row["content"]} for row in record["messages"] if row["role"] == "user" and not row.get("connection_setup")]
    goal = "\n\n".join(row["content"] for row in messages)
    constraints = copy.deepcopy(settings.get("constraints", {}))
    urls = []
    for value in re.findall(r"https?://[^\s<>\"']+", goal):
        value = value.rstrip(".,;:!?)})")
        part = urlsplit(value)
        if part.hostname and not part.username and not part.password and value not in urls:
            urls.append(value)
    sources = []
    excluded = []
    for source in sorted(set(HOSTS.values()), key=lambda item: goal.lower().find(item)):
        label = re.escape(source.replace("_", " "))
        if re.search(r"(?<!\w)" + label + r"(?!\w)", goal, re.I):
            if re.search(r"(?:without|exclude|do not use|don't use)\s+" + label + r"\b", goal, re.I):
                excluded.append(source)
            else:
                sources.append(source)
    if "sources" not in constraints and sources:
        constraints["sources"] = sources
    if "urls" not in constraints and urls:
        constraints["urls"] = urls
    if "allowed_origins" not in constraints and urls:
        origins = {f"{urlsplit(value).scheme}://{urlsplit(value).netloc}" for value in urls}
        origins.update(profile.get("origins", []))
        constraints["allowed_origins"] = sorted(origins)
    if "retrieval_policy" not in constraints and re.search(r"(?:website|웹사이트).*?(?:first|fallback|우선)|official.*first", goal, re.I):
        constraints["retrieval_policy"] = {"mode": "official_first", "browser_fallback": True}
    policy_input = {**constraints, "sources": constraints.get("sources", [])}
    policy = normalize_source_policy(policy_input, profile)
    # With no named search database, normal configured public search remains usable.
    if "sources" not in constraints and "source_policy" not in constraints:
        policy["allow"]["search"] = list((profile.get("source_policy") or {}).get("allow", {}).get("search", ["*"]))
    for operation in policy["allow"]:
        policy["exclude"][operation] = sorted(set(policy["exclude"].get(operation, [])) | set(excluded))
    permissions = [entry["name"] for entry in catalog if entry.get("read_only") or entry["name"] in COLLECTION_TOOLS]
    selected = constraints.get("allowed_tools", constraints.get("capabilities"))
    if selected is not None:
        permissions = [name for name in permissions if name in selected]
    value = {"schema_version": "ore.request-envelope/v1", "conversation_id": record["id"],
             "operator_message_id": message_id, "policy": "auto_within_scope", "mode": "execute",
             "goal": goal, "messages": messages, "constraints": constraints,
             "budget": Budget.model_validate(settings.get("budget") or {}).model_dump(),
             "backend": copy.deepcopy(settings.get("backend") or {"kind": "codex"}),
             "access_profile": profile.get("id", constraints.get("access_profile_ref", constraints.get("access_profile", "public"))),
             "access_profile_digest": canonical_digest(profile), "source_policy": policy,
             "allowed_tools": sorted(permissions), "source_order": sources,
             "requires_connection_authority": bool(CONSEQUENTIAL.search(request["content"]))}
    value["digest"] = canonical_digest(value)
    return value


def bind_request_envelope(proposed, envelope, profile):
    """Return host policy issues. Bind successful plans to the actual operator text."""
    issues = []
    mission = proposed["mission"]
    if envelope["requires_connection_authority"]:
        issues.append("connection_action_requires_authority")
    if canonical_digest(profile) != envelope["access_profile_digest"]:
        issues.append("access_profile_changed")
    requested_profile = mission.get("access_profile_ref", mission.get("access_profile", envelope["access_profile"]))
    if requested_profile != envelope["access_profile"]:
        issues.append("access_profile_expansion")
    if "backend" in mission and mission["backend"] != envelope["backend"]:
        issues.append("backend_requires_authority")
    budget = Budget.model_validate(proposed["budget"]).model_dump()
    for key, maximum in envelope["budget"].items():
        if maximum is not None and (budget.get(key) is None or budget[key] > maximum):
            issues.append("budget_expansion:" + key)
    allowed = set(envelope["allowed_tools"])
    origins = set(envelope["constraints"].get("allowed_origins", []))
    def visit(nodes):
        for node in nodes:
            used = [node["tool"]] if node["kind"] == "tool" else [step.get("tool", step.get("capability")) for step in node.get("steps", [])]
            if any(name not in allowed for name in used):
                issues.append("capability_requires_authority")
            url = node.get("inputs", {}).get("url")
            if origins and isinstance(url, str) and url.startswith(("http://", "https://")):
                if f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" not in origins:
                    issues.append("origin_expansion")
            for branch in ("body", "then", "else"):
                visit(node.get(branch, []))
    visit(proposed["workflow"]["nodes"])
    if issues:
        return sorted(set(issues))
    proposed["goal"] = mission["goal"] = envelope["goal"]
    mission["access_profile"] = envelope["access_profile"]
    mission["backend"] = copy.deepcopy(envelope["backend"])
    mission.pop("access_profile_ref", None)
    for key, value in envelope["constraints"].items():
        if key in ("source_policy", "allowed_tools", "capabilities"):
            continue
        mission[key] = copy.deepcopy(value)
        proposed["constraints"][key] = copy.deepcopy(value)
    # A generated scope cannot grant extra asset/support hosts or private access.
    scope = mission.setdefault("scope", {})
    trusted_scope = envelope["constraints"].get("scope", {})
    for key in ("origins", "asset_origins", "browser_support_origins", "allow_private_network", "allowed_hosts"):
        if key in trusted_scope:
            scope[key] = copy.deepcopy(trusted_scope[key])
        else:
            scope.pop(key, None)
    if "scope" in proposed["constraints"]:
        proposed["constraints"]["scope"] = copy.deepcopy(scope)
    # Preserve narrower proposed policies, but never admit something the trusted
    # envelope excludes. Publisher/PMC routes retain configured profile admission.
    selected = normalize_source_policy(mission, profile)
    for operation, permitted in envelope["source_policy"]["allow"].items():
        current = selected["allow"][operation]
        selected["allow"][operation] = permitted if "*" in current else current if "*" in permitted else sorted(set(current) & set(permitted))
        selected["exclude"][operation] = sorted(set(selected["exclude"].get(operation, [])) | set(envelope["source_policy"]["exclude"].get(operation, [])))
    mission["source_policy"] = selected
    proposed["constraints"]["source_policy"] = copy.deepcopy(selected)
    proposed["constraints"]["allowed_tools"] = sorted(allowed)
    proposed["request_envelope_digest"] = envelope["digest"]
    return []
