"""Discover authorized full-text candidates; never download PDF or attachment bytes."""
from __future__ import annotations

import mimetypes
import hashlib
import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

from .common import ScholarlyError, credential, doi, endpoint, get, integer, now, text

UNPAYWALL = "https://api.unpaywall.org/v2/"
PMC_BUCKET = "pmc-oa-opendata"
PMC = f"https://{PMC_BUCKET}.s3.amazonaws.com"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
XLINK = "{http://www.w3.org/1999/xlink}href"


def candidate(source, identifier, url, role, *, version="unknown", license=None, evidence_url=None, **extra):
    return {"id": f"{source}:{identifier}:{role}:" + hashlib.sha256(url.encode()).hexdigest()[:16],
            "source": source, "identifier": identifier, "title": None, "url": url, "role": role,
            "version": version, "license": license,
            "media_type": mimetypes.guess_type(urlsplit(url).path)[0], "downloaded": False,
            "provenance": {"source": source, "record_id": identifier, "retrieved_at": now(),
                           "endpoint": endpoint(evidence_url or url)}, **extra}


async def unpaywall(client, identifier, config):
    value = doi(identifier)
    if not value:
        raise ScholarlyError("invalid_identifier", "Unpaywall requires a DOI.", source="unpaywall")
    email = credential(config, "email", "UNPAYWALL_EMAIL", "unpaywall")
    url = UNPAYWALL + quote(value, safe="/")
    data = await get(client, url, "unpaywall", params={"email": email}, allow_not_found=True)
    if data is None:
        return {"source": "unpaywall", "status": "not_found", "identifier": value, "candidates": [], "supplement_status": "not_supported"}
    if not isinstance(data, dict) or "oa_locations" not in data:
        raise ScholarlyError("invalid_response", "Unpaywall response lacks OA locations.", source="unpaywall")
    if doi(data.get("doi")) != value:
        raise ScholarlyError("identity_mismatch", "Unpaywall returned a different DOI.", source="unpaywall")
    candidates = []
    seen = set()
    for location in data.get("oa_locations") or []:
        for field, role in (("url_for_pdf", "main_pdf"), ("url_for_landing_page", "landing_page")):
            target = location.get(field)
            if not target or (target, location.get("version"), role) in seen:
                continue
            if urlsplit(target).scheme not in ("http", "https"):
                continue
            seen.add((target, location.get("version"), role))
            candidates.append(candidate("unpaywall", value, target, role,
                version=location.get("version") or "unknown", license=location.get("license"), evidence_url=url, doi=value, title=data.get("title"),
                host_type=location.get("host_type"), repository_institution=location.get("repository_institution"),
                is_best=location.get("is_best", False), provider_updated=location.get("updated"),
                version_evidence="unpaywall.oa_locations.version"))
    return {"source": "unpaywall", "identifier": value,
            "status": "resolved" if any(c["role"] == "main_pdf" for c in candidates) else "partial" if candidates else "no_open_location",
            "candidates": candidates, "is_oa": data.get("is_oa"), "oa_status": data.get("oa_status"),
            "supplement_status": "not_supported", "provenance": {"retrieved_at": now(), "endpoint": endpoint(url)}}


def pmc_object_url(value: str, prefix: str) -> tuple[str, str | None]:
    """Bind every dataset object to the selected article version and trusted bucket."""
    parts = urlsplit(value)
    if parts.scheme == "s3" and parts.netloc == PMC_BUCKET:
        parts = parts._replace(scheme="https", netloc=f"{PMC_BUCKET}.s3.amazonaws.com")
    if parts.scheme != "https" or parts.netloc != f"{PMC_BUCKET}.s3.amazonaws.com":
        raise ScholarlyError("untrusted_artifact_origin", "PMC metadata references an unexpected object origin.", source="pmc")
    path = unquote(parts.path)
    if not path.startswith("/" + prefix + "/") or any(p in (".", "..") for p in path.split("/")):
        raise ScholarlyError("identity_mismatch", "PMC object does not belong to the selected article version.", source="pmc")
    digest = parse_qs(parts.query).get("md5", [None])[0]
    if digest is not None and not re.fullmatch(r"[0-9a-fA-F]{32}", digest):
        raise ScholarlyError("invalid_response", "PMC object has an invalid MD5 value.", source="pmc")
    return urlunsplit(parts), digest


