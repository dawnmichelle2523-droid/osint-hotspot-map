"""Deterministic 3-tier OSINT confidence scoring.

Official advisories come from a fixed registry of government, UN, and agency
sources. Every other incident is compared with the rest of the batch by
country or proximity (75 km), time (24 hours), and overlapping keywords or
category. No network calls and no model credits.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

PROXIMITY_KM = 75.0
WINDOW = timedelta(hours=24)

TIER_OFFICIAL = "official"
TIER_CORROBORATED = "corroborated"
TIER_UNVERIFIED = "unverified"

LABELS = {
    TIER_OFFICIAL: "Official Advisory",
    TIER_CORROBORATED: "Multi-Source Corroborated",
    TIER_UNVERIFIED: "Single-Source Unverified",
}

CONFIDENCE_FIELDS = (
    "confidence_tier",
    "confidence_label",
    "confidence_score",
    "corroborating_sources",
    "corroboration_count",
)

# Authoritative government, UN, and agency feeds. News wires are intentionally absent.
OFFICIAL_SOURCES = (
    {"id": "usgs", "label": "USGS", "markers": ("usgs", "earthquake.usgs.gov"), "feeds": ("usgs-quakes", "usgs-volcanoes"), "prefixes": ()},
    {"id": "eonet", "label": "NASA EONET", "markers": ("eonet", "eonet.gsfc.nasa.gov"), "feeds": ("eonet",), "prefixes": ("eonet-",)},
    {"id": "firms", "label": "NASA FIRMS", "markers": ("nasa firms", "firms.modaps"), "feeds": ("firms",), "prefixes": ()},
    {"id": "gdacs", "label": "GDACS", "markers": ("gdacs",), "feeds": ("gdacs-rss",), "prefixes": ("gdacs-",)},
    {"id": "nifc", "label": "NIFC", "markers": ("nifc", "wfigs"), "feeds": ("wfigs",), "prefixes": ()},
    {"id": "inciweb", "label": "InciWeb", "markers": ("inciweb",), "feeds": ("inciweb",), "prefixes": ()},
    {"id": "ntwc", "label": "NTWC", "markers": ("ntwc",), "feeds": ("tsunami-ntwc", "tsunami-ntwc-cap"), "prefixes": ()},
    {"id": "ptwc", "label": "PTWC", "markers": ("ptwc",), "feeds": ("tsunami-ptwc", "tsunami-ptwc-cap"), "prefixes": ()},
    {"id": "nws", "label": "NWS", "markers": ("nws", "weather.gov", "tsunami.gov"), "feeds": ("nws-alerts", "nws-tsunami"), "prefixes": ("nws-",)},
    {"id": "nhc", "label": "NHC", "markers": ("nhc", "nhc.noaa.gov"), "feeds": ("nhc", "nhc-ep", "nhc-cp"), "prefixes": ("nhc-",)},
    {"id": "jtwc", "label": "JTWC", "markers": ("jtwc",), "feeds": ("jtwc",), "prefixes": ()},
    {"id": "swpc", "label": "NOAA SWPC", "markers": ("swpc",), "feeds": ("swpc",), "prefixes": ()},
    {"id": "jma", "label": "JMA", "markers": ("jma", "japan meteorological"), "feeds": ("jma-eq", "jma-typhoon"), "prefixes": ("jma-",)},
    {"id": "emsc", "label": "EMSC", "markers": ("emsc", "seismicportal.eu"), "feeds": ("emsc",), "prefixes": ()},
    {"id": "meteoalarm", "label": "MeteoAlarm", "markers": ("meteoalarm",), "feeds": (), "prefixes": ("meteoalarm-",)},
    {"id": "uk-floods", "label": "UK Environment Agency", "markers": ("environment agency", "environment.data.gov.uk", "flood-monitoring", "uk flood"), "feeds": ("uk-floods",), "prefixes": ()},
    {"id": "met-norway", "label": "MET Norway", "markers": ("met norway", "api.met.no"), "feeds": ("met-norway",), "prefixes": ()},
    {"id": "eccc", "label": "ECCC", "markers": ("eccc", "weather.gc.ca"), "feeds": ("eccc",), "prefixes": ()},
    {"id": "alert-ready", "label": "Alert Ready", "markers": ("alert ready",), "feeds": ("alert-ready",), "prefixes": ()},
    {"id": "cemaden", "label": "CEMADEN", "markers": ("cemaden",), "feeds": ("cemaden",), "prefixes": ()},
    {"id": "nsw-rfs", "label": "NSW Rural Fire Service", "markers": ("rural fire", "nsw rfs"), "feeds": ("nsw-rfs",), "prefixes": ()},
    {"id": "vic-emergency", "label": "VicEmergency", "markers": ("vicemergency",), "feeds": ("vic-emergency",), "prefixes": ()},
    {"id": "qld-bushfire", "label": "Queensland Bushfire Alerts", "markers": ("queensland bushfire",), "feeds": ("qld-bushfire",), "prefixes": ()},
    {"id": "ndma", "label": "NDMA SACHET", "markers": ("ndma", "sachet"), "feeds": ("sachet",), "prefixes": ()},
    {"id": "reliefweb", "label": "ReliefWeb", "markers": ("reliefweb",), "feeds": (), "prefixes": ()},
    {"id": "ocha", "label": "OCHA", "markers": ("unocha", "ocha"), "feeds": (), "prefixes": ()},
    {"id": "ifrc", "label": "IFRC", "markers": ("ifrc",), "feeds": ("ifrc",), "prefixes": ()},
    {"id": "who", "label": "WHO", "markers": ("who.int", "disease outbreak news"), "feeds": ("who-don",), "prefixes": ()},
    {"id": "ecdc", "label": "ECDC", "markers": ("ecdc",), "feeds": ("ecdc",), "prefixes": ()},
    {"id": "africa-cdc", "label": "Africa CDC", "markers": ("africa cdc",), "feeds": ("africa-cdc",), "prefixes": ()},
    {"id": "crisis-group", "label": "Crisis Group", "markers": ("crisis group", "crisisgroup"), "feeds": ("wire-icg",), "prefixes": ()},
    {"id": "ukmto", "label": "UKMTO", "markers": ("ukmto",), "feeds": ("ukmto",), "prefixes": ()},
    {"id": "imb", "label": "IMB Piracy Reporting Centre", "markers": ("icc-ccs.org", "imb piracy"), "feeds": ("imb-prc",), "prefixes": ()},
    {"id": "cmf", "label": "Combined Maritime Forces", "markers": ("combined maritime",), "feeds": ("cmf",), "prefixes": ()},
    {"id": "faa", "label": "FAA", "markers": ("faa",), "feeds": ("faa",), "prefixes": ()},
    {"id": "cisa", "label": "CISA", "markers": ("cisa",), "feeds": ("cisa-kev", "cisa-news"), "prefixes": ("cisa-",)},
    {"id": "ncsc", "label": "UK NCSC", "markers": ("ncsc",), "feeds": ("ncsc-news", "ncsc-advisories"), "prefixes": ("ncsc-",)},
    {"id": "cert-eu", "label": "CERT-EU", "markers": ("cert-eu", "cert.europa.eu"), "feeds": ("cert-eu",), "prefixes": ()},
    {"id": "fcdo", "label": "UK FCDO", "markers": ("fcdo", "foreign-travel-advice"), "feeds": ("uk-fcdo-atom",), "prefixes": ()},
    {"id": "state-dept", "label": "US State Department", "markers": ("state department", "travel.state.gov"), "feeds": ("us-advisories", "us-advisories-rss"), "prefixes": ()},
    {"id": "nhmp", "label": "National Highways and Motorway Police", "markers": ("nhmpofficial", "nhmp"), "feeds": (), "prefixes": ()},
    {"id": "sargodha-police", "label": "Sargodha District Police", "markers": ("sargodhapolice",), "feeds": (), "prefixes": ()},
    {"id": "gac", "label": "Global Affairs Canada", "markers": ("global affairs canada", "travel.gc.ca"), "feeds": ("canada-advisories",), "prefixes": ()},
    {"id": "germany-travel", "label": "German Foreign Office", "markers": ("auswaertiges-amt.de", "german foreign office"), "feeds": ("de-travel",), "prefixes": ()},
    {"id": "smartraveller", "label": "Smartraveller", "markers": ("smartraveller",), "feeds": ("au-smartraveller",), "prefixes": ()},
    {"id": "cdc", "label": "US CDC", "markers": ("cdc.gov", "wwwnc.cdc.gov"), "feeds": ("cdc-travel",), "prefixes": ()},
    {"id": "copernicus", "label": "Copernicus EMS", "markers": ("copernicus",), "feeds": ("copernicus-ems",), "prefixes": ()},
    {"id": "un-news", "label": "UN News", "markers": ("news.un.org", "un news"), "feeds": ("un-news",), "prefixes": ()},
    {"id": "ua-air-alert", "label": "Ukrainian air-raid alerts", "markers": ("air-raid", "air_alert_ua"), "feeds": ("tg-air-alert-ua",), "prefixes": ()},
)

GENERIC_CATEGORIES = {
    "",
    "osint",
    "security",
    "miscellaneous",
    "other",
    "od",
    "news",
    "general",
    "update",
    "incident",
    "event",
    "misc",
}

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "over", "under",
    "near", "after", "before", "were", "was", "are", "been", "being", "have",
    "has", "had", "its", "their", "his", "her", "than", "then", "also", "new",
    "says", "said", "say", "report", "reports", "reported", "according",
    "update", "updates", "live", "breaking", "about", "amid", "across", "during",
    "while", "where", "when", "what", "which", "who", "whose", "will", "would",
    "could", "should", "may", "might", "not", "but", "you", "your", "our",
    "out", "off", "per", "via", "due",
}

_WORD = re.compile(r"[a-z0-9]{3,}")
_MEDIA_URL = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:"
    r"t\.me/(?:s/)?([a-z0-9_]+)/(\d+)"
    r"|(?:x|twitter)\.com/([a-z0-9_]+)/status/(\d+)"
    r"|(?:youtube\.com/watch\?v=|youtu\.be/)([a-z0-9_-]{6,})"
    r")",
    re.IGNORECASE,
)
_VIA = re.compile(
    r"\b(?:via|forwarded from|credit|quoting)\b[:\s@]*@?([a-z0-9_]{4,})",
    re.IGNORECASE,
)


def media_fingerprints(text: object) -> set[str]:
    """Shared post or video URLs. The same clip is one story, not two sources."""
    found = set()
    for match in _MEDIA_URL.finditer(str(text or "")):
        telegram_channel, telegram_id, x_user, x_id, youtube = match.groups()
        if telegram_channel and telegram_id:
            found.add(f"tg:{telegram_channel.lower()}/{telegram_id}")
        elif x_user and x_id:
            found.add(f"x:{x_user.lower()}/{x_id}")
        elif youtube:
            found.add("yt:" + youtube.lower())
    return found


def score_incidents(incidents: list) -> list:
    """Enrich incident dicts or GeoJSON features in place and return the same list."""
    prepared = []
    for index, item in enumerate(incidents or []):
        target, view = _view(item)
        if target is None or view is None:
            continue
        official = match_official(view)
        key, display = _source_identity(view, official, index)
        prepared.append({
            "target": target,
            "official": official is not None,
            "partisan": _is_partisan(view),
            "mbc": _is_mbc_family(view),
            "key": key,
            "display": display,
            "coords": _coords(view),
            "country": _country(view),
            "when": _when(view.get("timestamp") or view.get("published_at") or view.get("occurred_at")),
            "category": _category(view),
            "keywords": _keywords(view),
            "placed": _coords(view) is not None or bool(_country(view)),
            "clips": media_fingerprints(" ".join(
                str(view.get(key) or "")
                for key in ("title", "summary", "source", "source_url", "original_text")
            )),
            "text": " ".join(str(view.get(key) or "") for key in ("title", "summary", "original_text")),
            "identity": _identity_blob(view),
            "source_tier": " ".join(str(view.get("source_tier") or view.get("verification_tier") or "").split()),
        })

    neighbor_keys: list[dict[str, str]] = []
    for left in prepared:
        found = {left["key"]: left["display"]} if left["key"] and not left["key"].startswith("anon:") else {}
        for right in prepared:
            if left is right or not _corroborates(left, right):
                continue
            found[right["key"]] = right["display"]
        if left["key"] and not left["key"].startswith("anon:") and left["key"] not in found:
            found[left["key"]] = left["display"]
        neighbor_keys.append(found)

    for item, sources in zip(prepared, neighbor_keys):
        names = _unique_names(sources)
        count = max(1, len(sources) if sources else (0 if item["key"].startswith("anon:") else 1))
        if not sources and not item["key"].startswith("anon:"):
            names = [item["display"]] if item["display"] else []
            count = 1
        if item["official"]:
            tier = TIER_OFFICIAL
        elif count >= 2:
            tier = TIER_CORROBORATED
        else:
            tier = TIER_UNVERIFIED
            count = 1
            if item["display"] and not names:
                names = [item["display"]]
        item["target"]["confidence_tier"] = tier
        item["target"]["confidence_label"] = LABELS[tier]
        item["target"]["confidence_score"] = _score(tier, count, item["placed"], item.get("source_tier") or "")
        item["target"]["corroborating_sources"] = names
        item["target"]["corroboration_count"] = count
    return incidents


def apply_to_payload(payload: dict, score=None) -> dict:
    """Return a copy of an API payload with every incident collection scored.

    Feature confidence is copied onto summary rows that share a title, time,
    and country but have no source of their own. Rows that already name a
    source keep the score computed from their own collection.
    """
    if not isinstance(payload, dict):
        return payload
    score_fn = score or score_incidents
    keys = ("features", "events", "incidents", "items")
    if not any(isinstance(payload.get(key), list) for key in keys):
        return payload
    out = dict(payload)
    lookup: dict[tuple, dict] = {}
    features = out.get("features")
    if isinstance(features, list):
        clones = [_clone_feature(feature) for feature in features]
        score_fn([item for item in clones if isinstance(item, dict)])
        for feature in clones:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if isinstance(props, dict) and props.get("confidence_tier"):
                lookup.setdefault(_story_key(props), props)
        out["features"] = clones
    for key in ("events", "incidents", "items"):
        rows = out.get(key)
        if not isinstance(rows, list):
            continue
        clones = [dict(row) if isinstance(row, dict) else row for row in rows]
        score_fn([row for row in clones if isinstance(row, dict)])
        for row in clones:
            if not isinstance(row, dict) or _named_source(row):
                continue
            match = lookup.get(_story_key(row))
            if not match:
                continue
            for field in CONFIDENCE_FIELDS:
                if field in match:
                    row[field] = list(match[field]) if field == "corroborating_sources" else match[field]
        out[key] = clones
    return out


def match_official(incident: dict) -> dict | None:
    """Return the registry entry when the incident is an authoritative advisory."""
    view = incident.get("properties") if isinstance(incident.get("properties"), dict) else incident
    if not isinstance(view, dict):
        return None
    feed = str(view.get("feed_id") or "").strip().lower()
    blob = _identity_blob(view)
    for entry in OFFICIAL_SOURCES:
        if feed and (feed in entry["feeds"] or any(feed.startswith(prefix) for prefix in entry["prefixes"])):
            return entry
        if any(_marker_in(blob, marker) for marker in entry["markers"]):
            return entry
    if str(view.get("source_tier") or "").strip() == "Official":
        label = _raw_source_name(view) or "Official Advisory"
        return {"id": "tier:" + _slug(label), "label": label, "markers": (), "feeds": (), "prefixes": ()}
    return None


def _view(item):
    if not isinstance(item, dict):
        return None, None
    props = item.get("properties")
    geometry = item.get("geometry")
    if isinstance(props, dict) and (item.get("type") == "Feature" or isinstance(geometry, dict)):
        view = dict(props)
        if isinstance(geometry, dict):
            view["geometry"] = geometry
        return props, view
    return item, dict(item)


def _clone_feature(feature):
    if not isinstance(feature, dict):
        return feature
    clone = dict(feature)
    if isinstance(feature.get("properties"), dict):
        clone["properties"] = dict(feature["properties"])
    return clone


def _identity_blob(view: dict) -> str:
    parts = [
        view.get("source_name"),
        view.get("source_label"),
        view.get("source_handle"),
        view.get("handle"),
        view.get("account"),
        view.get("source"),
        view.get("source_url"),
        view.get("feed_id"),
        view.get("feed_name"),
        view.get("source_kind"),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _marker_in(blob: str, marker: str) -> bool:
    needle = marker.lower()
    if any(char in needle for char in " ./-"):
        return needle in blob
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", blob) is not None


def _raw_source_name(view: dict) -> str:
    for key in ("source_name", "source_label", "account", "source_handle", "handle"):
        text = " ".join(str(view.get(key) or "").split())
        if text:
            return text[:80]
    source = str(view.get("source") or view.get("source_url") or "")
    if "://" in source:
        host = source.split("://", 1)[1].split("/", 1)[0].lower()
        if host.startswith("www."):
            host = host[4:]
        return host[:80]
    return ""


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:80] or "source"


def _source_identity(view: dict, official: dict | None, index: int) -> tuple[str, str]:
    if official:
        return "official:" + str(official.get("id") or "source"), str(official.get("label") or "Official Advisory")
    name = _raw_source_name(view)
    if name:
        return "name:" + _slug(name), name
    return f"anon:{index}", "Unnamed source"


def _coords(view: dict) -> tuple[float, float] | None:
    lat = _number(view.get("lat"))
    lng = view.get("lng") if view.get("lng") is not None else view.get("lon")
    lng = _number(lng)
    if lat is None or lng is None:
        geometry = view.get("geometry")
        if isinstance(geometry, dict):
            coords = geometry.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lng = _number(coords[0])
                lat = _number(coords[1])
    if lat is None or lng is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    return lat, lng


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or math.isinf(number):
        return None
    return number


def _country(view: dict) -> str:
    return " ".join(str(view.get("country") or "").split()).casefold()


def _when(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _category(view: dict) -> str:
    raw = view.get("feed_category") or view.get("category") or ""
    text = " ".join(str(raw).split()).casefold()
    return "" if text in GENERIC_CATEGORIES else text


def _keywords(view: dict) -> set[str]:
    text = " ".join(
        str(view.get(key) or "")
        for key in ("title", "summary", "location_name", "area", "city", "governorate")
    ).lower()
    return {token for token in _WORD.findall(text) if token not in _STOPWORDS}


_MBC_MARKERS = ("alarabiya", "alarabiya_eng", "alarabiya_brk", "alhadath", "alhadath_brk", "alarabiya.net")


def _is_mbc_family(view: dict) -> bool:
    """Al Arabiya and Al Hadath belong to one network."""
    blob = _identity_blob(view)
    return any(_marker_in(blob, marker) for marker in _MBC_MARKERS)


def _is_partisan(view: dict) -> bool:
    """Advocacy accounts do not corroborate each other."""
    tier = " ".join(str(view.get("source_tier") or view.get("verification_tier") or "").split()).casefold()
    if "partisan" in tier and "nonpartisan" not in tier:
        return True
    blob = _identity_blob(view)
    return any(_marker_in(blob, handle) for handle in (
        "amharawarupdate", "aaa_amhara", "ashara_media",
        "exilenova_plus", "exilenova", "amk_mapping_", "amk_mapping",
        "qudsn", "yaffa_ps",
    ))


def _reposts(left: dict, right: dict) -> bool:
    """True when one item is the other's video or an explicit repost of it."""
    if left.get("clips") and right.get("clips") and (left["clips"] & right["clips"]):
        return True
    identity = right.get("identity") or ""
    for handle in _VIA.findall(left.get("text") or ""):
        if _marker_in(identity, handle.lower()):
            return True
    return False


