import hashlib
import stat
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from ore.vault import UnsafeArchive, Vault, VaultError, ZipLimits, detect_media_type, inspect_zip, safe_extract_zip


def make_pdf(path, text="Study title 10.1234/example"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    lines = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in text.splitlines()]
    commands = " 0 -12 Td ".join(f"({line}) Tj" for line in lines)
    stream.set_data(f"BT /F1 10 Tf 10 280 Td {commands} ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)
    return path


def test_pdf_identity_original_preservation_and_dedup(tmp_path):
    source = make_pdf(tmp_path / "main.pdf")
    before = source.read_bytes()
    vault = Vault(tmp_path / "vault")
    result = vault.commit_file(source, {"role": "main_pdf", "title": "Study title", "doi": "10.1234/example"})
    assert result["status"] == "verified"
    assert result["sha256"] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before
    assert vault.commit_file(source, {"role": "main_pdf"})["identity"] == "unknown"
    assert len(list(vault.objects.glob("*/*"))) == 1


SPACED_TITLE = (
    "Effect of Tafamidis on Cardiac Function in Patients With Transthyretin Amyloid Cardiomyopathy: "
    "A Post Hoc Analysis of the ATTR-ACT Randomized Clinical Trial"
)
COMPACT_SUBTITLE = SPACED_TITLE.split(": ")[0] + " APostHocAnalysisoftheATTR-ACTRandomizedClinicalTrial"


@pytest.mark.parametrize("extracted_title", [COMPACT_SUBTITLE, COMPACT_SUBTITLE.replace("Cardiac", "Car diac")])
def test_pdf_identity_repairs_spacing_only_with_exact_doi_and_full_title(tmp_path, extracted_title):
    source = make_pdf(tmp_path / "main.pdf", extracted_title + " 10.1001/jamacardio.2023.4147")
    before = source.read_bytes()
    result = Vault(tmp_path / "vault").commit_file(source, {"role": "main_pdf", "title": SPACED_TITLE,
        "doi": "10.1001/jamacardio.2023.4147", "version": "submitted_manuscript"})
    assert result["status"] == "verified"
    assert result["identity_evidence"]["doi_match"] is True
    assert result["identity_evidence"]["title_match_method"] == "doi_guarded_spacing_exact"
    assert result["version"] == "submitted_manuscript"
    assert source.read_bytes() == before


@pytest.mark.parametrize("extracted_title,observed_doi,expected_doi", [
    (COMPACT_SUBTITLE, "10.9999/different", "10.1001/jamacardio.2023.4147"),
    (COMPACT_SUBTITLE, "", "10.1001/jamacardio.2023.4147"),
    (COMPACT_SUBTITLE, "10.1001/jamacardio.2023.4147", None),
    (COMPACT_SUBTITLE.replace("Tafamidis", "Placebo"), "10.1001/jamacardio.2023.4147", "10.1001/jamacardio.2023.4147"),
    (COMPACT_SUBTITLE.replace("RandomizedClinicalTrial", ""), "10.1001/jamacardio.2023.4147", "10.1001/jamacardio.2023.4147"),
    ("x " * 1100 + COMPACT_SUBTITLE, "10.1001/jamacardio.2023.4147", "10.1001/jamacardio.2023.4147"),
])
def test_pdf_identity_spacing_fallback_rejects_insufficient_evidence(tmp_path, extracted_title, observed_doi, expected_doi):
    source = make_pdf(tmp_path / "main.pdf", extracted_title + " " + observed_doi)
    result = Vault(tmp_path / "vault").commit_file(source, {"role": "main_pdf", "title": SPACED_TITLE, "doi": expected_doi})
    assert result["status"] != "verified"
    assert result["identity_evidence"]["title_match"] is False
    assert result["identity_evidence"]["title_match_method"] is None


def test_pdf_identity_spacing_fallback_does_not_accept_short_title(tmp_path):
    source = make_pdf(tmp_path / "main.pdf", "ClinicalTrial 10.1234/example")
    result = Vault(tmp_path / "vault").commit_file(source, {"role": "main_pdf", "title": "Clinical Trial", "doi": "10.1234/example"})
    assert result["status"] == "needs_review"
    assert result["identity_evidence"]["title_match"] is False


LINE_WRAPPED_TITLE = (
    "Immediate multivessel revascularization may increase cardiac death and myocardial infarction "
    "in patients with ST-elevation myocardial infarction and multivessel coronary artery disease: "
    "data analysis from real world practice"
)


@pytest.mark.parametrize("replacement,observed_doi,verified", [
    ("infarc-\ntion", "10.3904/kjim.2014.119", True),
    ("infarc- \ntion", "10.3904/kjim.2014.119", True),
    ("infarc-\ntion", "10.9999/different", False),
    ("infarc-\ntion", "", False),
    ("infarc-tion", "10.3904/kjim.2014.119", False),
    ("ische-\nmia", "10.3904/kjim.2014.119", False),
])
def test_pdf_identity_unwraps_line_hyphens_only_with_matching_doi_and_full_title(
    tmp_path, replacement, observed_doi, verified,
):
    # The published title can follow affiliations/abstract, beyond the narrow
    # spacing-repair header. A different diagnosis must not pass on DOI alone.
    extracted = LINE_WRAPPED_TITLE.replace("infarction", replacement)
    source = make_pdf(tmp_path / "wrapped.pdf", "Affiliation " * 200 + "\n" + extracted + "\n" + observed_doi)
    before = source.read_bytes()
    result = Vault(tmp_path / "vault").commit_file(source, {
        "role": "main_pdf", "title": LINE_WRAPPED_TITLE, "doi": "10.3904/kjim.2014.119",
    })
    assert (result["status"] == "verified") is verified
    assert result["identity_evidence"]["title_match"] is verified
    assert result["identity_evidence"]["title_match_method"] == (
        "doi_guarded_line_hyphen_exact" if verified else None
    )
    assert source.read_bytes() == before


def test_login_html_wrong_identity_and_truncated_pdf(tmp_path):
    vault = Vault(tmp_path / "vault")
    source = tmp_path / "main.pdf"
    source.write_text("<!doctype html><html>Institution login</html>")
    assert vault.commit_file(source, {"role": "main_pdf"})["status"] == "invalid"
    make_pdf(source, "Different paper 10.9999/wrong")
    wrong = vault.commit_file(source, {"role": "main_pdf", "doi": "10.1234/expected"})
    assert wrong["identity"] == "mismatch"
    source.write_bytes(b"%PDF-1.7\ntruncated")
    assert vault.commit_file(source, {"role": "main_pdf"})["integrity"] == "invalid"


def test_corrupt_existing_object_is_not_silently_reused(tmp_path):
    source = tmp_path / "table.csv"
    source.write_text("a,b\n1,2\n")
    vault = Vault(tmp_path / "vault")
    result = vault.commit_file(source)
    vault.blob_path(result["sha256"]).write_bytes(b"corrupt")
    with pytest.raises(VaultError, match="corrupt"):
        vault.commit_file(source)
    assert source.read_text() == "a,b\n1,2\n"


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "C:/windows.txt", "a\\..\\escape.txt"])
def test_zip_paths_cannot_escape(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, "bad")
    with pytest.raises(UnsafeArchive):
        safe_extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_zip_symlinks_and_bombs_rejected(tmp_path):
    archive = tmp_path / "link.zip"
    member = zipfile.ZipInfo("link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(member, "/etc/passwd")
    with pytest.raises(UnsafeArchive, match="links"):
        inspect_zip(archive)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("repeated.txt", "0" * 10_000)
    with pytest.raises(UnsafeArchive, match="ratio"):
        inspect_zip(archive, ZipLimits(max_ratio=2))


def test_bundle_members_preserve_hash_and_relation(tmp_path):
    archive = tmp_path / "supp.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("tables/data.csv", "x,y\n1,2\n")
        output.writestr("readme.txt", "supplementary data")
    original = archive.read_bytes()
    vault = Vault(tmp_path / "vault")
    result = vault.commit_file(archive, {"role": "supplement"})
    assert result["status"] == "verified"
    members = vault.extract_zip(archive)
    assert len(members) == 2
    assert all(item["parent_sha256"] == result["sha256"] for item in members)
    assert archive.read_bytes() == original
    assert {item["sha256"] for item in members} == {item["sha256"] for item in result["members"]}


def test_existing_extraction_target_never_overwritten(tmp_path):
    archive = tmp_path / "supp.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("item.txt", "new")
    target = tmp_path / "out"
    target.mkdir()
    (target / "item.txt").write_text("existing")
    with pytest.raises(UnsafeArchive):
        safe_extract_zip(archive, target)
    assert (target / "item.txt").read_text() == "existing"


@pytest.mark.parametrize("filename", ["paper.pdf", "data.DOCX", "data.xlsx", "slides.pptx", "supplements.zip"])
def test_declared_strong_format_rejects_html_download_response(tmp_path, filename):
    source = tmp_path / "staged-opaque-id"
    source.write_text("\n<html><head><title>Preparing to download ...</title></head><body>Please wait</body></html>")
    result = Vault(tmp_path / "vault").commit_file(source, {"role": "supplement", "filename": filename})
    assert result["status"] == "invalid"
    assert result["integrity"] == "invalid"
    assert any(issue.startswith("expected_") and issue.endswith("text/html") for issue in result["issues"])


@pytest.mark.parametrize("filename", ["supplementaryFiles", "supplements.zip"])
def test_europepmc_xml_error_is_not_a_verified_supplement(tmp_path, filename):
    source = tmp_path / "staged-opaque-id"
    source.write_bytes(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><errorBean><errCode>0</errCode><errMsg>Article with id PMC10757867 is not open access one</errMsg></errorBean>')
    result = Vault(tmp_path / "vault").commit_file(source, {"role": "supplement", "filename": filename})
    assert result["status"] == "invalid"
    assert "api_error_xml_envelope" in result["issues"]


@pytest.mark.parametrize("filename,member,media_type", [
    ("supplement.docx", "word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("supplement.xlsx", "xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("supplement.pptx", "ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("supplement.zip", "data.xml", "application/zip"),
])
def test_declared_zip_document_format_requires_matching_package(tmp_path, filename, member, media_type):
    source = tmp_path / "opaque"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member, "<?xml version='1.0'?><document/>")
    vault = Vault(tmp_path / "vault")
    result = vault.commit_file(source, {"role": "supplement", "filename": filename})
    assert result["status"] == "verified"
    assert result["media_type"] == media_type
    wrong = "wrong.xlsx" if filename.endswith("docx") else "wrong.docx"
    assert vault.commit_file(source, {"role": "supplement", "filename": wrong})["status"] == "invalid"


@pytest.mark.parametrize("filename,content", [
    ("supplement.html", "<!doctype html><html><body>Supplementary Methods</body></html>"),
    ("supplement.xml", "<?xml version='1.0'?><supplement><methods>Methods</methods></supplement>"),
    ("supplement.xml", "<?xml version='1.0'?><data><errCode>0</errCode><errMsg>Study variable labels</errMsg></data>"),
])
def test_legitimate_html_xml_supplements_remain_valid(tmp_path, filename, content):
    source = tmp_path / "opaque"
    source.write_text(content)
    result = Vault(tmp_path / "vault").commit_file(source, {"role": "supplement", "filename": filename})
    assert result["status"] == "verified"
    assert result["issues"] == []


def test_explicit_format_expectation_for_extensionless_endpoint(tmp_path):
    source = tmp_path / "opaque"
    source.write_text("<?xml version='1.0'?><response>No archive</response>")
    result = Vault(tmp_path / "vault").commit_file(source, {"role": "supplement", "filename": "supplementaryFiles", "media_type": "application/zip"})
    assert result["status"] == "invalid"
    assert "expected_zip_received_application/xml" in result["issues"]


def test_trusted_archive_member_limits_preserve_total_bound_and_original_bytes(tmp_path):
    archive = tmp_path / 'videos.zip'
    payloads = {'movie1.avi': b'a' * 96, 'movie2.avi': b'b' * 96}
    with zipfile.ZipFile(archive, 'w') as output:
        for name, payload in payloads.items():output.writestr(name, payload)
    original = archive.read_bytes()
    restricted = Vault(tmp_path / 'restricted', zip_limits=ZipLimits(max_member_bytes=64, max_total_bytes=256))
    result = restricted.commit_file(archive, {'role': 'supplement', 'zip_limits': {'max_member_bytes': 999_999}})
    assert result['status'] == 'invalid'
    assert result['issues'] == ['unsafe_or_invalid_zip:archive expanded member size exceeds limit (96 > 64)']
    assert result['archive_limits']['max_member_bytes'] == 64
    with pytest.raises(UnsafeArchive, match='member size'):
        restricted.extract_zip(archive)
    sufficient = Vault(tmp_path / 'sufficient', zip_limits=ZipLimits(max_member_bytes=96, max_total_bytes=192))
    verified = sufficient.commit_file(archive, {'role': 'supplement'})
    assert verified['status'] == 'verified' and verified['sha256'] == hashlib.sha256(original).hexdigest()
    assert {item['name']: item['sha256'] for item in verified['members']} == {
        name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}
    assert sum(item['bytes'] for item in verified['members']) == 192
    assert verified['archive_limits']['max_member_bytes'] == 96
    assert verified['archive_limits']['max_total_bytes'] == 192
    total_limited = Vault(tmp_path / 'total', zip_limits=ZipLimits(max_member_bytes=128, max_total_bytes=191))
    rejected = total_limited.commit_file(archive, {'role': 'supplement'})
    assert rejected['status'] == 'invalid'
    assert rejected['issues'] == ['unsafe_or_invalid_zip:archive expanded total size exceeds limit (192 > 191)']
    assert archive.read_bytes() == original


def test_archive_default_accepts_large_members_with_unchanged_aggregate_bound(tmp_path):
    limits = Vault(tmp_path / 'vault').zip_limits
    assert limits.max_member_bytes == 512 * 1024 * 1024
    assert limits.max_total_bytes == 1024 * 1024 * 1024
    assert limits.max_ratio == 1000 and limits.max_members == 10_000 and limits.max_depth == 20


@pytest.mark.parametrize('failure', ['crc', 'path', 'ratio', 'count'])
def test_custom_member_limit_does_not_disable_other_archive_checks(tmp_path, failure):
    archive = tmp_path / 'supp.zip'
    name = '../movie.avi' if failure == 'path' else 'movie.avi'
    payload = b'unchanged CRC fixture' if failure == 'crc' else b'x' * 100
    compression = zipfile.ZIP_DEFLATED if failure == 'ratio' else zipfile.ZIP_STORED
    with zipfile.ZipFile(archive, 'w', compression) as output:
        output.writestr(name, payload)
        if failure == 'count':output.writestr('second.avi', 'second')
    if failure == 'crc':
        data = archive.read_bytes()
        assert data.count(payload) == 1
        archive.write_bytes(data.replace(payload, b'X' + payload[1:], 1))
    limits = ZipLimits(max_member_bytes=512, max_total_bytes=1024, max_ratio=2, max_members=1)
    result = Vault(tmp_path / 'vault', zip_limits=limits).commit_file(archive, {'role': 'supplement'})
    assert result['status'] == 'invalid'
    reason = {'crc': 'CRC', 'path': 'escapes', 'ratio': 'ratio', 'count': 'count'}[failure]
    assert any(reason in issue for issue in result['issues'])
    assert 'members' not in result


def test_stored_zip_with_pdf_first_member_is_verified_as_outer_archive(tmp_path):
    pdf = make_pdf(tmp_path / 'main.pdf')
    bundle = tmp_path / 'downloads.zip'
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_STORED) as output:
        output.write(pdf, 'article/main.pdf')
        output.writestr('manifest.csv', 'doi,file\n10.1234/example,article/main.pdf\n')
    before = bundle.read_bytes()
    assert before.startswith(b'PK\x03\x04') and b'%PDF-' in before[:1024]
    assert detect_media_type(bundle) == 'application/zip'
    result = Vault(tmp_path / 'vault').commit_file(bundle, {'role': 'collection_bundle', 'filename': bundle.name})
    assert result['status'] == 'verified' and result['media_type'] == 'application/zip'
    assert result['sha256'] == hashlib.sha256(before).hexdigest()
    assert {member['name'] for member in result['members']} == {'article/main.pdf', 'manifest.csv'}
    assert next(member for member in result['members'] if member['name'] == 'article/main.pdf')['sha256'] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert bundle.read_bytes() == before


@pytest.mark.parametrize('prefix', [b'', b'\n ', b'allowed PDF prefix\n'])
def test_pdf_detection_keeps_nonarchive_prefix_support(tmp_path, prefix):
    pdf = make_pdf(tmp_path / 'main.pdf')
    pdf.write_bytes(prefix + pdf.read_bytes())
    assert detect_media_type(pdf) == 'application/pdf'


def test_pdf_with_appended_zip_keeps_its_leading_pdf_format(tmp_path):
    pdf = make_pdf(tmp_path / 'main.pdf')
    with zipfile.ZipFile(pdf, 'a', zipfile.ZIP_STORED) as archive:
        archive.writestr('metadata.txt', 'embedded appendix')
    assert pdf.read_bytes().startswith(b'%PDF-') and zipfile.is_zipfile(pdf)
    assert detect_media_type(pdf) == 'application/pdf'


def test_invalid_zip_signature_with_embedded_pdf_is_not_verified(tmp_path):
    source = tmp_path / 'invalid.zip'
    source.write_bytes(b'PK\x03\x04' + b'%PDF-1.4\nInvalid container')
    assert detect_media_type(source) == 'application/zip'
    result = Vault(tmp_path / 'vault').commit_file(source, {'role': 'collection_bundle'})
    assert result['status'] == 'invalid'
    assert any(issue.startswith('unsafe_or_invalid_zip:') for issue in result['issues'])
