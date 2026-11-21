#!/usr/bin/env python3
# Web advisories follow the xAI docs read on 2026-11-21:
# https://docs.x.ai/developers/tools/web-search
# https://docs.x.ai/developers/model-capabilities/text/structured-outputs
# Client: OpenAI-compatible Responses API, base URL https://api.x.ai/v1,
# model grok-4.7. Tool type web_search. The Python example on that page puts
# domain limits under filters.allowed_domains (max 5). allowed_domains cannot
# be combined with excluded_domains, so mofa.go.jp is kept off the allow list
# and any result whose host matches website_only_checks_note is dropped.
# This script never requests mofa.go.jp itself.

"""Fetch official travel and maritime advisories into the same hotspots GeoJSON."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import fetch_x_osint as osint

# Five domains is the documented maximum for one web_search call.
TRAVEL_DOMAINS = [
    "gov.uk",
    "travel.state.gov",
    "travel.gc.ca",
    "safetravel.govt.nz",
    "smartraveller.gov.au",
]
MARITIME_DOMAINS = ["ukmto.org"]

HOST_HANDLE = (
    ("travel.state.gov", "TravelGov"),
    ("travel.gc.ca", "TravelGoC"),
    ("safetravel.govt.nz", "SafeTravelNZ"),
    ("smartraveller.gov.au", "Smartraveller"),
    ("ukmto.org", "UK_MTO"),
    ("gov.uk", "FCDOtravelGovUK"),
)

ADVISORY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "incidents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "longitude": {"type": "number"},
                    "latitude": {"type": "number"},
                    "severity": {"type": "string", "enum": list(osint.SEVERITIES)},
                    "summary": {"type": "string"},
                    "timestamp": {"type": "string"},
                    "source": {"type": "string"},
                    "region": {"type": "string", "enum": list(osint.REGIONS)},
                    "claimant": {"type": "string"},
                },
                "required": [
                    "longitude",
                    "latitude",
                    "severity",
                    "summary",
                    "timestamp",
                    "source",
                    "region",
                    "claimant",
                ],
            },
        }
    },
    "required": ["incidents"],
}


def handle_for_host(hostname: str) -> str | None:
    host = hostname.lower()
    for suffix, handle in HOST_HANDLE:
        if host == suffix or host.endswith("." + suffix):
            return handle
    return None


def host_allowed(url: str, suffixes: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def prepare_advisories(
    raw_items: list,
    *,
    batch: str,
    category: str,
    domains: list[str],
    blocked: set[str],
) -> list[dict]:
    features = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        parsed = urlparse(source)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if osint.url_host_blocked(source, blocked) or not host_allowed(source, domains):
            continue
        handle = handle_for_host(parsed.hostname)
        if not handle:
            continue
        if source in seen:
            continue
        coords = osint.valid_coord(item.get("longitude"), item.get("latitude"))
        if coords is None:
            continue
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in osint.SEVERITIES:
            continue
        summary = osint.one_sentence(item.get("summary"))
        if not summary:
            continue
        moment = osint.parse_timestamp(item.get("timestamp"))
        if moment is None:
            continue
        region = osint.normalize_region(item.get("region"))
        if region is None:
            continue
        seen.add(source)
        feature = osint.build_feature(
            lon=coords[0],
            lat=coords[1],
            severity=severity,
            summary=summary,
            timestamp=osint.format_timestamp(moment),
            source=source,
            handle=handle,
            batch=batch,
            tier="Official / Primary",
            region=region,
            category=category,
            cites_primary=True,
        )
        feature["properties"].pop("_cites_primary", None)
        features.append(feature)
    return features


def advisory_prompt(kind: str, category: str, blocked: set[str]) -> str:
    banned = ", ".join(sorted(blocked))
    return (
        f"Find current official {kind} that are still in effect for the Gulf, "
        "Saudi Arabia, Yemen, the Red Sea, Israel, Palestine, Lebanon, Jordan, Syria, "
        "Iran, Iraq, the Kurdistan Region, the Strait of Hormuz, Bab el-Mandeb, "
        "the Gulf of Oman, or the Arabian Sea.\n"
        f"category for every item is {category}. Use severity high for do-not-travel or a "
        "serious threat, medium for a significant warning, and low for routine caution.\n"
        "summary is one concise English sentence. Translate Arabic, Persian, and French.\n"
        "timestamp is the page's last-updated time as UTC ISO 8601 with a Z suffix, "
        "for example 2026-11-21T15:00:00Z. If the page has a date only, use T00:00:00Z on that date.\n"
        "source is the https URL of the advisory page on an allowed domain.\n"
        "longitude and latitude are a point in the country or sea area. "
        "Use the capital only for a country-wide advisory. Do not invent a place the page is not about.\n"
        "region is exactly one of: Gulf; Saudi & Yemen; Levant; Iran & Iraq; Maritime; Other / International.\n"
        "Gulf is Bahrain, Kuwait, Qatar, UAE, and Oman. Saudi & Yemen includes the Red Sea coast. "
        "Levant is Israel, Palestine, Lebanon, Jordan, and Syria. "
        "Iran & Iraq includes the Kurdistan Region. "
        "Maritime is the Strait of Hormuz, Bab el-Mandeb, Gulf of Oman, Arabian Sea, and UKMTO/JMIC. "
        "Other / International is Turkey, US/UN diplomatic statements, and broader advisories.\n"
        f"Do not open, fetch, or cite these hosts: {banned}. "
        "Japan MOFA (mofa.go.jp) is a manual note only and must not be fetched.\n"
        "claimant is the publishing office, or an empty string."
    )


def search_group(client, api_key: str, domains: list[str], kind: str, category: str, batch: str, blocked: set[str]):
    tool = {"type": "web_search", "filters": {"allowed_domains": domains}}
    incidents = osint.call_model(
        client,
        tool,
        advisory_prompt(kind, category, blocked),
        "advisory_batch",
        ADVISORY_SCHEMA,
        api_key,
    )
    if incidents is None:
        return None
    prepared = prepare_advisories(
        incidents,
        batch=batch,
        category=category,
        domains=domains,
        blocked=blocked,
    )
    print(f"{kind}: kept {len(prepared)} of {len(incidents)}")
    return prepared


def main() -> None:
    api_key = osint.exit_if_no_key()
    sources = osint.load_sources()
    notes = sources.get("website_only_checks_note") or []
    if isinstance(notes, list) and notes:
        print("Not fetching website-only checks:")
        for note in notes:
            print(f"  - {note}")
    blocked = osint.blocked_hosts(notes)
    excluded = osint.excluded_set(sources.get("excluded_handles") or [])
    client = osint.make_client(api_key)
    groups = (
        (
            TRAVEL_DOMAINS,
            "travel and embassy advisories",
            "Travel / Embassy Advisory",
            "travel_advisories_and_embassies",
        ),
        (
            MARITIME_DOMAINS,
            "maritime security advisories",
            "Maritime Advisory",
            "mandatory_and_maritime",
        ),
    )
    collected: list[dict] = []
    succeeded = 0
    for domains, kind, category, batch in groups:
        print(f"Searching {kind} on {', '.join(domains)}")
        prepared = search_group(client, api_key, domains, kind, category, batch, blocked)
        if prepared is None:
            continue
        succeeded += 1
        collected.extend(prepared)
    if succeeded == 0:
        print("All advisory searches failed. hotspots.json was not changed.")
        sys.exit(1)
    if succeeded < len(groups):
        print(f"{len(groups) - succeeded} advisory search(es) failed. Merging the searches that succeeded.")
    now = datetime.now(timezone.utc)
    try:
        stats = osint.merge_active_and_archive(collected, now=now, excluded=excluded)
    except RuntimeError as exc:
        print(f"{exc}. hotspots.json was not changed.")
        sys.exit(1)
    print(
        f"Wrote {stats['active']} incidents to {osint.HOTSPOTS.name} "
        f"({stats['archived']} moved to {osint.ARCHIVE.name})."
    )


if __name__ == "__main__":
    main()
