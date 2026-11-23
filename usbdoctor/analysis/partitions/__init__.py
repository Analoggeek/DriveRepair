"""Partition / filesystem analysis namespace."""

from usbdoctor.analysis.partitions.tables import (
    MBR_TYPES,
    GPT_TYPE_GUIDS,
    PartEntry,
    PartitionTableResult,
    TableIssue,
    analyze_gpt,
    describe_issue_repair,
    find_lost_partitions,
    looks_like_apm,
    parse_apm,
    parse_gpt_entries,
    parse_gpt_header,
    parse_guid,
    parse_mbr,
)

__all__ = [
    "MBR_TYPES",
    "GPT_TYPE_GUIDS",
    "PartEntry",
    "PartitionTableResult",
    "TableIssue",
    "analyze_gpt",
    "describe_issue_repair",
    "find_lost_partitions",
    "looks_like_apm",
    "parse_apm",
    "parse_gpt_entries",
    "parse_gpt_header",
    "parse_guid",
    "parse_mbr",
]
