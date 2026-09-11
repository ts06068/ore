"""Honest operation registry: implementation, configuration and live evidence differ."""
from __future__ import annotations

import copy
import os
from .common import ScholarlyError

SOURCES = [
    {"id": "pubmed", "name": "PubMed", "operations": ["search"], "route": "eutils_metadata", "credentials": {},
     "optional_credentials": {"api_key": "NCBI_API_KEY", "email": "NCBI_EMAIL"}, "max_page_size": 1000,
     "result_cap": 10000, "docs": "https://www.ncbi.nlm.nih.gov/books/NBK25499/",
     "limitations": ["ESearch result cap requires query partitions", "Journal Article is not an original-research classifier", "No PDF delivery"]},
    {"id": "crossref", "name": "Crossref", "operations": ["search"], "route": "rest_metadata", "credentials": {},
     "optional_credentials": {"email": "CROSSREF_EMAIL"}, "max_page_size": 1000,
     "docs": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
     "limitations": ["Publisher-deposited metadata and links do not establish full-text availability", "Journal names/multiple journals are filtered locally"]},
    {"id": "kci", "name": "KCI OAI-PMH", "operations": ["search"], "route": "oai_harvest_local_filter", "credentials": {},
     "docs": "https://kci.go.kr/kciportal/po/openapi/openDataOaiPmhView.kci",
     "limitations": ["Query is a local title/abstract substring; * harvests all", "OAI from/until are modification dates", "Filtered total is unknown until harvest completes", "Metadata and OA flags do not deliver PDF"]},
    {"id": "scienceon", "name": "ScienceON", "operations": ["search"], "route": "gateway_metadata", "credentials": {"client_id": "SCIENCEON_CLIENT_ID", "token": "SCIENCEON_ACCESS_TOKEN"},
     "protocol_verification": "official_documentation_unavailable_requires_live_validation",
     "docs": "https://scienceon.kisti.re.kr/apigateway/api/way/service/arti/serviceArtiSearchApi.do",
     "limitations": ["Externally issued access token required; no token-minting implementation", "Entitlement and response schema require live verification", "Year and journal filters are local"]},
    {"id": "dbpia", "name": "DBpia", "operations": ["search", "browser", "export_import"], "route": "search_api_metadata", "credentials": {"api_key": "DBPIA_API_KEY"},
     "docs": "https://api.dbpia.co.kr/openApi/about/search.do",
     "limitations": ["Search API is distinct from Business API and PDF delivery", "HTTPS endpoint only; no credential-bearing HTTP fallback"]},
    {"id": "wos", "name": "Web of Science Starter", "operations": ["search", "browser", "export_import"], "route": "starter_v1", "credentials": {"api_key": "WOS_API_KEY"},
     "max_page_size": 50, "docs": "https://developer.clarivate.com/apis/wos-starter/swagger",
     "limitations": ["API key/plan is separate from institutional browser entitlement", "Metadata API does not deliver publisher PDFs"]},
    {"id": "wos_expanded", "name": "Web of Science Expanded", "operations": ["search", "browser", "export_import"], "route": "expanded", "credentials": {"api_key": "WOS_EXPANDED_API_KEY"},
     "max_page_size": 100, "docs": "https://developer.clarivate.com/apis/wos/swagger",
     "limitations": ["Expanded license and record quota required", "Metadata API does not deliver publisher PDFs"]},
    {"id": "scopus", "name": "Scopus", "operations": ["search", "browser", "export_import"], "route": "search_api", "credentials": {"api_key": "SCOPUS_API_KEY"},
     "optional_credentials": {"insttoken": "SCOPUS_INSTTOKEN"}, "max_page_size": 200,
     "docs": "https://dev.elsevier.com/documentation/SCOPUSSearchAPI.wadl",
     "limitations": ["Institutional network/token entitlement is separate from API key", "Cursor pagination is used to avoid offset-only result limits", "No publisher PDF delivery"]},
    {"id": "unpaywall", "name": "Unpaywall", "operations": ["resolve"], "route": "doi_oa_locations", "credentials": {"email": "UNPAYWALL_EMAIL"},
     "docs": "https://unpaywall.org/data-format",
     "limitations": ["PDF URL may be absent", "Published, accepted and submitted versions remain distinct", "No supplement manifest"]},
    {"id": "pmc", "name": "PubMed Central Article Datasets", "operations": ["resolve"], "route": "pmc_oa_opendata_2026", "credentials": {},
     "optional_credentials": {"email": "NCBI_EMAIL"}, "docs": "https://pmc-oa-opendata.s3.amazonaws.com/README.txt",
     "limitations": ["Dataset membership does not guarantee a PDF", "media_urls includes figures; JATS establishes supplement relationships", "Integer article version is distinct from published/accepted status", "No legacy FTP fallback"]},
    {"id": "google_scholar", "name": "Google Scholar", "operations": ["browser", "export_import", "import_links"], "route": "browser_or_user_export", "credentials": {},
     "docs": "https://scholar.google.com/intl/en/scholar/help.html",
     "limitations": ["No search API implemented", "Search and citation exports cannot certify a complete journal inventory"]},
    {"id": "kiss", "name": "KISS", "operations": ["browser", "export_import", "import_links"], "route": "browser_or_user_export", "credentials": {},
     "docs": "https://kiss.kstudy.com/", "limitations": ["No search API implemented", "Full text depends on publisher and institutional access"]},
    {"id": "riss", "name": "RISS", "operations": ["browser", "export_import", "import_links"], "route": "browser_or_user_export", "credentials": {},
     "docs": "https://www.riss.kr/", "limitations": ["No RISS API connector implemented in this release", "Provider links, exports and institutional access are separate operations"]},
]
ALIASES = {"kci_oai": "kci", "wos_starter": "wos", "googlescholar": "google_scholar", "scholar": "google_scholar"}


def source_config(source: str, config: dict | None) -> tuple[str, dict]:
    source = ALIASES.get(source.lower(), source.lower())
    if source not in {s["id"] for s in SOURCES}:
        raise ScholarlyError("unknown_source", f"Unknown scholarly source: {source}.", source=source)
    values = dict(config or {})
    nested = values.pop("sources", {})
    if source in nested:
        values.update(nested[source])
    return source, values


def list_sources(config: dict | None = None) -> list[dict]:
    """API configuration presence is not connectivity, entitlement, or live verification."""
    result = []
    for source in SOURCES:
        entry = copy.deepcopy(source)
        _, values = source_config(source["id"], config)
        active_credentials = [row for row in values.get('credential_pool', []) if isinstance(row, dict) and row.get('enabled') is not False and row.get('api_key_ref')]
        if active_credentials and not values.get('api_key_ref'):
            values['api_key_ref'] = active_credentials[0]['api_key_ref']
        configured = True
        for name, default_env in source["credentials"].items():
            if name + "_ref" in values:
                present = bool(values[name + "_ref"]) and callable(values.get("secret_resolver"))
            else:
                present = bool(os.environ.get(values.get(name + "_env", default_env)))
            configured = configured and present
        api = "search" in entry["operations"] or "resolve" in entry["operations"]
        entry.update(capabilities={op: op in entry["operations"] for op in ("search", "resolve", "browser", "export_import", "import_links")},
                     implemented=api, configured=configured if api else None, live_verified=False,
                     live_verification=None, protocol_verification=entry.get("protocol_verification", "official_documentation_reviewed"))
        result.append(entry)
    return result
