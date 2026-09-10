"""Metadata search adapters. Each call retrieves one bounded provider page."""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from .common import (ScholarlyError, as_list, credential, encode_cursor, get, integer,
                     local_match, record, search_result, text, year, doi)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
CROSSREF = "https://api.crossref.org"
KCI = "https://open.kci.go.kr/oai/request"
SCIENCEON = "https://apigateway.kisti.re.kr/openapicall.do"
DBPIA = "https://api.dbpia.co.kr/v2/search/search.xml"
WOS = "https://api.clarivate.com/apis/wos-starter/v1/documents"
WOS_EXPANDED = "https://api.clarivate.com/api/wos"
SCOPUS = "https://api.elsevier.com/content/search/scopus"


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def required_dict(value, source, key):
    result = value.get(key) if isinstance(value, dict) else None
    if not isinstance(result, dict):
        raise ScholarlyError("invalid_response", f"Missing {source} response envelope.", source=source)
    return result


def next_page(source, signature, offset, count, total, **state):
    if count == 0 and total is not None and offset < total:
        raise ScholarlyError("pagination_incomplete", "An empty page precedes the reported result count.", source=source)
    return encode_cursor(source, signature, **state) if count and (total is None or offset + count < total) else None


async def pubmed(client, query, yf, yt, journals, limit, state, sig, config):
    source = "pubmed"
    offset = integer(state.get("offset"), 0)
    if offset < 0 or offset >= 10000:
        raise ScholarlyError("result_cap", "PubMed ESearch exposes only the first 10,000 matches; partition the query.", source=source)
    term = f"({query})"
    if yf is not None or yt is not None:
        term += f' AND ("{yf or 1500}/01/01"[Date - Publication] : "{yt or 2100}/12/31"[Date - Publication])'
    if journals:
        term += " AND (" + " OR ".join(quoted(j) + "[Journal]" for j in journals) + ")"
    shared = {"db": "pubmed", "tool": "ore-scholarly"}
    key = credential(config, "api_key", "NCBI_API_KEY", source, required=False)
    email = credential(config, "email", "NCBI_EMAIL", source, required=False)
    if key:
        shared["api_key"] = key
    if email:
        shared["email"] = email
    page_size = min(limit, 10000 - offset)
    data = await get(client, EUTILS + "esearch.fcgi", source,
                     params={**shared, "term": term, "retmode": "json", "retstart": offset, "retmax": page_size})
    result = required_dict(data, source, "esearchresult")
    if result.get("ERROR") or result.get("errorlist"):
        raise ScholarlyError("provider_query_error", "PubMed could not interpret the query.", source=source)
    total = integer(result.get("count"))
    if total is None or not isinstance(result.get("idlist"), list):
        raise ScholarlyError("invalid_response", "PubMed response lacks count or ID list.", source=source)
    ids = result["idlist"]
    records = []
    if ids:
        root = await get(client, EUTILS + "efetch.fcgi", source,
                         params={**shared, "id": ",".join(ids), "retmode": "xml"}, kind="xml")
        for article in root.findall("PubmedArticle"):
            citation = article.find("MedlineCitation")
            item = article.find("MedlineCitation/Article")
            pmid = text(citation, "PMID")
            identifiers = {e.get("IdType"): text(e) for e in article.findall("PubmedData/ArticleIdList/ArticleId")}
            authors = []
            for author in item.findall("AuthorList/Author") if item is not None else []:
                authors.append(text(author, "CollectiveName") or " ".join(filter(None, [text(author, "ForeName"), text(author, "LastName")])))
            records.append(record(source, pmid, url=EUTILS + "efetch.fcgi", pmid=pmid,
                doi=doi(identifiers.get("doi")), pmcid=identifiers.get("pmc"), title=text(item, "ArticleTitle"),
                abstract="\n".join(text(a) for a in item.findall("Abstract/AbstractText")) if item is not None else "",
                journal=text(item, "Journal/Title"), issns=[text(e) for e in item.findall("Journal/ISSN")] if item is not None else [],
                volume=text(item, "Journal/JournalIssue/Volume") or None, issue=text(item, "Journal/JournalIssue/Issue") or None,
                year=year(text(item, "Journal/JournalIssue/PubDate")), authors=authors,
                article_type=[text(t) for t in item.findall("PublicationTypeList/PublicationType")] if item is not None else [],
                urls=[f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"], identifiers=identifiers))
        if {r["id"] for r in records} != set(ids):
            raise ScholarlyError("metadata_incomplete", "PubMed EFetch did not return every requested article ID.", source=source,
                                 details={"requested_count": len(ids), "returned_count": len(records)})
    if not ids and offset < min(total, 10000):
        raise ScholarlyError("pagination_incomplete", "PubMed returned an unexpected empty page.", source=source)
    following = offset + len(ids)
    continuation = encode_cursor(source, sig, offset=following) if ids and following < min(total, 10000) else None
    return search_result(source, query, records, total, continuation, total > 10000,
                         provider_query=term, coverage_limit=10000, warnings=["partition_query_for_full_inventory"] if total > 10000 else [])


async def crossref(client, query, yf, yt, journals, limit, state, sig, config):
    source = "crossref"
    filters = []
    if yf is not None:
        filters.append(f"from-pub-date:{yf}-01-01")
    if yt is not None:
        filters.append(f"until-pub-date:{yt}-12-31")
    exact_doi = doi(query)
    remote_journal = journals and len(journals) == 1 and re.fullmatch(r"\d{4}-\d{3}[\dXx]", journals[0])
    url = f"{CROSSREF}/journals/{quote(journals[0], safe='')}/works" if remote_journal else CROSSREF + "/works"
    if exact_doi:
        if state:
            raise ScholarlyError("invalid_cursor", "Exact Crossref DOI lookup has no continuation.", source=source)
        url = CROSSREF + "/works/" + quote(exact_doi, safe="")
    params = {} if exact_doi else {"query": query, "rows": limit, "cursor": state.get("token", "*")}
    if filters and not exact_doi:
        params["filter"] = ",".join(filters)
    email = credential(config, "email", "CROSSREF_EMAIL", source, required=False)
    if email:
        params["mailto"] = email
    response = await get(client, url, source, params=params, allow_not_found=bool(exact_doi))
    if response is None:
        return search_result(source, query, [], 0, lookup="exact_doi")
    result = required_dict(response, source, "message")
    if exact_doi:
        if doi(result.get("DOI")) != exact_doi:
            raise ScholarlyError("identity_mismatch", "Crossref returned a different DOI.", source=source)
        result = {"items": [result], "total-results": 1}
    items = result.get("items")
    if not isinstance(items, list):
        raise ScholarlyError("invalid_response", "Crossref response lacks items.", source=source)
    records = []
    for item in items:
        value = doi(item.get("DOI"))
        date = next((item[k].get("date-parts", [[]])[0] for k in ("published", "issued", "published-print", "published-online")
                     if isinstance(item.get(k), dict) and item[k].get("date-parts")), [])
        entry = record(source, value or item.get("URL", ""), url=url, doi=value,
            title=" ".join(as_list(item.get("title"))), journal="; ".join(as_list(item.get("container-title"))),
            issns=item.get("ISSN", []), volume=item.get("volume"), issue=item.get("issue"), year=integer(date[0]) if date else None,
            authors=[" ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "") for a in item.get("author", [])],
            article_type=item.get("type"), urls=[item["URL"]] if item.get("URL") else [],
            links=item.get("link", []), licenses=item.get("license", []), source_data=item)
        if exact_doi:
            if local_match(entry, None, yf, yt, journals):
                records.append(entry)
        elif remote_journal or local_match(entry, None, None, None, journals):
            records.append(entry)
    if exact_doi:
        return search_result(source, query, records, len(records), lookup="exact_doi", provider_total=1)
    total = integer(result.get("total-results"))
    offset = integer(state.get("offset"), 0)
    token = result.get("next-cursor")
    has_more = len(items) >= limit and (total is None or offset + len(items) < total)
    if has_more and (not token or token == state.get("token")):
        raise ScholarlyError("pagination_stalled", "Crossref did not advance its cursor.", source=source)
    continuation = encode_cursor(source, sig, token=token, offset=offset + len(items)) if has_more else None
    local_filter = bool(journals and not remote_journal)
    return search_result(source, query, records, None if local_filter else total, continuation,
                         provider_total=total, journal_filter="local_exact" if local_filter else "provider", provider_query=query)


def kci_record(element):
    header = element.find("header")
    info = element.find(".//articleInfo")
    j = element.find(".//journalInfo")
    title_nodes = element.findall(".//article-title")
    preferred = next((e for e in title_nodes if e.get("lang") == "original"), title_nodes[0] if title_nodes else None)
    identifier = (info.get("article-id") if info is not None else None) or text(header, "identifier")
    value = record("kci", identifier, url=KCI, title=text(preferred),
        journal=text(j, "journal-name"), issns=[text(j, p) for p in ("pissn", "eissn") if text(j, p)],
        volume=text(j, "volume") or None, issue=text(j, "issue") or None,
        year=year(text(j, "pub-year")), doi=doi(text(info, "doi")),
        authors=[text(e) for e in element.findall(".//author-group/author")],
        abstract="\n".join(text(e) for e in element.findall(".//abstract-group/abstract")),
        article_type=text(info, "article-regularity") or None,
        urls=[text(info, "url")] if text(info, "url") else [],
        open_access_flag=text(info, "orte-open-yn") or None)
    value["provenance"]["oai_identifier"] = text(header, "identifier")
    value["provenance"]["oai_datestamp"] = text(header, "datestamp")
    return value


async def kci(client, query, yf, yt, journals, limit, state, sig, config):
    source = "kci"
    token = state.get("token")
    params = {"verb": "ListRecords", "resumptionToken": token} if token else {
        "verb": "ListRecords", "metadataPrefix": "oai_kci", "set": "ARTI"}
    if not token:
        for key in ("from", "until"):
            if config.get("oai_" + key):
                params[key] = config["oai_" + key]
    root = await get(client, KCI, source, params=params, kind="xml")
    error = root.find("error")
    if error is not None:
        code = error.get("code", "unknown")
        if code == "noRecordsMatch":
            return search_result(source, query, [], None, harvest_complete=True, provider_total=0)
        raise ScholarlyError("oai_" + code, "KCI OAI-PMH rejected the harvest request.", source=source)
    container = root.find("ListRecords")
    if container is None:
        raise ScholarlyError("invalid_response", "KCI response lacks ListRecords.", source=source)
    raw = container.findall("record")
    offset = integer(state.get("offset"), 0)
    if offset < 0 or offset > len(raw):
        raise ScholarlyError("invalid_cursor", "OAI page changed or cursor offset is invalid.", source=source)
    records = []
    deleted = []
    consumed = offset
    for element in raw[offset:]:
        consumed += 1
        header = element.find("header")
        if header is not None and header.get("status") == "deleted":
            deleted.append(text(header, "identifier"))
            continue
        item = kci_record(element)
        if local_match(item, None if query == "*" else query, yf, yt, journals):
            records.append(item)
        if len(records) >= limit:
            break
    rt = container.find("resumptionToken")
    next_token = text(rt)
    total = integer(rt.get("completeListSize")) if rt is not None else None
    if consumed < len(raw):
        continuation = encode_cursor(source, sig, token=token, offset=consumed)
    elif next_token:
        if next_token == token:
            raise ScholarlyError("pagination_stalled", "KCI repeated its OAI resumption token.", source=source)
        continuation = encode_cursor(source, sig, token=next_token, offset=0)
    else:
        continuation = None
    return search_result(source, query, records, None, continuation, provider_total=total,
        filter_semantics="local_title_abstract_substring_and_publication_year_and_exact_journal",
        oai_date_semantics="record_modification_time", deleted_identifiers=deleted,
        harvest_complete=continuation is None, scanned_records=consumed-offset)


def xml_fields(element):
    """ScienceON metadata items are identified by metaCode, with XML-name fallback."""
    values = {}
    for child in element.iter():
        key = child.get("metaCode") or child.get("metacode") or child.get("name") or child.tag
        value = text(child)
        if value:
            values.setdefault(key.upper(), []).append(value)
    return values


async def scienceon(client, query, yf, yt, journals, limit, state, sig, config):
    source = "scienceon"
    cid = credential(config, "client_id", "SCIENCEON_CLIENT_ID", source)
    token = credential(config, "token", "SCIENCEON_ACCESS_TOKEN", source)
    page = integer(state.get("page"), 1)
    if page < 1:
        raise ScholarlyError("invalid_cursor", "ScienceON page must be positive.", source=source)
    params = {"client_id": cid, "token": token, "version": "1.0", "action": "search", "target": "ARTI",
              "searchQuery": json.dumps({"BI": query}, ensure_ascii=False), "curPage": page, "rowCount": limit}
    root = await get(client, SCIENCEON, source, params=params, kind="xml")
    code = text(root, ".//errorCode") or text(root, ".//error-code") or text(root, ".//statusCode")
    if code and code not in ("0", "0000", "200"):
        raise ScholarlyError("provider_error", "ScienceON returned a service error; check token validity and entitlement.", source=source)
    raw = root.findall(".//record")
    total = integer(text(root, ".//totalCount") or text(root, ".//totalcount") or text(root, ".//total-count") or text(root, ".//TotalCount"))
    if total is None and not raw:
        raise ScholarlyError("invalid_response", "ScienceON response lacks records and total count.", source=source)
    records = []
    for element in raw:
        fields = xml_fields(element)
        def field(*names):
            return next((fields[n][0] for n in names if fields.get(n)), "")
        identifier = field("CN", "CONTROL_NO", "CONTROLNO") or element.get("id", "")
        title = field("TI", "TITLE", "TITLE_KO", "ARTI_TITLE")
        if not identifier or not title:
            raise ScholarlyError("invalid_response", "ScienceON record lacks control number or title.", source=source)
        entry = record(source, identifier, url=SCIENCEON, title=title,
            journal=field("SO", "JOURNAL_NAME", "JOURNALNAME"), year=year(field("PY", "PUB_YEAR", "PUBYEAR")),
            issns=fields.get("SN", fields.get("ISSN", [])), doi=doi(field("DI", "DOI")), authors=fields.get("AU", fields.get("AUTHOR", [])),
            volume=field("VL", "VOLUME", "VOLNO1") or None, issue=field("IS", "ISSUE", "VOLNO2") or None,
            article_type=field("DT", "DOCTYPE") or None, abstract=field("AB", "ABSTRACT"),
            urls=[field("URL", "CONTENTURL", "FULLTEXTURL")] if field("URL", "CONTENTURL", "FULLTEXTURL") else [f"https://scienceon.kisti.re.kr/srch/selectPORSrchArticle.do?cn={quote(identifier, safe='')}"],
            source_fields=fields)
        if local_match(entry, None, yf, yt, journals):
            records.append(entry)
    continuation = next_page(source, sig, (page-1)*limit, len(raw), total, page=page+1) if len(raw) >= limit or total is not None else None
    local_filter = bool(journals or yf is not None or yt is not None)
    return search_result(source, query, records, None if local_filter else total, continuation,
                         provider_total=total, filter_semantics="local_publication_year_and_exact_journal" if local_filter else "provider")


async def dbpia(client, query, yf, yt, journals, limit, state, sig, config):
    source = "dbpia"
    key = credential(config, "api_key", "DBPIA_API_KEY", source)
    page = integer(state.get("page"), 1)
    if page < 1:
        raise ScholarlyError("invalid_cursor", "DBpia page must be positive.", source=source)
    params = {"key": key, "target": "se_adv", "searchall": query, "itype": 1,
              "pagecount": limit, "pagenumber": page}
    if yf is not None or yt is not None:
        params.update(pyear=3, pyear_start=yf or 1500, pyear_end=yt or 2100)
    root = await get(client, DBPIA, source, params=params, kind="xml")
    error = root.find(".//error")
    errorcode = text(root, ".//error/code") or text(root, ".//error_code")
    if not errorcode and error is not None:
        match = re.search(r"E\d{4}", text(error))
        errorcode = match.group() if match else "unknown"
    if errorcode:
        if errorcode == "E0016":
            return search_result(source, query, [], 0)
        code = "rate_limited" if errorcode == "E0017" else "access_required" if errorcode in ("E0001", "E0002", "E0012", "E0014") else "provider_error"
        raise ScholarlyError(code, f"DBpia service error {errorcode}.", source=source)
    total = integer(text(root, ".//totalcount"))
    if total is None:
        raise ScholarlyError("invalid_response", "DBpia response lacks totalcount.", source=source)
    raw = root.findall(".//items/item")
    records = []
    for element in raw:
        link = text(element, "link_url")
        identifier = text(element, "node_id") or link
        entry = record(source, identifier, url=DBPIA, title=text(element, "title"),
            journal=text(element, "publication/name"), year=year(text(element, "issue/yymm")),
            issue=text(element, "issue/num") or text(element, "issue/name") or None,
            authors=[text(a, "name") for a in element.findall("authors/author")],
            article_type=text(element, "ctype") or None, urls=[link] if link else [],
            pages=text(element, "pages"), price_yn=text(element, "price_yn") or None,
            business_api_link=text(element, "link_api") or None)
        if local_match(entry, None, None, None, journals):
            records.append(entry)
    continuation = next_page(source, sig, (page-1)*limit, len(raw), total, page=page+1)
    return search_result(source, query, records, None if journals else total, continuation,
                         provider_total=total, journal_filter="local_exact" if journals else "none")


def wos_query(query, yf, yt, journals, config):
    value = query if config.get("query_mode") == "native" else f"TS=({quoted(query)})"
    if yf is not None or yt is not None:
        value = f"({value}) AND PY=({yf or 1500}-{yt or 2100})"
    if journals:
        value += " AND SO=(" + " OR ".join(quoted(j) for j in journals) + ")"
    return value


async def wos(client, query, yf, yt, journals, limit, state, sig, config):
    source = "wos"
    if limit > 50:
        raise ScholarlyError("invalid_limit", "WoS Starter supports at most 50 records per page.", source=source)
    key = credential(config, "api_key", "WOS_API_KEY", source)
    page = integer(state.get("page"), 1)
    if page < 1:
        raise ScholarlyError("invalid_cursor", "WoS page must be positive.", source=source)
    provider_query = wos_query(query, yf, yt, journals, config)
    result = await get(client, WOS, source, params={"q": provider_query, "db": config.get("database", "WOS"),
                       "limit": limit, "page": page}, headers={"X-ApiKey": key})
    metadata = required_dict(result, source, "metadata")
    total = integer(metadata.get("total"))
    hits = result.get("hits")
    if total is None or not isinstance(hits, list):
        raise ScholarlyError("invalid_response", "WoS Starter response lacks total or hits.", source=source)
    records = []
    for hit in hits:
        publication = hit.get("source", {})
        identifiers = hit.get("identifiers", {})
        records.append(record(source, hit.get("uid", ""), url=WOS, title=hit.get("title", ""),
            doi=doi(identifiers.get("doi")), journal=publication.get("sourceTitle", ""),
            issns=[identifiers[k] for k in ("issn", "eissn") if identifiers.get(k)],
            volume=publication.get("volume"), issue=publication.get("issue"), year=integer(publication.get("publishYear")),
            authors=[a.get("displayName", a.get("wosStandard", "")) for a in hit.get("names", {}).get("authors", [])],
            article_type=hit.get("types", []), urls=[hit["links"]["record"]] if hit.get("links", {}).get("record") else [],
            source_data=hit))
    continuation = next_page(source, sig, (page-1)*limit, len(hits), total, page=page+1)
    return search_result(source, query, records, total, continuation, provider_query=provider_query)


async def wos_expanded(client, query, yf, yt, journals, limit, state, sig, config):
    source = "wos_expanded"
    if limit > 100:
        raise ScholarlyError("invalid_limit", "WoS Expanded supports at most 100 records per request.", source=source)
    key = credential(config, "api_key", "WOS_EXPANDED_API_KEY", source)
    first = integer(state.get("first"), 1)
    if first < 1:
        raise ScholarlyError("invalid_cursor", "WoS firstRecord must be positive.", source=source)
    provider_query = wos_query(query, yf, yt, journals, config)
    result = await get(client, WOS_EXPANDED, source,
        params={"databaseId": config.get("database", "WOS"), "usrQuery": provider_query, "count": limit, "firstRecord": first},
        headers={"X-ApiKey": key, "Accept": "application/json"})
    query_result = required_dict(result, source, "QueryResult")
    total = integer(query_result.get("RecordsFound"))
    raw = result.get("Data", {}).get("Records", {}).get("records", {}).get("REC", [])
    raw = as_list(raw)
    if total is None:
        raise ScholarlyError("invalid_response", "WoS Expanded response lacks RecordsFound.", source=source)
    records = []
    for item in raw:
        summary = item.get("static_data", {}).get("summary", {})
        titles = {t.get("type"): t.get("content", "") for t in as_list(summary.get("titles", {}).get("title"))}
        publication = summary.get("pub_info", {})
        identifiers = {i.get("type"): i.get("value") for i in as_list(item.get("dynamic_data", {}).get("cluster_related", {}).get("identifiers", {}).get("identifier"))}
        records.append(record(source, item.get("UID", ""), url=WOS_EXPANDED, title=titles.get("item", ""),
            journal=titles.get("source", ""), doi=doi(identifiers.get("doi")), year=integer(publication.get("pubyear")),
            volume=publication.get("vol"), issue=publication.get("issue"),
            authors=[a.get("full_name", "") for a in as_list(summary.get("names", {}).get("name"))],
            article_type=as_list(summary.get("doctypes", {}).get("doctype")),
            urls=[f"https://www.webofscience.com/wos/woscc/full-record/{quote(item.get('UID', ''), safe=':')}"], source_data=item))
    continuation = next_page(source, sig, first-1, len(raw), total, first=first+len(raw))
    return search_result(source, query, records, total, continuation, provider_query=provider_query)


async def scopus(client, query, yf, yt, journals, limit, state, sig, config):
    source = "scopus"
    view = config.get("view", "STANDARD").upper()
    maximum = 200 if view == "STANDARD" else 25
    if view not in ("STANDARD", "COMPLETE", "COMPONENT") or limit > maximum:
        raise ScholarlyError("invalid_limit", f"Scopus {view} requires a valid view and limit <= {maximum}.", source=source)
    key = credential(config, "api_key", "SCOPUS_API_KEY", source)
    insttoken = credential(config, "insttoken", "SCOPUS_INSTTOKEN", source, required=False)
    headers = {"X-ELS-APIKey": key, "Accept": "application/json"}
    if insttoken:
        headers["X-ELS-Insttoken"] = insttoken
    provider_query = query if config.get("query_mode") == "native" else f"TITLE-ABS-KEY({quoted(query)})"
    if yf is not None:
        provider_query = f"({provider_query}) AND PUBYEAR > {yf - 1}"
    if yt is not None:
        provider_query = f"({provider_query}) AND PUBYEAR < {yt + 1}"
    if journals:
        parts = [f"ISSN({quoted(j)})" if re.fullmatch(r"\d{4}-?\d{3}[\dXx]", j) else f"SRCTITLE({quoted(j)})" for j in journals]
        provider_query += " AND (" + " OR ".join(parts) + ")"
    token = state.get("token", "*")
    data = await get(client, SCOPUS, source, params={"query": provider_query, "count": limit,
                     "cursor": token, "view": view}, headers=headers)
    result = required_dict(data, source, "search-results")
    total = integer(result.get("opensearch:totalResults"))
    if total is None:
        raise ScholarlyError("invalid_response", "Scopus response lacks totalResults.", source=source)
    raw = as_list(result.get("entry"))
    if raw and "error" in raw[0]:
        if total == 0:
            raw = []
        else:
            raise ScholarlyError("provider_error", "Scopus returned an entry error.", source=source)
    records = []
    for item in raw:
        records.append(record(source, item.get("eid") or item.get("dc:identifier", ""), url=SCOPUS,
            title=item.get("dc:title", ""), journal=item.get("prism:publicationName", ""),
            doi=doi(item.get("prism:doi")), year=year(item.get("prism:coverDate")),
            volume=item.get("prism:volume"), issue=item.get("prism:issueIdentifier"),
            issns=[item[k] for k in ("prism:issn", "prism:eIssn") if item.get(k)],
            authors=[a.get("authname", "") for a in item.get("author", [])] or [item["dc:creator"]] if item.get("dc:creator") else [a.get("authname", "") for a in item.get("author", [])],
            article_type=item.get("subtypeDescription"),
            urls=[link["@href"] for link in item.get("link", []) if link.get("@ref") in ("scopus", "scopus-citedby") and link.get("@href")],
            source_data=item))
    offset = integer(state.get("offset"), 0)
    more = offset + len(raw) < total
    next_token = result.get("cursor", {}).get("@next")
    if more and (not raw or not next_token or next_token == token):
        raise ScholarlyError("pagination_stalled", "Scopus did not provide an advancing cursor before the reported total.", source=source)
    continuation = encode_cursor(source, sig, token=next_token, offset=offset+len(raw)) if more else None
    return search_result(source, query, records, total, continuation, provider_query=provider_query, pagination="cursor")


SEARCHERS = {"pubmed": pubmed, "crossref": crossref, "kci": kci, "scienceon": scienceon,
             "dbpia": dbpia, "wos": wos, "wos_expanded": wos_expanded, "scopus": scopus}
