# Actual 2024 journal test — 2026-09-11

**Partial file retrieval passed; complete official-issue collection did not pass.**

Four real Astra/high issue jobs attempted JACC 83(1), Circulation 149(1), EHJ 45(1), and JAMA Cardiology 9(1). None sealed the official issue TOC or downloaded a file through the systematic issue job. Separately scoped original-article samples and explicitly identified operator-directed ORE route probes retained these actual files:

| Journal / issue | Main PDFs | Supplement originals | Main version boundary |
| --- | ---: | ---: | --- |
| JACC 83(1) | 1 | 0 | Accepted manuscript; publisher final unverified |
| Circulation 149(1) | 1 | 1 PDF | Explicit PMC JATS Version of Record; correction incorporation unknown |
| EHJ 45(1) | 1 | 1 DOCX | Repository manuscript assertion retained |
| JAMA Cardiology 9(1) | 1 | 3 PDFs | Repository manuscript assertion retained |

The export contains **9 unique originals, 8,400,320 bytes: eight PDFs and one DOCX**. Each file was revalidated in the isolated parser; SHA-256, size and the ZIP member hashes were independently checked. Main PDFs pass DOI/title identity. Supplement parent relationships come from actual publisher links or PMC JATS, not from a DOI/title expectation inside the attachment. XML, derived text, duplicate records, challenge pages and API errors are excluded.

JACC's retained paper is DOI `10.1016/j.jacc.2023.10.026`; its initial alternate DOI `10.1016/j.jacc.2023.10.029` yielded no valid original files. The other samples are Circulation `10.1161/circulationaha.123.066680`, EHJ `10.1093/eurheartj/ehad431`, and JAMA `10.1001/jamacardio.2023.4147`. The JACC main and EHJ publisher DOCX were obtained by operator-directed ORE requests after the LLM path failed or missed a route. They are not evidence of unattended end-to-end success.

## User-authorized challenge retry

The user explicitly requested a reset. ORE's existing audited operator budget-renewal method retained the three historical attempts and granted three additional attempts plus 120 seconds for each origin/authentication context. No actor automatically reset counters or represented the renewal as a solved challenge. Actual Astra screenshot-guided checkbox clicks occurred three times per journal after that authorization. All four ended with the challenge still visible and no attempts remaining.

The first renewed attempt recorded an ORE scope denial for `https://brunhild.challenges.cloudflare.com`; that exact origin was admitted for support requests. Two remaining attempts per journal still failed. A later review corrected the initial causal interpretation: Cloudflare documents failed lookups on strict challenge subdomains as expected probes, so this observation does not establish a missing required dependency or explain the challenge loop. The original apex support-script scope defect is separate. Navigation/download/profile restrictions remain enforced. See [the current browser diagnosis](browser-repair-2026-09-11.md).

## Defects exposed and fixed

- An exact support-origin denial was removed; the later subdomain DNS failure is an expected probe and does not establish the cause of the remaining challenge loop.
- PDF text extraction removed spaces from the JAMA subtitle. A long, complete whitespace-insensitive title match now requires an exact first-page DOI and records its matching method.
- PMC JATS `sec-type="supplementary-material"` was missed; explicitly typed sections now establish supplement relationships. Explicit publication-state labels are distinguished from numeric dataset/JATS versions, and correction incorporation remains unknown.
- A resolver candidate ID passed without a sealed requirement ID produced an internal error. Invalid pairs now fail early with an actionable message.
- Declared PDF/Office/ZIP filenames were not passed to the validator. Known formats are checked against actual bytes; a known structured API error envelope is rejected. The actual EHJ DOCX passes, while the actual 1,817-byte download HTML and 165-byte API error XML fail. Historical download records are preserved, with separate revalidation evidence.

## Evidence and limits

Local report: `.ore/reports/real-2024-journal-test.json`; downloadable bundle: `.ore/exports/real-2024-journals.zip`; per-file evidence: `.ore/exports/real-2024-journals/manifest.json`. Raw reports remain local and are excluded from distributions. Final regression: **274 tests + 6 subtests passed**, 80.16 seconds, two dependency warnings. Local wheels/source archives/npm tarball were rebuilt and clean-install checked; nothing was published to a registry.

The four-journal release acceptance gate remains unmet. The exact whole-issue original denominator, all publisher attachments, and final-version coverage are unverified. JACC's known supplement remains missing. No 20-year collection or automatic challenge success is claimed.
