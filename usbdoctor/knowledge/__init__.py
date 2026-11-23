"""Repair knowledge: offline curated database plus allow-listed online research."""

from usbdoctor.knowledge.database import (
    ENTRIES,
    KnowledgeEntry,
    export_json,
    get,
    load_entries,
    search,
)
from usbdoctor.knowledge.online import (
    ALLOWED_HOSTS,
    VENDOR_LINKS,
    OnlineResearcher,
    SourceResult,
    VendorLink,
    build_research_queries,
    is_allowed,
    vendor_links_for,
)

__all__ = [
    "ENTRIES",
    "KnowledgeEntry",
    "export_json",
    "get",
    "load_entries",
    "search",
    "ALLOWED_HOSTS",
    "VENDOR_LINKS",
    "OnlineResearcher",
    "SourceResult",
    "VendorLink",
    "build_research_queries",
    "is_allowed",
    "vendor_links_for",
]