async def pmc_identifier(client, identifier, config):
    value = str(identifier).strip()
    if re.fullmatch(r"PMC\d+(?:\.\d+)?", value, re.I):
        return value.upper()
    if not (doi(value) or re.fullmatch(r"\d+", value)):
        raise ScholarlyError("invalid_identifier", "PMC requires a PMCID, PMID or DOI.", source="pmc")
    params = {"ids": doi(value) or value, "format": "json", "tool": "ore-scholarly"}
    email = credential(config, "email", "NCBI_EMAIL", "pmc", required=False)
    if email:
        params["email"] = email
    data = await get(client, IDCONV, "pmc", params=params)
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ScholarlyError("invalid_response", "PMC ID converter response lacks records.", source="pmc")
    ids = {item["pmcid"].upper() for item in data["records"] if item.get("pmcid")}
    if not ids:
        return None
    if len(ids) != 1:
        raise ScholarlyError("ambiguous_identifier", "Identifier maps to multiple PMC articles.", source="pmc")
    return ids.pop()


async def pmc_versions(client, pmcid, config):
    versions, token, seen = set(), None, set()
    max_pages = integer(config.get("max_version_pages"), 10)
    if not 1 <= max_pages <= 100:
        raise ScholarlyError("invalid_config", "max_version_pages must be between 1 and 100.", source="pmc")
    for _ in range(max_pages):
        params = {"list-type": 2, "prefix": pmcid + ".", "delimiter": "/"}
        if token:
            params["continuation-token"] = token
        root = await get(client, PMC + "/", "pmc", params=params, kind="xml")
        if root.tag != "ListBucketResult":
            raise ScholarlyError("invalid_response", "PMC bucket response is not a version listing.", source="pmc")
        for element in root.findall("CommonPrefixes/Prefix"):
            match = re.fullmatch(re.escape(pmcid) + r"\.(\d+)/?", text(element))
            if match:
                versions.add(int(match.group(1)))
        if text(root, "IsTruncated").lower() != "true":
            return sorted(versions)
        token = text(root, "NextContinuationToken")
        if not token or token in seen:
            raise ScholarlyError("pagination_stalled", "PMC version listing did not advance.", source="pmc")
        seen.add(token)
    raise ScholarlyError("result_cap", "PMC version listing exceeded its configured page budget.", source="pmc")


def supplement_links(root):
    """JATS relationships, not filename guesses or all media, determine role."""
    result = []
    for element in root.iter():
        if element.tag not in ("supplementary-material", "inline-supplementary-material"):
            continue
        refs = []
        for child in element.iter():
            href = child.get(XLINK) or child.get("href")
            if href and child.tag in ("supplementary-material", "inline-supplementary-material", "media", "ext-link"):
                refs.append(href)
        result.append({"id": element.get("id"), "label": text(element, "label"),
                       "caption": text(element, "caption"), "hrefs": list(dict.fromkeys(refs))})
    return result


def choose_media(href, media):
    path = unquote(urlsplit(href).path)
    name = PurePosixPath(path).name
    exact = [m for m in media if unquote(urlsplit(m[0]).path).split("/")[-1] == name]
    if not exact:
        exact = [m for m in media if PurePosixPath(unquote(urlsplit(m[0]).path)).stem == name]
    return exact[0] if len(exact) == 1 else None


