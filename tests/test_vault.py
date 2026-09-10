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
