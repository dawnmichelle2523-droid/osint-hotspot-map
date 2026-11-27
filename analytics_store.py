"""Persistent historical incidents, analytics, and report-delivery subscriptions.

The database lives under DATA_DIR (an explicit path, a writable /data volume,
or the directory of an absolute DB_PATH, otherwise ./data). Rows are kept
permanently. Nothing in this module deletes incidents because of age.

Classification is regex-only. Incoming feed items never call xAI, even when
ENABLE_XAI_INGEST or XAI_API_KEY is set. The key stays reserved for on-demand
report narrative, risk-assessment drafts, and Grok Ops.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATEGORIES = ("ACT", "SFA", "VC", "CU", "HS", "OD")
CATEGORY_NAMES = {
    "ACT": "Armed Conflict & Terrorism",
    "SFA": "Security Force Activity",
    "VC": "Violent Crime",
    "CU": "Civil Unrest",
    "HS": "Hazards & Safety",
    "OD": "Operational Disruption",
}
SEVERITIES = ("HIGH", "MED", "LOW", "INFO")
REGIONS = (
    "Middle East",
    "UK & Europe",
    "Africa",
    "South Asia",
    "Americas",
    "Asia-Pacific",
    "Maritime",
    "Global",
)
CASUALTY_DISCLAIMER = (
    "Reported fatalities and injuries are extracted from open-source reporting "
    "with lightweight pattern matching. They are not independently verified, "
    "may be incomplete or contested, and can be duplicated across sources."
)

_REGION_MEMBERS = {
    "Middle East": (
        "Bahrain", "Cyprus", "Egypt", "Iran", "Iraq", "Israel", "Jordan", "Kuwait",
        "Lebanon", "Oman", "Palestine", "Qatar", "Saudi Arabia", "Syria", "Turkey",
        "United Arab Emirates", "Yemen",
    ),
    "UK & Europe": (
        "Albania", "Andorra", "Austria", "Belarus", "Belgium", "Bosnia and Herzegovina",
        "Bulgaria", "Croatia", "Czech Republic", "Denmark", "Estonia", "Finland",
        "France", "Germany", "Greece", "Hungary", "Iceland", "Ireland", "Italy",
        "Kosovo", "Latvia", "Liechtenstein", "Lithuania", "Luxembourg", "Malta",
        "Moldova", "Monaco", "Montenegro", "Netherlands", "North Macedonia", "Norway",
        "Poland", "Portugal", "Romania", "Russia", "San Marino", "Serbia", "Slovakia",
        "Slovenia", "Spain", "Sweden", "Switzerland", "Ukraine", "United Kingdom",
        "Vatican City",
    ),
    "Africa": (
        "Algeria", "Angola", "Benin", "Botswana", "Burkina Faso", "Burundi", "Cabo Verde",
        "Cameroon", "Central African Republic", "Chad", "Comoros", "Congo",
        "Democratic Republic of the Congo", "Djibouti", "Equatorial Guinea", "Eritrea",
        "Eswatini", "Ethiopia", "Gabon", "Gambia", "Ghana", "Guinea", "Guinea-Bissau",
        "Ivory Coast", "Kenya", "Lesotho", "Liberia", "Libya", "Madagascar", "Malawi",
        "Mali", "Mauritania", "Mauritius", "Morocco", "Mozambique", "Namibia", "Niger",
        "Nigeria", "Rwanda", "Senegal", "Sierra Leone", "Somalia", "South Africa",
        "South Sudan", "Sudan", "Tanzania", "Togo", "Tunisia", "Uganda", "Zambia",
        "Zimbabwe",
    ),
    "South Asia": (
        "Afghanistan", "Bangladesh", "Bhutan", "India", "Maldives", "Nepal", "Pakistan",
        "Sri Lanka",
    ),
    "Asia-Pacific": (
        "Australia", "Brunei", "Cambodia", "China", "Fiji", "Indonesia", "Japan",
        "Kazakhstan", "Kyrgyzstan", "Laos", "Malaysia", "Mongolia", "Myanmar",
        "New Zealand", "North Korea", "Papua New Guinea", "Philippines", "Singapore",
        "South Korea", "Taiwan", "Tajikistan", "Thailand", "Timor-Leste", "Turkmenistan",
        "Uzbekistan", "Vietnam",
    ),
    "Americas": (
        "Antigua and Barbuda", "Argentina", "Bahamas", "Barbados", "Belize", "Bolivia",
        "Brazil", "Canada", "Chile", "Colombia", "Costa Rica", "Cuba", "Dominica",
        "Dominican Republic", "Ecuador", "El Salvador", "Grenada", "Guatemala", "Guyana",
        "Haiti", "Honduras", "Jamaica", "Mexico", "Nicaragua", "Panama", "Paraguay",
        "Peru", "Saint Lucia", "Suriname", "Trinidad and Tobago", "United States",
        "Uruguay", "Venezuela",
    ),
}
COUNTRY_REGION = {
    country: region
    for region, names in _REGION_MEMBERS.items()
    for country in names
}
COUNTRY_LOOKUP = {name.lower(): name for name in COUNTRY_REGION}
COUNTRY_ALIASES = {
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "america": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "britain": "United Kingdom",
    "great britain": "United Kingdom",
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "ksa": "Saudi Arabia",
    "turkiye": "Turkey",
    "türkiye": "Turkey",
    "czechia": "Czech Republic",
    "russian federation": "Russia",
    "syrian arab republic": "Syria",
    "islamic republic of iran": "Iran",
    "republic of korea": "South Korea",
    "korea": "South Korea",
    "dprk": "North Korea",
    "palestine": "Palestine",
    "palestinian territories": "Palestine",
    "gaza strip": "Palestine",
    "west bank": "Palestine",
    "cote d'ivoire": "Ivory Coast",
    "côte d'ivoire": "Ivory Coast",
    "drc": "Democratic Republic of the Congo",
    "dr congo": "Democratic Republic of the Congo",
    "burma": "Myanmar",
    "viet nam": "Vietnam",
    "republic of moldova": "Moldova",
    "united republic of tanzania": "Tanzania",
    "lao": "Laos",
}
REGION_ALIASES = {
    "middle east": "Middle East",
    "mena": "Middle East",
    "uk & europe": "UK & Europe",
    "uk and europe": "UK & Europe",
    "europe": "UK & Europe",
    "eu": "UK & Europe",
    "africa": "Africa",
    "south asia": "South Asia",
    "americas": "Americas",
    "south america": "Americas",
    "north america": "Americas",
    "latin america": "Americas",
    "asia-pacific": "Asia-Pacific",
    "asia pacific": "Asia-Pacific",
    "apac": "Asia-Pacific",
    "maritime": "Maritime",
    "global": "Global",
    "world": "Global",
    "all": "Global",
}
_STORED_ALIASES = {
    "United States": ("USA", "US", "United States of America"),
    "United Kingdom": ("UK", "Britain", "Great Britain"),
    "United Arab Emirates": ("UAE", "U.A.E."),
    "Saudi Arabia": ("KSA",),
    "Turkey": ("Turkiye", "Türkiye"),
    "Czech Republic": ("Czechia",),
    "Russia": ("Russian Federation",),
}
_MARITIME_COUNTRIES = ("Maritime", "Red Sea / Maritime", "Strait of Hormuz / Gulf")
_PLACES = {
    "deir ez-zor": ("Deir ez-Zor", "Syria"),
    "deir ezzor": ("Deir ez-Zor", "Syria"),
    "el fasher": ("El Fasher", "Sudan"),
    "port sudan": ("Port Sudan", "Sudan"),
    "bab el-mandeb": ("Bab el-Mandeb", ""),
    "bab al mandab": ("Bab el-Mandeb", ""),
    "gulf of aden": ("Gulf of Aden", ""),
    "strait of hormuz": ("Strait of Hormuz", ""),
    "aleppo": ("Aleppo", "Syria"),
    "damascus": ("Damascus", "Syria"),
    "idlib": ("Idlib", "Syria"),
    "homs": ("Homs", "Syria"),
    "hama": ("Hama", "Syria"),
    "latakia": ("Latakia", "Syria"),
    "raqqa": ("Raqqa", "Syria"),
    "daraa": ("Daraa", "Syria"),
    "khartoum": ("Khartoum", "Sudan"),
    "kyiv": ("Kyiv", "Ukraine"),
    "kiev": ("Kyiv", "Ukraine"),
    "kharkiv": ("Kharkiv", "Ukraine"),
    "london": ("London", "United Kingdom"),
    "beirut": ("Beirut", "Lebanon"),
    "baghdad": ("Baghdad", "Iraq"),
    "gaza": ("Gaza", "Palestine"),
    "rafah": ("Rafah", "Palestine"),
    "sanaa": ("Sanaa", "Yemen"),
    "sana'a": ("Sanaa", "Yemen"),
    "aden": ("Aden", "Yemen"),
    "tehran": ("Tehran", "Iran"),
    "amman": ("Amman", "Jordan"),
    "mosul": ("Mosul", "Iraq"),
    "donetsk": ("Donetsk", "Ukraine"),
}
_PLACE_KEYS = sorted(_PLACES, key=len, reverse=True)

_CATEGORY_RULES = (
    ("HS", re.compile(
        r"\b(?:earthquakes?|flood(?:ing|s)?|wildfires?|hurricanes?|cyclones?|typhoons?|"
        r"tsunamis?|landslides?|volcan(?:o|ic)|droughts?|epidemic|outbreak|pandemic|"
        r"storm surge|magnitude\s+\d|road traffic|traffic collision)\b",
        re.I,
    ), 5),
    ("CU", re.compile(
        r"\b(?:protests?|demonstrations?|riots?|civil unrest|\bunrest\b|marchers|walkouts?)\b",
        re.I,
    ), 4),
    ("ACT", re.compile(
        r"\b(?:airstrikes?|air strikes?|shelling|artillery|missiles?|rockets?|ieds?|"
        r"vbieds?|car bombs?|suicide bombs?|terror(?:ist|ism)|militants?|insurgents?|"
        r"firefights?|clashes?|drone strikes?|bombard(?:ment|ed)|armed (?:attack|group|clash)|"
        r"isis|daesh|houthis?|hostil(?:e|ities))\b",
        re.I,
    ), 5),
    ("SFA", re.compile(
        r"\b(?:arrest(?:ed|s)?|detained|detention|checkpoints?|security forces|"
        r"police raid|raid by|curfew|patrols?)\b",
        re.I,
    ), 4),
    ("VC", re.compile(
        r"\b(?:murders?|homicides?|robber(?:y|ies)|armed robber(?:y|ies)|assaults?|"
        r"kidnapp?(?:ing|ed)|abductions?|stabbings?|carjack(?:ing|ed)?|shootings?|gang)\b",
        re.I,
    ), 4),
    ("OD", re.compile(
        r"\b(?:airport (?:closure|closed|shutdown)|border (?:closure|closed)|"
        r"power (?:outage|cut)|blackouts?|port (?:closure|disruption)|"
        r"flights? cancel(?:led|ed)?|notams?|airspace|travel advisory|"
        r"disrupt(?:ion|ed)|blockade|cyber)\b",
        re.I,
    ), 3),
)
_HINTS = {
    "ACT": "ACT",
    "ARMED CONFLICT & TERRORISM": "ACT",
    "ARMED CONFLICT AND TERRORISM": "ACT",
    "SFA": "SFA",
    "SECURITY FORCE ACTIVITY": "SFA",
    "VC": "VC",
    "VIOLENT CRIME": "VC",
    "CU": "CU",
    "CIVIL UNREST": "CU",
    "HS": "HS",
    "HAZARDS & SAFETY": "HS",
    "HAZARDS AND SAFETY": "HS",
    "NATURAL DISASTERS": "HS",
    "OD": "OD",
    "OPERATIONAL DISRUPTION": "OD",
    "INDIRECT FIRE (IDF) AND AIR STRIKES": "ACT",
    "SMALL ARMS FIRE (SAF)": "ACT",
    "SUICIDE / VEHICLE-BORNE IMPROVISED EXPLOSIVE DEVICE (S/VBIED)": "ACT",
    "IMPROVISED EXPLOSIVE DEVICE (IED)": "ACT",
    "EXPLOSIVELY FORMED PROJECTILE (EFP)": "ACT",
    "UNEXPLODED ORDNANCE (UXO)": "HS",
    "KIDNAP": "VC",
    "ROAD TRAFFIC COLLISION (RTC)": "HS",
    "DETENTION / ARREST (CHECKPOINTS)": "SFA",
    "MARITIME ADVISORY": "OD",
    "TRAVEL / EMBASSY ADVISORY": "OD",
    "OTHER / ADVISORY": "OD",
    "MISCELLANEOUS": "OD",
}
_HIGH_RE = re.compile(
    r"\b(?:massacre|airstrikes?|air strikes?|terror(?:ist|ism)|suicide bombs?|"
    r"car bombs?|vbieds?|dozens (?:killed|dead)|mass casualty|substantial threat)\b",
    re.I,
)
_MED_RE = re.compile(
    r"\b(?:clashes?|explosions?|riots?|wounded|injured|earthquakes?|flood(?:ing)?|"
    r"shelling|firefights?)\b",
    re.I,
)
_INFO_RE = re.compile(
    r"\b(?:advisories|advisory|notams?|forecast|no attacks|situation update)\b|"
    r"\b(?:airport|border|port) (?:closure|closed)\b",
    re.I,
)
_LOW_RE = re.compile(r"\b(?:arrest(?:ed|s)?|checkpoints?|protests?|demonstrations?|patrols?)\b", re.I)
_FATAL_RES = (
    re.compile(r"\b(\d{1,6})\s+(?:people\s+|persons\s+|civilians\s+|soldiers\s+|troops\s+)?(?:were\s+|was\s+)?(?:killed|dead|slain)\b", re.I),
    re.compile(r"\bkills?\s+(\d{1,6})\b", re.I),
    re.compile(r"\bdeath toll(?:\s+of|\s+reached|:)?\s+(\d{1,6})\b", re.I),
    re.compile(r"\b(\d{1,6})\s+fatalit(?:y|ies)\b", re.I),
)
_INJURY_RES = (
    re.compile(r"\b(\d{1,6})\s+(?:people\s+|persons\s+|civilians\s+)?(?:were\s+|was\s+)?(?:injured|wounded|hurt)\b", re.I),
    re.compile(r"\b(?:injures|wounds|injured|wounded)\s+(\d{1,6})\b", re.I),
    re.compile(r"\bleaves\s+(\d{1,6})\s+(?:people\s+)?(?:injured|wounded)\b", re.I),
)
_NONE_FATAL_RE = re.compile(r"\bno (?:reported )?(?:fatalities|deaths|people killed)\b", re.I)
_NONE_INJURY_RE = re.compile(r"\bno (?:reported )?(?:injuries|wounded)\b", re.I)
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_SEA_RE = re.compile(
    r"\b(?:bab el-mandeb|bab al mandab|red sea|gulf of aden|strait of hormuz|"
    r"persian gulf|arabian gulf|arabian sea)\b",
    re.I,
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CADENCES = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}

_lock = threading.Lock()
_store: "Store | None" = None
_store_key: str | None = None


def ingest_uses_model() -> bool:
    """Background ingest never spends model tokens.

    ENABLE_XAI_INGEST is intentionally ignored. Classification stays on-device
    so a feed poll cannot call the xAI API.
    """
    return False


def _fold(value: object) -> str:
    text = " ".join(str(value or "").replace("&", " and ").split()).casefold()
    return text.replace("’", "'")


def canonical_country(value: object) -> str:
    key = _fold(value)
    if not key:
        return ""
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    return COUNTRY_LOOKUP.get(key, "")


def country_match_values(value: object) -> list[str]:
    canonical = canonical_country(value)
    raw = " ".join(str(value or "").split())
    values = {item for item in (canonical, raw) if item}
    if canonical:
        values.update(_STORED_ALIASES.get(canonical, ()))
    return sorted(values)


def canonical_region(value: object) -> str:
    key = _fold(value).replace(" and ", " and ")
    spaced = " ".join(str(value or "").replace("&", " and ").split()).casefold()
    if spaced in REGION_ALIASES:
        return REGION_ALIASES[spaced]
    if key in REGION_ALIASES:
        return REGION_ALIASES[key]
    for name in REGIONS:
        if _fold(name) == key or _fold(name) == spaced:
            return name
    return ""


def resolve_scope(value: object) -> tuple[str, str]:
    """Return (country|region|global, canonical label)."""
    raw = " ".join(str(value or "").split())
    if not raw:
        return "global", "Global"
    region = canonical_region(raw)
    if region == "Global":
        return "global", "Global"
    if region:
        return "region", region
    country = canonical_country(raw)
    if country:
        return "country", country
    return "country", raw


def region_for_country(country: str, explicit: object = "") -> str:
    if country in COUNTRY_REGION:
        return COUNTRY_REGION[country]
    named = canonical_region(explicit)
    if named and named != "Global":
        return named
    if country in _MARITIME_COUNTRIES or _fold(explicit) == "maritime":
        return "Maritime"
    return "Other"


def _can_write(path: Path) -> bool:
    if not path.is_dir():
        return False
    probe = path / f".georisk-write-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def _absolute_db_dir(db_path: str) -> Path | None:
    if not db_path:
        return None
    candidate = Path(db_path).expanduser()
    if not candidate.is_absolute():
        return None
    folder = candidate if candidate.is_dir() else candidate.parent
    if _can_write(folder):
        return folder
    return None


def resolve_data_dir(
    environ: dict | None = None,
    mount: Path | None = None,
    local: Path | None = None,
) -> Path:
    """DATA_DIR, else a writable /data, else an absolute DB_PATH directory, else ./data."""
    envmap = os.environ if environ is None else environ
    mount_path = Path("/data") if mount is None else Path(mount)
    local_path = ROOT / "data" if local is None else Path(local)
    chosen = str(envmap.get("DATA_DIR") or "").strip()
    if chosen:
        path = Path(chosen).expanduser()
        if path.suffix == ".db":
            path.parent.mkdir(parents=True, exist_ok=True)
            return path.parent
        path.mkdir(parents=True, exist_ok=True)
        return path
    if mount_path.is_dir() and _can_write(mount_path):
        return mount_path
    folder = _absolute_db_dir(str(envmap.get("DB_PATH") or "").strip())
    if folder is not None:
        return folder
    local_path.mkdir(parents=True, exist_ok=True)
    return local_path


def uses_durable_volume(path: Path) -> bool:
    """True when the operator set DATA_DIR or the resolved folder is the /data volume."""
    if str(os.environ.get("DATA_DIR") or "").strip():
        return True
    try:
        return path.resolve() == Path("/data").resolve()
    except OSError:
        return str(path) == "/data"


def database_file(data_dir: Path | None = None) -> Path:
    folder = data_dir or resolve_data_dir()
    return folder / "incidents.db"


def format_ts(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: object, default: datetime | None = None) -> datetime | None:
    text = " ".join(str(value or "").split())
    if not text:
        return default
    candidate = text.replace("Z", "+00:00")
    moment = None
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        moment = None
    if moment is None:
        cleaned = text.replace("GMT", "").strip()
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
    if moment is None:
        return default
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _max_match(patterns: tuple[re.Pattern[str], ...], text: str) -> int | None:
    found: list[int] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            try:
                found.append(int(match.group(1)))
            except (TypeError, ValueError):
                continue
    return max(found) if found else None


def extract_casualties(text: object) -> tuple[int | None, int | None]:
    blob = " ".join(str(text or "").split())
    fatalities = _max_match(_FATAL_RES, blob)
    injuries = _max_match(_INJURY_RES, blob)
    if fatalities is None:
        word = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b(?:\s+\w+){0,3}\s+(?:killed|dead)\b", blob, re.I)
        if word:
            fatalities = _WORD_NUMBERS[word.group(1).lower()]
        elif _NONE_FATAL_RE.search(blob):
            fatalities = 0
    if injuries is None:
        word = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b(?:\s+\w+){0,3}\s+(?:injured|wounded)\b", blob, re.I)
        if word:
            injuries = _WORD_NUMBERS[word.group(1).lower()]
        elif _NONE_INJURY_RE.search(blob):
            injuries = 0
    return fatalities, injuries


def _hint_category(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    upper = text.upper()
    if upper in _HINTS:
        return _HINTS[upper]
    return ""


def classify_category(text: object, hinted: object = "") -> str:
    blob = str(text or "")
    hint = _hint_category(hinted)
    scores = {code: 0 for code in CATEGORIES}
    for code, pattern, weight in _CATEGORY_RULES:
        if pattern.search(blob):
            scores[code] += weight
    best = max(CATEGORIES, key=lambda code: (scores[code], -CATEGORIES.index(code)))
    if scores[best] == 0:
        return hint or "OD"
    if (
        hint == "OD"
        and re.search(r"\badvisory\b|\bno attacks\b", blob, re.I)
        and not re.search(r"\b(?:killed|airstrike|air strike|wounded|explosion)\b", blob, re.I)
    ):
        return "OD"
    return best


def _severity_from_hint(value: object) -> str:
    key = _fold(value)
    if key in {"high", "critical", "severe", "substantial"}:
        return "HIGH"
    if key in {"medium", "med", "moderate"}:
        return "MED"
    if key in {"low", "minor"}:
        return "LOW"
    if key in {"info", "information", "advisory"}:
        return "INFO"
    return ""


def _severity_from_text(text: str) -> str:
    if _HIGH_RE.search(text):
        return "HIGH"
    if _MED_RE.search(text):
        return "MED"
    if _INFO_RE.search(text):
        return "INFO"
    if _LOW_RE.search(text):
        return "LOW"
    return ""


def classify_severity(
    text: object,
    hinted: object = "",
    fatalities: int | None = None,
    injuries: int | None = None,
) -> str:
    blob = str(text or "")
    base = _severity_from_hint(hinted) or _severity_from_text(blob) or "LOW"
    if fatalities is not None and fatalities >= 5:
        return "HIGH"
    if fatalities is not None and fatalities >= 1 and base in {"LOW", "INFO"}:
        return "MED"
    if injuries is not None and injuries >= 1 and base in {"LOW", "INFO"}:
        return "MED"
    return base


def classify_text(title: object = "", summary: object = "", category_hint: object = "", severity_hint: object = "") -> dict:
    blob = " ".join(part for part in (str(title or ""), str(summary or "")) if part).strip()
    fatalities, injuries = extract_casualties(blob)
    category = classify_category(blob, category_hint)
    severity = classify_severity(blob, severity_hint, fatalities, injuries)
    return {
        "category": category,
        "category_name": CATEGORY_NAMES[category],
        "severity": severity,
        "fatalities": fatalities,
        "injuries": injuries,
    }


def _coords(lat: object, lng: object) -> tuple[float | None, float | None]:
    try:
        lat_n = round(float(lat), 5)
        lng_n = round(float(lng), 5)
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat_n <= 90 and -180 <= lng_n <= 180):
        return None, None
    if lat_n == 0 and lng_n == 0:
        return None, None
    return lat_n, lng_n


def _find_place(text: str) -> tuple[str, str] | None:
    hay = text.lower()
    for key in _PLACE_KEYS:
        if re.search(r"\b" + re.escape(key) + r"\b", hay):
            return _PLACES[key]
    return None


def normalize_incident(raw: dict, now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else raw
    title = " ".join(str(props.get("title") or "").split())[:240]
    summary = " ".join(str(props.get("summary") or props.get("original_text") or "").split())[:500]
    if not title and summary:
        title = summary[:180]
    classified = classify_text(
        title,
        summary,
        props.get("category") or props.get("feed_category") or "",
        props.get("severity") or "",
    )
    country = canonical_country(props.get("country"))
    if not country:
        cleaned = " ".join(str(props.get("country") or "").split())
        country = cleaned
    subregion = " ".join(str(props.get("subregion") or props.get("sub_region") or props.get("governorate") or props.get("area") or "").split())
    city = " ".join(str(props.get("city") or props.get("location_name") or "").split())
    place = _find_place(f"{title} {summary} {subregion} {city}")
    if place:
        if not subregion:
            subregion = place[0]
        if not country and place[1]:
            country = place[1]
    if not subregion:
        subregion = city
    explicit_region = props.get("region") or props.get("theatre")
    region = region_for_country(country, explicit_region)
    if not country and (_SEA_RE.search(f"{title} {summary}") or canonical_region(explicit_region) == "Maritime"):
        region = "Maritime"
    lat, lng = _coords(props.get("lat"), props.get("lng"))
    if lat is None:
        geometry = raw.get("geometry") if isinstance(raw.get("geometry"), dict) else {}
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lat, lng = _coords(coords[1], coords[0])
    timestamp = parse_ts(props.get("timestamp") or props.get("published_at") or props.get("ingested_at"), moment)
    source_url = " ".join(str(props.get("source_url") or props.get("source") or "").split())
    source_name = " ".join(str(props.get("source_name") or props.get("source_label") or props.get("account") or "").split())[:120]
    category = classified["category"]
    explicit_category = str(props.get("category") or "").strip().upper()
    if explicit_category in CATEGORIES:
        category = explicit_category
    fatalities = classified["fatalities"]
    injuries = classified["injuries"]
    if "fatalities" in props and props.get("fatalities") is not None:
        try:
            fatalities = int(props["fatalities"])
        except (TypeError, ValueError):
            pass
    if "injuries" in props and props.get("injuries") is not None:
        try:
            injuries = int(props["injuries"])
        except (TypeError, ValueError):
            pass
    item = {
        "timestamp": format_ts(timestamp or moment),
        "title": title or "Untitled incident",
        "summary": summary,
        "country": country,
        "region": region,
        "subregion": subregion[:120],
        "city": city[:120],
        "category": category,
        "severity": classified["severity"],
        "fatalities": fatalities,
        "injuries": injuries,
        "lat": lat,
        "lng": lng,
        "source_name": source_name,
        "source_url": source_url[:500],
    }
    item["dedupe_key"] = dedupe_key(item)
    return item


def dedupe_key(item: dict) -> str:
    url = str(item.get("source_url") or "").strip().lower()
    if url:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()
    raw = "|".join(
        str(item.get(field) or "")
        for field in ("timestamp", "title", "country", "subregion")
    ).casefold()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Store:
    """SQLite historical archive. Callers share one connection behind a lock."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _ensure(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                subregion TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                fatalities INTEGER,
                injuries INTEGER,
                lat REAL,
                lng REAL,
                source_name TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                ingested_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_timestamp ON incidents(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_country ON incidents(country)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_region ON incidents(region)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_subregion ON incidents(subregion)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_category ON incidents(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(severity)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                period TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                threat_level TEXT NOT NULL,
                risk_index REAL NOT NULL,
                narrative_source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                html TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_generated ON reports(generated_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_label TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                cadence TEXT NOT NULL,
                recipients TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sent_at TEXT,
                last_test_at TEXT,
                last_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def incident_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()
        return int(row["n"])

    def index_columns(self) -> dict[str, list[str]]:
        with self._lock:
            indexes = self._conn.execute("PRAGMA index_list('incidents')").fetchall()
            found: dict[str, list[str]] = {}
            for index in indexes:
                name = str(index[1])
                cols = self._conn.execute(f"PRAGMA index_info('{name}')").fetchall()
                found[name] = [str(col[2]) for col in cols]
        return found

    def upsert_incident(self, raw: dict, now: datetime | None = None) -> int | None:
        if ingest_uses_model():
            raise RuntimeError("historical ingest must not call a model")
        item = normalize_incident(raw, now=now)
        title = item["title"]
        summary = item["summary"]
        try:
            import feed_quality

            if feed_quality.is_noise(title, summary, item["source_url"]):
                return None
        except Exception:
            if not str(raw.get("title") or raw.get("properties", {}).get("title") or "").strip() and title == "Untitled incident":
                return None
        ingested = format_ts(now or datetime.now(timezone.utc))
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM incidents WHERE dedupe_key = ?",
                (item["dedupe_key"],),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = self._conn.execute(
                """
                INSERT INTO incidents (
                    dedupe_key, timestamp, title, summary, country, region, subregion, city,
                    category, severity, fatalities, injuries, lat, lng, source_name, source_url, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["dedupe_key"], item["timestamp"], item["title"], item["summary"],
                    item["country"], item["region"], item["subregion"], item["city"],
                    item["category"], item["severity"], item["fatalities"], item["injuries"],
                    item["lat"], item["lng"], item["source_name"], item["source_url"], ingested,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def rows_between(
        self,
        start: datetime,
        end: datetime,
        scope: str,
        category: str = "",
    ) -> list[dict]:
        kind, label = resolve_scope(scope)
        clauses = ["timestamp >= ?", "timestamp < ?"]
        params: list[object] = [format_ts(start), format_ts(end)]
        if category:
            clauses.append("category = ?")
            params.append(category)
        if kind == "country":
            values = country_match_values(label)
            marks = ",".join("?" for _ in values)
            clauses.append(f"country IN ({marks})")
            params.extend(values)
        elif kind == "region" and label == "Maritime":
            marks = ",".join("?" for _ in _MARITIME_COUNTRIES)
            clauses.append(f"(region = ? OR country IN ({marks}))")
            params.append("Maritime")
            params.extend(_MARITIME_COUNTRIES)
        elif kind == "region":
            countries = list(_REGION_MEMBERS.get(label, ()))
            marks = ",".join("?" for _ in countries) or "''"
            clauses.append(f"(region = ? OR country IN ({marks}))")
            params.append(label)
            params.extend(countries)
        sql = (
            "SELECT timestamp, title, summary, country, region, subregion, city, category, "
            "severity, fatalities, injuries, lat, lng, source_name FROM incidents WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def save_report(self, record: dict) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO reports (
                    scope_type, scope, period, period_start, period_end, generated_at,
                    threat_level, risk_index, narrative_source, payload_json, html
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["scope_type"], record["scope"], record["period"],
                    record["period_start"], record["period_end"], record["generated_at"],
                    record["threat_level"], record["risk_index"], record["narrative_source"],
                    record["payload_json"], record["html"],
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_reports(self, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, scope_type, scope, period, period_start, period_end, generated_at,
                       threat_level, risk_index, narrative_source
                FROM reports ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_report(self, report_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return dict(row) if row else None

    def meta_get(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        return "" if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO store_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def save_schedule(self, payload: dict, now: datetime | None = None) -> dict:
        moment = format_ts(now or datetime.now(timezone.utc))
        label = " ".join(str(payload.get("client_label") or "").split())[:120]
        if not label:
            raise ValueError("Client label is required.")
        scope_type, scope = resolve_scope(payload.get("scope") or "")
        if not str(payload.get("scope") or "").strip():
            raise ValueError("Scope is required.")
        cadence = _CADENCES.get(str(payload.get("cadence") or "").strip().lower())
        if not cadence:
            raise ValueError("Cadence must be daily, weekly, or monthly.")
        recipients = parse_recipients(payload.get("recipients"))
        if not recipients:
            raise ValueError("At least one valid recipient email is required.")
        enabled = 1 if payload.get("enabled", True) else 0
        schedule_id = payload.get("id")
        with self._lock:
            if schedule_id:
                existing = self._conn.execute(
                    "SELECT id FROM delivery_schedules WHERE id = ?",
                    (int(schedule_id),),
                ).fetchone()
                if existing is None:
                    raise ValueError("Subscription was not found.")
                self._conn.execute(
                    """
                    UPDATE delivery_schedules
                    SET client_label = ?, scope_type = ?, scope = ?, cadence = ?,
                        recipients = ?, enabled = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (label, scope_type, scope, cadence, json.dumps(recipients), enabled, moment, int(schedule_id)),
                )
                saved_id = int(schedule_id)
            else:
                cursor = self._conn.execute(
                    """
                    INSERT INTO delivery_schedules (
                        client_label, scope_type, scope, cadence, recipients, enabled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (label, scope_type, scope, cadence, json.dumps(recipients), enabled, moment, moment),
                )
                saved_id = int(cursor.lastrowid)
            self._conn.commit()
        found = self.get_schedule(saved_id)
        if found is None:
            raise ValueError("Subscription was not saved.")
        return found

    def list_schedules(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM delivery_schedules ORDER BY client_label, id"
            ).fetchall()
        return [_schedule_dict(row) for row in rows]

    def get_schedule(self, schedule_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM delivery_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        return _schedule_dict(row) if row else None

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM delivery_schedules WHERE id = ?",
                (schedule_id,),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def mark_schedule(self, schedule_id: int, *, sent_at: datetime | None = None, test_at: datetime | None = None, status: str = "", error: str = "") -> None:
        assignments = ["last_status = ?", "last_error = ?", "updated_at = ?"]
        params: list[object] = [status[:120], error[:300], format_ts(datetime.now(timezone.utc))]
        if sent_at is not None:
            assignments.append("last_sent_at = ?")
            params.append(format_ts(sent_at))
        if test_at is not None:
            assignments.append("last_test_at = ?")
            params.append(format_ts(test_at))
        params.append(schedule_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE delivery_schedules SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            self._conn.commit()


def _schedule_dict(row: sqlite3.Row) -> dict:
    try:
        recipients = json.loads(row["recipients"] or "[]")
    except json.JSONDecodeError:
        recipients = []
    if not isinstance(recipients, list):
        recipients = []
    return {
        "id": int(row["id"]),
        "client_label": row["client_label"],
        "scope_type": row["scope_type"],
        "scope": row["scope"],
        "cadence": row["cadence"],
        "recipients": [str(item) for item in recipients],
        "enabled": bool(row["enabled"]),
        "last_sent_at": row["last_sent_at"] or "",
        "last_test_at": row["last_test_at"] or "",
        "last_status": row["last_status"] or "",
        "last_error": row["last_error"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def parse_recipients(value: object) -> list[str]:
    if isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[,;\n]+", str(value or ""))
    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        email = part.strip()
        key = email.casefold()
        if not email or key in seen or not _EMAIL_RE.match(email):
            continue
        seen.add(key)
        found.append(email)
    return found


def get_store() -> Store:
    global _store, _store_key
    path = str(database_file())
    with _lock:
        if _store is None or _store_key != path:
            if _store is not None:
                _store.close()
            _store = Store(path)
            _store_key = path
        return _store


def reset_store() -> None:
    global _store, _store_key
    with _lock:
        if _store is not None:
            _store.close()
        _store = None
        _store_key = None


def store_incident(raw: dict, now: datetime | None = None, store: Store | None = None) -> int | None:
    return (store or get_store()).upsert_incident(raw, now=now)


def math_floor_tenth(value: float) -> float:
    import math

    return math.floor(value * 10 + 0.5) / 10.0


def risk_index(counts: dict[str, int], days: float) -> float:
    import math

    weighted = (
        5.0 * counts.get("HIGH", 0)
        + 3.0 * counts.get("MED", 0)
        + 1.0 * counts.get("LOW", 0)
        + 0.2 * counts.get("INFO", 0)
    )
    per_day = weighted / max(float(days), 1.0)
    value = 10.0 * (1.0 - math.exp(-per_day / 4.0))
    return math_floor_tenth(min(10.0, max(0.0, value)))


def risk_level(index: float) -> str:
    if index >= 7.5:
        return "EXTREME"
    if index >= 5.0:
        return "HIGH"
    if index >= 2.5:
        return "MEDIUM"
    return "LOW"


def percent_delta(current: float, baseline: float) -> float | None:
    if baseline == 0:
        if current == 0:
            return 0.0
        return None
    return round((current - baseline) / baseline * 100.0, 1)


def trend_arrow(current: float, baseline: float, percent: float | None, tolerance: float = 5.0) -> str:
    if baseline == 0 and current == 0:
        return "↔"
    if baseline == 0 and current > 0:
        return "↑"
    if percent is None:
        return "↔"
    if percent > tolerance:
        return "↑"
    if percent < -tolerance:
        return "↓"
    return "↔"


def risk_arrow(current: float, prior: float) -> str:
    if current > prior + 0.3:
        return "↑"
    if current < prior - 0.3:
        return "↓"
    return "↔"


def resolve_period(period: object, days: object = None) -> tuple[str, int, int, str]:
    name = str(period or "").strip().lower()
    if days not in (None, ""):
        try:
            length = int(days)
        except (TypeError, ValueError):
            length = 7
        length = max(1, min(length, 365))
        if length == 1:
            return "daily", 1, 30, "30-day average"
        if length == 7:
            return "weekly", 7, 8, "8-week average"
        if length == 30:
            return "monthly", 30, 3, "3-month average"
        if length == 90:
            return "custom", 90, 3, "3-period average"
        if length == 365:
            return "custom", 365, 1, "prior year"
        slices = 8 if length <= 14 else 3 if length <= 45 else 1
        label = {8: "8-period average", 3: "3-period average", 1: "prior period"}[slices]
        return "custom", length, slices, label
    if name == "daily":
        return "daily", 1, 30, "30-day average"
    if name == "monthly":
        return "monthly", 30, 3, "3-month average"
    if name == "custom":
        return "weekly", 7, 8, "8-week average"
    return "weekly", 7, 8, "8-week average"


def _empty_bucket() -> dict:
    return {
        "categories": {code: 0 for code in CATEGORIES},
        "severity": {level: 0 for level in SEVERITIES},
        "total": 0,
        "fatalities": 0,
        "injuries": 0,
    }


def _add_row(bucket: dict, row: dict) -> None:
    category = row["category"] if row["category"] in CATEGORIES else "OD"
    severity = row["severity"] if row["severity"] in SEVERITIES else "LOW"
    bucket["categories"][category] += 1
    bucket["severity"][severity] += 1
    bucket["total"] += 1
    if row.get("fatalities"):
        bucket["fatalities"] += int(row["fatalities"])
    if row.get("injuries"):
        bucket["injuries"] += int(row["injuries"])


def _bucket_name(row: dict, axis: str) -> str:
    if axis == "country":
        if row.get("country"):
            return str(row["country"])
        if row.get("region") == "Maritime":
            return "Maritime"
        return "Unspecified"
    return str(row.get("subregion") or row.get("city") or "Unspecified")


def _significant(bucket: dict) -> int:
    return bucket["severity"]["HIGH"] + bucket["severity"]["MED"]


def query_analytics(
    scope: str,
    period: str = "weekly",
    days: object = None,
    category: str = "",
    now: datetime | None = None,
    store: Store | None = None,
) -> dict:
    code = str(category or "").strip().upper()
    if code and code not in CATEGORIES:
        raise ValueError("Category must be one of ACT, SFA, VC, CU, HS, OD.")
    period_name, length, slices, baseline_label = resolve_period(period, days)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    kind, label = resolve_scope(scope)
    lookback = moment - timedelta(days=length * (slices + 1))
    rows = (store or get_store()).rows_between(lookback, moment, label if kind != "global" else "Global", code)
    windows = []
    for index in range(slices, -1, -1):
        finish = moment - timedelta(days=length * index)
        start = finish - timedelta(days=length)
        windows.append((start, finish, _empty_bucket(), []))
    for row in rows:
        stamp = parse_ts(row["timestamp"])
        if stamp is None:
            continue
        for start, finish, bucket, members in windows:
            if start <= stamp < finish:
                _add_row(bucket, row)
                members.append(row)
                break
    current_start, current_end, current, current_rows = windows[-1]
    prior = windows[-2][2] if len(windows) > 1 else _empty_bucket()
    baseline_buckets = [item[2] for item in windows[:-1]]
    baseline_total = sum(item["total"] for item in baseline_buckets) / len(baseline_buckets)
    baseline_sig = sum(_significant(item) for item in baseline_buckets) / len(baseline_buckets)
    total_percent = percent_delta(current["total"], baseline_total)
    sig_percent = percent_delta(_significant(current), baseline_sig)
    current_risk = risk_index(current["severity"], length)
    prior_risk = risk_index(prior["severity"], length)
    axis = "subregion" if kind == "country" else "country"
    names: set[str] = set()
    grouped_now: dict[str, dict] = {}
    grouped_prior: dict[str, dict] = {}
    for row in current_rows:
        name = _bucket_name(row, axis)
        names.add(name)
        grouped_now.setdefault(name, _empty_bucket())
        _add_row(grouped_now[name], row)
    for row in (windows[-2][3] if len(windows) > 1 else []):
        name = _bucket_name(row, axis)
        names.add(name)
        grouped_prior.setdefault(name, _empty_bucket())
        _add_row(grouped_prior[name], row)
    breakdown = []
    for name in names:
        bucket = grouped_now.get(name, _empty_bucket())
        previous = grouped_prior.get(name, _empty_bucket())
        delta = percent_delta(bucket["total"], previous["total"])
        index = risk_index(bucket["severity"], length)
        breakdown.append({
            "name": name,
            **bucket["categories"],
            "total": bucket["total"],
            "prior_total": previous["total"],
            "percent_delta": delta,
            "arrow": trend_arrow(bucket["total"], previous["total"], delta),
            "risk_index": index,
            "risk_level": risk_level(index),
            "severity": bucket["severity"],
        })
    breakdown.sort(key=lambda item: (-item["total"], item["name"]))
    series = []
    for start, finish, bucket, _members in windows:
        series.append({
            "start": format_ts(start),
            "end": format_ts(finish),
            "label": start.strftime("%d %b").lstrip("0"),
            "count": bucket["total"],
            "significant": _significant(bucket),
        })
    points = []
    for row in current_rows:
        if row["lat"] is None or row["lng"] is None:
            continue
        points.append({
            "lat": row["lat"],
            "lng": row["lng"],
            "severity": row["severity"],
            "category": row["category"],
            "title": row["title"][:180],
            "subregion": row["subregion"] or row["city"] or row["country"],
        })
        if len(points) >= 400:
            break
    covered = [item for item in breakdown if item["total"] > 0]
    return {
        "ok": True,
        "scope": label,
        "scope_type": kind,
        "period": period_name,
        "days": length,
        "baseline_slices": slices,
        "baseline_label": baseline_label,
        "start": format_ts(current_start),
        "end": format_ts(current_end),
        "category": code,
        "events": current["total"],
        "incidents": current["total"],
        "totals": {
            "current": current["total"],
            "baseline": round(baseline_total, 2),
            "baseline_label": baseline_label,
            "percent_delta": total_percent,
            "arrow": trend_arrow(current["total"], baseline_total, total_percent),
            "prior_period": prior["total"],
        },
        "severity": current["severity"],
        "significant": {
            "current": _significant(current),
            "baseline": round(baseline_sig, 2),
            "percent_delta": sig_percent,
            "arrow": trend_arrow(_significant(current), baseline_sig, sig_percent),
        },
        "risk": {
            "index": current_risk,
            "level": risk_level(current_risk),
            "prior_index": prior_risk,
            "arrow": risk_arrow(current_risk, prior_risk),
            "direction": {"↑": "up", "↓": "down", "↔": "flat"}[risk_arrow(current_risk, prior_risk)],
        },
        "casualties": {
            "fatalities": current["fatalities"],
            "injuries": current["injuries"],
            "disclaimer": CASUALTY_DISCLAIMER,
        },
        "breakdown_axis": axis,
        "coverage_count": len(covered),
        "coverage_label": "sub-regions / cities" if axis == "subregion" else "countries",
        "breakdown": breakdown,
        "series": series,
        "points": points,
    }


def scopes_payload(store: Store | None = None) -> dict:
    db = store or get_store()
    with db._lock:
        extra = [
            str(row["country"])
            for row in db._conn.execute(
                "SELECT DISTINCT country FROM incidents WHERE country != '' ORDER BY country"
            )
        ]
    countries = sorted(set(COUNTRY_REGION) | set(extra))
    return {
        "ok": True,
        "regions": list(REGIONS),
        "countries": countries,
        "categories": [{"code": code, "name": CATEGORY_NAMES[code]} for code in CATEGORIES],
        "periods": ["daily", "weekly", "monthly"],
        "days": [7, 30, 90, 365],
    }


def sync_sqlite(path: Path | str, store: Store | None = None) -> int:
    """Copy live feed rows into the permanent archive. No network and no model calls."""
    if ingest_uses_model():
        raise RuntimeError("historical ingest must not call a model")
    source = Path(path)
    if not source.exists():
        return 0
    try:
        conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return 0
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "items" not in tables:
            return 0
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        wanted = [
            "title", "summary", "original_text", "country", "region", "governorate",
            "sub_region", "location_name", "lat", "lng", "severity", "category",
            "published_at", "ingested_at", "source_name", "source_url",
        ]
        fields = [name for name in wanted if name in columns]
        if "title" not in fields:
            return 0
        rows = conn.execute(f"SELECT {', '.join(fields)} FROM items").fetchall()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    added = 0
    target = store or get_store()
    before = target.incident_count()
    for row in rows:
        item = dict(row)
        item["timestamp"] = item.get("published_at") or item.get("ingested_at")
        item["subregion"] = item.get("governorate") or item.get("sub_region") or ""
        item["city"] = item.get("location_name") or ""
        target.upsert_incident(item)
    after = target.incident_count()
    added = max(0, after - before)
    return added


def backfill_geojson_once(store: Store | None = None) -> int:
    target = store or get_store()
    if target.meta_get("geojson_backfill") == "done":
        return 0
    added_before = target.incident_count()
    for name in ("hotspots.json", "archive.json"):
        path = ROOT / name
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            continue
        for feature in features:
            if isinstance(feature, dict):
                target.upsert_incident(feature)
    target.meta_set("geojson_backfill", "done")
    return max(0, target.incident_count() - added_before)


def sync_live(store: Store | None = None) -> int:
    if ingest_uses_model():
        raise RuntimeError("historical ingest must not call a model")
    target = store or get_store()
    added = 0
    try:
        import serve_map

        added += sync_sqlite(serve_map.FEEDS_DB, target)
    except Exception as exc:
        print(f"Live history sync skipped: {exc.__class__.__name__}.")
    try:
        added += backfill_geojson_once(target)
    except Exception as exc:
        print(f"Archive backfill skipped: {exc.__class__.__name__}.")
    return added
