"""Deterministic corroboration and location-precision badges.

Both fields are derived from feed identifiers, source tiers, the existing
cross-source confidence cluster, stored corroboration counts, and the
coordinate provenance already recorded by the ingest pipeline. No model
calls and no network access.
"""

from __future__ import annotations

import re

import confidence_scoring

CORROBORATION_OFFICIAL = "official"
CORROBORATION_MULTI = "corroborated"
CORROBORATION_SINGLE = "single"

CORROBORATION_LABELS = {
    CORROBORATION_OFFICIAL: "Official Source",
    CORROBORATION_MULTI: "Multi-Source",
    CORROBORATION_SINGLE: "Single Source",
}

PRECISION_EXACT = "exact"
PRECISION_CITY = "city"
PRECISION_REGIONAL = "regional"

PRECISION_LABELS = {
    PRECISION_EXACT: "Exact / Site",
    PRECISION_CITY: "City / District",
    PRECISION_REGIONAL: "Regional / Country",
}

ORIGIN_EXACT = {"feed-point", "site", "facility", "event", "epicentre", "epicenter"}
ORIGIN_CITY = {"place", "city", "district", "geocode", "locality", "area"}
ORIGIN_REGIONAL = {"centroid", "country", "region", "admin", "sea", "fallback"}

# Pipeline labels that already say how a coordinate was chosen.
# Bare "exact" is excluded: live annotation stamps it on any lat/lng, including centroids.
PRECISION_EXACT_LABELS = {"exact site", "site", "facility"}
PRECISION_CITY_LABELS = {
    "city / area",
    "city / governorate",
    "city / district",
    "city",
    "district",
    "town",
    "local area",
    "locality",
}
PRECISION_REGIONAL_LABELS = {
    "country",
    "country / sea fallback",
    "damascus default fallback",
    "regional",
    "region",
    "province",
    "centroid",
    "sea",
}

EXACT_FEED_IDS = {
    "usgs-quakes",
    "usgs-volcanoes",
    "firms",
    "acled",
    "emsc",
    "faa",
    "wfigs",
    "nsw-rfs",
    "vic-emergency",
    "qld-bushfire",
    "gdacs-rss",
    "jma-typhoon",
}
CITY_FEED_IDS = {
    "gdelt",
    "uk-floods",
    "cemaden",
    "eccc",
    "met-norway",
    "alert-ready",
    "nws-alerts",
    "nws-tsunami",
}
REGIONAL_FEED_IDS = {
    "uk-fcdo-atom",
    "us-advisories",
    "us-advisories-rss",
    "canada-advisories",
    "amu",
    "afintl",
    "cisa-kev",
    "cisa-news",
    "ncsc-news",
    "ncsc-advisories",
    "cert-eu",
    "swpc",
    "insight",
    "deepstate",
    "who-don",
    "bbc-uk",
    "bbc-europe",
    "bbc-africa",
    "wire-bbc",
    "wire-aj",
    "wire-f24",
    "wire-dw",
    "wire-guardian",
    "wire-icg",
    "balkan",
    "allafrica",
    "dabanga",
    "tamazuj",
    "africa-cdc",
    "dawn",
    "hindu",
    "dhaka",
    "kathmandu",
    "cmf",
    "gvp",
    "sachet",
    "inciweb",
    "jma-eq",
}

EXACT_MARKERS = (
    "earthquake.usgs.gov",
    "volcanoes.usgs.gov",
    "usgs",
    "firms.modaps",
    "nasa firms",
    "eonet.gsfc.nasa.gov",
    "eonet",
    "seismicportal.eu",
    "emsc",
    "gdacs.org",
    "gdacs",
    "nasstatus.faa.gov",
    "acleddata.com",
    "acled",
)
CITY_MARKERS = (
    "environment.data.gov.uk",
    "flood-monitoring",
    "api.weather.gov",
    "gdeltproject.org",
    "emergency.vic.gov.au",
)
REGIONAL_MARKERS = (
    "travel.state.gov",
    "foreign-travel-advice",
    "travel.gc.ca",
    "cisa.gov",
    "ncsc.gov.uk",
    "cert.europa.eu",
    "deepstatemap.live",
)

