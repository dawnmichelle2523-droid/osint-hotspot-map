"""Publish-once daily Area of Operations briefings.

Each subscribed country or region gets one UK English edition per UTC day.
The file lives under resolve_data_dir() / daily_briefings / YYYY-MM-DD /
<ao_slug>.json so a read-only Vercel bundle never has to be written.

The default generator is deterministic and costs $0. Claude Haiku 5.5 runs
only when a publish call sets use_claude. GET, page loads, and the morning
workflow never call Claude or xAI.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote

from flask import Blueprint, Response, request

import analytics_store
import claude_client
import geo_split

ROOT = Path(__file__).resolve().parent
CLAUDE_MODEL = "claude-haiku-5-5"
BRIEF_MAX_TOKENS = 1200
LANGUAGE = "en-GB"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_SUBSCRIBED = ("syria", "sudan", "middle-east", "ukraine", "sahel")
LEVELS = ("LOW", "MODERATE", "SUBSTANTIAL", "SEVERE", "CRITICAL")
SECTION_SPECS = (
    ("executive", "Executive Threat Assessment & Overall Posture"),
    ("developments", "Key Security Developments (Last 24–72 Hours)"),
    ("flashpoints", "Regional / Governorate Flashpoints & Spillover"),
    ("advisories", "Travel, Aviation & Infrastructure Advisories"),
    ("posture", "Operational Impact & Recommended Posture"),
    ("watchlist", "72-Hour Watchlist & Early-Warning Indicators"),
)
WINDOW_HOURS = 72

# Incident region labels used by the stored hotspot file, mapped to one region AO.
_BUCKETS = {
    "levant": "middle-east",
    "saudi & yemen": "middle-east",
    "saudi and yemen": "middle-east",
    "gulf": "middle-east",
    "gulf states": "middle-east",
    "middle east": "middle-east",
    "maritime": "maritime",
    "red sea": "maritime",
    "russia & ukraine": "eastern-europe",
    "russia and ukraine": "eastern-europe",
    "sahel": "sahel",
    "horn of africa": "east-africa-horn",
    "east africa": "east-africa-horn",
    "east africa / horn of africa": "east-africa-horn",
    "indo-pacific": "indo-pacific",
    "indo pacific": "indo-pacific",
    "apac": "indo-pacific",
    "asia-pacific": "asia-pacific",
    "asia pacific": "asia-pacific",
    "south asia": "indo-pacific",
    "europe": "europe",
    "eastern europe": "eastern-europe",
    "uk & europe": "europe",
    "uk and europe": "europe",
    "africa": "africa",
    "americas": "americas",
    "global": "global",
}

_ADJECTIVES = {
    "Syria": ("Syrian",),
    "Iraq": ("Iraqi",),
    "Iran": ("Iranian",),
    "Israel": ("Israeli",),
    "Lebanon": ("Lebanese",),
    "Palestine": ("Palestinian",),
    "Jordan": ("Jordanian",),
    "Yemen": ("Yemeni",),
    "Saudi Arabia": ("Saudi",),
    "Sudan": ("Sudanese",),
    "Ukraine": ("Ukrainian",),
    "Russia": ("Russian",),
    "Nigeria": ("Nigerian",),
    "Niger": ("Nigerien",),
    "Mali": ("Malian",),
    "Somalia": ("Somali",),
    "Ethiopia": ("Ethiopian",),
    "Egypt": ("Egyptian",),
    "Libya": ("Libyan",),
    "Chad": ("Chadian",),
    "Oman": ("Omani",),
}

_FLOOR = {
    "syria": "SEVERE",
    "sudan": "SEVERE",
    "yemen": "SEVERE",
    "ukraine": "SEVERE",
    "israel-gaza": "SEVERE",
    "lebanon": "SUBSTANTIAL",
    "sahel": "SUBSTANTIAL",
    "middle-east": "SUBSTANTIAL",
    "east-africa-horn": "SUBSTANTIAL",
    "eastern-europe": "SUBSTANTIAL",
    "nigeria": "SUBSTANTIAL",
    "maritime": "SUBSTANTIAL",
}

_US_TO_UK = (
    ("organizations", "organisations"),
    ("organization", "organisation"),
    ("mobilization", "mobilisation"),
    ("armored", "armoured"),
    ("defense", "defence"),
    ("traveling", "travelling"),
    ("traveled", "travelled"),
    ("labeling", "labelling"),
    ("labeled", "labelled"),
    ("centers", "centres"),
    ("center", "centre"),
    ("colors", "colours"),
    ("color", "colour"),
    ("programs", "programmes"),
    ("program", "programme"),
)

_NOTES = {
    "syria": {
        "spillover": (
            "Spillover sits on the Lebanese, Iraqi, Jordanian, and Israeli borders, and in the "
            "north-east where control is divided by governorate. A strike or a crossing closure "
            "on one side of the border usually changes the movement plan the same day."
        ),
        "advisory": (
            "The airspace of Syria remains a conflict zone. Check the current NOTAM and any EASA "
            "conflict-zone bulletin before a flight is even pencilled in. Land borders open and "
            "close with little notice. Road movement outside a cleared corridor, and power or "
            "telecoms outside the main cities, should be treated as unreliable."
        ),
    },
    "sudan": {
        "spillover": (
            "Fighting and displacement spill into South Sudan, Chad, and the wider Horn of Africa. "
            "Port Sudan remains the external lifeline, and that coast is part of the Red Sea risk picture."
        ),
        "advisory": (
            "Khartoum is not a normal aviation plan. Confirm whether Port Sudan or an external hub "
            "is the realistic airhead, and read the current NOTAM. Overland routes are broken by "
            "checkpoints. Fuel, banking, and medical care are unreliable."
        ),
    },
    "ukraine": {
        "spillover": (
            "The war spills into Black Sea shipping, border states, and the wider eastern European "
            "air picture. Energy and transport strikes inside Ukraine change the risk for neighbouring capitals."
        ),
        "advisory": (
            "Ukrainian airspace is closed to normal civil aviation. Do not plan an overflight. "
            "Rail and road corridors change with strikes on energy and transport. Power and shelter "
            "plans need a current local check."
        ),
    },
    "lebanon": {
        "spillover": (
            "Lebanon is tied to the fighting in Israel and Gaza, to Syrian border dynamics, and to "
            "armed-group decisions in the south and the Bekaa."
        ),
        "advisory": (
            "Beirut airport and Lebanese airspace have been interrupted in this conflict cycle. "
            "Use the current NOTAM. Southern roads and the Syrian crossings are the first to close."
        ),
    },
    "yemen": {
        "spillover": (
            "Yemen's coast and the Houthi hold on the Red Sea, Bab el-Mandeb, and the Gulf of Aden "
            "carry the risk into Saudi Arabia and international shipping."
        ),
        "advisory": (
            "The Sana'a flight information region remains a conflict-zone airspace. Sea routes off "
            "the west and south coasts need a current maritime advisory. Inland roads are not a "
            "casual plan."
        ),
    },
    "israel-gaza": {
        "spillover": (
            "Israel and Gaza share a single operational picture with Lebanon, the West Bank, and "
            "regional missile and air-defence activity. A change in Gaza or on the northern border "
            "moves the civil-aviation and shelter plan together."
        ),
        "advisory": (
            "Airports and air routes can close at short notice. FCDO advice distinguishes Gaza, "
            "parts of the West Bank, and areas of northern Israel near the Lebanese and Syrian "
            "borders. Shelter, siren, and road discipline are part of the movement plan."
        ),
    },
    "nigeria": {
        "spillover": (
            "Kidnap, banditry, and insurgent violence in the north sit on the Sahel picture. "
            "Unrest in one state often displaces road traffic into the next."
        ),
        "advisory": (
            "Abuja and Lagos are commercially open and still need a current crime and protest check. "
            "Northern road movement, especially at night, is a different risk. Confirm the airline "
            "and the airport status on the day."
        ),
    },
    "middle-east": {
        "spillover": (
            "Active fighting in Syria, Yemen, Israel and Gaza, and parts of Iraq sits beside tightly "
            "governed Gulf capitals. Maritime risk on the Red Sea, Bab el-Mandeb, and the Strait of "
            "Hormuz is part of this Area of Operations, not a separate problem."
        ),
        "advisory": (
            "Conflict-zone airspace covers Syria, Yemen, Iraq, and Iran. Gulf hubs are generally open "
            "and still need the day's NOTAM. Land borders and maritime routes change with little notice."
        ),
    },
    "east-africa-horn": {
        "spillover": (
            "Sudan's war, Somali insurgency, and Ethiopian local conflict feed one another through "
            "displacement, ports, and the Red Sea approaches."
        ),
        "advisory": (
            "Sudanese airspace is restricted and Khartoum is not a normal airhead. Mogadishu movement "
            "needs a current security plan. Nairobi remains the usual medevac and aviation hub for the Horn."
        ),
    },
    "sahel": {
        "spillover": (
            "Insurgent and junta-era controls cross Mali, Niger, Burkina Faso, and the Lake Chad basin "
            "into Nigeria and Chad. A coup or an offensive rarely stays inside one border."
        ),
        "advisory": (
            "Several Sahel capitals have published airspace and overland restrictions. Night road "
            "movement outside the capital is a high-risk plan. Confirm the political control of the "
            "route, not only the map line."
        ),
    },
    "eastern-europe": {
        "spillover": (
            "The war in Ukraine is the driver. Moldova, Belarus, and the Black Sea, plus air and "
            "energy links into Poland and Romania, carry the spillover."
        ),
        "advisory": (
            "Do not overfly Ukraine. Western Russian airspace is not a normal civil route. Elsewhere "
            "in the region, commercial aviation is open and still needs a NOTAM check, with extra "
            "attention to energy and rail disruption."
        ),
    },
    "indo-pacific": {
        "spillover": (
            "The strategic pressure points are the Taiwan Strait, the Korean peninsula, the South "
            "China Sea, and Afghanistan. Most other hubs are commercially open."
        ),
        "advisory": (
            "Afghan airspace needs a current risk acceptance. Taiwan Strait and South China Sea "
            "transits need an air and maritime check. Treat other regional hubs as open only after "
            "the day's NOTAM."
        ),
    },
    "global": {
        "spillover": (
            "This edition is the worldwide roll-up. It does not replace the country or regional "
            "Area of Operations brief for a deployment."
        ),
        "advisory": (
            "No single border or airspace regime applies. Use the country edition, the current "
            "NOTAM, and UK FCDO travel advice before travel."
        ),
    },
}

briefings_bp = Blueprint("daily_ao_briefings", __name__)


@dataclass(frozen=True)
class Area:
    slug: str
    name: str
    kind: str
    members: tuple[str, ...]
    regions: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    spillover: str = ""
    advisory: str = ""


def _slugify(value: object) -> str:
    text = str(value or "").lower().replace("&", " and ").replace("/", " ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _countries_in(region_label: str) -> tuple[str, ...]:
    return tuple(name for name, region, *_coords in geo_split.COUNTRY_ROWS if region == region_label)


def _build_catalog() -> dict[str, Area]:
    areas: dict[str, Area] = {}

    def add(area: Area) -> None:
        if area.slug and SLUG_RE.match(area.slug) and area.slug not in areas:
            areas[area.slug] = area

    region_members = {
        "middle-east": _countries_in("Middle East"),
        "europe": _countries_in("Europe"),
        "africa": _countries_in("Africa"),
        "asia-pacific": _countries_in("Asia-Pacific"),
        "americas": _countries_in("Americas"),
        "maritime": ("Maritime",),
        "global": (),
        "east-africa-horn": ("Sudan", "South Sudan", "Ethiopia", "Eritrea", "Djibouti", "Somalia", "Kenya"),
        "sahel": ("Mali", "Niger", "Burkina Faso", "Chad", "Mauritania", "Nigeria"),
        "eastern-europe": ("Ukraine", "Moldova", "Belarus", "Poland", "Romania", "Russia"),
        "indo-pacific": _countries_in("Asia-Pacific"),
    }
    region_names = {row["id"]: row["label"] for row in geo_split.REGIONS}
    region_names.update({
        "east-africa-horn": "East Africa / Horn of Africa",
        "sahel": "Sahel",
        "eastern-europe": "Eastern Europe",
        "indo-pacific": "Indo-Pacific",
    })
    region_order = (
        "middle-east",
        "east-africa-horn",
        "sahel",
        "eastern-europe",
        "indo-pacific",
        "global",
        "europe",
        "africa",
        "asia-pacific",
        "americas",
        "maritime",
    )
    region_keywords = {
        "middle-east": ("middle east", "red sea", "hormuz", "bab el-mandeb", "bab al-mandab", "houthi"),
        "east-africa-horn": ("horn of africa", "east africa"),
        "sahel": ("sahel",),
        "eastern-europe": ("eastern europe", "black sea"),
        "indo-pacific": ("indo-pacific", "south china sea", "taiwan strait"),
        "maritime": ("maritime", "ukmto", "strait of hormuz", "bab el-mandeb", "red sea"),
        "global": (),
    }
    for slug in region_order:
        notes = _NOTES.get(slug, {})
        add(Area(
            slug=slug,
            name=region_names[slug],
            kind="region",
            members=tuple(region_members.get(slug, ())),
            regions=(slug,),
            keywords=region_keywords.get(slug, ()),
            spillover=notes.get("spillover", ""),
            advisory=notes.get("advisory", ""),
        ))

    priority = ("Syria", "Sudan", "Ukraine", "Lebanon", "Yemen", "Nigeria")
    ordered = sorted(
        geo_split.countries(),
        key=lambda row: (priority.index(row["name"]) if row["name"] in priority else len(priority), row["name"]),
    )
    macro_slug = {row["label"]: row["id"] for row in geo_split.REGIONS}
    extra_region = {
        "Sudan": ("east-africa-horn",),
        "South Sudan": ("east-africa-horn",),
        "Ethiopia": ("east-africa-horn",),
        "Somalia": ("east-africa-horn",),
        "Kenya": ("east-africa-horn",),
        "Mali": ("sahel",),
        "Niger": ("sahel",),
        "Chad": ("sahel",),
        "Nigeria": ("sahel",),
        "Ukraine": ("eastern-europe",),
        "Russia": ("eastern-europe",),
        "Poland": ("eastern-europe",),
    }
    for row in ordered:
        if row["name"] == "Maritime":
            continue
        slug = _slugify(row["name"])
        parent = macro_slug.get(row["region"], _slugify(row["region"]))
        regions = tuple(dict.fromkeys((parent, *extra_region.get(row["name"], ()))))
        notes = _NOTES.get(slug, {})
        add(Area(
            slug=slug,
            name=row["name"],
            kind="country",
            members=(row["name"],),
            regions=regions,
            spillover=notes.get("spillover", ""),
            advisory=notes.get("advisory", ""),
        ))
    notes = _NOTES["israel-gaza"]
    add(Area(
        slug="israel-gaza",
        name="Israel/Gaza",
        kind="country",
        members=("Israel", "Palestine"),
        regions=("middle-east",),
        aliases=("israel gaza", "gaza", "israel/gaza"),
        keywords=("gaza", "west bank"),
        spillover=notes["spillover"],
        advisory=notes["advisory"],
    ))
    return areas


AREAS = _build_catalog()
_ALIASES: dict[str, str] = {}
for _area in AREAS.values():
    _ALIASES[_area.slug] = _area.slug
    _ALIASES[_area.name.lower()] = _area.slug
    _ALIASES[_slugify(_area.name)] = _area.slug
    for _alias in _area.aliases:
        _ALIASES[_alias.lower()] = _area.slug
        _ALIASES[_slugify(_alias)] = _area.slug


def get_area(value: object) -> Area | None:
    raw = " ".join(str(value or "").split())
    if not raw or len(raw) > 120:
        return None
    if ".." in raw or "/" in raw or "\\" in raw:
        folded = raw.lower()
        if folded in _ALIASES:
            return AREAS[_ALIASES[folded]]
        return None
    key = raw.lower()
    if key in _ALIASES and _ALIASES[key] in AREAS:
        return AREAS[_ALIASES[key]]
    slug = _slugify(raw)
    if slug in AREAS:
        return AREAS[slug]
    canon = geo_split.canonical_country(raw)
    if canon and _slugify(canon) in AREAS:
        return AREAS[_slugify(canon)]
    return None


def subscribed_slugs() -> list[str]:
    raw = os.environ.get("GEORISKINTEL_SUBSCRIBED_AOS", "")
    if raw.strip():
        found: list[str] = []
        for part in raw.split(","):
            area = get_area(part.strip())
            if area and area.slug not in found:
                found.append(area.slug)
        if found:
            return found
    return [slug for slug in DEFAULT_SUBSCRIBED if slug in AREAS]


def catalog_areas() -> list[dict]:
    rows = []
    for area in AREAS.values():
        rows.append({
            "ao_slug": area.slug,
            "ao_name": area.name,
            "ao_type": area.kind,
            "regions": list(area.regions),
        })
    rows.sort(key=lambda row: (0 if row["ao_type"] == "region" else 1, row["ao_name"]))
    return rows


def utc_now(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def day_string(now: datetime | None = None) -> str:
    return utc_now(now).strftime("%Y-%m-%d")


def format_gmt(moment: datetime) -> str:
    moment = utc_now(moment)
    return f"{moment.day} {moment.strftime('%B %Y, %H:%M')} GMT"


def _valid_date(value: object) -> str:
    text = str(value or "").strip()
    if not DATE_RE.match(text):
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return text


def data_root() -> Path:
    """Writable data directory. Always resolve_data_dir(), including on Vercel."""
    return Path(analytics_store.resolve_data_dir())


def edition_file(slug: str, day: str) -> Path:
    return data_root() / "daily_briefings" / day / f"{slug}.json"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _briefing_root() -> Path:
    return data_root() / "daily_briefings"


def _dates_on_disk() -> list[str]:
    root = _briefing_root()
    if not root.is_dir():
        return []
    found = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        if child.is_dir() and _valid_date(child.name):
            found.append(child.name)
    return sorted(found)


def _to_uk(text: str) -> str:
    def swap(match: re.Match[str], uk: str) -> str:
        word = match.group(0)
        if word.isupper():
            return uk.upper()
        if word[0].isupper():
            return uk[0].upper() + uk[1:]
        return uk

    cleaned = text
    for american, british in _US_TO_UK:
        cleaned = re.sub(rf"\b{american}\b", lambda match, uk=british: swap(match, uk), cleaned, flags=re.I)
    return cleaned


_ROLE_NAME = re.compile(
    r"\b(?:[Ss]taff|[Oo]fficers?|[Rr]eviewers?|[Oo]perators?|[Dd]rivers?|[Mm]edics?|[Gg]uards?|"
    r"[Aa]nalysts?|[Aa]dvis[eo]rs?|[Tt]ravell?ers?|[Ee]mployees?|[Cc]ontractors?|[Cc]allsigns?|"
    r"[Pp]assengers?|[Pp]rincipals?|[Nn]ames?)\b\s*[:\-]?\s*[A-Z][a-zA-Z'’.\-]+(?:\s+[A-Z][a-zA-Z'’.\-]+){0,2}"
)


def _public(value: object, limit: int = 500) -> str:
    text = _ROLE_NAME.sub("[removed]", str(value or ""))
    return _to_uk(claude_client.scrub_text(text, limit))


def _read_feature_file(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        features = payload.get("features") or []
    elif isinstance(payload, list):
        features = payload
    else:
        return []
    return [item for item in features if isinstance(item, dict)]


def load_stored_incidents() -> list[dict]:
    """Stored hotspot file plus the in-process live feed. No model API is called."""
    items: list[dict] = []
    items.extend(_read_feature_file(ROOT / "hotspots.json"))
    items.extend(_read_feature_file(ROOT / "archive.json"))
    if "serve_map" in sys.modules:
        try:
            import serve_map

            extra = serve_map.combined_features()
            if isinstance(extra, list):
                items.extend(item for item in extra if isinstance(item, dict))
        except Exception:
            pass
    return items


def _flatten(item: dict) -> dict:
    props = item.get("properties") if isinstance(item.get("properties"), dict) else item
    if not isinstance(props, dict):
        props = {}
    return {
        "title": _public(props.get("title") or "", 400),
        "summary": _public(props.get("summary") or "", 600),
        "severity": _public(props.get("severity") or "", 40),
        "category": _public(props.get("category") or "", 80),
        "country": _public(props.get("country") or "", 80),
        "region": _public(props.get("region") or props.get("theatre") or "", 80),
        "theatre": _public(props.get("theatre") or "", 80),
        "area": _public(props.get("area") or props.get("location_name") or props.get("governorate") or "", 80),
        "timestamp": _public(props.get("timestamp") or props.get("published_at") or "", 40),
        "source_name": _public(props.get("source_name") or props.get("account") or props.get("source_label") or props.get("handle") or "", 80),
    }


def _blob(props: dict) -> str:
    return " ".join(str(props.get(key) or "") for key in (
        "title", "summary", "country", "region", "theatre", "area", "category",
    ))


def _named(name: str, props: dict) -> bool:
    country = " ".join(str(props.get("country") or "").split())
    if country and (country.lower() == name.lower() or geo_split.canonical_country(country) == name):
        return True
    blob = _blob(props)
    if name == "Sudan":
        blob = re.sub(r"\bSouth Sudan\b", " ", blob, flags=re.I)
    if re.search(rf"\b{re.escape(name)}\b", blob, re.I):
        return True
    for adjective in _ADJECTIVES.get(name, ()):
        if re.search(rf"\b{re.escape(adjective)}\b", blob, re.I):
            return True
    return False


def incident_matches(area: Area, item: dict) -> bool:
    props = _flatten(item)
    if not props["title"] and not props["summary"]:
        return False
    if area.slug == "global":
        return True
    if area.kind == "country":
        if area.slug == "israel-gaza":
            if any(_named(member, props) for member in area.members):
                return True
            blob = _blob(props).lower()
            return any(keyword in blob for keyword in area.keywords)
        return any(_named(member, props) for member in area.members)
    region_field = " ".join(str(props.get("region") or props.get("theatre") or "").split()).lower()
    if region_field == area.name.lower() or _slugify(region_field) == area.slug:
        return True
    if _BUCKETS.get(region_field) == area.slug:
        return True
    if any(_named(member, props) for member in area.members):
        return True
    blob = _blob(props).lower()
    return any(keyword in blob for keyword in area.keywords)


def _severity_rank(value: str) -> int:
    text = value.strip().lower()
    if text in {"critical", "extreme"}:
        return 5
    if text in {"severe", "very high", "high"}:
        return 4
    if text in {"substantial", "medium"}:
        return 3
    if text in {"moderate", "low"}:
        return 2
    if text:
        return 1
    return 0


def _level_from_rank(rank: int, high_count: int) -> str:
    if rank >= 5 or high_count >= 3:
        return "CRITICAL"
    if rank >= 4:
        return "SEVERE"
    if rank >= 3:
        return "SUBSTANTIAL"
    if rank >= 2:
        return "MODERATE"
    return "LOW"


def _higher(left: str, right: str) -> str:
    if left not in LEVELS:
        left = "LOW"
    if right not in LEVELS:
        right = "LOW"
    return left if LEVELS.index(left) >= LEVELS.index(right) else right


def _stamp_sort(value: str) -> datetime:
    parsed = analytics_store.parse_ts(value)
    if parsed is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed


def select_incidents(area: Area, incidents: list[dict], now: datetime) -> tuple[list[dict], bool]:
    matched: list[dict] = []
    seen: set[str] = set()
    for item in incidents:
        if not isinstance(item, dict) or not incident_matches(area, item):
            continue
        flat = _flatten(item)
        key = re.sub(r"[^a-z0-9]+", " ", flat["title"].lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        matched.append(flat)
    in_window = []
    older = []
    for item in matched:
        moment = analytics_store.parse_ts(item.get("timestamp"))
        if moment is not None:
            age = (now - moment).total_seconds() / 3600
            if 0 <= age <= WINDOW_HOURS:
                in_window.append(item)
                continue
        older.append(item)
    fresh = bool(in_window)
    chosen = in_window if in_window else older
    chosen.sort(key=lambda item: _stamp_sort(item.get("timestamp") or ""), reverse=True)
    chosen.sort(key=lambda item: _severity_rank(item.get("severity") or ""), reverse=True)
    cap = 12 if area.slug == "global" else 8
    return chosen[:cap], fresh


def _threat_level(area: Area, incidents: list[dict]) -> str:
    ranks = [_severity_rank(item.get("severity") or "") for item in incidents]
    if not ranks:
        derived = "LOW"
        highs = 0
    else:
        derived = _level_from_rank(max(ranks), sum(1 for rank in ranks if rank >= 4))
        highs = sum(1 for rank in ranks if rank >= 4)
        if highs >= 3:
            derived = "CRITICAL"
    return _higher(derived, _FLOOR.get(area.slug, "MODERATE" if area.kind == "region" else "LOW"))


def _default_spillover(area: Area) -> str:
    if area.spillover:
        return area.spillover
    if area.kind == "country":
        return (
            f"Watch neighbouring states for displacement, airspace closures, and border restrictions "
            f"that would change a movement into or out of {area.name}."
        )
    members = ", ".join(area.members[:6]) if area.members else "the countries in this theatre"
    return (
        f"Treat {area.name} as one theatre. A sharp change in {members} can close a border, "
        "an air route, or a port used by the others."
    )


def _default_advisory(area: Area) -> str:
    if area.advisory:
        return area.advisory
    return (
        f"Check the current NOTAM, UK FCDO foreign travel advice for {area.name}, and the local "
        "security-force picture before travelling. Treat border, airport, and main-road status as "
        "time-sensitive. Confirm fuel, power, and medical-evacuation routes with the assistance centre."
    )


def _section(number: int, spec: tuple[str, str], body: str, **extra: object) -> dict:
    section_id, title = spec
    payload = {
        "number": number,
        "id": section_id,
        "title": f"{number}. {title}",
        "body": _public(body, 5000),
    }
    payload.update(extra)
    return payload


def _development_line(item: dict) -> str:
    place = item.get("area") or item.get("country") or item.get("region") or "Unspecified area"
    severity = (item.get("severity") or "unspecified").upper()
    line = f"{place}: {item.get('title') or 'Open-source item'} Severity {severity}."
    if item.get("timestamp"):
        line += f" Timestamp {item['timestamp']}."
    if item.get("source_name"):
        line += f" Source {item['source_name']}."
    return line


def build_deterministic(area: Area, incidents: list[dict] | None = None, now: datetime | None = None) -> dict:
    """Six-section UK English edition from stored OSINT. No network call."""
    moment = utc_now(now)
    pool = list(incidents) if incidents is not None else load_stored_incidents()
    chosen, fresh = select_incidents(area, pool, moment)
    level = _threat_level(area, chosen)
    stamp = format_gmt(moment)
    if fresh:
        if len(chosen) == 1:
            window_note = (
                f"1 open-source item for {area.name} falls inside the last 72 hours "
                "and grounds the points below."
            )
        else:
            window_note = (
                f"{len(chosen)} open-source items for {area.name} fall inside the last 72 hours "
                "and ground the points below."
            )
    elif chosen:
        window_note = (
            f"No stored open-source item for {area.name} falls inside the last 72 hours. "
            "The points below are the latest stored OSINT and should be read with that age in mind."
        )
    else:
        window_note = (
            f"The stored OSINT set has no labelled item for {area.name} today. "
            "The posture below is the standing assessment, not a claim that the theatre is quiet."
        )

    executive = (
        f"GeoRiskIntel assesses the overall posture for the {area.name} Area of Operations as {level} "
        f"as at {stamp}. This UK English executive edition covers a {area.kind} Area of Operations. "
        f"{window_note} Threat levels are colour-coded for the duty desk. "
        "The edition contains no personal data and is decision support for authorised readers, not a movement order."
    )

    if chosen:
        developments = window_note + "\n\n" + "\n\n".join(_development_line(item) for item in chosen)
    else:
        developments = window_note

    groups: dict[str, list[dict]] = {}
    for item in chosen:
        key = item.get("area") or item.get("country") or item.get("region") or area.name
        groups.setdefault(key, []).append(item)
    flash_lines = [_default_spillover(area)]
    if groups:
        for name, rows in list(groups.items())[:6]:
            top = max(rows, key=lambda row: _severity_rank(row.get("severity") or ""))
            noun = "item" if len(rows) == 1 else "items"
            flash_lines.append(
                f"{name}: {len(rows)} open-source {noun}, highest labelled severity "
                f"{(top.get('severity') or 'unspecified').upper()}. Latest: {top.get('title') or 'Open-source item'}."
            )
    else:
        flash_lines.append(
            f"No governorate or city cluster is available from today's stored feed for {area.name}. "
            "Re-check the live map before treating any district as quiet."
        )
    flashpoints = "\n".join(flash_lines)

    infra_hits = []
    for item in chosen:
        text = f"{item.get('title') or ''} {item.get('category') or ''}".lower()
        if any(token in text for token in (
            "air", "airport", "notam", "border", "road", "port", "maritime", "shipping",
            "power", "energy", "water", "telecom", "infrastructure", "flight",
        )):
            infra_hits.append(item.get("title") or "")
    advisory = _default_advisory(area)
    if infra_hits:
        advisory += " Stored reporting also flags: " + "; ".join(infra_hits[:4]) + "."
    else:
        advisory += " No fresh infrastructure incident is labelled in the stored set for this edition."

    posture = (
        f"Recommended posture for {area.name} remains aligned to {level}. "
        "A humanitarian organisation should confirm its coordination centre before travelling, "
        "and should keep the movement programme under review.\n"
        "Media. Assume access can be withdrawn by a single checkpoint or airspace notice. "
        "Do not publish a live location. Pool movement where the threat is SEVERE or CRITICAL.\n"
        "NGO. Pair programme delivery with a current access and medevac plan. "
        "Stand down a route that lost its last confirmed clearance.\n"
        "Corporate. Freeze non-essential travel into the highest-threat governorates. "
        "Keep staff on the published movement programme and a reached-base check.\n"
        "Close protection. Keep armoured movement plans current, brief the day's colour code, "
        "and treat a change in air or border status as a reason to hold."
    )

    watch = [
        "Official statements from defence ministries and any change to UK FCDO travel advice.",
        "NOTAMs, airport closures, and conflict-zone bulletins for the flight information region.",
        "Signs of mobilisation, protest, or armed-group movement that would close a road or a border.",
        "Disruption to power, telecoms, fuel, or the medical-evacuation route.",
    ]
    keyword_blob = " ".join(item.get("title") or "" for item in chosen).lower()
    if any(token in keyword_blob for token in ("strike", "shell", "missile", "drone", "airstrike")):
        watch.append("Further strikes or drone activity, and the civil-defence response that follows them.")
    if any(token in keyword_blob for token in ("protest", "unrest", "riot", "demonstration")):
        watch.append("Crowd mobilisation and the roads the authorities close around it.")
    if any(token in keyword_blob for token in ("maritime", "shipping", "houthi", "vessel", "tanker")):
        watch.append("Further attacks or warnings against commercial shipping, and diversions around the chokepoint.")
    if any(token in keyword_blob for token in ("border", "crossing")):
        watch.append("Short-notice closure of land borders and the queue that builds behind them.")
    watchlist = "\n".join(f"{index}. {line}" for index, line in enumerate(watch[:6], start=1))

    sections = [
        _section(1, SECTION_SPECS[0], executive, threat_level=level),
        _section(2, SECTION_SPECS[1], developments, items=[
            {
                "title": item.get("title") or "",
                "area": item.get("area") or item.get("country") or "",
                "severity": item.get("severity") or "",
                "timestamp": item.get("timestamp") or "",
                "source_name": item.get("source_name") or "",
            }
            for item in chosen
        ]),
        _section(3, SECTION_SPECS[2], flashpoints),
        _section(4, SECTION_SPECS[3], advisory),
        _section(5, SECTION_SPECS[4], posture),
        _section(6, SECTION_SPECS[5], watchlist),
    ]
    return _pack_edition(
        area,
        moment,
        level,
        sections,
        generator="deterministic",
        mode="deterministic",
        model=None,
        cost_usd=0,
        api_calls=0,
        incident_count=len(chosen),
        grounded_in_window=fresh,
        published=True,
    )


def _pack_edition(
    area: Area,
    moment: datetime,
    level: str,
    sections: list[dict],
    *,
    generator: str,
    mode: str,
    model: str | None,
    cost_usd: float,
    api_calls: int,
    incident_count: int,
    grounded_in_window: bool,
    published: bool,
) -> dict:
    return {
        "ok": True,
        "published": published,
        "preview": not published,
        "cached": False,
        "cached_for_today": published and moment.strftime("%Y-%m-%d") == day_string(),
        "ao_slug": area.slug,
        "ao_name": area.name,
        "ao_type": area.kind,
        "date": moment.strftime("%Y-%m-%d"),
        "threat_level": level if level in LEVELS else "MODERATE",
        "published_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_at_gmt": format_gmt(moment),
        "generator": generator,
        "model": model,
        "mode": mode,
        "cost_usd": cost_usd,
        "api_calls": api_calls,
        "language": LANGUAGE,
        "pii": False,
        "incident_count": incident_count,
        "grounded_in_window": grounded_in_window,
        "edition_id": uuid.uuid4().hex,
        "sections": sections,
    }


def _claude_prompt(area: Area, incidents: list[dict], draft: dict) -> tuple[str, str]:
    system = (
        "You are a senior geopolitical analyst writing a GeoRiskIntel daily Area of Operations briefing. "
        "Use strict UK English (organisation, programme, centre, colour, armoured, mobilisation, defence, travelling). "
        "Write six sections and nothing else. No personal data, no private names, no telephone numbers, no email addresses. "
        "Ground every development in the supplied open-source items. Do not invent casualties or quotations. "
        "Return only JSON with keys threat_level (one of LOW, MODERATE, SUBSTANTIAL, SEVERE, CRITICAL) "
        "and sections (an array of six objects with keys title and body)."
    )
    public_items = []
    for item in incidents[:8]:
        flat = item if "title" in item and "severity" in item and "properties" not in item else _flatten(item)
        public_items.append({
            "title": flat.get("title") or "",
            "severity": flat.get("severity") or "",
            "area": flat.get("area") or flat.get("country") or "",
            "timestamp": flat.get("timestamp") or "",
            "source_name": flat.get("source_name") or "",
        })
    user = json.dumps({
        "area": area.name,
        "type": area.kind,
        "standing_threat": draft.get("threat_level"),
        "incidents": public_items,
        "section_titles": [spec[1] for spec in SECTION_SPECS],
    }, ensure_ascii=False)
    if len(user) > 7000:
        user = user[:7000]
    return system, user


def _parse_claude(text: str) -> dict | None:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _edition_from_claude(area: Area, draft: dict, text: str, usage: dict, now: datetime) -> dict | None:
    parsed = _parse_claude(text)
    if not parsed:
        return None
    raw_sections = parsed.get("sections")
    if not isinstance(raw_sections, list) or len(raw_sections) < 6:
        return None
    sections = []
    for index, spec in enumerate(SECTION_SPECS):
        block = raw_sections[index] if isinstance(raw_sections[index], dict) else {}
        body = _public(block.get("body") or block.get("text") or "", 5000)
        if len(body) < 40:
            return None
        sections.append(_section(index + 1, spec, body))
    level = str(parsed.get("threat_level") or draft.get("threat_level") or "MODERATE").strip().upper()
    if level not in LEVELS:
        level = str(draft.get("threat_level") or "MODERATE")
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cost = claude_client.estimate_usd(CLAUDE_MODEL, input_tokens, output_tokens) or 0
    edition = _pack_edition(
        area,
        now,
        level,
        sections,
        generator=CLAUDE_MODEL,
        mode="claude",
        model=CLAUDE_MODEL,
        cost_usd=cost,
        api_calls=1,
        incident_count=int(draft.get("incident_count") or 0),
        grounded_in_window=bool(draft.get("grounded_in_window")),
        published=True,
    )
    edition["usage"] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_usd": cost,
    }
    return edition


def _try_claude(area: Area, incidents: list[dict], draft: dict, now: datetime) -> tuple[dict | None, bool]:
    """Return (edition, attempted). attempted is False when no key is configured."""
    key = claude_client.get_anthropic_api_key()
    if not key:
        return None, False
    system, user = _claude_prompt(area, incidents, draft)
    text, usage, failure = claude_client._call_anthropic(
        key, CLAUDE_MODEL, system, user, BRIEF_MAX_TOKENS, "low",
    )
    if failure or not text:
        return None, True
    return _edition_from_claude(area, draft, text, usage if isinstance(usage, dict) else {}, now), True


def build_edition(area: Area, incidents: list[dict], *, use_claude: bool = False, now: datetime | None = None) -> dict:
    moment = utc_now(now)
    draft = build_deterministic(area, incidents, now=moment)
    if not use_claude:
        return draft
    produced, attempted = _try_claude(area, incidents, draft, moment)
    if produced is not None:
        return produced
    fallback = dict(draft)
    fallback["sections"] = list(draft["sections"])
    fallback["generator"] = "deterministic_fallback"
    fallback["mode"] = "claude"
    fallback["model"] = CLAUDE_MODEL
    fallback["cost_usd"] = 0
    fallback["api_calls"] = 1 if attempted else 0
    fallback["edition_id"] = draft["edition_id"]
    return fallback


def _serve(edition: dict, *, cached: bool) -> dict:
    served = dict(edition)
    served["ok"] = True
    published = bool(edition.get("published"))
    served["published"] = published
    served["preview"] = not published
    served["cached"] = bool(cached and published)
    served["cached_for_today"] = bool(published and edition.get("date") == day_string())
    return served


def preview_edition(area: Area, incidents: list[dict] | None = None, now: datetime | None = None) -> dict:
    """On-the-fly $0 preview. Does not write a file and does not call Claude."""
    edition = build_deterministic(area, incidents, now=now)
    edition["published"] = False
    edition["preview"] = True
    edition["cached"] = False
    edition["cached_for_today"] = False
    edition["generator"] = "deterministic"
    edition["cost_usd"] = 0
    edition["api_calls"] = 0
    return edition


def publish_ao(
    slug: object,
    *,
    use_claude: bool = False,
    force: bool = False,
    now: datetime | None = None,
    incidents: list[dict] | None = None,
) -> dict:
    area = get_area(slug)
    if area is None:
        return {"ok": False, "error": "Unknown Area of Operations.", "ao_slug": _slugify(slug)}
    moment = utc_now(now)
    day = moment.strftime("%Y-%m-%d")
    path = edition_file(area.slug, day)
    if path.is_file() and not force:
        stored = _read_json(path)
        if stored:
            served = _serve(stored, cached=True)
            served["cached"] = True
            return served
    pool = list(incidents) if incidents is not None else load_stored_incidents()
    edition = build_edition(area, pool, use_claude=use_claude, now=moment)
    edition["published"] = True
    edition["preview"] = False
    edition["cached"] = False
    _write_json(path, edition)
    served = _serve(edition, cached=False)
    served["cached"] = False
    return served


def _flag(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n", ""}:
        return False
    return default


def _wants_claude(payload: dict) -> bool:
    if "use_claude" in payload:
        return _flag(payload.get("use_claude"), False)
    mode = str(payload.get("mode") or "deterministic").strip().lower()
    return mode == "claude"


def publish_request(payload: dict, *, now: datetime | None = None, incidents: list[dict] | None = None) -> tuple[dict, int]:
    if not isinstance(payload, dict):
        payload = {}
    use_claude = _wants_claude(payload)
    force = _flag(payload.get("force"), False)
    if _flag(payload.get("all_subscribed"), False):
        slugs = subscribed_slugs()
    elif isinstance(payload.get("ao_slugs"), list) and payload.get("ao_slugs"):
        slugs = [str(item) for item in payload["ao_slugs"] if str(item or "").strip()]
    elif str(payload.get("ao_slug") or "").strip():
        slugs = [str(payload.get("ao_slug")).strip()]
    else:
        return {"ok": False, "error": "Provide ao_slug, ao_slugs, or all_subscribed."}, 400
    editions = []
    errors = []
    for slug in slugs:
        result = publish_ao(slug, use_claude=use_claude, force=force, now=now, incidents=incidents)
        if not result.get("ok"):
            errors.append({"ao_slug": _slugify(slug), "error": result.get("error") or "Unknown Area of Operations."})
            continue
        editions.append(result)
    if not editions:
        status = 404 if len(slugs) == 1 else 400
        return {
            "ok": False,
            "error": errors[0]["error"] if errors else "No Area of Operations was published.",
            "errors": errors,
        }, status
    cost = round(sum(float(item.get("cost_usd") or 0) for item in editions), 6)
    return {
        "ok": True,
        "mode": "claude" if use_claude else "deterministic",
        "use_claude": use_claude,
        "force": force,
        "count": len(editions),
        "cost_usd": cost,
        "published": editions,
        "errors": errors,
    }, 200


def _index_row(edition: dict) -> dict:
    return {
        "ao_name": edition.get("ao_name") or "",
        "ao_slug": edition.get("ao_slug") or "",
        "ao_type": edition.get("ao_type") or "",
        "threat_level": edition.get("threat_level") or "",
        "published_at": edition.get("published_at") or "",
        "published_at_gmt": edition.get("published_at_gmt") or "",
        "generator": edition.get("generator") or "",
        "cached_for_today": bool(edition.get("date") == day_string() and edition.get("published", True)),
        "published": True,
        "date": edition.get("date") or "",
    }


def _editions_for(day: str) -> list[dict]:
    folder = _briefing_root() / day
    if not folder.is_dir():
        return []
    rows = []
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        if path.name.endswith(".tmp"):
            continue
        payload = _read_json(path)
        if payload and payload.get("ao_slug"):
            rows.append(payload)
    rows.sort(key=lambda item: (
        -LEVELS.index(item["threat_level"]) if item.get("threat_level") in LEVELS else 0,
        str(item.get("ao_name") or ""),
    ))
    return rows


def list_published(date: object = None) -> tuple[dict, int]:
    today = day_string()
    explicit = str(date or "").strip()
    if explicit:
        chosen = _valid_date(explicit)
        if not chosen:
            return {"ok": False, "error": "Date must be YYYY-MM-DD."}, 400
    elif _editions_for(today):
        chosen = today
    else:
        on_disk = _dates_on_disk()
        chosen = on_disk[-1] if on_disk else today
    editions = _editions_for(chosen)
    input_guess = 2500
    return {
        "ok": True,
        "date": chosen,
        "today": today,
        "is_today": chosen == today,
        "using_latest": not explicit and chosen != today,
        "count": len(editions),
        "today_count": len(_editions_for(today)),
        "briefings": [_index_row(item) for item in editions],
        "areas": catalog_areas(),
        "subscribed": subscribed_slugs(),
        "claude": {
            "model": CLAUDE_MODEL,
            "configured": claude_client.configured(),
            "estimated_usd_ceiling": claude_client.estimate_usd(CLAUDE_MODEL, input_guess, BRIEF_MAX_TOKENS),
            "note": "Published list-price ceiling for one short Haiku 5.5 briefing. Deterministic publish is $0.",
        },
    }, 200


def _latest_date(slug: str) -> str:
    found = []
    for day in _dates_on_disk():
        if edition_file(slug, day).is_file():
            found.append(day)
    return found[-1] if found else ""


def get_published(slug: object, date: object = None, incidents: list[dict] | None = None) -> tuple[dict, int]:
    area = get_area(slug)
    if area is None:
        return {"ok": False, "error": "Unknown Area of Operations."}, 404
    explicit = str(date or "").strip()
    if explicit:
        chosen = _valid_date(explicit)
        if not chosen:
            return {"ok": False, "error": "Date must be YYYY-MM-DD."}, 400
        stored = _read_json(edition_file(area.slug, chosen))
        if stored:
            return _serve(stored, cached=True), 200
        if chosen == day_string():
            return _serve(preview_edition(area, incidents), cached=False), 200
        return {
            "ok": False,
            "error": "No published edition for that date.",
            "ao_slug": area.slug,
            "date": chosen,
        }, 404
    stored = _read_json(edition_file(area.slug, day_string()))
    if stored:
        return _serve(stored, cached=True), 200
    preview = preview_edition(area, incidents)
    latest = _latest_date(area.slug)
    if latest:
        preview["latest_published_date"] = latest
    return _serve(preview, cached=False), 200


def configured_secrets() -> list[str]:
    found = []
    for key in ("CRON_SECRET", "PUBLISH_SECRET"):
        value = os.environ.get(key, "").strip()
        if value and value not in found:
            found.append(value)
    return found


def authorized(header: object) -> bool:
    """Bearer token when a publisher secret is set. Open when neither secret is set."""
    secrets = configured_secrets()
    if not secrets:
        return True
    raw = str(header or "").strip()
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    for secret in secrets:
        token_bytes = token.encode("utf-8")
        secret_bytes = secret.encode("utf-8")
        if len(token_bytes) != len(secret_bytes):
            continue
        if hmac.compare_digest(token_bytes, secret_bytes):
            return True
    return False


def _load_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pack(status: int, payload: dict) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8")


def dispatch(method: str, path: str, body: bytes = b"", authorization: str = "") -> tuple[int, str, bytes] | None:
    """Stdlib-server entry. GET never publishes and never calls Claude."""
    route, _, query = path.partition("?")
    verb = str(method or "GET").upper()
    params = parse_qs(query, keep_blank_values=True)
    date = (params.get("date") or [""])[0]
    if route == "/api/briefings/published" and verb == "GET":
        payload, status = list_published(date or None)
        return _pack(status, payload)
    prefix = "/api/briefings/published/"
    if route.startswith(prefix) and verb == "GET":
        slug = unquote(route[len(prefix):]).strip("/")
        if not slug or "/" in slug:
            return _pack(404, {"ok": False, "error": "Unknown Area of Operations."})
        payload, status = get_published(slug, date or None)
        return _pack(status, payload)
    if route == "/api/briefings/publish" and verb == "GET":
        return _pack(405, {
            "ok": False,
            "error": "Use POST to publish. GET does not generate an edition or call Claude.",
        })
    if route == "/api/briefings/publish" and verb == "POST":
        if not authorized(authorization):
            return _pack(401, {"ok": False, "error": "Unauthorised."})
        payload, status = publish_request(_load_body(body))
        return _pack(status, payload)
    return None


def _json_response(payload: dict, status: int) -> Response:
    response = Response(json.dumps(payload, ensure_ascii=False), status=status, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


@briefings_bp.get("/api/briefings/published")
def api_published_index():
    payload, status = list_published(request.args.get("date"))
    return _json_response(payload, status)


@briefings_bp.get("/api/briefings/published/<ao_slug>")
def api_published_one(ao_slug: str):
    payload, status = get_published(ao_slug, request.args.get("date"))
    return _json_response(payload, status)


@briefings_bp.post("/api/briefings/publish")
def api_publish():
    if not authorized(request.headers.get("Authorization", "")):
        return _json_response({"ok": False, "error": "Unauthorised."}, 401)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    payload, status = publish_request(body)
    return _json_response(payload, status)


@briefings_bp.get("/api/briefings/publish")
def api_publish_get():
    return _json_response({
        "ok": False,
        "error": "Use POST to publish. GET does not generate an edition or call Claude.",
    }, 405)

