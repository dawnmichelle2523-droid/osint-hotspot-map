"""Client-branded white-label studio and embeddable country-risk widget.

The widget reads the local live feed (hotspots and stored rows) and the same
risk index the reports use. It never calls xAI. The "Powered by GeoRiskIntel"
bar is rendered by the server for every mode and is not controlled by query
parameters. The official mark at /static/logo.png sits in that bar. A client
logo, accent, theme, or hide flag cannot replace or remove it.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from flask import Blueprint, Response, request

import analytics_store
from country_incidents import _coords, _matches_country, _props, _when

whitelabel_bp = Blueprint("whitelabel", __name__)

ROOT = Path(__file__).resolve().parent
STUDIO_PAGE = ROOT / "static" / "white-label.html"
ATTRIBUTION = "Powered by GeoRiskIntel"
PRODUCT_LOGO_PATH = "/static/logo.png"
DEFAULT_ACCENT = "#D4AF37"
DEFAULT_COUNTRY = "Yemen"
MODES = {"badge", "ticker", "map"}
THEMES = {"dark", "light"}
WINDOW_DAYS = 7
HEX_COLOR = re.compile(r"^#(?:[0-9A-F]{3}|[0-9A-F]{6})$")
CONFIDENCE_LABELS = {"Verified", "Single-source", "Unverified", "Corroborated", "Claim", "Monitoring"}
TIER_COLORS = {
    "LOW": "#0F766E",
    "MEDIUM": "#D97706",
    "HIGH": "#EA580C",
    "EXTREME": "#E11D48",
}
SEVERITY_DOTS = {"high": "#E11D48", "medium": "#D97706", "low": "#0F766E"}
CENTROIDS = {
    "Yemen": (15.55, 48.52),
    "Syria": (34.80, 38.99),
    "Iraq": (33.22, 43.68),
    "Lebanon": (33.85, 35.86),
    "Israel": (31.05, 34.85),
    "Jordan": (31.24, 36.51),
    "Saudi Arabia": (23.89, 45.08),
    "United Arab Emirates": (23.42, 53.85),
    "Egypt": (26.82, 30.80),
    "Turkey": (38.96, 35.24),
    "Iran": (32.43, 53.69),
    "Sudan": (12.86, 30.22),
    "Ukraine": (48.38, 31.17),
    "United Kingdom": (54.00, -2.50),
    "France": (46.23, 2.21),
    "Germany": (51.17, 10.45),
    "United States": (39.83, -98.58),
    "Canada": (56.13, -106.35),
    "Nigeria": (9.08, 8.68),
    "Kenya": (0.02, 37.91),
    "Somalia": (5.15, 46.20),
    "India": (20.59, 78.96),
    "Pakistan": (30.38, 69.35),
    "China": (35.86, 104.20),
    "Japan": (36.20, 138.25),
    "Australia": (-25.27, 133.78),
    "Brazil": (-14.24, -51.93),
    "Mexico": (23.63, -102.55),
    "Middle East": (29.00, 43.00),
    "UK & Europe": (50.00, 10.00),
    "Africa": (2.00, 20.00),
    "Americas": (15.00, -90.00),
    "Asia-Pacific": (15.00, 120.00),
    "South Asia": (22.00, 79.00),
    "Maritime": (14.00, 48.00),
    "Global": (15.00, 10.00),
}


def _page_origin() -> str:
    """Scheme and host of this widget response. Paths and header junk are dropped."""
    try:
        root = request.url_root or ""
    except RuntimeError:
        return ""
    parts = urlsplit(root)
    scheme = (parts.scheme or "").lower()
    netloc = parts.netloc or ""
    if scheme not in {"http", "https"} or not netloc:
        return ""
    if any(ch in netloc for ch in " ;\"'<>\\,") or any(ord(ch) < 33 for ch in netloc):
        return ""
    return f"{scheme}://{netloc}"


def _product_logo_src() -> str:
    """Official GeoRiskIntel mark. Never read from the query string or client logo."""
    origin = _page_origin()
    if not origin:
        return PRODUCT_LOGO_PATH
    return origin + PRODUCT_LOGO_PATH


def _widget_csp() -> str:
    """Allow the same-origin mark and https client logos. Block everything else."""
    origin = _page_origin()
    img_src = "img-src 'self' https:"
    if origin:
        # Sandboxed embeds have an opaque origin, so 'self' does not match the
        # host that served the document. Name that host explicitly.
        img_src += " " + origin
    return (
        "frame-ancestors *; "
        "default-src 'none'; "
        f"{img_src}; "
        "style-src 'unsafe-inline'; "
        "script-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "object-src 'none'"
    )


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def sanitize_org(value: object) -> str:
    """Plain organization name. Tags and scriptable schemes are removed."""
    text = str(value or "")
    text = re.sub(r"(?i)javascript\s*:", "", text)
    text = re.sub(r"(?i)vbscript\s*:", "", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("<", " ").replace(">", " ")
    text = "".join(ch for ch in text if ch.isprintable())
    return " ".join(text.split())[:80]


def sanitize_accent(value: object) -> str:
    """Accept a #RGB or #RRGGBB colour and return a 6-digit hex. Anything else is the product gold."""
    text = " ".join(str(value or "").split())
    if not text:
        return DEFAULT_ACCENT
    if not text.startswith("#"):
        text = "#" + text
    text = text.upper()
    if not HEX_COLOR.fullmatch(text):
        return DEFAULT_ACCENT
    if len(text) == 4:
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text


