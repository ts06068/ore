"""Bounded native-browser copies of an already authorized mission.

Only packaged journal context supplies implicit browser and asset dependencies. Collection
criteria, profile identity, source policy and challenge budgets remain unchanged.
"""
from copy import deepcopy
from urllib.parse import unquote, urlsplit

from .desktop_browser import desktop_origins, issue_checkpoint_identity
from .operator_access import checkpoint_origin
from .policy import AccessDenied, AccessPolicy


def journal_browser_context(url):
    if not isinstance(url, str):
        return {}
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return {}
    path = unquote(parsed.path)
    if (parsed.username or parsed.password or "\\" in path
            or {".", ".."}.intersection(path.split("/"))):
        return {}
    if (parsed.scheme == "https" and parsed.hostname == "academic.oup.com"
            and port in (None, 443)
            and (parsed.path == "/eurheartj" or parsed.path.startswith("/eurheartj/"))):
        try:
            from ore_scholarly.packs import load_rune
            pack = load_rune("ehj")
        except ImportError:
            return {}
        return {"protocol_id": pack["protocol_id"], "digest": pack["digest"],
                "support_origins": pack["scope"].get("browser_support_origins", []),
                "asset_origins": pack["scope"].get("asset_origins", []),
                "success_text": [pack["context"]["journal_title"], "Oxford Academic"]}
    return {}


def native_target_checkpoint(mission, profile, url):
    """Configure access evidence only; this never establishes TOC completeness."""
    if not url:
        return
    if issue_checkpoint_identity(url):
        mission["desktop_issue_checkpoint"] = url
    elif not profile.get("desktop_success_text") and not mission.get("desktop_success_text"):
        markers = journal_browser_context(url).get("success_text")
        if markers:
            mission["desktop_success_text"] = markers
            mission["desktop_success_text_source"] = "journal_browser_context"


def native_scope_copy(mission, profile, url=None):
    if profile.get("require_companion") or profile.get("browser_backend") == "companion":
        raise AccessDenied("This access profile requires the companion browser")
    copied, access = deepcopy(mission), deepcopy(profile)
    scope = copied.setdefault("scope", {})
    origins = copied.get("allowed_origins") or scope.get("origins") or []
    if not origins and url:
        origins = [checkpoint_origin(url)]
    if not isinstance(origins, list) or not origins:
        raise AccessDenied("Desktop browser requires an explicit finite origin allowlist")
    additions = []
    context = journal_browser_context(url)
    for values in (scope.get("asset_origins", []), scope.get("browser_support_origins", []),
                   access.get("browser_support_origins", []), context.get("support_origins", []),
                   context.get("asset_origins", [])):
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise AccessDenied("Desktop support origins must be an explicit finite list")
        additions.extend(values)
    if any(not isinstance(value, str) for value in origins) or len(origins) + len(additions) > 512:
        raise AccessDenied("Desktop origin list is invalid or too large")
    probe = "https://brunhild.challenges.cloudflare.com"
    omitted = [value for value in set(additions) if value == probe and value not in origins]
    additions = [value for value in additions if value not in omitted]
    copied["desktop_omitted_origins"] = [{"origin": value, "reason": "expected_negative_dns_probe"} for value in omitted]
    copied["allowed_origins"] = sorted(set(origins) | set(additions))
    if context:
        copied["desktop_context"] = {"protocol_id": context["protocol_id"], "digest": context["digest"],
                                     "scope": "browser_session_only"}
    access.update(browser_backend="desktop_chrome", require_desktop=True)
    native_target_checkpoint(copied, access, url)
    return copied, access


async def native_browser_scope(mission, profile, url):
    if not url:
        raise AccessDenied("Native browser selection requires a starting URL")
    # Check before deriving any origin or adding packaged frame dependencies.
    await AccessPolicy(mission, profile).check(url)
    copied, access = native_scope_copy(mission, profile, url)
    await desktop_origins(copied, access)
    await AccessPolicy(copied, access).check(url)
    return copied, access
