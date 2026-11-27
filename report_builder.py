"""ThreatMapr-style GeoRiskGlobal reports, HTML, email, and the delivery scheduler.

Grok is used only while building the executive summary, key developments, and
outlook. A 403, quota error, missing XAI_API_KEY, or any other failure falls
back to the statistical narrative. Ingest never reaches this module's model call.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

import analytics_store as store

LONDON = ZoneInfo("Europe/London")
MODEL_URL = "https://api.x.ai/v1/chat/completions"
BADGES = ("↑ RISING", "• NEW", "↔ STABLE", "↓ FALLING")
CADENCE_TITLE = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
    "custom": "CUSTOM",
}
CENTROIDS = {
    "Syria": (35.0, 38.0, 7),
    "Lebanon": (33.9, 35.8, 8),
    "Israel": (31.4, 34.9, 7),
    "Palestine": (31.9, 35.2, 8),
    "Iraq": (33.2, 43.7, 6),
    "Iran": (32.4, 53.7, 5),
    "Yemen": (15.6, 48.0, 6),
    "Saudi Arabia": (24.0, 45.0, 5),
    "United Arab Emirates": (24.2, 54.3, 7),
    "Sudan": (15.5, 30.2, 6),
    "Chad": (15.4, 18.7, 5),
    "Ukraine": (49.0, 31.4, 6),
    "United Kingdom": (54.5, -2.5, 6),
    "United States": (39.8, -98.6, 4),
    "Middle East": (29.5, 42.0, 5),
    "UK & Europe": (51.0, 10.0, 4),
    "Africa": (2.0, 20.0, 3),
    "South Asia": (24.0, 80.0, 4),
    "Americas": (14.0, -80.0, 3),
    "Asia-Pacific": (8.0, 115.0, 3),
    "Maritime": (16.0, 48.0, 4),
    "Global": (20.0, 10.0, 2),
}
IMPLICATIONS = {
    "ACT": "Armed contact raises the immediate risk to movement, fixed sites, and any staff near the named area.",
    "SFA": "Security-force activity can slow movement, add checkpoints, and change access with little notice.",
    "VC": "Violent crime affects door-to-door movement, drivers, and staff transiting after dark.",
    "CU": "Civil unrest can close roads, draw security forces, and escalate with short warning.",
    "HS": "The hazard can disrupt movement and essential services even where no armed actor is involved.",
    "OD": "Operational disruption can strand travellers, delay cargo, and invalidate a route plan.",
}
WATCH = {
    "ACT": "Watch for follow-on strikes, casualty updates, and official restrictions on the same area.",
    "SFA": "Watch for checkpoint expansion, raids, and curfews announced by local authorities.",
    "VC": "Watch for repeat offences, police alerts, and changes to the recommended route.",
    "CU": "Watch for crowd size, strike calls, and security-force deployments.",
    "HS": "Watch for aftershocks, flood levels, weather warnings, and route closures.",
    "OD": "Watch for airport, border, port, and airspace notices before the next movement.",
}
METHODOLOGY = (
    "GeoRiskGlobal counts each stored open-source incident once. Categories are "
    "Armed Conflict & Terrorism (ACT), Security Force Activity (SFA), Violent Crime (VC), "
    "Civil Unrest (CU), Hazards & Safety (HS), and Operational Disruption (OD). "
    "Severity is HIGH, MED, LOW, or INFO. The rolling baseline is the 30-day average "
    "for a daily report, the 8-week average for a weekly report, and the 3-month average "
    "for a monthly report. The Risk Index runs from 0.0 to 10.0 and maps to LOW, MEDIUM, "
    "HIGH, or EXTREME. Background collection does not call a language model. "
    "When a narrative says it was prepared with Grok, the figures themselves still come "
    "from the stored counts. Casualty figures are pattern-extracted from the source text "
    "and are not a verified toll. Narratives are written in UK English."
)
_start_lock = threading.Lock()
_started = False


def _esc(value: object) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _period_phrase(period: str) -> str:
    return {"daily": "day", "weekly": "week", "monthly": "month"}.get(period, "period")


def _format_range(start: str, end: str) -> str:
    start_m = store.parse_ts(start)
    end_m = store.parse_ts(end)
    if start_m is None or end_m is None:
        return f"{start} – {end}"
    left = start_m.strftime("%d %b %Y").lstrip("0")
    right = end_m.strftime("%d %b %Y").lstrip("0")
    return f"{left} – {right}"


def _pct_text(value: float | None, arrow: str) -> str:
    if value is None:
        return f"{arrow} vs a zero baseline"
    sign = "+" if value > 0 else ""
    return f"{arrow} {sign}{value:.1f}%"


def normalize_badge(value: object, fallback: str) -> str:
    text = str(value or "")
    upper = text.upper()
    if "NEW" in upper:
        return "• NEW"
    if "FALL" in upper or "↓" in text:
        return "↓ FALLING"
    if "RIS" in upper or "↑" in text:
        return "↑ RISING"
    if "STABLE" in upper or "↔" in text:
        return "↔ STABLE"
    return fallback if fallback in BADGES else "↔ STABLE"


def _leading_category(row: dict) -> str:
    best = "OD"
    best_n = -1
    for code in store.CATEGORIES:
        count = int(row.get(code) or 0)
        if count > best_n:
            best = code
            best_n = count
    return best


def _badge_for(current: int, prior: int, arrow: str) -> str:
    if prior == 0 and current > 0:
        return "• NEW"
    if arrow == "↑":
        return "↑ RISING"
    if arrow == "↓":
        return "↓ FALLING"
    return "↔ STABLE"


def fallback_narrative(analytics: dict) -> dict:
    scope = analytics["scope"]
    totals = analytics["totals"]
    risk = analytics["risk"]
    significant = analytics["significant"]
    casualties = analytics["casualties"]
    period = _period_phrase(analytics["period"])
    delta = _pct_text(totals["percent_delta"], totals["arrow"])
    leaders = sorted(analytics["breakdown"], key=lambda item: item["total"], reverse=True)
    top_name = leaders[0]["name"] if leaders and leaders[0]["total"] else "the monitored area"
    top_code = _leading_category(leaders[0]) if leaders and leaders[0]["total"] else "OD"
    summary = (
        f"During this {period}, GeoRiskGlobal logged {totals['current']} incidents in {scope}, "
        f"{delta} against the {totals['baseline_label']} of {totals['baseline']}. "
        f"The Risk Index is {risk['index']:.1f} / 10 ({risk['level']}), {risk['direction']} "
        f"versus the prior {period}. Significant incidents (MED and HIGH) total "
        f"{significant['current']} ({_pct_text(significant['percent_delta'], significant['arrow'])} "
        f"versus that baseline). The busiest area in the table is {top_name}. "
        f"Open-source reporting mentions {casualties['fatalities']} fatalities and "
        f"{casualties['injuries']} injuries."
    )
    developments = []
    for row in leaders:
        if row["total"] <= 0 and row["prior_total"] <= 0:
            continue
        code = _leading_category(row)
        badge = _badge_for(row["total"], row["prior_total"], row["arrow"])
        developments.append({
            "title": f"{row['name']}: {store.CATEGORY_NAMES[code]}",
            "badge": badge,
            "summary": (
                f"{row['name']} logged {row['total']} incidents this {period} "
                f"against {row['prior_total']} in the prior {period}. "
                f"ACT {row['ACT']}, SFA {row['SFA']}, VC {row['VC']}, "
                f"CU {row['CU']}, HS {row['HS']}, OD {row['OD']}."
            ),
            "implications": IMPLICATIONS[code],
            "what_to_watch": WATCH[code],
        })
        if len(developments) >= 4:
            break
    if not developments:
        developments.append({
            "title": f"{scope}: monitoring posture",
            "badge": "↔ STABLE",
            "summary": f"No material incidents were logged for {scope} in this {period}.",
            "implications": "The absence of logged events is not a guarantee of a quiet ground picture.",
            "what_to_watch": "Watch the next collection cycle and any official advisory before movement.",
        })
    if risk["arrow"] == "↑":
        outlook = (
            f"The near-term posture for {scope} is firmer than the prior {period}. "
            f"Treat {store.CATEGORY_NAMES[top_code]} as the lead driver and keep movement plans reversible."
        )
    elif risk["arrow"] == "↓":
        outlook = (
            f"Pressure in {scope} eased versus the prior {period}. "
            "Keep the same controls in place until a second quiet period confirms the change."
        )
    else:
        outlook = (
            f"The posture in {scope} is steady against the prior {period}. "
            "Continue routine monitoring and re-check the named areas before travel."
        )
    return {
        "executive_summary": summary,
        "key_developments": developments,
        "outlook": outlook,
    }


def _quota(text: str) -> bool:
    lowered = text.lower()
    markers = ("quota", "insufficient_quota", "billing", "credit balance", "rate limit", "too many requests")
    return any(marker in lowered for marker in markers)


def _parse_model_json(content: str) -> dict | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def synthesize(analytics: dict) -> dict | None:
    key = (os.environ.get("XAI_API_KEY") or "").strip()
    if not key:
        return None
    facts = {
        "scope": analytics["scope"],
        "period": analytics["period"],
        "date_range": _format_range(analytics["start"], analytics["end"]),
        "events": analytics["events"],
        "baseline": analytics["totals"],
        "significant": analytics["significant"],
        "risk": analytics["risk"],
        "severity": analytics["severity"],
        "casualties": {
            "fatalities": analytics["casualties"]["fatalities"],
            "injuries": analytics["casualties"]["injuries"],
        },
        "breakdown": [
            {key: row[key] for key in ("name", "total", "prior_total", "arrow", "risk_index", *store.CATEGORIES)}
            for row in analytics["breakdown"][:12]
        ],
    }
    system = (
        "You write a security assessment for GeoRiskGlobal. Use only the counts provided. "
        "Do not invent incidents, casualties, or places. Return JSON with keys "
        "executive_summary (string), outlook (string), and key_developments (array). "
        "Each key development has title, badge, summary, implications, what_to_watch. "
        "badge must be one of: ↑ RISING, • NEW, ↔ STABLE, ↓ FALLING. "
        "Write every string in UK English, using British spelling and conventions."
    )
    try:
        response = requests.post(
            MODEL_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": os.environ.get("XAI_REPORT_MODEL", "grok-3-mini"),
                "temperature": 0.2,
                "max_tokens": 900,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
                ],
            },
            timeout=25,
        )
    except requests.RequestException:
        return None
    raw = response.text or ""
    if response.status_code in {401, 403, 429} or response.status_code >= 400 or _quota(raw):
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if _quota(json.dumps(body)):
        return None
    content = ""
    choices = body.get("choices") if isinstance(body, dict) else None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = str(message.get("content") or "")
    parsed = _parse_model_json(content)
    if not parsed or not str(parsed.get("executive_summary") or "").strip():
        return None
    return parsed


def _merge_narrative(analytics: dict, model: dict | None) -> tuple[dict, str]:
    base = fallback_narrative(analytics)
    if not model:
        return base, "fallback"
    developments = []
    supplied = model.get("key_developments")
    if not isinstance(supplied, list):
        supplied = []
    for index, item in enumerate(supplied[:6]):
        if not isinstance(item, dict):
            continue
        fallback = base["key_developments"][min(index, len(base["key_developments"]) - 1)]
        developments.append({
            "title": " ".join(str(item.get("title") or fallback["title"]).split())[:180],
            "badge": normalize_badge(item.get("badge"), fallback["badge"]),
            "summary": " ".join(str(item.get("summary") or fallback["summary"]).split())[:700],
            "implications": " ".join(str(item.get("implications") or fallback["implications"]).split())[:500],
            "what_to_watch": " ".join(str(item.get("what_to_watch") or fallback["what_to_watch"]).split())[:500],
        })
    if not developments:
        developments = base["key_developments"]
    outlook = " ".join(str(model.get("outlook") or "").split()) or base["outlook"]
    summary = " ".join(str(model.get("executive_summary") or "").split()) or base["executive_summary"]
    return {
        "executive_summary": summary,
        "key_developments": developments,
        "outlook": outlook,
    }, "grok"


def _map_view(scope: str, points: list[dict]) -> tuple[float, float, int]:
    if points:
        lats = [float(point["lat"]) for point in points]
        lngs = [float(point["lng"]) for point in points]
        lat = sum(lats) / len(lats)
        lng = sum(lngs) / len(lngs)
        span = max(max(lats) - min(lats), max(lngs) - min(lngs))
        if span < 2:
            zoom = 8
        elif span < 8:
            zoom = 6
        elif span < 25:
            zoom = 5
        elif span < 60:
            zoom = 4
        else:
            zoom = 3
        return round(lat, 4), round(lng, 4), zoom
    lat, lng, zoom = CENTROIDS.get(scope, (20.0, 10.0, 2))
    return lat, lng, zoom


def _overviews(analytics: dict) -> list[dict]:
    period = _period_phrase(analytics["period"])
    rows = []
    for item in analytics["breakdown"]:
        if item["total"] <= 0:
            continue
        code = _leading_category(item)
        rows.append({
            "name": item["name"],
            "text": (
                f"{item['name']} recorded {item['total']} incidents this {period} "
                f"({item['arrow']} versus {item['prior_total']} previously). "
                f"The lead category is {store.CATEGORY_NAMES[code]}. "
                f"Risk Index {item['risk_index']:.1f} / 10 ({item['risk_level']})."
            ),
        })
        if len(rows) >= 12:
            break
    return rows


def build_document(analytics: dict, narrative: dict, source: str) -> dict:
    cadence = CADENCE_TITLE.get(analytics["period"], "WEEKLY")
    risk = analytics["risk"]
    return {
        "brand": "GeoRiskGlobal",
        "scope": analytics["scope"],
        "scope_type": analytics["scope_type"],
        "cadence": analytics["period"],
        "cadence_title": f"SECURITY ASSESSMENT · {cadence}",
        "date_range": _format_range(analytics["start"], analytics["end"]),
        "subtitle": "What changed, why it matters, and what to watch next.",
        "threat_level": risk["level"],
        "risk_index": risk["index"],
        "risk_index_label": f"{risk['index']:.1f} / 10",
        "risk_arrow": risk["arrow"],
        "coverage_count": analytics["coverage_count"],
        "coverage_label": analytics["coverage_label"],
        "events_logged": analytics["events"],
        "confidentiality": "For clients only, not for copy or distribution.",
        "analytics": analytics,
        "executive_summary": narrative["executive_summary"],
        "key_developments": narrative["key_developments"],
        "outlook": narrative["outlook"],
        "overviews": _overviews(analytics),
        "methodology": METHODOLOGY,
        "narrative_source": source,
        "generated_at": store.format_ts(datetime.now(timezone.utc)),
    }


def _level_bar(active: str) -> str:
    parts = []
    for level in ("LOW", "MEDIUM", "HIGH", "EXTREME"):
        klass = "on" if level == active else ""
        parts.append(f'<span class="seg {level.lower()} {klass}">{level}</span>')
    return '<div class="level-bar">' + "".join(parts) + "</div>"


def _severity_rows(severity: dict) -> str:
    colors = {"HIGH": "#9f1239", "MED": "#b45309", "LOW": "#1d4ed8", "INFO": "#475569"}
    peak = max([1, *[int(severity.get(level) or 0) for level in store.SEVERITIES]])
    rows = []
    for level in store.SEVERITIES:
        count = int(severity.get(level) or 0)
        width = max(2, round(count / peak * 100)) if count else 0
        rows.append(
            "<div class=\"sev-row\">"
            f"<span>{level}</span>"
            f"<span class=\"track\"><span class=\"fill\" style=\"width:{width}%;background:{colors[level]}\"></span></span>"
            f"<strong>{count}</strong></div>"
        )
    return "".join(rows)


def _series_chart(series: list[dict]) -> str:
    peak = max([1, *[int(item["count"]) for item in series]])
    bars = []
    for item in series:
        height = round(int(item["count"]) / peak * 100)
        bars.append(
            "<div class=\"col\">"
            f"<span class=\"count\">{item['count']}</span>"
            f"<span class=\"stick\" style=\"height:{max(height, 2)}%\"></span>"
            f"<span class=\"lbl\">{_esc(item['label'])}</span></div>"
        )
    return '<div class="chart">' + "".join(bars) + "</div>"


def _table(analytics: dict) -> str:
    axis = "Sub-region / city" if analytics["breakdown_axis"] == "subregion" else "Country"
    head = "".join(f"<th>{code}</th>" for code in store.CATEGORIES)
    body = []
    for row in analytics["breakdown"][:40]:
        cells = "".join(f"<td>{int(row[code])}</td>" for code in store.CATEGORIES)
        body.append(
            "<tr>"
            f"<td>{_esc(row['name'])}</td>{cells}<td>{row['total']}</td>"
            f"<td>{_esc(row['arrow'])}</td><td>{row['risk_index']:.1f}</td></tr>"
        )
    if not body:
        body.append(f'<tr><td colspan="10">No incidents logged in this period.</td></tr>')
    return (
        f"<table><thead><tr><th>{axis}</th>{head}<th>Total</th><th></th><th>Risk</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _header(document: dict) -> str:
    return (
        '<header class="run">'
        f"<span>GeoRiskGlobal</span><span>{_esc(document['scope'])}</span>"
        f"<span>{_esc(document['cadence_title'].split('·')[-1].strip())}</span>"
        f"<span>{_esc(document['date_range'])}</span></header>"
    )


def _footer(document: dict, page: str) -> str:
    return (
        '<footer class="run">'
        "<span>Powered by GeoRiskGlobal</span>"
        f"<span>{_esc(document['confidentiality'])}</span>"
        f"<span>{_esc(page)}</span></footer>"
    )


def render_html(document: dict) -> str:
    analytics = document["analytics"]
    points = analytics["points"]
    lat, lng, zoom = _map_view(document["scope"], points)
    point_json = json.dumps(points, ensure_ascii=False).replace("<", "\\u003c")
    pin_rows = "".join(
        f"<li><strong>{_esc(point['category'])}</strong> {_esc(point['severity'])} — {_esc(point['title'])}</li>"
        for point in points[:30]
    ) or "<li>No geolocated incidents in this period. The map is centred on the selected scope.</li>"
    developments = []
    for item in document["key_developments"]:
        developments.append(
            "<article class=\"dev\">"
            f"<div class=\"dev-top\"><h3>{_esc(item['title'])}</h3>"
            f"<span class=\"badge\">{_esc(item['badge'])}</span></div>"
            f"<p>{_esc(item['summary'])}</p>"
            f"<p><em>Implications.</em> {_esc(item['implications'])}</p>"
            f"<p><em>What to watch.</em> {_esc(item['what_to_watch'])}</p></article>"
        )
    overviews = "".join(
        f"<h3>{_esc(item['name'])}</h3><p>{_esc(item['text'])}</p>"
        for item in document["overviews"]
    ) or "<p>No sub-area had a logged incident in this period.</p>"
    totals = analytics["totals"]
    significant = analytics["significant"]
    risk = analytics["risk"]
    casualties = analytics["casualties"]
    source_note = (
        "Narrative prepared with Grok from these counts, in UK English."
        if document["narrative_source"] == "grok"
        else "Narrative prepared in UK English from the stored counts. Grok was not used."
    )
    css = """
    @page { size: A4; margin: 12mm; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #e7e5e4; color: #1c1917; font: 14px/1.45 Georgia, "Times New Roman", serif; }
    .sheet { background: #fff; width: min(210mm, 100%); min-height: 277mm; margin: 12px auto; padding: 16mm 14mm 18mm;
      box-shadow: 0 8px 24px rgba(0,0,0,.12); page-break-after: always; break-after: page; position: relative; }
    .cover { background: #102A63; color: #f8fafc; display: flex; flex-direction: column; justify-content: space-between; }
    .eyebrow { letter-spacing: .16em; text-transform: uppercase; color: #D4AF37; font: 700 12px/1.2 "Segoe UI", sans-serif; margin: 0 0 10px; }
    .cover h1 { font: 700 34px/1.15 "Segoe UI", sans-serif; margin: 0 0 8px; }
    .cover h2 { font: 600 16px/1.3 "Segoe UI", sans-serif; color: #D4AF37; margin: 0 0 12px; letter-spacing: .04em; }
    .sub { font: 16px/1.4 "Segoe UI", sans-serif; max-width: 36ch; }
    .gold { height: 3px; width: 72px; background: #D4AF37; margin: 16px 0; }
    .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 18px; }
    .stat { border: 1px solid rgba(212,175,55,.45); padding: 10px 12px; }
    .stat b { display: block; font: 700 22px/1.1 "Segoe UI", sans-serif; }
    .stat span { font: 12px/1.3 "Segoe UI", sans-serif; color: #e2e8f0; }
    .level-bar { display: flex; margin-top: 16px; font: 700 12px/1 "Segoe UI", sans-serif; }
    .seg { flex: 1; text-align: center; padding: 8px 4px; background: #1e293b; color: #94a3b8; }
    .seg.on.low { background: #0f766e; color: white; }
    .seg.on.medium { background: #b45309; color: white; }
    .seg.on.high { background: #c2410c; color: white; }
    .seg.on.extreme { background: #9f1239; color: white; }
    .run { display: flex; justify-content: space-between; gap: 8px; font: 11px/1.3 "Segoe UI", sans-serif; color: #57534e; }
    header.run { border-bottom: 2px solid #102A63; padding-bottom: 6px; margin-bottom: 12px; color: #102A63; letter-spacing: .04em; text-transform: uppercase; }
    footer.run { border-top: 1px solid #d6d3d1; padding-top: 6px; margin-top: 16px; }
    h2 { font: 700 20px/1.2 "Segoe UI", sans-serif; color: #102A63; margin: 0 0 8px; }
    h3 { font: 700 15px/1.3 "Segoe UI", sans-serif; margin: 12px 0 4px; }
    .toc { padding-left: 18px; }
    .toc a { color: #102A63; }
    #incident-map { height: 280px; border: 1px solid #d6d3d1; margin: 8px 0; }
    .pins { font: 12px/1.4 "Segoe UI", sans-serif; }
    .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .card { border: 1px solid #e7e5e4; padding: 10px 12px; }
    .card strong { font: 700 26px/1 "Segoe UI", sans-serif; color: #102A63; }
    .sev-row, .col { font: 12px/1.2 "Segoe UI", sans-serif; }
    .sev-row { display: grid; grid-template-columns: 52px 1fr 32px; gap: 6px; align-items: center; margin: 4px 0; }
    .track { background: #f5f5f4; height: 8px; }
    .fill { display: block; height: 8px; }
    .chart { display: flex; align-items: end; gap: 4px; height: 120px; }
    .col { display: flex; flex-direction: column; justify-content: end; align-items: center; height: 100%; flex: 1; }
    .stick { display: block; width: 100%; background: #102A63; }
    .count, .lbl { color: #57534e; }
    table { width: 100%; border-collapse: collapse; font: 12px/1.3 "Segoe UI", sans-serif; }
    th, td { border-bottom: 1px solid #e7e5e4; text-align: left; padding: 4px 3px; }
    th { color: #102A63; }
    .dev { border-top: 1px solid #e7e5e4; padding: 8px 0; page-break-inside: avoid; }
    .dev-top { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
    .badge { font: 700 11px/1 "Segoe UI", sans-serif; color: #102A63; border: 1px solid #D4AF37; padding: 3px 6px; white-space: nowrap; }
    .brand-mark { height: 56px; width: auto; display: block; margin: 0 0 18px; }
    .note { font: 12px/1.4 "Segoe UI", sans-serif; color: #57534e; }
    @media print {
      body { background: white; }
      .sheet { box-shadow: none; margin: 0; width: auto; }
      .cover, .seg.on { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    }
    """
    cover = f"""
    <section class="sheet cover">
      <div>
        <img class="brand-mark" src="/static/logo-pdf.png?v=5" alt="GeoRiskGlobal">
        <p class="eyebrow">GeoRiskGlobal | {_esc(document['scope'])}</p>
        <h1>GeoRiskGlobal | {_esc(document['scope'])}</h1>
        <h2>{_esc(document['cadence_title'])}</h2>
        <p class="sub">{_esc(document['date_range'])}</p>
        <div class="gold"></div>
        <p class="sub">{_esc(document['subtitle'])}</p>
        {_level_bar(document['threat_level'])}
        <div class="stats">
          <div class="stat"><b>{_esc(document['threat_level'])}</b><span>Threat level</span></div>
          <div class="stat"><b>{_esc(document['risk_index_label'])}</b><span>Risk Index {_esc(document['risk_arrow'])}</span></div>
          <div class="stat"><b>{document['coverage_count']}</b><span>{_esc(document['coverage_label'])} covered</span></div>
          <div class="stat"><b>{document['events_logged']}</b><span>Events logged</span></div>
        </div>
      </div>
      {_footer(document, "Cover")}
    </section>
    """
    contents = f"""
    <section class="sheet" id="contents">
      {_header(document)}
      <h2>Contents</h2>
      <ol class="toc">
        <li><a href="#map">Incident map</a></li>
        <li><a href="#dashboard">Period dashboard</a></li>
        <li><a href="#summary">Executive summary</a></li>
        <li><a href="#developments">Key developments</a></li>
        <li><a href="#outlook">Outlook</a></li>
        <li><a href="#overviews">Sub-region / country overviews</a></li>
        <li><a href="#method">Report guide and methodology</a></li>
      </ol>
      <h2 id="map">Incident map</h2>
      <div id="incident-map"></div>
      <ul class="pins">{pin_rows}</ul>
      {_footer(document, "Contents and map")}
    </section>
    """
    dashboard = f"""
    <section class="sheet" id="dashboard">
      {_header(document)}
      <h2>Period dashboard</h2>
      <div class="metrics">
        <div class="card"><strong>{totals['current']}</strong><p>Recorded incidents / events, { _esc(_pct_text(totals['percent_delta'], totals['arrow'])) } versus the {_esc(totals['baseline_label'])} ({totals['baseline']}).</p></div>
        <div class="card"><strong>{significant['current']}</strong><p>Significant (MED+HIGH), {_esc(_pct_text(significant['percent_delta'], significant['arrow']))} versus the baseline ({significant['baseline']}).</p></div>
        <div class="card"><strong>{risk['index']:.1f}</strong><p>Risk Index / 10 · { _esc(risk['level']) } · trend {_esc(risk['arrow'])}</p></div>
        <div class="card"><strong>{casualties['fatalities']} / {casualties['injuries']}</strong><p>Reported fatalities and injuries. {_esc(casualties['disclaimer'])}</p></div>
      </div>
      <h3>Severity</h3>
      {_severity_rows(analytics['severity'])}
      <h3>Trend</h3>
      {_series_chart(analytics['series'])}
      <h3>Incidents by area and category</h3>
      {_table(analytics)}
      {_footer(document, "Dashboard")}
    </section>
    """
    narrative = f"""
    <section class="sheet" id="summary">
      {_header(document)}
      <h2>Executive summary</h2>
      <p>{_esc(document['executive_summary'])}</p>
      <p class="note">{_esc(source_note)}</p>
      <h2 id="developments">Key developments</h2>
      {''.join(developments)}
      {_footer(document, "Assessment")}
    </section>
    <section class="sheet" id="outlook">
      {_header(document)}
      <h2>Outlook</h2>
      <p>{_esc(document['outlook'])}</p>
      <h2 id="overviews">Sub-region / country overviews</h2>
      {overviews}
      {_footer(document, "Outlook")}
    </section>
    <section class="sheet" id="method">
      {_header(document)}
      <h2>Report guide and methodology</h2>
      <p>{_esc(document['methodology'])}</p>
      <p>{_esc(store.CASUALTY_DISCLAIMER)}</p>
      <p>Categories: ACT Armed Conflict &amp; Terrorism, SFA Security Force Activity, VC Violent Crime, CU Civil Unrest, HS Hazards &amp; Safety, OD Operational Disruption.</p>
      {_footer(document, "Methodology")}
    </section>
    """
    script = f"""
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
      const points = {point_json};
      document.addEventListener("DOMContentLoaded", function () {{
        if (!window.L || !document.getElementById("incident-map")) return;
        const map = L.map("incident-map").setView([{lat}, {lng}], {zoom});
        L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
          attribution: "&copy; OpenStreetMap"
        }}).addTo(map);
        points.forEach(function (point) {{
          L.circleMarker([point.lat, point.lng], {{
            radius: 6, color: "#102A63", weight: 1, fillColor: "#D4AF37", fillOpacity: 0.9
          }}).bindPopup((point.category || "") + " " + (point.severity || "") + " — " + (point.title || "")).addTo(map);
        }});
      }});
    </script>
    """
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<link rel=\"icon\" href=\"/static/favicon.ico?v=5\" sizes=\"any\">"
        "<link rel=\"icon\" type=\"image/png\" href=\"/static/favicon.png?v=5\">"
        f"<title>GeoRiskGlobal | {_esc(document['scope'])}</title><style>{css}</style></head>"
        f"<body>{cover}{contents}{dashboard}{narrative}{script}</body></html>"
    )


def generate(
    scope: str,
    period: str = "weekly",
    days: object = None,
    category: str = "",
    now: datetime | None = None,
    use_grok: bool = True,
    db: store.Store | None = None,
) -> dict:
    analytics = store.query_analytics(scope, period, days, category, now=now, store=db)
    model = synthesize(analytics) if use_grok else None
    narrative, source = _merge_narrative(analytics, model)
    document = build_document(analytics, narrative, source)
    html = render_html(document)
    document["html"] = html
    payload = {key: value for key, value in document.items() if key not in {"html", "analytics"}}
    payload["analytics_summary"] = {
        "events": analytics["events"],
        "totals": analytics["totals"],
        "risk": analytics["risk"],
        "significant": analytics["significant"],
    }
    target = db or store.get_store()
    report_id = target.save_report({
        "scope_type": document["scope_type"],
        "scope": document["scope"],
        "period": document["cadence"],
        "period_start": analytics["start"],
        "period_end": analytics["end"],
        "generated_at": document["generated_at"],
        "threat_level": document["threat_level"],
        "risk_index": document["risk_index"],
        "narrative_source": source,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "html": html,
    })
    document["id"] = report_id
    document["ok"] = True
    return document


def plain_summary(document: dict) -> str:
    lines = [
        f"GeoRiskGlobal | {document['scope']}",
        document["cadence_title"],
        document["date_range"],
        document["subtitle"],
        f"Threat level {document['threat_level']}. Risk Index {document['risk_index_label']}.",
        f"Events logged: {document['events_logged']}.",
        document["executive_summary"],
        document["outlook"],
        document["confidentiality"],
    ]
    return "\n\n".join(lines)


def from_address() -> str:
    return (
        (os.environ.get("SMTP_FROM") or "").strip()
        or (os.environ.get("SMTP_USER") or "").strip()
        or "reports@georiskglobal.local"
    )


def build_message(document: dict, recipients: list[str], client_label: str = "") -> MIMEMultipart:
    message = MIMEMultipart("alternative")
    label = " ".join(str(client_label or "").split())
    prefix = f"{label} — " if label else ""
    message["Subject"] = (
        f"{prefix}GeoRiskGlobal {document['cadence_title'].split('·')[-1].strip().title()} "
        f"Security Assessment — {document['scope']} ({document['date_range']})"
    )
    message["From"] = from_address()
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(plain_summary(document), "plain", "utf-8"))
    message.attach(MIMEText(document["html"], "html", "utf-8"))
    return message


def smtp_settings() -> tuple[str, int, str, str]:
    host = (os.environ.get("SMTP_HOST") or "").strip()
    try:
        port = int(os.environ.get("SMTP_PORT") or "587")
    except ValueError:
        port = 587
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = os.environ.get("SMTP_PASS") or os.environ.get("SMTP_PASSWORD") or ""
    return host, port, user, password


def smtp_send(message: MIMEMultipart) -> dict:
    host, port, user, password = smtp_settings()
    if not host:
        return {"ok": False, "error": "SMTP is not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USER, and SMTP_PASS."}
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            if port != 25:
                smtp.starttls()
                smtp.ehlo()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:
        return {"ok": False, "error": f"Email failed: {exc.__class__.__name__}."}
    return {"ok": True}


def default_recipients() -> list[str]:
    raw = os.environ.get("REPORT_RECIPIENTS") or os.environ.get("SMTP_TO") or ""
    return store.parse_recipients(raw)


def _public_report(document: dict) -> dict:
    return {
        "ok": True,
        "id": document.get("id"),
        "scope": document["scope"],
        "scope_type": document["scope_type"],
        "period": document["cadence"],
        "cadence_title": document["cadence_title"],
        "date_range": document["date_range"],
        "threat_level": document["threat_level"],
        "risk_index": document["risk_index"],
        "risk_index_label": document["risk_index_label"],
        "events_logged": document["events_logged"],
        "coverage_count": document["coverage_count"],
        "narrative_source": document["narrative_source"],
        "html": document["html"],
    }


def send_document(document: dict, recipients: list[str], sender=None, client_label: str = "") -> dict:
    if not recipients:
        return {"ok": False, "error": "Add at least one recipient, or set REPORT_RECIPIENTS."}
    message = build_message(document, recipients, client_label=client_label)
    result = (sender or smtp_send)(message)
    result["recipients"] = recipients
    return result


def load_report(report_id: int, db: store.Store | None = None) -> dict | None:
    row = (db or store.get_store()).get_report(report_id)
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    payload["html"] = row["html"]
    payload["id"] = row["id"]
    payload["scope"] = row["scope"]
    payload["scope_type"] = row["scope_type"]
    payload["cadence"] = row["period"]
    payload["narrative_source"] = row["narrative_source"]
    payload["threat_level"] = row["threat_level"]
    payload["risk_index"] = row["risk_index"]
    payload["risk_index_label"] = f"{float(row['risk_index']):.1f} / 10"
    payload["date_range"] = _format_range(row["period_start"], row["period_end"])
    payload["cadence_title"] = f"SECURITY ASSESSMENT · {CADENCE_TITLE.get(row['period'], 'WEEKLY')}"
    if "events_logged" not in payload:
        payload["events_logged"] = payload.get("analytics_summary", {}).get("events", 0)
    return payload


def period_key(cadence: str, moment: datetime) -> str:
    local = moment.astimezone(LONDON)
    if cadence == "daily":
        return local.strftime("%Y-%m-%d")
    if cadence == "monthly":
        return local.strftime("%Y-%m")
    return local.strftime("%G-W%V")


def schedule_is_due(schedule: dict, now: datetime) -> bool:
    if not schedule.get("enabled"):
        return False
    last = store.parse_ts(schedule.get("last_sent_at"))
    if last is None:
        return True
    return period_key(schedule["cadence"], last) != period_key(schedule["cadence"], now)


def london_ready(now: datetime, gate_hour: int | None) -> bool:
    if gate_hour is None:
        return True
    return now.astimezone(LONDON).hour >= gate_hour


def deliver_schedule(
    schedule: dict,
    *,
    now: datetime | None = None,
    sender=None,
    test: bool = False,
    db: store.Store | None = None,
) -> dict:
    moment = now or datetime.now(timezone.utc)
    document = generate(schedule["scope"], schedule["cadence"], now=moment, use_grok=True, db=db)
    result = send_document(document, schedule["recipients"], sender=sender, client_label=schedule.get("client_label") or "")
    target = db or store.get_store()
    if result.get("ok"):
        if test:
            target.mark_schedule(schedule["id"], test_at=moment, status="test sent", error="")
        else:
            target.mark_schedule(schedule["id"], sent_at=moment, status="sent", error="")
    else:
        target.mark_schedule(schedule["id"], status="error", error=str(result.get("error") or "send failed"))
    result["schedule_id"] = schedule["id"]
    result["report_id"] = document["id"]
    result["test"] = test
    return result


def process_due_schedules(
    now: datetime | None = None,
    sender=None,
    gate_hour: int | None = 7,
    db: store.Store | None = None,
) -> list[dict]:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if not london_ready(moment, gate_hour):
        return []
    target = db or store.get_store()
    sent = []
    for schedule in target.list_schedules():
        if not schedule_is_due(schedule, moment):
            continue
        try:
            sent.append(deliver_schedule(schedule, now=moment, sender=sender, test=False, db=target))
        except Exception as exc:
            target.mark_schedule(schedule["id"], status="error", error=exc.__class__.__name__)
    return sent


def _json(payload: dict, status: int = 200) -> dict:
    return {"status": status, "mimetype": "application/json; charset=utf-8", "body": payload}


def _query_value(query, key: str, default: str = "") -> str:
    if query is None:
        return default
    if hasattr(query, "get"):
        value = query.get(key, default)
        if isinstance(value, (list, tuple)):
            return str(value[0] if value else default)
        return str(value if value is not None else default)
    return default


def dispatch(method: str, path: str, query, body: dict | None) -> dict:
    path = path.split("?", 1)[0].rstrip("/") or "/"
    body = body or {}
    try:
        if path == "/api/analytics" and method == "GET":
            scope = _query_value(query, "scope") or _query_value(query, "country") or _query_value(query, "region") or "Global"
            if _query_value(query, "country") and not _query_value(query, "scope"):
                scope = _query_value(query, "country")
            elif _query_value(query, "region") and not _query_value(query, "scope") and not _query_value(query, "country"):
                scope = _query_value(query, "region")
            days = _query_value(query, "days")
            analytics = store.query_analytics(
                scope,
                _query_value(query, "period") or "weekly",
                days or None,
                _query_value(query, "category"),
            )
            return _json(analytics)
        if path == "/api/reports/scopes" and method == "GET":
            return _json(store.scopes_payload())
        if path == "/api/reports" and method == "GET":
            return _json({"ok": True, "reports": store.get_store().list_reports()})
        if path == "/api/reports/generate" and method == "POST":
            scope = body.get("scope") or body.get("country") or body.get("region") or ""
            if not str(scope).strip():
                return _json({"ok": False, "error": "Choose a country or region."}, 400)
            document = generate(
                str(scope),
                str(body.get("period") or "weekly"),
                body.get("days") or None,
                str(body.get("category") or ""),
                use_grok=bool(body.get("use_grok", True)),
            )
            return _json(_public_report(document))
        if path == "/api/reports/send" and method == "POST":
            recipients = store.parse_recipients(body.get("recipients")) or default_recipients()
            if body.get("report_id"):
                document = load_report(int(body["report_id"]))
                if document is None:
                    return _json({"ok": False, "error": "Report not found."}, 404)
            else:
                scope = body.get("scope") or ""
                if not str(scope).strip():
                    return _json({"ok": False, "error": "Choose a report or a scope."}, 400)
                document = generate(str(scope), str(body.get("period") or "weekly"), body.get("days") or None)
            result = send_document(document, recipients)
            result["ok"] = bool(result.get("ok"))
            return _json(result)
        if path == "/api/reports/schedules" and method == "GET":
            return _json({"ok": True, "schedules": store.get_store().list_schedules()})
        if path == "/api/reports/schedules" and method == "POST":
            saved = store.get_store().save_schedule(body)
            return _json({"ok": True, "schedule": saved})
        if path == "/api/reports/schedules/delete" and method == "POST":
            removed = store.get_store().delete_schedule(int(body.get("id") or 0))
            return _json({"ok": removed})
        if path == "/api/reports/schedules/test" and method == "POST":
            schedule = store.get_store().get_schedule(int(body.get("id") or 0))
            if schedule is None:
                return _json({"ok": False, "error": "Subscription not found."}, 404)
            result = deliver_schedule(schedule, test=True)
            return _json(result)
        match = re.fullmatch(r"/api/reports/(\d+)/html", path)
        if match and method == "GET":
            row = store.get_store().get_report(int(match.group(1)))
            if row is None:
                return _json({"ok": False, "error": "Report not found."}, 404)
            headers = {}
            if _query_value(query, "download") == "1":
                headers["Content-Disposition"] = f'attachment; filename="georiskglobal-report-{match.group(1)}.html"'
            return {
                "status": 200,
                "mimetype": "text/html; charset=utf-8",
                "body": row["html"],
                "headers": headers,
            }
        match = re.fullmatch(r"/api/reports/(\d+)", path)
        if match and method == "GET":
            document = load_report(int(match.group(1)))
            if document is None:
                return _json({"ok": False, "error": "Report not found."}, 404)
            document["ok"] = True
            return _json(document)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        print(f"{method} {path} failed: {exc.__class__.__name__}.")
        return _json({"ok": False, "error": "The report service could not complete that request."}, 500)
    return _json({"ok": False, "error": "Not found."}, 404)


def _loop() -> None:
    time.sleep(8)
    while True:
        try:
            store.sync_live()
        except Exception as exc:
            print(f"Historical sync failed: {exc.__class__.__name__}.")
        try:
            process_due_schedules()
        except Exception as exc:
            print(f"Report scheduler failed: {exc.__class__.__name__}.")
        time.sleep(120)


def start_background() -> None:
    """Sync the archive and send due client reports. Safe to call more than once."""
    global _started
    with _start_lock:
        if _started:
            return
        if (os.environ.get("DISABLE_HISTORICAL_SYNC") or "").strip().lower() in {"1", "true", "yes"}:
            return
        _started = True
    threading.Thread(target=_loop, name="georisk-reports", daemon=True).start()