PARSER_ORIGINS = {
    "usgs": "feed-point",
    "geojson": "feed-point",
    "gdacs": "feed-point",
    "gdacs-rss": "feed-point",
    "emsc": "feed-point",
    "faa": "site",
    "volcano": "site",
    "wa": "feed-point",
    "cemaden": "place",
    "floods": "place",
    "gdelt": "place",
    "jma": "centroid",
    "jma-typhoon": "feed-point",
    "uk-atom": "centroid",
    "us-advisory": "centroid",
    "us-rss": "centroid",
    "canada-advisory": "centroid",
    "rss-centroid": "centroid",
    "kev": "centroid",
    "swpc": "centroid",
    "insight": "centroid",
    "deepstate": "region",
    "nws": "place",
    "nhc": "centroid",
    "atom-point": "feed-point",
    "cap": "place",
    "ifrc": "feed-point",
    "who": "centroid",
    "cisa": "centroid",
    "placed-rss": "centroid",
    "security-rss": "centroid",
}

_ORIGIN_PRECISION = {
    "feed-point": "Exact",
    "site": "Exact Site",
    "facility": "Exact Site",
    "event": "Exact",
    "place": "City / Area",
    "city": "City / Area",
    "district": "City / District",
    "centroid": "Country",
    "country": "Country",
    "region": "Country",
    "admin": "Country",
    "sea": "Country / Sea Fallback",
    "fallback": "Country / Sea Fallback",
}

_COUNTRY_OR_SEA = {
    "iran", "iraq", "syria", "yemen", "sudan", "oman", "qatar", "kuwait",
    "bahrain", "jordan", "israel", "saudi", "saudi arabia", "lebanon",
    "uae", "united arab emirates", "gaza", "red sea", "bab el mandeb",
    "bab al mandab", "strait of hormuz", "hormuz", "persian gulf",
    "arabian gulf", "gulf of oman", "afghanistan", "ukraine", "japan",
    "united states", "united kingdom", "canada", "australia", "libya",
    "somalia", "ethiopia", "pakistan", "india", "maritime",
}
_SITE_RE = re.compile(
    r"\b(crossing|airport|airbase|air base|checkpoint|refinery|terminal|canal|naval base|facility|port)\b",
    re.IGNORECASE,
)
_ADMIN_RE = re.compile(
    r"\b(governorate|province|oblast|state|region|prefecture)\b",
    re.IGNORECASE,
)


def default_coord_origin(feed: dict) -> str:
    """Coordinate provenance for one catalog feed, before a parser override."""
    if not isinstance(feed, dict):
        return ""
    feed_id = str(feed.get("id") or "").strip().lower()
    bucket = _feed_bucket(feed_id)
    if bucket == PRECISION_EXACT:
        return "feed-point"
    if bucket == PRECISION_CITY:
        return "place"
    if bucket == PRECISION_REGIONAL:
        return "centroid"
    return PARSER_ORIGINS.get(str(feed.get("parser") or "").strip().lower(), "")


def precision_for_origin(origin: object) -> str:
    return _ORIGIN_PRECISION.get(str(origin or "").strip().lower(), "")


def score_with_stored_count(incidents: list) -> list:
    """Run confidence scoring without discarding a count already stored on the row.

    Ingest increments corroboration_count when another channel matches the
    same story. The cluster scorer replaces that count with the size of the
    current batch, so the earlier count is kept beside it for the badge.
    """
    stored = [_positive_int(_mapping(item).get("corroboration_count")) for item in incidents or []]
    confidence_scoring.score_incidents(incidents)
    for item, count in zip(incidents or [], stored):
        target = _mapping(item)
        if target is not None and count is not None:
            target["_stored_corroboration_count"] = count
    return incidents


