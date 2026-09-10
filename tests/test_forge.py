import zipfile

import pytest
from pypdf import PdfWriter

from ore.forge import Forge, ForgeError, extract_text
from ore.vault import Vault, sha256_file


def test_html_text_tables_and_derived_provenance(tmp_path):
    source = tmp_path / "report.html"
    source.write_text('<html><script>do_not_include()</script><h1>Report</h1><table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>7</td></tr></table></html>')
    before = sha256_file(source)
    result = Forge(Vault(tmp_path / "vault")).extract({"id": "original", "path": str(source)})
    assert "do_not_include" not in result["text"]
    assert result["tables"] == [[["Name", "Value"], ["A", "7"]]]
    assert result["artifact"]["source_sha256"] == before
    assert result["artifact"]["source_artifact_id"] == "original"
    assert result["table_artifact"]["relation"] == "derived_from"
    assert sha256_file(source) == before


def test_blank_pdf_is_not_successful_text_extraction(tmp_path):
    source = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    with source.open("wb") as output:
        writer.write(output)
    result = Forge(tmp_path / "vault").extract(source)
    assert result["status"] == "needs_ocr"
    assert "artifact" not in result


def test_csv_quotes_and_character_limit(tmp_path):
    source = tmp_path / "table.csv"
    source.write_text('name,value\n"A,B",123\n')
    result = extract_text(source, max_characters=5)
    assert result["tables"][0][1] == ["A,B", "123"]
    assert result["status"] == "partial"
    assert len(result["text"]) == 5


def test_xlsx_preserves_missing_column_and_shared_string(tmp_path):
    source = tmp_path / "data.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="urn:x"/>')
        archive.writestr("xl/sharedStrings.xml", '<sst xmlns="urn:x"><si><t>Label</t></si></sst>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="urn:x"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><v>3</v></c></row></sheetData></worksheet>')
    result = extract_text(source)
    assert result["tables"] == [[["Label", "", "3"]]]


def test_xml_entity_declarations_are_rejected(tmp_path):
    source = tmp_path / "data.xml"
    source.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "abc">]><x>&a;</x>')
    with pytest.raises(ForgeError, match="entity"):
        extract_text(source)


def test_unknown_binary_is_reported_without_fake_derived_text(tmp_path):
    source = tmp_path / "media.bin"
    source.write_bytes(b"\x00\xff\x81\x02")
    result = Forge(tmp_path / "vault").extract(source)
    assert result["status"] == "unsupported"
    assert "artifact" not in result