def sanitize_logo(value: object) -> str:
    """https logo URLs only. javascript:, data:, and markup breakouts are dropped."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 400:
        return ""
    if any(ch in raw for ch in "<>\"'\\ \n\r\t"):
        return ""
    if any(token in raw.lower() for token in ("%00", "%0a", "%0d")):
        return ""
    try:
        decoded = unquote(raw)
    except Exception:
        return ""
    lowered = decoded.lower()
    if "javascript:" in lowered or "vbscript:" in lowered or lowered.startswith("data:"):
        return ""
    if "data:" in lowered:
        return ""
    if any(ch in decoded for ch in "<>\"'\\\n\r\t"):
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not parts.netloc:
        return ""
    if parts.username or parts.password:
        return ""
    host = parts.hostname or ""
    if not host or ".." in host or any(ch in host for ch in "<>\"'"):
        return ""
    return raw


def sanitize_place(value: object) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(ch for ch in text if ch.isalnum() or ch in " &'.,-/")
    return text[:60].strip(" -/,.'")


def _choice(value: object, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def read_brand(args) -> dict:
    """Brand and scope from the query string. Attribution flags are ignored."""
    country = sanitize_place(args.get("country"))
    region = sanitize_place(args.get("region"))
    if not country and not region:
        country = DEFAULT_COUNTRY
    return {
        "org": sanitize_org(args.get("org")),
        "logo": sanitize_logo(args.get("logo")),
        "accent": sanitize_accent(args.get("accent")),
        "theme": _choice(args.get("theme"), THEMES, "dark"),
        "mode": _choice(args.get("mode"), MODES, "badge"),
        "country": country,
        "region": region,
    }


def _members(region: str) -> list[str]:
    name = analytics_store.canonical_region(region) or region
    if not name or name == "Global":
        return []
    return [country for country, label in analytics_store.COUNTRY_REGION.items() if label == name]


def _in_scope(props: dict, country: str, region: str) -> bool:
    if country:
        return _matches_country(props, country)
    canonical = analytics_store.canonical_region(region)
    if not region or canonical == "Global":
        return True
    label = canonical or region
    if _matches_country(props, label):
        return True
    for member in _members(label):
        if _matches_country(props, member):
            return True
    return False


def _severity(value: object) -> str:
    text = str(value or "medium").strip().lower()
    if text in {"high", "medium", "low"}:
        return text
    if text in {"extreme", "critical", "severe"}:
        return "high"
    return "medium"


def _confidence(props: dict) -> str:
    explicit = str(props.get("confidence") or "").strip().lower()
    if explicit in {"confirmed", "verified"}:
        return "Verified"
    if explicit == "claim":
        return "Claim"
    tier = str(props.get("verification_tier") or "").strip().lower()
    if "official" in tier or "primary" in tier:
        return "Verified"
    if "single-source" in tier or "single source" in tier or "state-affiliated" in tier:
        return "Single-source"
    if "unverified" in tier or explicit == "unverified":
        return "Unverified"
    try:
        if int(props.get("corroboration_count") or 0) >= 2:
            return "Corroborated"
    except (TypeError, ValueError):
        pass
    return "Monitoring"


def collect_live_features() -> list[dict]:
    """Local live incidents only. No network and no model calls."""
    features: list[dict] = []
    try:
        import serve_map

        features.extend(serve_map.load_features() or [])
    except Exception:
        return features
    try:
        import serve_map

        database = Path(serve_map.FEEDS_DB)
        if database.is_file() and database.stat().st_size > 0:
            features.extend(serve_map.feed_features() or [])
    except Exception:
        pass
    return features


def _existing_archive() -> Path | None:
    candidates = []
    for key in ("GEORISKINTEL_DATA_DIR", "DATA_DIR"):
        data_dir = (os.environ.get(key) or "").strip()
        if data_dir:
            folder = Path(data_dir)
            candidates.append(folder if folder.suffix == ".db" else folder / "incidents.db")
    try:
        candidates.append(analytics_store.resolve_data_dir() / "incidents.db")
    except OSError:
        pass
    candidates.append(Path("/data/incidents.db"))
    candidates.append(ROOT / "data" / "incidents.db")
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _archive_risk(scope: str) -> dict | None:
    path = _existing_archive()
    if path is None or not scope:
        return None
    try:
        db = analytics_store.Store(path)
        try:
            return analytics_store.query_analytics(scope, period="weekly", store=db)
        finally:
            db.close()
    except Exception:
        return None


def _mentions(item: dict, country: str) -> bool:
    """True when the incident itself names the country, not only a shared region tag."""
    if not country:
        return False
    wanted = " ".join(country.casefold().split())
    blob = " ".join(
        str(item.get(key) or "")
        for key in ("title", "area", "country")
    ).casefold()
    return wanted in blob


def _pick_headline(items: list[dict], country: str) -> dict | None:
    """Prefer a verified headline that names the country, then any headline that names it."""
    if not items:
        return None

    def newest(pool: list[dict]) -> dict:
        return max(pool, key=lambda item: item["when_sort"])

    verified = [item for item in items if item["confidence"] == "Verified"]
    named = [item for item in items if _mentions(item, country)] if country else []
    named_verified = [item for item in named if item["confidence"] == "Verified"]
    if named_verified:
        return newest(named_verified)
    if named:
        return newest(named)
    if verified:
        return newest(verified)
    return newest(items)


def load_widget_facts(country: str, region: str, features: list | None = None) -> dict:
    """7-day risk, count, headline, and confidence for one country or region."""
    rows = collect_live_features() if features is None else features
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    display_country = analytics_store.canonical_country(country) or country
    display_region = analytics_store.canonical_region(region) or region
    label = display_country or display_region or "Global"
    matched = []
    try:
        import feed_quality
    except Exception:
        feed_quality = None
    for feature in rows or []:
        if not isinstance(feature, dict):
            continue
        props = _props(feature)
        title = " ".join(str(props.get("title") or "").split())
        summary = " ".join(str(props.get("summary") or props.get("description") or "").split())
        if not title:
            continue
        if feed_quality is not None:
            try:
                if feed_quality.is_noise(title, summary, props.get("source")):
                    continue
            except Exception:
                pass
        if not _in_scope(props, display_country, display_region):
            continue
        when = _when(props.get("timestamp"), now)
        if when is not None and when < cutoff:
            continue
        lat, lng = _coords(feature, props)
        severity = _severity(props.get("severity"))
        confidence = _confidence(props)
        if confidence not in CONFIDENCE_LABELS:
            confidence = "Monitoring"
        matched.append({
            "title": title[:180],
            "area": " ".join(str(props.get("area") or props.get("location_name") or "").split())[:80],
            "country": " ".join(str(props.get("country") or "").split())[:80],
            "region": " ".join(str(props.get("region") or "").split())[:80],
            "severity": severity,
            "confidence": confidence,
            "lat": lat,
            "lng": lng,
            "when_sort": when or datetime.min.replace(tzinfo=timezone.utc),
        })
    counts = {"HIGH": 0, "MED": 0, "LOW": 0, "INFO": 0}
    for item in matched:
        bucket = {"high": "HIGH", "medium": "MED", "low": "LOW"}[item["severity"]]
        counts[bucket] += 1
    index = analytics_store.risk_index(counts, WINDOW_DAYS)
    tier = analytics_store.risk_level(index)
    chosen = _pick_headline(matched, display_country)
    if chosen is None:
        archive = _archive_risk(label)
        if archive and archive.get("events"):
            index = float(archive["risk"]["index"])
            tier = str(archive["risk"]["level"])
            if tier not in TIER_COLORS:
                tier = analytics_store.risk_level(index)
            points = []
            for point in archive.get("points") or []:
                if point.get("lat") is None or point.get("lng") is None:
                    continue
                points.append({
                    "lat": point["lat"],
                    "lng": point["lng"],
                    "severity": str(point.get("severity") or "low").lower(),
                    "title": " ".join(str(point.get("title") or "").split())[:180],
                })
            headline = ""
            if archive.get("points"):
                headline = " ".join(str(archive["points"][0].get("title") or "").split())[:180]
            if not headline:
                headline = (
                    f"No verified OSINT incidents logged for {label} in the last {WINDOW_DAYS} days; "
                    "baseline advisory & regional monitoring active"
                )
            return {
                "label": label,
                "country": display_country,
                "region": display_region,
                "tier": tier,
                "index": index,
                "incident_count": int(archive["events"]),
                "headline": headline,
                "headline_kind": "Latest archive headline" if archive.get("points") else "Monitoring baseline",
                "confidence": "Monitoring",
                "points": points[:40],
                "severity_counts": counts,
            }
        headline = (
            f"No verified OSINT incidents logged for {label} in the last {WINDOW_DAYS} days; "
            "baseline advisory & regional monitoring active"
        )
        return {
            "label": label,
            "country": display_country,
            "region": display_region,
            "tier": tier if tier in TIER_COLORS else "LOW",
            "index": index,
            "incident_count": 0,
            "headline": headline,
            "headline_kind": "Monitoring baseline",
            "confidence": "Monitoring",
            "points": [],
            "severity_counts": counts,
        }
    points = []
    for item in matched:
        if item["lat"] is None or item["lng"] is None:
            continue
        points.append({
            "lat": item["lat"],
            "lng": item["lng"],
            "severity": item["severity"],
            "title": item["title"],
        })
        if len(points) >= 40:
            break
    kind = "Latest verified headline" if chosen["confidence"] == "Verified" else "Latest headline"
    return {
        "label": label,
        "country": display_country,
        "region": display_region,
        "tier": tier if tier in TIER_COLORS else "LOW",
        "index": index,
        "incident_count": len(matched),
        "headline": chosen["title"],
        "headline_kind": kind,
        "confidence": chosen["confidence"],
        "points": points,
        "severity_counts": counts,
    }


def _initials(org: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", org) if part]
    if not parts:
        return "GR"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _logo_html(org: str, logo: str, accent: str) -> str:
    if logo:
        label = org or "Client logo"
        return (
            f'<img class="logo" src="{_esc(logo)}" alt="{_esc(label)}" '
            'referrerpolicy="no-referrer" width="32" height="32">'
        )
    letters = _esc(_initials(org))
    return (
        f'<svg class="mark" viewBox="0 0 64 64" role="img" aria-label="{_esc(org or "Client badge")}">'
        f'<rect width="64" height="64" rx="14" fill="{accent}"/>'
        f'<text x="32" y="40" text-anchor="middle" font-size="24" font-family="Segoe UI, sans-serif" '
        f'font-weight="700" fill="#FFFFFF">{letters}</text></svg>'
    )


def _map_svg(facts: dict, accent: str, theme: str) -> str:
    """Zoom the mini-map to the incidents so markers are visible. Hover titles need no script."""
    grid = "#CBD5E1" if theme == "light" else "#334155"
    ink = "#0F172A" if theme == "light" else "#E2E8F0"
    plotted = []
    for point in facts.get("points") or []:
        try:
            lat = float(point["lat"])
            lng = float(point["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        plotted.append((lat, lng, str(point.get("severity") or ""), str(point.get("title") or "")))
    if not plotted:
        centroid = CENTROIDS.get(facts.get("country") or "") or CENTROIDS.get(facts.get("label") or "")
        if centroid:
            plotted.append((float(centroid[0]), float(centroid[1]), "", str(facts.get("label") or "")))
    if not plotted:
        title = _esc(f"{facts.get('label') or 'Country'} incidents")
        return f'<svg class="map" viewBox="0 0 360 180" role="img" aria-label="{title}"><title>{title}</title></svg>'
    lats = [item[0] for item in plotted]
    lngs = [item[1] for item in plotted]
    south = min(lats)
    north = max(lats)
    west = min(lngs)
    east = max(lngs)
    pad_lat = max(3.5, (north - south) * 0.55)
    pad_lng = max(3.5, (east - west) * 0.55)
    south -= pad_lat
    north += pad_lat
    west -= pad_lng
    east += pad_lng
    span_lng = max(east - west, 1.0)
    span_lat = max(north - south, 1.0)
    if span_lng / span_lat < 2:
        grow = (span_lat * 2 - span_lng) / 2
        west -= grow
        east += grow
    else:
        grow = (span_lng / 2 - span_lat) / 2
        south -= grow
        north += grow
    span_lng = east - west
    span_lat = north - south

    def project(lat: float, lng: float) -> tuple[float, float]:
        x = (lng - west) / span_lng * 360.0
        y = (north - lat) / span_lat * 180.0
        return max(8.0, min(352.0, x)), max(8.0, min(172.0, y))

    lines = []
    step = 10 if span_lng < 40 else 20
    start_lng = int(west // step) * step
    lng = start_lng
    while lng <= east:
        x, _ = project(north, lng)
        lines.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="180" stroke="{grid}" stroke-width="0.6"/>')
        lng += step
    start_lat = int(south // step) * step
    lat = start_lat
    while lat <= north:
        _, y = project(lat, west)
        lines.append(f'<line x1="0" y1="{y:.1f}" x2="360" y2="{y:.1f}" stroke="{grid}" stroke-width="0.6"/>')
        lat += step
    dots = []
    for lat, lng, severity, label in plotted:
        x, y = project(lat, lng)
        color = SEVERITY_DOTS.get(severity, accent)
        tip = _esc(label or facts.get("label") or "Incident")
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="{color}" stroke="#ffffff" stroke-width="1">'
            f"<title>{tip}</title></circle>"
        )
    place = _esc(facts.get("label") or "Country")
    title = _esc(f"{facts.get('label') or 'Country'} incidents")
    return (
        f'<svg class="map" viewBox="0 0 360 180" role="img" aria-label="{title}">'
        f"<title>{title}</title>{''.join(lines)}{''.join(dots)}"
        f'<text x="8" y="16" fill="{ink}" font-size="11" font-family="Segoe UI, sans-serif" font-weight="700">{place}</text>'
        f"</svg>"
    )


def _powered(home: str) -> str:
    """Fixed attribution bar. The logo source is the product file, not brand input."""
    return (
        f'<a class="powered" data-attribution="required" href="{_esc(home)}" '
        'target="_blank" rel="noopener noreferrer">'
        f'<img class="powered-logo" src="{_esc(_product_logo_src())}" alt="GeoRiskIntel" '
        f'decoding="async">{ATTRIBUTION}</a>'
    )


def render_widget(facts: dict, brand: dict, home: str) -> str:
    """Self-contained widget. The attribution bar is always part of the document."""
    mode = brand["mode"] if brand["mode"] in MODES else "badge"
    theme = brand["theme"] if brand["theme"] in THEMES else "dark"
    accent = brand["accent"] if HEX_COLOR.fullmatch(str(brand["accent"])) else DEFAULT_ACCENT
    if len(accent) == 4:
        accent = "#" + "".join(ch * 2 for ch in accent[1:])
    tier = facts.get("tier") if facts.get("tier") in TIER_COLORS else "LOW"
    tier_color = TIER_COLORS[tier]
    org = brand.get("org") or ""
    logo = _logo_html(org, brand.get("logo") or "", accent)
    place = _esc(facts.get("label") or "Country")
    region = _esc(facts.get("region") or "")
    headline = _esc(facts.get("headline") or "")
    kind = _esc(facts.get("headline_kind") or "Latest headline")
    confidence = _esc(facts.get("confidence") if facts.get("confidence") in CONFIDENCE_LABELS else "Monitoring")
    count = int(facts.get("incident_count") or 0)
    index = facts.get("index")
    try:
        index_text = f"{float(index):.1f}"
    except (TypeError, ValueError):
        index_text = "0.0"
    org_html = f'<strong class="org">{_esc(org)}</strong>' if org else ""
    region_html = f'<span class="region">{region}</span>' if region and region != place else ""
    counts = facts.get("severity_counts") or {}
    mix = f'{counts.get("HIGH", 0)} high · {counts.get("MED", 0)} medium · {counts.get("LOW", 0)} low'
    map_html = _map_svg(facts, accent, theme) if mode == "map" else ""
    ticker_copy = (
        f"{place} · {tier} · {count} in 7 days · {headline} · {confidence}"
    )
    if mode == "ticker":
        body = (
            f'<div class="ticker">{logo}<div class="scroll"><div class="track">'
            f"<span>{org_html} {ticker_copy}</span>"
            f'<span aria-hidden="true">{ticker_copy}</span>'
            f"</div></div></div>"
        )
    else:
        body = (
            f'<header class="top">{logo}<div><p class="place">{place}</p>'
            f'<p class="sub">{org_html}{region_html}</p></div>'
            f'<span class="tier" style="background:{tier_color}">{tier}</span></header>'
            f"{map_html}"
            f'<p class="stats"><b data-count="{count}">{count}</b> in the last 7 days'
            f'<span class="index">Index {index_text} / 10</span></p>'
            f'<p class="mix">{_esc(mix)}</p>'
            f'<p class="kind">{kind}</p><p class="headline">{headline}</p>'
            f'<p class="confidence">Confidence <span>{confidence}</span></p>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <link rel="icon" href="/static/favicon.png">
  <title>{_esc(org or "GeoRiskIntel")} country risk</title>
  <style>
    :root {{ color-scheme: {theme}; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; height: 100%; }}
    body {{
      display: flex; flex-direction: column; overflow: hidden;
      background: #07111F; color: #E2E8F0;
      font: 13px/1.4 "Segoe UI", system-ui, sans-serif;
      border: 2px solid {accent};
      --accent: {accent};
    }}
    body[data-theme="light"] {{ background: #F8FAFC; color: #0F172A; }}
    main {{ flex: 1 1 auto; min-height: 0; overflow: auto; padding: 10px 12px 6px; }}
    .top {{ display: flex; align-items: center; gap: 8px; }}
    .logo, .mark {{ width: 32px; height: 32px; border-radius: 8px; flex: 0 0 auto; object-fit: cover; display: block; }}
    .place {{ margin: 0; font-size: 16px; font-weight: 800; }}
    .sub {{ margin: 0; color: #94A3B8; font-size: 12px; display: flex; gap: 6px; flex-wrap: wrap; }}
    body[data-theme="light"] .sub, body[data-theme="light"] .kind, body[data-theme="light"] .mix {{ color: #475569; }}
    .org {{ color: var(--accent); }}
    .tier {{
      margin-left: auto; color: #fff; border-radius: 999px; padding: 3px 8px;
      font-size: 11px; font-weight: 800; letter-spacing: 0.06em;
    }}
    .map {{ width: 100%; height: 150px; display: block; margin-top: 8px; background: rgba(15, 23, 42, 0.35); border-radius: 8px; }}
    body[data-theme="light"] .map {{ background: #E2E8F0; }}
    .stats {{ margin: 8px 0 0; font-weight: 700; }}
    .stats b {{ font-size: 20px; color: var(--accent); }}
    .index {{ margin-left: 8px; font-weight: 650; font-size: 12px; }}
    .mix, .kind {{ margin: 2px 0 0; color: #94A3B8; font-size: 11px; }}
    .headline {{ margin: 4px 0 0; font-weight: 650; }}
    .confidence span {{
      display: inline-block; margin-left: 4px; border: 1px solid var(--accent); color: var(--accent);
      border-radius: 999px; padding: 1px 7px; font-size: 11px; font-weight: 800;
    }}
    .ticker {{ display: flex; align-items: center; gap: 8px; min-width: 0; height: 100%; }}
    .scroll {{ overflow: hidden; min-width: 0; flex: 1; }}
    .track {{ display: flex; width: max-content; animation: ticker 28s linear infinite; }}
    .track span {{ padding-right: 48px; white-space: nowrap; }}
    @keyframes ticker {{ from {{ transform: translateX(0); }} to {{ transform: translateX(-50%); }} }}
    @media (prefers-reduced-motion: reduce) {{ .track {{ animation: none; }} }}
    a.powered {{
      display: flex !important; visibility: visible !important; opacity: 1 !important;
      align-items: center; justify-content: center; gap: 6px; flex: 0 0 auto;
      min-height: 28px; margin: 0; padding: 4px 10px;
      background: #102A63; color: #D4AF37 !important;
      font-size: 11px; font-weight: 800; letter-spacing: 0.04em;
      text-decoration: none; position: relative !important;
    }}
    a.powered .powered-logo {{
      display: block !important; visibility: visible !important; opacity: 1 !important;
      height: 18px !important; width: auto !important; max-width: none !important;
      object-fit: contain !important; border-radius: 3px; flex: 0 0 auto;
      margin: 0; background: transparent; pointer-events: none;
    }}
    body[data-theme="light"] a.powered {{ background: #102A63; color: #D4AF37 !important; }}
    body[data-theme="light"] a.powered .powered-logo {{ filter: none !important; }}
    body[data-mode="ticker"] {{ flex-direction: row; align-items: stretch; }}
    body[data-mode="ticker"] main {{ display: flex; align-items: center; padding: 0 8px; }}
    body[data-mode="ticker"] a.powered {{ border-radius: 0; white-space: nowrap; }}
  </style>
</head>
<body data-theme="{theme}" data-mode="{mode}" data-tier="{tier}" data-incident-count="{count}" data-confidence="{confidence}" data-country="{place}" data-live="1" style="--accent:{accent}">
  <main>{body}</main>
  {_powered(home)}
</body>
</html>
"""


def _product_home() -> str:
    root = request.url_root or "/"
    if not root.endswith("/"):
        root += "/"
    return root


def _embed_headers(response: Response) -> Response:
    """Embed headers for customer intranets. No cookies and no third-party origins."""
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = _widget_csp()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    # A client portal iframe must not receive a tracking or session cookie.
    if "Set-Cookie" in response.headers:
        del response.headers["Set-Cookie"]
    return response


@whitelabel_bp.get("/white-label")
def white_label_studio() -> Response:
    if not STUDIO_PAGE.is_file():
        return Response("White-label studio is unavailable.", status=404, mimetype="text/plain")
    response = Response(STUDIO_PAGE.read_bytes(), mimetype="text/html; charset=utf-8")
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@whitelabel_bp.get("/embed/widget")
def embed_widget() -> Response:
    brand = read_brand(request.args)
    facts = load_widget_facts(brand["country"], brand["region"])
    response = Response(
        render_widget(facts, brand, _product_home()),
        mimetype="text/html; charset=utf-8",
    )
    return _embed_headers(response)