def classify_corroboration(incident: dict) -> dict:
    """Return level, label, source_count, and a UK English rationale."""
    view = _view_dict(incident)
    official = confidence_scoring.match_official(view)
    tier = str(view.get("confidence_tier") or "").strip().lower()
    source_tier = str(view.get("source_tier") or "").strip()
    is_official = official is not None or tier == confidence_scoring.TIER_OFFICIAL or source_tier == "Official"
    names = _source_names(view)
    scored = _positive_int(view.get("corroboration_count"))
    stored = _positive_int(view.get("_stored_corroboration_count"))
    counts = [value for value in (scored, stored, len(names) or None) if value]
    source_count = max(counts) if counts else 1
    clustered = tier == confidence_scoring.TIER_CORROBORATED or (scored or 0) >= 2 or len(names) >= 2
    if is_official:
        level = CORROBORATION_OFFICIAL
    elif clustered or source_count >= 2:
        level = CORROBORATION_MULTI
    else:
        level = CORROBORATION_SINGLE
        source_count = 1
    return {
        "level": level,
        "label": CORROBORATION_LABELS[level],
        "source_count": source_count,
        "rationale": _corroboration_rationale(level, source_count, official, view, names, clustered),
    }


def classify_location_precision(incident: dict) -> dict:
    """Return level, label, and a UK English rationale for the coordinate."""
    view = _view_dict(incident)
    origin = str(view.get("coord_origin") or "").strip().lower()
    precision = " ".join(str(view.get("geo_precision") or "").split()).lower()
    feed_id = str(view.get("feed_id") or "").strip().lower()
    blob = _identity_blob(view)
    level, rationale = _precision_from_signals(view, origin, precision, feed_id, blob)
    return {
        "level": level,
        "label": PRECISION_LABELS[level],
        "rationale": rationale,
    }


def annotate_incident(incident: dict) -> dict:
    """Attach both badge objects. Existing fields are left in place."""
    target = _mapping(incident)
    if target is None:
        return incident
    view = _view_dict(incident)
    target["corroboration"] = classify_corroboration(view)
    target["location_precision"] = classify_location_precision(view)
    target.pop("_stored_corroboration_count", None)
    return incident


