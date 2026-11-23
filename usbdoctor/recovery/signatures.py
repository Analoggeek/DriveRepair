"""File-signature ("magic") detection used by content carving.

Each entry knows how to find the *end* of a file as well as its start, because
carving without an end condition produces megabytes of junk per photo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

#: Bytes that strongly indicate a header. Ordered longest-first per type.
JPEG_START = b"\xff\xd8\xff"
PNG_START = b"\x89PNG\r\n\x1a\n"
GIF87 = b"GIF87a"
GIF89 = b"GIF89a"
PDF_START = b"%PDF-"
ZIP_START = b"PK\x03\x04"
OLE_START = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc/.xls/.ppt
RIFF = b"RIFF"
FLAC = b"fLaC"
OGG = b"OggS"
MP3_ID3 = b"ID3"
MP4_FTYP = b"ftyp"
SEVENZ = b"7z\xbc\xaf\x27\x1c"
RAR4 = b"Rar!\x1a\x07\x00"
RAR5 = b"Rar!\x1a\x07\x01\x00"
GZIP = b"\x1f\x8b"
SQLITE = b"SQLite format 3\x00"
MP4_ATOMS = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid"}


@dataclass(frozen=True)
class Signature:
    key: str
    label: str
    category: str  # image | video | audio | document | archive | database
    extensions: tuple[str, ...]
    headers: tuple[bytes, ...]
    max_size: int
    #: Smallest plausible file.  Deliberately low: a 100-byte JPEG thumbnail or a
    #: 300-byte PDF is a real file, and an aggressive floor silently discards
    #: recoverable data.  Only structurally impossible lengths are filtered.
    min_size: int = 24
    #: Optional terminator search; returns absolute end offset or None.
    find_end: Optional[Callable[[bytes, int], Optional[int]]] = None


def _jpeg_end(buf: bytes, start: int) -> Optional[int]:
    idx = buf.find(b"\xff\xd9", start)
    return idx + 2 if idx >= 0 else None


def _png_end(buf: bytes, start: int) -> Optional[int]:
    idx = buf.find(b"IEND", start)
    return idx + 8 if idx >= 0 else None


def _pdf_end(buf: bytes, start: int) -> Optional[int]:
    idx = buf.rfind(b"%%EOF", start)
    return idx + 5 if idx >= 0 else None


def _zip_end(buf: bytes, start: int) -> Optional[int]:
    idx = buf.find(b"PK\x05\x06", start)
    if idx < 0:
        return None
    end_idx = idx + 22
    if len(buf) >= end_idx:
        import struct

        comment_len = struct.unpack("<H", buf[idx + 20 : idx + 22])[0]
        end_idx += comment_len
    return end_idx


def _gif_end(buf: bytes, start: int) -> Optional[int]:
    idx = buf.find(b"\x3b", start)
    return idx + 1 if idx >= 0 else None


SIGNATURES: tuple[Signature, ...] = (
    Signature("jpeg", "JPEG image", "image", (".jpg", ".jpeg"), (JPEG_START,), 64 << 20, 64, _jpeg_end),
    Signature("png", "PNG image", "image", (".png",), (PNG_START,), 64 << 20, 64, _png_end),
    Signature("gif", "GIF image", "image", (".gif",), (GIF87, GIF89), 32 << 20, 32, _gif_end),
    Signature("pdf", "PDF document", "document", (".pdf",), (PDF_START,), 256 << 20, 64, _pdf_end),
    Signature("zip", "ZIP archive", "archive", (".zip",), (ZIP_START,), 512 << 20, 64, _zip_end),
    Signature("office", "Office document", "document", (".doc", ".xls", ".ppt"), (OLE_START,), 256 << 20, 256),
    Signature("sevenzip", "7-Zip archive", "archive", (".7z",), (SEVENZ,), 512 << 20, 32),
    Signature("rar", "RAR archive", "archive", (".rar",), (RAR4, RAR5), 512 << 20, 32),
    Signature("gzip", "GZip archive", "archive", (".gz",), (GZIP,), 128 << 20, 32),
    Signature("flac", "FLAC audio", "audio", (".flac",), (FLAC,), 256 << 20, 32),
    Signature("ogg", "OGG audio", "audio", (".ogg", ".oga"), (OGG,), 128 << 20, 32),
    Signature("mp3", "MP3 audio", "audio", (".mp3",), (MP3_ID3,), 64 << 20, 128),
    Signature("sqlite", "SQLite database", "database", (".db", ".sqlite"), (SQLITE,), 512 << 20, 100),
)

#: Maximum bytes searched for a terminator after a header.
END_SEARCH_WINDOW = 4 << 20


def find_signatures(chunk: bytes, *, category_filter: Optional[set[str]] = None) -> list[tuple[int, Signature]]:
    """Locate every known header inside ``chunk``."""
    found: list[tuple[int, Signature]] = []
    seen_offsets: set[int] = set()
    for sig in SIGNATURES:
        if category_filter and sig.category not in category_filter:
            continue
        for header in sig.headers:
            start = 0
            while True:
                idx = chunk.find(header, start)
                if idx < 0:
                    break
                if idx not in seen_offsets:
                    found.append((idx, sig))
                    seen_offsets.add(idx)
                start = idx + 1
    found.sort(key=lambda item: item[0])
    return found


def extension_for(sig: Signature) -> str:
    return sig.extensions[0]


def looks_like_text(data: bytes) -> bool:
    if not data:
        return False
    printable = sum(1 for b in data[:4096] if 32 <= b < 127 or b in (9, 10, 13))
    return printable / max(1, len(data[:4096])) > 0.92


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def supported_extensions() -> list[str]:
    """Every file extension the carver can recognise."""
    return sorted({ext for sig in SIGNATURES for ext in sig.extensions})
