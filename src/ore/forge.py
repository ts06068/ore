"""Bounded derived text/table extraction; original artifacts remain untouched."""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pypdf import PdfReader, __version__ as pypdf_version

from .vault import Vault, ZipLimits, detect_media_type, inspect_zip, sha256_file


class ForgeError(RuntimeError):
    pass


def _xml(payload: bytes):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ForgeError("XML DTDs and entity declarations are not supported")
    return ET.fromstring(payload)


def _xml_text(element):
    return " ".join(text.strip() for text in element.itertext() if text.strip())


def capabilities() -> dict[str, Any]:
    ocr_python = importlib.util.find_spec("pytesseract") is not None and importlib.util.find_spec("PIL") is not None
    return {"pdf_text": True, "html": True, "csv": True, "json": True, "xml": True,
            "docx": True, "xlsx": True, "pptx": True,
            "image_ocr": bool(ocr_python and shutil.which("tesseract")),
            "pdf_ocr": bool(ocr_python and shutil.which("tesseract") and shutil.which("pdftoppm"))}


def _ocr_image(path: Path, language: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_+]+", language):
        raise ForgeError("invalid OCR language code")
    if not capabilities()["image_ocr"]:
        raise ForgeError("OCR requires the ore-engine[ocr] dependencies and the tesseract executable")
    from PIL import Image
    import pytesseract
    with Image.open(path) as image:
        return pytesseract.image_to_string(image, lang=language, timeout=60)


def extract_text(path: str | Path, *, media_type: str | None = None, ocr: bool = False,
                 language: str = "eng", max_pages: int = 1000,
                 max_characters: int = 5_000_000, max_input_bytes: int = 256 * 1024 * 1024) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file() or max_pages < 1 or max_characters < 1:
        raise ForgeError("regular source file and positive extraction limits are required")
    if path.stat().st_size > max_input_bytes:
        raise ForgeError("source exceeds extraction input limit")
    kind = media_type or detect_media_type(path)
    warnings, tables, pages = [], [], []
    text_value = ""
    status = "extracted"
    if kind == "application/pdf":
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ForgeError("encrypted PDF requires an authorized decrypted input")
        page_count = len(reader.pages)
        for page in reader.pages[:max_pages]:
            pages.append(page.extract_text() or "")
        if page_count > max_pages:
            warnings.append("page_limit_reached")
        if ocr and any(not item.strip() for item in pages):
            if not capabilities()["pdf_ocr"]:
                warnings.append("pdf_ocr_unavailable")
            else:
                with tempfile.TemporaryDirectory(prefix="ore-ocr-") as directory:
                    prefix = str(Path(directory) / "page")
                    subprocess.run([shutil.which("pdftoppm"), "-f", "1", "-l", str(min(page_count, max_pages)),
                                    "-r", "150", "-png", str(path), prefix],
                                   check=True, capture_output=True, timeout=300)
                    images = sorted(Path(directory).glob("page-*.png"),
                                    key=lambda item: int(item.stem.rsplit("-", 1)[1]))
                    for index, image in enumerate(images):
                        if index < len(pages) and not pages[index].strip():
                            pages[index] = _ocr_image(image, language)
        if any(not item.strip() for item in pages):
            warnings.append("pages_without_extractable_text")
        text_value = "\n\n".join(pages)
        if not text_value.strip():
            status = "needs_ocr"
    elif kind == "text/html":
        soup = BeautifulSoup(path.read_bytes(), "html.parser")
        for item in soup(["script", "style", "noscript", "template"]):
            item.decompose()
        for table in soup.find_all("table"):
            rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                    for row in table.find_all("tr")]
            tables.append(rows)
        text_value = soup.get_text("\n", strip=True)
    elif kind in ("text/csv", "text/tab-separated-values"):
        payload = path.read_text(encoding="utf-8-sig")
        delimiter = "\t" if kind == "text/tab-separated-values" else ","
        rows = list(csv.reader(io.StringIO(payload), delimiter=delimiter))
        tables.append(rows)
        text_value = "\n".join("\t".join(row) for row in rows)
    elif kind == "application/json":
        text_value = json.dumps(json.loads(path.read_text(encoding="utf-8-sig")), ensure_ascii=False, indent=2)
    elif kind in ("application/xml", "text/xml"):
        text_value = _xml_text(_xml(path.read_bytes()))
    elif "officedocument" in kind:
        inspect_zip(path, ZipLimits(max_member_bytes=min(max_input_bytes, 64 * 1024 * 1024), max_total_bytes=max_input_bytes))
        with zipfile.ZipFile(path) as archive:
            if "word/document.xml" in archive.namelist():
                root = _xml(archive.read("word/document.xml"))
                paragraphs = ["".join(node.itertext()) for node in root.iter() if node.tag.endswith("}p")]
                text_value = "\n".join(paragraphs)
                for table in root.iter():
                    if table.tag.endswith("}tbl"):
                        tables.append([[" ".join("".join(p.itertext()) for p in cell if p.tag.endswith("}p"))
                                        for cell in row if cell.tag.endswith("}tc")]
                                       for row in table if row.tag.endswith("}tr")])
            elif "xl/workbook.xml" in archive.namelist():
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    shared = [_xml_text(item) for item in _xml(archive.read("xl/sharedStrings.xml"))]
                sheets = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
                expanded_cells = 0
                for sheet in sheets[:max_pages]:
                    rows = []
                    for row in _xml(archive.read(sheet)).iter():
                        if not row.tag.endswith("}row"):
                            continue
                        cells = []
                        for cell in row:
                            if not cell.tag.endswith("}c"):
                                continue
                            reference = cell.attrib.get("r", "")
                            letters = re.match(r"[A-Z]+", reference)
                            index = 0
                            if letters:
                                for letter in letters.group():
                                    index = index * 26 + ord(letter) - ord("A") + 1
                                index -= 1
                            else:
                                index = len(cells)
                            if index > 16383:
                                raise ForgeError("spreadsheet cell outside supported column range")
                            expanded_cells += max(0, index + 1 - len(cells))
                            if expanded_cells > 1_000_000:
                                raise ForgeError("spreadsheet expanded cell limit exceeded")
                            while len(cells) <= index:
                                cells.append("")
                            raw = ""
                            for element in cell:
                                if element.tag.endswith("}v"):
                                    raw = element.text or ""
                                elif element.tag.endswith("}is"):
                                    raw = _xml_text(element)
                            if cell.attrib.get("t") == "s":
                                raw = shared[int(raw)] if raw else ""
                            cells[index] = raw
                        rows.append(cells)
                    tables.append(rows)
                text_value = "\n\n".join("\n".join("\t".join(row) for row in table) for table in tables)
                if len(sheets) > max_pages:
                    warnings.append("sheet_limit_reached")
            elif "ppt/presentation.xml" in archive.namelist():
                slides = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                                key=lambda name: int(re.search(r"slide(\d+)\.xml", name).group(1)))
                pages = [_xml_text(_xml(archive.read(name))) for name in slides[:max_pages]]
                text_value = "\n\n".join(pages)
                if len(slides) > max_pages:
                    warnings.append("slide_limit_reached")
            else:
                raise ForgeError("unrecognized Office document")
    elif kind.startswith("image/") and ocr:
        text_value = _ocr_image(path, language)
    elif kind.startswith("text/"):
        text_value = path.read_text(encoding="utf-8-sig")
    else:
        return {"status": "unsupported", "media_type": kind, "text": "", "tables": [], "pages": [],
                "warnings": ["no_extractor_for_media_type"], "source_sha256": sha256_file(path)}
    if len(text_value) > max_characters:
        text_value = text_value[:max_characters]
        warnings.append("character_limit_reached")
    if any(item.endswith("limit_reached") for item in warnings):
        status = "partial"
    elif status == "extracted" and "pages_without_extractable_text" in warnings:
        status = "partial"
    return {"status": status, "media_type": kind, "text": text_value, "tables": tables,
            "pages": pages, "warnings": warnings, "source_sha256": sha256_file(path),
            "extractor": {"name": "ore.forge", "version": "0.1.0", "pypdf": pypdf_version}}


