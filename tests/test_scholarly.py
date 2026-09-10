"""Offline provider contract tests; synthetic responses are not live-access evidence."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from ore_scholarly import ScholarlyError, list_sources, list_runes, load_rune, resolve, search
from ore_scholarly.common import encode_cursor, fingerprint


def xml(value):
    return httpx.Response(200, text=value, headers={"Content-Type": "application/xml"})


def secrets(**overrides):
    values = {"api_key_ref": "test/key", "email_ref": "test/email", "client_id_ref": "test/client", "token_ref": "test/token",
              "secret_resolver": lambda ref: "fixture@invalid.test" if "email" in ref else "synthetic-fixture-credential"}
    return {**values, **overrides}


class ScholarlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_crossref_cursor_preserves_query_and_filters(self):
        observed = []
        def handle(request):
            params = dict(request.url.params)
            observed.append(params)
            second = params["cursor"] != "*"
            return httpx.Response(200, json={"message": {"total-results": 2, "next-cursor": "next2" if second else "next1", "items": [
                {"DOI": "10.1234/b" if second else "10.1234/a", "title": ["Study"], "container-title": ["Journal"],
                 "published": {"date-parts": [[2024, 2]]}, "type": "journal-article"}]}})
        config = {"transport": httpx.MockTransport(handle)}
        first = await search("crossref", "trial", limit=1, year_from=2024, year_to=2025, config=config)
        second = await search("crossref", "trial", limit=1, year_from=2024, year_to=2025, cursor=first["next_cursor"], config=config)
        self.assertEqual([first["records"][0]["doi"], second["records"][0]["doi"]], ["10.1234/a", "10.1234/b"])
        self.assertEqual(observed[0]["filter"], observed[1]["filter"])
        self.assertEqual(observed[1]["query"], "trial")
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(first["records"][0]["eligibility"], "unclassified")
        with self.assertRaises(ScholarlyError) as cm:
            await search("crossref", "different", limit=1, cursor=first["next_cursor"], config=config)
        self.assertEqual(cm.exception.code, "invalid_cursor")

    async def test_crossref_exact_doi_uses_identity_endpoint(self):
        def handle(request):
            self.assertNotIn("query", request.url.params)
            self.assertIn("10.1234", request.url.path)
            return httpx.Response(200, json={"message": {"DOI": "10.1234/exact", "title": ["Exact trial"],
                "URL": "https://doi.org/10.1234/exact", "published": {"date-parts": [[2024]]}}})
        result = await search("crossref", "https://doi.org/10.1234/exact", year_from=2024, config={"transport": httpx.MockTransport(handle)})
        self.assertEqual(result["lookup"], "exact_doi")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["records"][0]["url"], "https://doi.org/10.1234/exact")
        self.assertIsNone(result["next_cursor"])

    async def test_crossref_local_journal_filter_does_not_invent_total(self):
        def handle(request):
            return httpx.Response(200, json={"message": {"total-results": 5, "next-cursor": "next", "items": [
                {"DOI": "10.1234/a", "title": ["Trial"], "container-title": ["Other Journal"]}]}})
        result = await search("crossref", "trial", journals=["Requested Journal"], limit=1, config={"transport": httpx.MockTransport(handle)})
        self.assertEqual(result["records"], [])
        self.assertEqual(result["provider_total"], 5)
        self.assertIsNone(result["total"])
        self.assertTrue(result["next_cursor"])

    async def test_pubmed_metadata_and_explicit_search_cap(self):
        def handle(request):
            if request.url.path.endswith("esearch.fcgi"):
                return httpx.Response(200, json={"esearchresult": {"count": "10001", "idlist": ["123"]}})
            return xml('''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
              <ArticleTitle>A <i>randomized</i> trial</ArticleTitle><Journal><Title>Journal</Title><ISSN>1234-5678</ISSN>
              <JournalIssue><Volume>4</Volume><Issue>1</Issue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>
              <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation>
              <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/test</ArticleId><ArticleId IdType="pmc">PMC123</ArticleId></ArticleIdList></PubmedData>
              </PubmedArticle></PubmedArticleSet>''')
        config = {"transport": httpx.MockTransport(handle)}
        result = await search("pubmed", "trial", limit=1, config=config)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total"], 10001)
        self.assertEqual(result["records"][0]["title"], "A randomized trial")
        self.assertEqual(result["records"][0]["pmcid"], "PMC123")
        sig = fingerprint("pubmed", "trial", None, None, None, 1, config)
        with self.assertRaises(ScholarlyError) as cm:
            await search("pubmed", "trial", limit=1, cursor=encode_cursor("pubmed", sig, offset=10000), config=config)
        self.assertEqual(cm.exception.code, "result_cap")

    async def test_kci_page_remainder_and_oai_datestamp(self):
        calls = []
        def handle(request):
            calls.append(dict(request.url.params))
            if request.url.params.get("resumptionToken") == "next-server":
                return xml('<OAI-PMH><ListRecords><resumptionToken/></ListRecords></OAI-PMH>')
            records = ''.join(f'''<record><header><identifier>oai:kci:{n}</identifier><datestamp>2026-08-01</datestamp></header>
               <metadata><kci><journalInfo><journal-name>Journal</journal-name><pub-year>2024</pub-year></journalInfo>
               <articleInfo article-id="{n}"><title-group><article-title lang="original">Trial {n}</article-title></title-group>
               <article-regularity>정규논문</article-regularity></articleInfo></kci></metadata></record>''' for n in (1,2))
            return xml('<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>'+records+'<resumptionToken completeListSize="2">next-server</resumptionToken></ListRecords></OAI-PMH>')
        config = {"transport": httpx.MockTransport(handle), "oai_from": "2026-01-01"}
        first = await search("kci", "Trial", limit=1, year_from=2024, config=config)
        second = await search("kci", "Trial", limit=1, year_from=2024, cursor=first["next_cursor"], config=config)
        final = await search("kci", "Trial", limit=1, year_from=2024, cursor=second["next_cursor"], config=config)
        self.assertEqual([first["records"][0]["id"], second["records"][0]["id"]], ["1", "2"])
        self.assertEqual(first["records"][0]["year"], 2024)
        self.assertEqual(first["records"][0]["provenance"]["oai_datestamp"], "2026-08-01")
        self.assertIsNone(first["total"])
        self.assertEqual(calls[0]["from"], "2026-01-01")
        self.assertEqual(set(calls[2]), {"verb", "resumptionToken"})
        self.assertTrue(final["harvest_complete"])

    async def test_kci_deleted_and_invalid_token(self):
        def handle(request):
            return xml('<OAI-PMH><error code="badResumptionToken">expired</error></OAI-PMH>')
        with self.assertRaises(ScholarlyError) as cm:
            await search("kci", "*", config={"transport": httpx.MockTransport(handle)})
        self.assertEqual(cm.exception.code, "oai_badResumptionToken")

    async def test_scienceon_xml_envelope_and_local_year(self):
        def handle(request):
            self.assertEqual(json.loads(request.url.params["searchQuery"]), {"BI":"임상"})
            return xml('''<MetaData><resultSummary><TotalCount>1</TotalCount><statusCode>200</statusCode></resultSummary>
            <parameterData><item metaCode="CN">not-a-record</item></parameterData><recordList><record>
            <item metaCode="CN">JAKO123</item><item metaCode="Title">임상 연구</item><item metaCode="Pubyear">2024</item>
            <item metaCode="JournalName">Journal</item><item metaCode="DOI">10.1234/science</item>
            <item metaCode="Author">Kim</item></record></recordList></MetaData>''')
        result = await search("scienceon", "임상", year_from=2024, limit=1, config=secrets(transport=httpx.MockTransport(handle)))
        self.assertEqual(result["records"][0]["id"], "JAKO123")
        self.assertEqual(result["records"][0]["authors"], ["Kim"])
        self.assertIsNone(result["total"])
        self.assertIsNone(result["next_cursor"])

    async def test_dbpia_page_and_error(self):
        def handle(request):
            self.assertEqual(request.url.params["pyear_start"], "2020")
            self.assertEqual(request.url.params["pyear_end"], "2025")
            return xml('''<root><paramdata><totalcount>2</totalcount></paramdata><result><items><item>
            <title>Trial</title><ctype>article</ctype><publication><name>Journal</name></publication>
            <issue><num>3(1)</num><yymm>2024.01</yymm></issue><authors><author><name>Kim</name></author></authors>
            <link_url>https://www.dbpia.co.kr/journal/articleDetail?nodeId=NODE1</link_url></item></items></result></root>''')
        result = await search("dbpia", "trial", limit=1, year_from=2020, year_to=2025, config=secrets(transport=httpx.MockTransport(handle)))
        self.assertTrue(result["next_cursor"])
        self.assertEqual(result["records"][0]["year"], 2024)
        with self.assertRaises(ScholarlyError) as cm:
            await search("dbpia", "trial", config=secrets(transport=httpx.MockTransport(lambda req: xml('<root><error><code>E0014</code></error></root>'))))
        self.assertEqual(cm.exception.code, "access_required")

    async def test_wos_starter_and_expanded(self):
        def starter(request):
            self.assertEqual(request.headers["X-ApiKey"], "synthetic-fixture-credential")
            self.assertIn('PY=(2024-2025)', request.url.params["q"])
            return httpx.Response(200, json={"metadata":{"total":2},"hits":[{"uid":"WOS:1","title":"Trial","source":{"sourceTitle":"Journal","publishYear":2024},"identifiers":{"doi":"10.1234/wos"}}]})
        first = await search("wos", "trial", year_from=2024, year_to=2025, limit=1, config=secrets(transport=httpx.MockTransport(starter)))
        self.assertEqual(first["records"][0]["doi"], "10.1234/wos")
        self.assertTrue(first["next_cursor"])
        def expanded(request):
            return httpx.Response(200, json={"QueryResult":{"RecordsFound":1},"Data":{"Records":{"records":{"REC":{"UID":"WOS:2",
               "static_data":{"summary":{"titles":{"title":[{"type":"item","content":"Expanded trial"},{"type":"source","content":"Journal"}]},"pub_info":{"pubyear":2024}}},
               "dynamic_data":{"cluster_related":{"identifiers":{"identifier":{"type":"doi","value":"10.1234/expanded"}}}}}}}}})
        second = await search("wos_expanded", "trial", config=secrets(transport=httpx.MockTransport(expanded)))
        self.assertEqual(second["records"][0]["doi"], "10.1234/expanded")
        self.assertIsNone(second["next_cursor"])

    async def test_scopus_cursor_over_5000_and_stall(self):
        def handle(request):
            return httpx.Response(200, json={"search-results":{"opensearch:totalResults":"6000","cursor":{"@next":"next"},
                "entry":[{"eid":"2-s2.0-1","dc:title":"Trial","prism:coverDate":"2024-01-01","prism:doi":"10.1234/scopus"}]}})
        config=secrets(transport=httpx.MockTransport(handle))
        first=await search("scopus","trial",limit=1,config=config)
        self.assertFalse(first["truncated"])
        self.assertEqual(first["total"],6000)
        with self.assertRaises(ScholarlyError) as cm:
            await search("scopus","trial",limit=1,cursor=first["next_cursor"],config=config)
        self.assertEqual(cm.exception.code,"pagination_stalled")

    async def test_credentials_errors_and_shared_client(self):
        config={"transport":httpx.MockTransport(lambda req:httpx.Response(200,json={}))}
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ScholarlyError) as cm:
                await search("wos","trial",config=config)
            self.assertEqual(cm.exception.code,"credentials_missing")
        with self.assertRaises(ScholarlyError) as cm:
            await search("wos","trial",config={**config,"api_key":"must-not-be-literal"})
        self.assertEqual(cm.exception.code,"literal_credential")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(429,headers={"Retry-After":"60"},text="echoed-secret"))) as client:
            with self.assertRaises(ScholarlyError) as cm:
                await search("wos","trial",config=secrets(client=client))
            self.assertEqual(cm.exception.retry_after,"60")
            self.assertNotIn("echoed-secret",str(cm.exception.to_dict()))
            self.assertFalse(client.is_closed)

    async def test_unpaywall_versions_nullable_pdf_and_no_supplement_claim(self):
        def handle(request):
            return httpx.Response(200,json={"doi":"10.1234/oa","is_oa":True,"oa_locations":[
                {"version":"acceptedVersion","url_for_pdf":"https://repo.example/accepted.pdf","license":"cc-by"},
                {"version":"publishedVersion","url_for_pdf":None,"url_for_landing_page":"https://publisher.example/article"}]})
        result=await resolve("unpaywall","https://doi.org/10.1234/OA",config=secrets(transport=httpx.MockTransport(handle)))
        self.assertEqual([r["version"] for r in result["candidates"]],["acceptedVersion","publishedVersion"])
        self.assertEqual([r["role"] for r in result["candidates"]],["main_pdf","landing_page"])
        self.assertEqual(result["supplement_status"],"not_supported")
        self.assertTrue(all(not r["downloaded"] for r in result["candidates"]))

    async def test_pmc_discovers_latest_version_and_supplement_relationships(self):
        requests=[]
        def handle(request):
            requests.append(str(request.url))
            if request.url.path=="/":
                return xml('<ListBucketResult><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes><IsTruncated>false</IsTruncated></ListBucketResult>')
            if request.url.path.endswith('.json'):
                return httpx.Response(200,json={"pmcid":"PMC123","version":2,"doi":"10.1234/pmc","is_manuscript":True,"license_code":"TDM",
                    "xml_url":"s3://pmc-oa-opendata/PMC123.2/PMC123.2.xml", "pdf_url":"s3://pmc-oa-opendata/PMC123.2/PMC123.2.pdf",
                    "media_urls":["s3://pmc-oa-opendata/PMC123.2/figure.jpg","s3://pmc-oa-opendata/PMC123.2/data.xlsx?md5="+"a"*32]})
            if request.url.path.endswith('.xml'):
                return xml('<article xmlns:xlink="http://www.w3.org/1999/xlink"><body><fig><graphic xlink:href="figure.jpg"/></fig><supplementary-material id="S1"><label>Data</label><media xlink:href="data.xlsx"/></supplementary-material></body></article>')
            self.fail('Resolver requested binary bytes')
        result=await resolve("pmc","PMC123",config={"transport":httpx.MockTransport(handle)})
        self.assertEqual(result["selected_versions"],[2])
        supplement=[r for r in result["candidates"] if r["role"]=="supplement"]
        self.assertEqual(len(supplement),1)
        self.assertEqual(supplement[0]["expected_md5"],"a"*32)
        self.assertEqual(supplement[0]["version"],"author_manuscript")
        self.assertTrue(any(r["role"]=="media" and "figure.jpg" in r["url"] for r in result["candidates"]))
        self.assertTrue(all("PMC123.1/" not in url for url in requests))
        self.assertTrue(all(not url.endswith('.pdf') for url in requests))

    async def test_pmc_pdf_missing_is_partial_and_untrusted_object_rejected(self):
        def handle(request):
            return httpx.Response(200,json={"pmcid":"PMC123","version":1,"xml_url":"s3://pmc-oa-opendata/PMC123.1/PMC123.1.xml","media_urls":[]})
        result=await resolve("pmc","PMC123.1",config={"transport":httpx.MockTransport(handle),"inspect_jats":False})
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["observations"][0]["main_pdf_status"],"not_available_in_dataset")
        self.assertEqual(result["candidates"][0]["version"],"unknown")
        def malicious(request):
            return httpx.Response(200,json={"pmcid":"PMC123","version":1,"xml_url":"https://internal.example/secret"})
        with self.assertRaises(ScholarlyError) as cm:
            await resolve("pmc","PMC123.1",config={"transport":httpx.MockTransport(malicious)})
        self.assertEqual(cm.exception.code,"untrusted_artifact_origin")

    async def test_pmc_id_converter_and_version_listing_pagination(self):
        calls=[]
        def handle(request):
            calls.append(str(request.url))
            if "idconv" in request.url.path:
                return httpx.Response(200,json={"records":[{"pmcid":"PMC123","doi":"10.1234/id"}]})
            if request.url.path=="/":
                if request.url.params.get("continuation-token"):
                    return xml('<ListBucketResult><CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes><IsTruncated>false</IsTruncated></ListBucketResult>')
                return xml('<ListBucketResult><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes><IsTruncated>true</IsTruncated><NextContinuationToken>next</NextContinuationToken></ListBucketResult>')
            return httpx.Response(200,json={"pmcid":"PMC123","version":2,"doi":"10.1234/id"})
        result=await resolve("pmc","10.1234/id",config={"transport":httpx.MockTransport(handle)})
        self.assertEqual(result["available_versions"],[1,2])
        self.assertTrue(any("/tools/idconv/api/v1/articles/" in value for value in calls))

    async def test_registry_browser_only_runes_and_secret_configuration(self):
        sources={s["id"]:s for s in list_sources({"sources":{"wos":secrets()}})}
        self.assertTrue(sources["wos"]["configured"])
        self.assertFalse(sources["wos"]["live_verified"])
        self.assertFalse(sources["google_scholar"]["capabilities"]["search"])
        self.assertTrue(sources["google_scholar"]["capabilities"]["browser"])
        with self.assertRaises(ScholarlyError) as cm:
            await search("google_scholar","trial")
        self.assertEqual(cm.exception.code,"operation_unsupported")
        self.assertEqual(len(list_runes()),7)
        rune=load_rune("jama-cardiology")
        self.assertEqual(rune["context"]["first_issue_date"],"2016-04-01")
        self.assertTrue(rune["digest"].startswith("sha256:"))
        self.assertFalse(rune["verification"]["live_verified"])
        with self.assertRaises(ScholarlyError):
            load_rune("../../secret")

    async def test_redirect_and_xml_entities_are_not_followed(self):
        for response, code in [(httpx.Response(302,headers={"Location":"https://other.example/"}),"redirect_requires_review"),
            (xml('<!DOCTYPE x [<!ENTITY external SYSTEM "file:///etc/passwd">]><x>&external;</x>'),"invalid_response")]:
            with self.subTest(code=code):
                with self.assertRaises(ScholarlyError) as cm:
                    await search("kci","*",config={"transport":httpx.MockTransport(lambda req:response)})
                self.assertEqual(cm.exception.code,code)


if __name__ == "__main__":
    unittest.main()