def apply_to_payload(payload: dict) -> dict:
    """Copy badge objects onto feature properties and flat incident rows.

    A summary row with no coordinate of its own inherits location precision
    from the feature that shares its title, time, and country.
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    lookup: dict[tuple, dict] = {}
    features = out.get("features")
    if isinstance(features, list):
        clones = [_clone_feature(feature) for feature in features]
        for feature in clones:
            if isinstance(feature, dict):
                annotate_incident(feature)
                props = feature.get("properties")
                if isinstance(props, dict) and props.get("location_precision"):
                    lookup.setdefault(_story_key(props), props)
        out["features"] = clones
    for key in ("events", "incidents", "items"):
        rows = out.get(key)
        if not isinstance(rows, list):
            continue
        clones = []
        for row in rows:
            if not isinstance(row, dict):
                clones.append(row)
                continue
            clone = dict(row)
            annotate_incident(clone)
            if not _has_geo_signal(clone):
                match = lookup.get(_story_key(clone))
                borrowed = match.get("location_precision") if isinstance(match, dict) else None
                if isinstance(borrowed, dict):
                    clone["location_precision"] = dict(borrowed)
            clones.append(clone)
        out[key] = clones
    return out


def _precision_from_signals(view: dict, origin: str, precision: str, feed_id: str, blob: str) -> tuple[str, str]:
    place = _place_label(view)
    country = " ".join(str(view.get("country") or "").split())[:80]
    where = place or country
    if origin in ORIGIN_REGIONAL:
        return PRECISION_REGIONAL, _regional_rationale(origin, precision, where)
    if origin in ORIGIN_CITY:
        return PRECISION_CITY, _city_rationale(place)
    if origin in ORIGIN_EXACT:
        return PRECISION_EXACT, _exact_rationale(view, place)

    if precision in PRECISION_REGIONAL_LABELS:
        return PRECISION_REGIONAL, _regional_rationale(origin, precision, where)
    if precision in PRECISION_CITY_LABELS:
        return PRECISION_CITY, _city_rationale(place)
    if precision in PRECISION_EXACT_LABELS:
        return PRECISION_EXACT, _exact_rationale(view, place)

    family = _feed_bucket(feed_id) or _marker_bucket(blob)
    if precision in {"exact", "precise"}:
        if family == PRECISION_REGIONAL:
            return PRECISION_REGIONAL, _regional_rationale("centroid", precision, where)
        if family == PRECISION_CITY:
            return PRECISION_CITY, _city_rationale(place)
        return PRECISION_EXACT, _exact_rationale(view, place)
    if family:
        if family == PRECISION_EXACT:
            return PRECISION_EXACT, _exact_rationale(view, place)
        if family == PRECISION_CITY:
            return PRECISION_CITY, _city_rationale(place)
        return PRECISION_REGIONAL, _regional_rationale("centroid", precision, where)

    folded = place.casefold()
    if folded and (_SITE_RE.search(place) or _SITE_RE.search(str(view.get("area") or ""))):
        return PRECISION_EXACT, "Facility or site coordinates from the location name. This pin marks a specific place, not a country centre."
    if folded and (folded in _COUNTRY_OR_SEA or _ADMIN_RE.search(place)):
        return PRECISION_REGIONAL, _regional_rationale("", precision, where)
    if folded:
        return PRECISION_CITY, _city_rationale(place)
    if _coords(view) is not None:
        return PRECISION_CITY, "Coordinates were supplied without a place type, so this pin is treated as local-area precision rather than an exact site."
    if country:
        return PRECISION_REGIONAL, _regional_rationale("", "", country)
    return PRECISION_REGIONAL, "No precise coordinates were recorded, so placement is treated as regional."


def _feed_bucket(feed_id: str) -> str:
    feed = str(feed_id or "").strip().lower()
    if not feed:
        return ""
    if feed.startswith("eonet"):
        return PRECISION_EXACT
    if feed.startswith("meteoalarm-") or feed.startswith("bbc-") or feed.startswith("wire-") or feed.startswith("nhc"):
        return PRECISION_REGIONAL
    if feed.startswith("nws-"):
        return PRECISION_CITY
    if feed in EXACT_FEED_IDS:
        return PRECISION_EXACT
    if feed in CITY_FEED_IDS:
        return PRECISION_CITY
    if feed in REGIONAL_FEED_IDS:
        return PRECISION_REGIONAL
    return ""


def _marker_bucket(blob: str) -> str:
    if any(_marker_in(blob, marker) for marker in REGIONAL_MARKERS):
        return PRECISION_REGIONAL
    if any(_marker_in(blob, marker) for marker in CITY_MARKERS):
        return PRECISION_CITY
    if any(_marker_in(blob, marker) for marker in EXACT_MARKERS):
        return PRECISION_EXACT
    return ""


def _exact_rationale(view: dict, place: str) -> str:
    official = confidence_scoring.match_official(view)
    name = str(official.get("label") or "").strip() if official else ""
    if not name:
        name = _feed_display(view)
    if name:
        return f"Precise event or facility coordinates from {name}. This pin marks the reported site."
    if place:
        return f"Precise event or facility coordinates ({place}). This pin marks the reported site."
    return "Precise event or facility coordinates. This pin marks the reported site."


def _city_rationale(place: str) -> str:
    if place:
        return f"City or district precision ({place}). This pin marks a town or local area, not an exact site."
    return "City or district precision. This pin marks a town or local area, not an exact site."


def _regional_rationale(origin: str, precision: str, place: str) -> str:
    if precision == "damascus default fallback":
        return "Fallback placement around Damascus. This pin is not an exact site."
    if "sea" in precision or origin == "sea":
        where = place or "the named sea area"
        return f"Country or sea fallback for {where}. This pin is not an exact site."
    if place:
        return f"Approximate regional or country placement ({place}). This pin is a centroid, not an exact site."
    return "Approximate regional or country placement. This pin is a centroid, not an exact site."


def _corroboration_rationale(level: str, source_count: int, official: dict | None, view: dict, names: list[str], clustered: bool) -> str:
    if level == CORROBORATION_OFFICIAL:
        label = str((official or {}).get("label") or "").strip()
        if not label and str(view.get("source_tier") or "").strip() == "Official":
            label = _plain_source(view) or "an official source"
        feed = f"Official institutional feed ({label})." if label else "Official institutional feed."
        if source_count >= 2 and clustered:
            return f"{feed} Corroborated across {source_count} independent sources."
        return feed
    if level == CORROBORATION_MULTI:
        if clustered:
            return f"Corroborated across {source_count} independent sources."
        return f"Corroborated by {source_count} reports of the same incident."
    source = _plain_source(view) or (names[0] if names else "")
    if source and not source.lower().startswith("unnamed"):
        return f"Single source report ({source}) with no independent corroboration yet."
    return "Single source report with no independent corroboration yet."


def _feed_display(view: dict) -> str:
    feed = str(view.get("feed_id") or "").strip().lower()
    names = {
        "usgs-quakes": "USGS",
        "usgs-volcanoes": "USGS",
        "firms": "NASA FIRMS",
        "acled": "ACLED",
        "emsc": "EMSC",
        "faa": "FAA",
        "gdacs-rss": "GDACS",
    }
    return names.get(feed, "")


def _plain_source(view: dict) -> str:
    for key in ("source_name", "source_label", "account", "source_handle", "handle"):
        text = " ".join(str(view.get(key) or "").split())
        if text and " · " not in text:
            return text[:80]
    return ""


def _source_names(view: dict) -> list[str]:
    raw = view.get("corroborating_sources")
    if not isinstance(raw, list):
        return []
    names = []
    seen = set()
    for item in raw:
        text = " ".join(str(item or "").split())
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        names.append(text)
    return names


def _place_label(view: dict) -> str:
    for key in ("location_name", "area", "governorate"):
        text = " ".join(str(view.get(key) or "").split())
        if text:
            return text[:80]
    return ""


def _has_geo_signal(row: dict) -> bool:
    if str(row.get("coord_origin") or "").strip():
        return True
    if str(row.get("geo_precision") or "").strip():
        return True
    if str(row.get("feed_id") or "").strip():
        return True
    if _place_label(row):
        return True
    return _coords(row) is not None


def _coords(view: dict):
    return confidence_scoring._coords(view)


def _story_key(props: dict) -> tuple[str, str, str]:
    title = " ".join(str(props.get("title") or "").split()).casefold()[:180]
    stamp = str(props.get("timestamp") or props.get("published_at") or "")
    country = " ".join(str(props.get("country") or "").split()).casefold()
    return title, stamp, country


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 1:
        return None
    return number


def _mapping(item):
    if not isinstance(item, dict):
        return None
    props = item.get("properties")
    geometry = item.get("geometry")
    if isinstance(props, dict) and (item.get("type") == "Feature" or isinstance(geometry, dict)):
        return props
    return item


def _view_dict(item) -> dict:
    if not isinstance(item, dict):
        return {}
    props = item.get("properties")
    geometry = item.get("geometry")
    if isinstance(props, dict) and (item.get("type") == "Feature" or isinstance(geometry, dict)):
        view = dict(props)
        if isinstance(geometry, dict):
            view["geometry"] = geometry
        return view
    return dict(item)


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