class Forge:
    def __init__(self, vault: Vault | str | Path):
        self.vault = vault if isinstance(vault, Vault) else Vault(vault)

    @staticmethod
    def capabilities():
        return capabilities()

    def extract(self, source: dict[str, Any] | str | Path, **options) -> dict[str, Any]:
        artifact = source if isinstance(source, dict) else {"path": str(source)}
        if "media_type" not in options and artifact.get("media_type"):
            options["media_type"] = artifact["media_type"]
        result = extract_text(artifact["path"], **options)
        if result["status"] == "unsupported" or not result["text"]:
            return result
        with tempfile.TemporaryDirectory(prefix="forge-", dir=self.vault.staging) as directory:
            output = Path(directory) / "extracted.txt"
            output.write_text(result["text"], encoding="utf-8")
            derived = self.vault.commit_file(output, {"role": "extracted_text", "filename": "extracted.txt"})
            derived.update(source_sha256=result["source_sha256"], source_artifact_id=artifact.get("id"),
                           extraction_status=result["status"], extractor=result.get("extractor"),
                           relation="derived_from")
            result["artifact"] = derived
            if result["tables"]:
                table_file = Path(directory) / "tables.json"
                table_file.write_text(json.dumps(result["tables"], ensure_ascii=False), encoding="utf-8")
                table_artifact = self.vault.commit_file(table_file, {"role": "extracted_tables", "filename": "tables.json"})
                table_artifact.update(source_sha256=result["source_sha256"], relation="derived_from")
                result["table_artifact"] = table_artifact
        return result

    derive = extract


# Compatibility for callers that request extraction without committing derivatives.
extract_file = extract_text