def _corroborates(left: dict, right: dict) -> bool:
    if left["key"].startswith("anon:") or right["key"].startswith("anon:") or left["key"] == right["key"]:
        return False
    if _reposts(left, right) or _reposts(right, left):
        return False
    if left.get("partisan") and right.get("partisan"):
        return False
    if left.get("mbc") and right.get("mbc"):
        return False
    if left["when"] is None or right["when"] is None:
        return False
    if abs(left["when"] - right["when"]) > WINDOW:
        return False
    if not _same_place(left, right):
        return False
    if left["category"] and left["category"] == right["category"]:
        return True
    shared = left["keywords"] & right["keywords"]
    if len(shared) >= 2:
        return True
    return any(len(token) >= 6 for token in shared)


def _same_place(left: dict, right: dict) -> bool:
    if left["coords"] and right["coords"]:
        return _distance_km(left["coords"], right["coords"]) <= PROXIMITY_KM
    return bool(left["country"] and left["country"] == right["country"])


def _distance_km(left: tuple[float, float], right: tuple[float, float]) -> float:
    radius = 6371.0088
    lat1, lon1 = math.radians(left[0]), math.radians(left[1])
    lat2, lon2 = math.radians(right[0]), math.radians(right[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(hav)))


def _unique_names(sources: dict[str, str]) -> list[str]:
    names = []
    seen = set()
    for name in sources.values():
        cleaned = " ".join(str(name or "").split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(cleaned)
    names.sort(key=str.casefold)
    return names


def _score(tier: str, count: int, placed: bool, source_tier: str = "") -> int:
    if tier == TIER_OFFICIAL:
        value = 92 + 2 * max(0, count - 1)
    elif tier == TIER_CORROBORATED:
        # Stay under a lone official advisory (92) no matter how many wires agree.
        value = min(88, 62 + 8 * max(0, count - 2))
    else:
        value = 36 if placed else 24
    # A newer digital outlet sits below an established national outlet such as The Standard.
    if "secondary national" in source_tier.casefold():
        value -= 8
    return max(0, min(100, int(value)))


def _story_key(props: dict) -> tuple[str, str, str]:
    title = " ".join(str(props.get("title") or "").split()).casefold()[:180]
    stamp = str(props.get("timestamp") or props.get("published_at") or "")
    country = " ".join(str(props.get("country") or "").split()).casefold()
    return title, stamp, country


def _named_source(row: dict) -> bool:
    for key in ("source_name", "source_label", "account", "source_handle", "handle"):
        if " ".join(str(row.get(key) or "").split()):
            return True
    return False