async def pmc(client, identifier, config):
    value = await pmc_identifier(client, identifier, config)
    if value is None:
        return {"source": "pmc", "identifier": identifier, "status": "not_found", "candidates": []}
    base, _, explicit = value.partition(".")
    mode = config.get("version_selection", "latest")
    if mode not in ("latest", "all"):
        raise ScholarlyError("invalid_config", "version_selection must be latest or all.", source="pmc")
    versions = [int(explicit)] if explicit else await pmc_versions(client, base, config)
    selected = versions if explicit or mode == "all" else versions[-1:]
    candidates, observations, issues = [], [], []
    for version_number in selected:
        prefix = f"{base}.{version_number}"
        metadata_url = f"{PMC}/{prefix}/{prefix}.json"
        metadata = await get(client, metadata_url, "pmc", allow_not_found=True)
        if metadata is None:
            issues.append({"article_version": prefix, "code": "version_metadata_missing"})
            continue
        if not isinstance(metadata, dict) or str(metadata.get("pmcid", "")).upper() != base or integer(metadata.get("version")) != version_number:
            raise ScholarlyError("identity_mismatch", "PMC metadata does not match the selected article version.", source="pmc")
        if doi(identifier) and doi(metadata.get("doi")) != doi(identifier):
            raise ScholarlyError("identity_mismatch", "PMC article version has a different DOI.", source="pmc")
        version_type = "author_manuscript" if metadata.get("is_manuscript") else "unknown"
        common = {"version": version_type, "article_version": version_number, "pmcid": base,
                  "doi": doi(metadata.get("doi")), "title": metadata.get("title"), "license": metadata.get("license_code"),
                  "evidence_url": metadata_url, "is_retracted": metadata.get("is_retracted"),
                  "is_pmc_openaccess": metadata.get("is_pmc_openaccess"),
                  "is_historical_ocr": metadata.get("is_historical_ocr"),
                  "version_evidence": "pmc_dataset.is_manuscript; a false value does not prove publishedVersion"}
        xml_url = None
        media = [pmc_object_url(url, prefix) for url in metadata.get("media_urls") or []]
        for field, role in (("pdf_url", "main_pdf"), ("xml_url", "full_text_xml"), ("text_url", "full_text_text")):
            if metadata.get(field):
                target, digest = pmc_object_url(metadata[field], prefix)
                candidates.append(candidate("pmc", prefix, target, role, expected_md5=digest, **common))
                if field == "xml_url":
                    xml_url = target
        unmatched = {url for url, _ in media}
        observation = {"article_version": prefix, "main_pdf_status": "candidate_found" if metadata.get("pdf_url") else "not_available_in_dataset",
                       "supplement_status": "uninspected", "supplement_references": []}
        if xml_url and config.get("inspect_jats", True):
            try:
                root = await get(client, xml_url, "pmc", kind="xml")
                references = supplement_links(root)
                observation["supplement_references"] = references
                observation["supplement_status"] = "references_found" if references else "none_observed_in_jats"
                for reference in references:
                    if not reference["hrefs"]:
                        issues.append({"article_version": prefix, "code": "supplement_without_link", "id": reference["id"]})
                    for href in reference["hrefs"]:
                        found = choose_media(href, media)
                        if found:
                            target, digest = found
                            unmatched.discard(target)
                            if not any(c["url"] == target and c["role"] == "supplement" for c in candidates):
                                candidates.append(candidate("pmc", prefix, target, "supplement", expected_md5=digest,
                                    relationship_evidence={"tag": "supplementary-material", "href": href, "id": reference["id"]}, **common))
                        elif urlsplit(href).scheme in ("http", "https"):
                            candidates.append(candidate("pmc", prefix, href, "supplement", media_type=None,
                                external_reference=True, relationship_evidence={"tag": "supplementary-material", "href": href}, **common))
                            issues.append({"article_version": prefix, "code": "external_supplement_requires_inspection"})
                        else:
                            issues.append({"article_version": prefix, "code": "supplement_object_unresolved", "href": href})
            except ScholarlyError as exc:
                if exc.code in ("identity_mismatch", "untrusted_artifact_origin"):
                    raise
                observation["supplement_status"] = "inspection_failed"
                issues.append({"article_version": prefix, "code": exc.code, "phase": "jats_inspection"})
        for target, digest in media:
            if target in unmatched:
                candidates.append(candidate("pmc", prefix, target, "media", expected_md5=digest,
                    relationship_evidence="dataset.media_urls; supplement role unproven", **common))
        observations.append(observation)
    has_pdf = any(c["role"] == "main_pdf" for c in candidates)
    return {"source": "pmc", "identifier": identifier, "pmcid": base, "candidates": candidates,
            "status": "resolved" if has_pdf and not issues else "partial" if candidates else "not_in_dataset",
            "available_versions": versions, "selected_versions": selected, "version_selection": "explicit" if explicit else mode,
            "observations": observations, "issues": issues,
            "provenance": {"dataset": "pmc-oa-opendata", "retrieved_at": now(), "endpoint": PMC}}


RESOLVERS = {"unpaywall": unpaywall, "pmc": pmc}
