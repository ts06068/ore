"""Immutable original-byte storage and bounded format/identity verification."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pypdf import PdfReader
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException


STRONG_FORMATS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
}


def _known_api_error_xml(path: Path, count: int) -> bool:
    # Europe PMC returns this explicit error envelope with HTTP 200. Keep this
    # bounded and structural; ordinary HTML/XML supplementary files stay valid.
    if count > 64 * 1024:
        return False
    try:
        root = SafeET.fromstring(path.read_bytes())
    except (SafeET.ParseError, DefusedXmlException):
        return False
    local = lambda tag: tag.rsplit("}", 1)[-1]
    fields = {local(child.tag): child for child in root}
    return (local(root.tag) == "errorBean" and "errCode" in fields and "errMsg" in fields
            and bool("".join(fields["errMsg"].itertext()).strip()))


class VaultError(RuntimeError):
    pass


class UnsafeArchive(VaultError):
    pass


@dataclass(frozen=True)
class ZipLimits:
    max_members: int = 10_000
    max_member_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_ratio: float = 1000
    max_depth: int = 20


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _member_path(name: str, limits: ZipLimits) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name or re.match(r"^[a-zA-Z]:", name):
        raise UnsafeArchive(f"unsafe archive member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or len(path.parts) > limits.max_depth:
        raise UnsafeArchive(f"archive path escapes its root or depth limit: {name!r}")
    if not path.parts:
        raise UnsafeArchive("empty archive member path")
    return path


def inspect_zip(path: str | Path, limits: ZipLimits | None = None) -> list[dict[str, Any]]:
    """Validate every path and size before decompression, then verify each CRC."""
    limits = limits or ZipLimits()
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > limits.max_members:
            raise UnsafeArchive("archive member count exceeds limit")
        total, seen, files = 0, set(), set()
        for member in members:
            parsed = _member_path(member.filename, limits)
            name = str(parsed)
            if name in seen:
                raise UnsafeArchive("duplicate normalized archive member")
            seen.add(name)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise UnsafeArchive("archive links and special files are not supported")
            if member.flag_bits & 1:
                raise UnsafeArchive("encrypted archive cannot be safely inspected")
            if not member.is_dir():
                files.add(name)
            total += member.file_size
            if member.file_size > limits.max_member_bytes or total > limits.max_total_bytes:
                raise UnsafeArchive("archive expanded size exceeds limit")
            if member.file_size and member.file_size / max(member.compress_size, 1) > limits.max_ratio:
                raise UnsafeArchive("archive compression ratio exceeds limit")
        for name in seen:
            if any(str(parent) in files for parent in PurePosixPath(name).parents if str(parent) != "."):
                raise UnsafeArchive("archive file shadows a parent directory")
        result = []
        for member in members:
            if member.is_dir():
                continue
            digest, count = hashlib.sha256(), 0
            with archive.open(member) as stream:
                while block := stream.read(min(1024 * 1024, limits.max_member_bytes + 1)):
                    count += len(block)
                    if count > member.file_size or count > limits.max_member_bytes:
                        raise UnsafeArchive("archive decompression exceeds declared size")
                    digest.update(block)
            if count != member.file_size:
                raise UnsafeArchive("archive member size mismatch")
            result.append({"name": member.filename, "bytes": count, "sha256": digest.hexdigest()})
        return result


def safe_extract_zip(path: str | Path, destination: str | Path, limits: ZipLimits | None = None) -> list[dict[str, Any]]:
    limits = limits or ZipLimits()
    members = inspect_zip(path, limits)
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for member in members:
        target = root.joinpath(*PurePosixPath(member["name"]).parts)
        if not target.resolve().is_relative_to(root):
            raise UnsafeArchive("existing output path escapes extraction root")
        if target.exists() or target.is_symlink():
            raise UnsafeArchive("refusing to overwrite an existing extraction target")
        for parent in target.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise UnsafeArchive("existing output parent is a symlink")
    written = []
    try:
        with zipfile.ZipFile(path) as archive:
            for member in members:
                target = root.joinpath(*PurePosixPath(member["name"]).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                # O_EXCL and O_NOFOLLOW protect the final component from replacement.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(target, flags, 0o600)
                written.append(target)
                with os.fdopen(fd, "wb") as output, archive.open(member["name"]) as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                if sha256_file(target) != member["sha256"]:
                    raise UnsafeArchive("archive changed between validation and extraction")
                member["path"] = str(target)
        return members
    except BaseException:
        for target in written:
            target.unlink(missing_ok=True)
        raise


def detect_media_type(path: str | Path, filename: str | None = None) -> str:
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.read(4096)
    lower = head.lstrip().lower()
    if b"%PDF-" in head[:1024]:
        return "application/pdf"
    if lower.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "text/html"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
            if "word/document.xml" in names:
                return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if "xl/workbook.xml" in names:
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if "ppt/presentation.xml" in names:
                return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        except zipfile.BadZipFile:
            pass
        return "application/zip"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if lower.startswith(b"<?xml"):
        return "application/xml"
    guessed = mimetypes.guess_type(filename or path.name)[0]
    if guessed and guessed not in ("application/pdf", "application/zip"):
        return guessed
    try:
        head.decode("utf-8")
        return "text/plain" if b"\x00" not in head else "application/octet-stream"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _normal(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _pdf_identity(reader: PdfReader, expected: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    title, doi = str(expected.get("title") or ""), str(expected.get("doi") or "").strip().lower()
    doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:").strip()
    if not title and not doi:
        return ("unknown" if expected.get("role") in ("main", "main_pdf") or expected.get("require_identity") else "not_required"), {}
    front = reader.pages[0].extract_text() or "" if reader.pages else ""
    metadata_title = str((reader.metadata or {}).get("/Title", ""))
    normalized = _normal(front[:12_000] + " " + metadata_title)
    normalized_title = _normal(title)
    title_match = bool(title and len(normalized_title) >= 8 and normalized_title in normalized)
    title_match_method = "normalized_exact" if title_match else None
    observed_dois = [item.rstrip(".,;)") for item in re.findall(r"10\.\d{4,9}/[^\s<>\"\]]+", front.lower())]
    doi_match = bool(doi and doi in observed_dois)
    # PDF positioning can omit word spaces or insert them inside words. Permit
    # only an exact complete long title in the first-page header, independently
    # anchored by the expected DOI; do not use fuzzy or partial-title matching.
    compact_title = normalized_title.replace(" ", "")
    if (not title_match and doi_match and len(compact_title) >= 40
            and len(normalized_title.split()) >= 6
            and compact_title in _normal(front[:2_000]).replace(" ", "")):
        title_match = True
        title_match_method = "doi_guarded_spacing_exact"
    # Typeset words can wrap with a discretionary hyphen later on the first
    # page, after a column of affiliations or the abstract. Remove only a
    # letter-to-letter hyphen at a real line break, then require the complete
    # normalized title and an independently matching DOI. Inline hyphens and
    # arbitrary spacing keep their existing meaning.
    unwrapped_front = re.sub(r"(?<=[^\W\d_])-[ \t]*(?:\r\n|\n|\r)[ \t]*(?=[^\W\d_])", "", front[:12_000])
    if (not title_match and doi_match and len(compact_title) >= 40
            and len(normalized_title.split()) >= 6
            and unwrapped_front != front[:12_000]
            and normalized_title in _normal(unwrapped_front)):
        title_match = True
        title_match_method = "doi_guarded_line_hyphen_exact"
    evidence = {"method": "first_page_and_pdf_metadata", "title_match": title_match,
                "title_match_method": title_match_method,
                "doi_match": doi_match, "observed_dois": observed_dois[:20]}
    if title_match and (not doi or doi_match or not observed_dois):
        return "verified", evidence
    if doi_match and not title:
        return "verified", evidence
    if doi and observed_dois and not doi_match and not title_match:
        return "mismatch", evidence
    return "unknown", evidence


class Vault:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.objects = self.root / "objects"
        self.staging = self.root / "staging"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    def blob_path(self, sha256: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("invalid SHA-256")
        return self.objects / sha256[:2] / sha256

    def commit_file(self, path: str | Path, expected: dict[str, Any] | None = None) -> dict[str, Any]:
        expected = dict(expected or {})
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise VaultError("input must be a regular, non-symlink file")
        digest, count, issues = hashlib.sha256(), 0, []
        before = source.stat()
        descriptor, temporary = tempfile.mkstemp(prefix="snapshot-", dir=self.staging)
        snapshot = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as incoming:
                while block := incoming.read(1024 * 1024):
                    count += len(block)
                    if expected.get("max_bytes") is not None and count > expected["max_bytes"]:
                        raise VaultError("file exceeds allowed byte budget")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                issues.append("source_changed_during_snapshot")
            sha256 = digest.hexdigest()
            media_type = detect_media_type(snapshot, expected.get("filename") or source.name)
            integrity, identity, evidence = "verified", "not_required", {}
            members = None
            if not count:
                issues.append("empty_file")
            if expected.get("sha256") and expected["sha256"].lower() != sha256:
                issues.append("checksum_mismatch")
            if expected.get("expected_bytes") is not None and expected["expected_bytes"] != count:
                issues.append("size_mismatch")
            wants_pdf = expected.get("role") in ("main", "main_pdf") or expected.get("media_type") == "application/pdf"
            if wants_pdf and media_type != "application/pdf":
                issues.append("expected_pdf_received_" + media_type)
                identity = "unknown"
            declared_format = STRONG_FORMATS.get(Path(expected.get("filename") or source.name).suffix.lower())
            required_formats = {declared_format, expected.get("media_type")} & set(STRONG_FORMATS.values())
            for required in sorted(required_formats):
                # OOXML documents are valid ZIP containers too; their more
                # specific extensions still require the matching document type.
                compatible = media_type == required or (required == "application/zip"
                    and media_type in set(STRONG_FORMATS.values()) - {"application/pdf"})
                if not compatible:
                    label = next(ext[1:] for ext, value in STRONG_FORMATS.items() if value == required)
                    issue = "expected_" + label + "_received_" + media_type
                    if issue not in issues:
                        issues.append(issue)
            if media_type in ("application/xml", "text/xml", "text/plain") and _known_api_error_xml(snapshot, count):
                issues.append("api_error_xml_envelope")
            if media_type == "application/pdf":
                try:
                    with snapshot.open("rb") as stream:
                        stream.seek(max(0, count - 2048))
                        if b"%%EOF" not in stream.read():
                            issues.append("missing_pdf_eof")
                    reader = PdfReader(snapshot, strict=False)
                    if reader.is_encrypted and not reader.decrypt(""):
                        integrity, identity = "unverified", "unknown"
                        issues.append("encrypted_pdf")
                    else:
                        if not len(reader.pages):
                            issues.append("pdf_has_no_pages")
                        identity, evidence = _pdf_identity(reader, expected)
                except Exception as exc:
                    issues.append("invalid_pdf:" + type(exc).__name__)
            elif media_type == "application/zip" or "officedocument" in media_type:
                try:
                    members = inspect_zip(snapshot)
                except (UnsafeArchive, zipfile.BadZipFile, RuntimeError, OSError) as exc:
                    issues.append("unsafe_or_invalid_zip:" + str(exc))
            elif expected.get("require_identity") or expected.get("doi") or expected.get("title"):
                identity = "unknown"
            hard_issues = [item for item in issues if item != "encrypted_pdf"]
            if hard_issues:
                integrity = "invalid"
            status = "invalid" if integrity == "invalid" or identity == "mismatch" else (
                "verified" if integrity == "verified" and identity in ("verified", "not_required") else "needs_review")
            destination = self.blob_path(sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(snapshot, destination)
            except FileExistsError:
                if destination.is_symlink() or sha256_file(destination) != sha256:
                    raise VaultError("existing content-addressed object is corrupt")
            result = {"sha256": sha256, "path": str(destination), "media_type": media_type, "bytes": count,
                      "integrity": integrity, "identity": identity, "status": status, "issues": issues,
                      "identity_evidence": evidence, "original_name": expected.get("filename") or source.name}
            for key in ("role", "version", "resource_id", "source_url"):
                if key in expected:
                    result[key] = expected[key]
            if members is not None:
                result["members"] = members
            return result
        finally:
            snapshot.unlink(missing_ok=True)

    def extract_zip(self, path: str | Path, *, parent_sha256: str | None = None, limits: ZipLimits | None = None):
        parent_sha256 = parent_sha256 or sha256_file(path)
        with tempfile.TemporaryDirectory(prefix="archive-", dir=self.staging) as directory:
            members = safe_extract_zip(path, directory, limits)
            return [{**self.commit_file(member["path"], {"role": "supplement_member", "filename": member["name"]}),
                     "parent_sha256": parent_sha256, "member_name": member["name"]} for member in members]
