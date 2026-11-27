"""Seven-day country incident summary for the risk assessment builder.

The report uses public incident fields only. Personal and medical text is not
accepted and is not returned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ALIASES = {
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "america": "united states",
    "uk": "united kingdom",
    "uae": "united arab emirates",
    "turkiye": "turkey",
    "czechia": "czech republic",
}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _norm(value: object) -> str:
    text = " ".join(str(value or "").lower().replace("&", " and ").split())
    return ALIASES.get(text, text)


def _props(feature: dict) -> dict:
    if "properties" in feature and isinstance(feature.get("properties"), dict):
        return feature["properties"]
    return feature


def _coords(feature: dict, props: dict):
    geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    lat = props.get("lat")
    lng = props.get("lng")
    if isinstance(coords, list) and len(coords) >= 2:
        lng = coords[0] if lng is None else lng
        lat = coords[1] if lat is None else lat
    try:
        lat_n = round(float(lat), 5)
        lng_n = round(float(lng), 5)
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat_n <= 90 and -180 <= lng_n <= 180):
        return None, None
    return lat_n, lng_n


def _when(value: object, now: datetime):
    text = " ".join(str(value or "").split())
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        moment = None
    if moment is None:
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.strptime(text.replace("GMT", "").strip(), fmt.replace(" %Z", ""))
                break
            except ValueError:
                continue
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _matches_country(props: dict, country: str) -> bool:
    wanted = _norm(country)
    if not wanted:
        return False
    fields = [
        props.get("country"),
        props.get("region"),
        props.get("theatre"),
        props.get("area"),
        props.get("location_name"),
    ]
    for field in fields:
        name = _norm(field)
        if not name:
            continue
        if name == wanted or wanted in name or name in wanted:
            return True
    return False


def _matches_area(props: dict, area: str) -> bool:
    wanted = _norm(area)
    if not wanted:
        return True
    blob = " ".join(
        _norm(props.get(key))
        for key in ("area", "location_name", "governorate", "title", "summary")
    )
    return wanted in blob


def _source_link(props: dict) -> str:
    for key in ("source", "source_url"):
        link = str(props.get(key) or "").strip()
        if link.startswith("https://") or link.startswith("http://"):
            return link
    return ""


def build_report(features: list[dict], country: str, area: str = "", days: int = 7, now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    window = max(1, min(int(days or 7), 30))
    cutoff = moment - timedelta(days=window)
    country_name = " ".join(str(country or "").split()) or "the selected country"
    seen = set()
    matched = []
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        props = _props(feature)
        title = " ".join(str(props.get("title") or "").split())
        if not title:
            continue
        try:
            import feed_quality

            if feed_quality.is_noise(title, props.get("summary"), props.get("source")):
                continue
        except Exception:
            pass
        if not _matches_country(props, country_name):
            continue
        if not _matches_area(props, area):
            continue
        when = _when(props.get("timestamp"), moment)
        if when is not None and when < cutoff:
            continue
        key = (_source_link(props) or title).lower()
        if key in seen:
            continue
        seen.add(key)
        severity = str(props.get("severity") or "medium").strip().lower()
        if severity not in SEVERITY_RANK:
            severity = "medium"
        lat, lng = _coords(feature, props)
        matched.append({
            "title": title[:180],
            "summary": " ".join(str(props.get("summary") or "").split())[:320],
            "timestamp": props.get("timestamp") or "",
            "area": props.get("area") or props.get("location_name") or props.get("governorate") or "",
            "severity": severity,
            "category": props.get("feed_category") or props.get("category") or "OSINT",
            "source_name": props.get("source_name") or props.get("source_label") or "OSINT",
            "source": _source_link(props),
            "lat": lat,
            "lng": lng,
            "_when": when or datetime.min.replace(tzinfo=timezone.utc),
        })
    matched.sort(key=lambda item: (SEVERITY_RANK[item["severity"]], -item["_when"].timestamp()))
    by_severity = {"high": 0, "medium": 0, "low": 0}
    by_category: dict[str, int] = {}
    for item in matched:
        by_severity[item["severity"]] += 1
        label = str(item["category"] or "OSINT")
        by_category[label] = by_category.get(label, 0) + 1
    public = []
    for item in matched[:12]:
        public.append({key: value for key, value in item.items() if key != "_when"})
    note = ""
    if not matched:
        note = (
            f"No verified OSINT incidents logged for {country_name} in the last {window} days; "
            "baseline advisory & regional monitoring active"
        )
    return {
        "ok": True,
        "country": country_name,
        "area": " ".join(str(area or "").split()),
        "days": window,
        "total": len(matched),
        "by_severity": by_severity,
        "by_category": by_category,
        "incidents": public,
        "note": note,
    }
