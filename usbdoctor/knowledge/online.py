"""Online research constrained to trustworthy, verifiable sources.

Design rules (they are enforced in code, not by convention):

* Only HTTPS, and only hosts on an explicit allow-list.
* Results always carry their source URL so the user can verify them.
* Nothing downloaded here is executed; installer downloads go through
  :mod:`usbdoctor.firmware.verify` with hash/signature checks.
* Failure is reported as a failure — never replaced by invented text.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from usbdoctor.core.errors import KnowledgeSourceError
from usbdoctor.version import APP_NAME, __version__

#: Domains we will talk to.  Anything else is refused.
ALLOWED_HOSTS: tuple[str, ...] = (
    "www.kingston.com",
    "www.sandisk.com",
    "support.sandisk.com",
    "www.samsung.com",
    "semiconductor.samsung.com",
    "www.transcend-info.com",
    "support.hp.com",
    "www.crucial.com",
    "support.wdc.com",
    "www.seagate.com",
    "www.lexar.com",
    "learn.microsoft.com",
    "support.apple.com",
    "www.smartmontools.org",
    "www.cgsecurity.org",
    "www.usb.org",
    "www.sdcard.org",
    "www.snia.org",
    "t13.org",
)

USER_AGENT = f"{APP_NAME.replace(' ', '')}/{__version__} (+https://github.com/Analoggeek/DriveRepair)"
MAX_BYTES = 512 * 1024
TIMEOUT = 12


@dataclass
class SourceResult:
    url: str
    title: str
    snippet: str
    official: bool
    retrieved_at: float = field(default_factory=time.time)
    status: str = "ok"
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "official": self.official,
            "retrieved_at": self.retrieved_at,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class VendorLink:
    vendor: str
    risk: str
    title: str
    url: str
    notes: str

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "title": self.title,
            "url": self.url,
            "notes": self.notes,
        }


#: Official support pages used as authoritative references.  These are stable,
#: documented entry points rather than deep links that rot.
VENDOR_LINKS: tuple[VendorLink, ...] = (
    VendorLink("Kingston", "low", "Kingston support & SSD/USB tools",
               "https://www.kingston.com/en/support", "Official support portal and product tools"),
    VendorLink("SanDisk", "low", "SanDisk / Western Digital support",
               "https://support.sandisk.com/", "Warranty, diagnostics and firmware guidance"),
    VendorLink("Samsung", "low", "Samsung Memory support & Magician",
               "https://semiconductor.samsung.com/consumer-storage/support/tools/",
               "Samsung Magician: official SSD diagnostics (SSD models only)"),
    VendorLink("Western Digital", "low", "WD Support & Dashboard",
               "https://support.wdc.com/downloads.aspx", "WD Dashboard official diagnostics"),
    VendorLink("Seagate", "low", "Seagate SeaTools",
               "https://www.seagate.com/support/downloads/seatools/", "Official drive diagnostics"),
    VendorLink("Crucial", "low", "Crucial Storage Executive",
               "https://www.crucial.com/support/storage-executive", "Official SSD health tool"),
    VendorLink("Transcend", "low", "Transcend support & tools",
               "https://www.transcend-info.com/Support/", "Official support portal"),
    VendorLink("Lexar", "low", "Lexar support",
               "https://www.lexar.com/support/", "Official support portal"),
    VendorLink("Microsoft", "low", "CHKDSK reference",
               "https://learn.microsoft.com/windows-server/administration/windows-commands/chkdsk",
               "Authoritative documentation for the Windows file-system checker"),
    VendorLink("Apple", "low", "Apple Disk Utility / First Aid",
               "https://support.apple.com/guide/disk-utility/repair-a-disk-dskutl1040/mac",
               "Apple's documented volume repair procedure"),
    VendorLink("smartmontools", "low", "smartmontools (SMART access)",
               "https://www.smartmontools.org/", "Open-source SMART tooling used by this app"),
    VendorLink("CGSecurity", "low", "TestDisk & PhotoRec",
               "https://www.cgsecurity.org/", "Open-source partition recovery and file carving"),
    VendorLink("USB-IF", "low", "USB Implementers Forum",
               "https://www.usb.org/", "Authoritative USB specifications and compliance"),
)


def is_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        return False
    return parsed.hostname in ALLOWED_HOSTS


def vendor_links_for(brand: Optional[str]) -> list[VendorLink]:
    if not brand:
        return list(VENDOR_LINKS)
    lowered = brand.lower()
    matched = [link for link in VENDOR_LINKS if link.vendor.lower() in lowered or lowered in link.vendor.lower()]
    return matched or list(VENDOR_LINKS)


def build_research_queries(
    *,
    brand: Optional[str],
    model: Optional[str],
    vid: Optional[str],
    pid: Optional[str],
    firmware: Optional[str],
    error_codes: list[str],
    filesystem: Optional[str],
) -> list[str]:
    """Compose precise search terms from the real device identity."""
    queries = []
    identity = " ".join(part for part in (brand, model) if part)[:120]
    if identity:
        queries.append(f"{identity} RAW file system repair")
        queries.append(f"{identity} firmware update official")
    if vid and pid:
        queries.append(f"USB VID {vid} PID {pid} controller firmware")
    if firmware:
        queries.append(f"{brand or ''} firmware {firmware} update".strip())
    for code in error_codes[:4]:
        queries.append(f"USB drive error {code} repair")
    if filesystem:
        queries.append(f"{filesystem} file system corruption repair")
    return queries


class OnlineResearcher:
    """Fetches and excerpts *allow-listed* pages; records provenance."""

    def __init__(self, *, offline: bool = False, timeout: float = TIMEOUT) -> None:
        self.offline = offline
        self.timeout = timeout
        self._cache: dict[str, SourceResult] = {}

    def fetch(self, url: str) -> SourceResult:
        if not is_allowed(url):
            raise KnowledgeSourceError(
                f"Refusing to contact a non-allow-listed source: {url}",
                hint="Only official vendor and standards-body domains are queried.",
            )
        if self.offline:
            return SourceResult(url=url, title="", snippet="", official=True, status="offline",
                                error="Network research is disabled in Settings.")
        if url in self._cache:
            return self._cache[url]
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        context = ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                if response.geturl() and not is_allowed(response.geturl()):
                    raise KnowledgeSourceError("Redirected to a non-allow-listed host.", url=response.geturl())
                raw = response.read(MAX_BYTES)
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            result = SourceResult(url=url, title="", snippet="", official=True, status="http_error",
                                  error=f"HTTP {exc.code}")
            self._cache[url] = result
            return result
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            result = SourceResult(url=url, title="", snippet="", official=True, status="network_error",
                                  error=str(exc)[:200])
            self._cache[url] = result
            return result
        text = raw.decode(charset, "replace")
        title, snippet = _extract_text(text)
        result = SourceResult(url=url, title=title, snippet=snippet, official=True)
        self._cache[url] = result
        return result

    def research(
        self,
        *,
        brand: Optional[str] = None,
        model: Optional[str] = None,
        vid: Optional[str] = None,
        pid: Optional[str] = None,
        firmware: Optional[str] = None,
        error_codes: Optional[list[str]] = None,
        filesystem: Optional[str] = None,
        max_pages: int = 3,
    ) -> dict:
        """Return official references plus the exact queries that were used.

        We deliberately do **not** scrape arbitrary search engines: the returned
        material is limited to allow-listed official pages, and every item shows
        its URL so nothing has to be taken on trust.
        """
        queries = build_research_queries(
            brand=brand,
            model=model,
            vid=vid,
            pid=pid,
            firmware=firmware,
            error_codes=error_codes or [],
            filesystem=filesystem,
        )
        links = vendor_links_for(brand)
        pages: list[SourceResult] = []
        for link in links[:max_pages]:
            try:
                pages.append(self.fetch(link.url))
            except KnowledgeSourceError:
                continue
        return {
            "queries": queries,
            "official_pages": [page.to_dict() for page in pages],
            "vendor_links": [link.to_dict() for link in links],
            "disclaimer": (
                "Only allow-listed official vendor and standards-body pages are queried. "
                "Open the URLs above to read the original guidance; USB Doctor AI never "
                "downloads or runs software without your explicit approval and hash verification."
            ),
        }


def _extract_text(html: str) -> tuple[str, str]:
    import re

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    body = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"&[a-z]+;", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return title[:200], body[:1200]


def save_research_report(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
