import hashlib
import stat
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from ore.vault import UnsafeArchive, Vault, VaultError, ZipLimits, inspect_zip, safe_extract_zip


def make_pdf(path, text="Study title 10.1234/example"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 10 280 Td ({text}) Tj ET".encode())
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
