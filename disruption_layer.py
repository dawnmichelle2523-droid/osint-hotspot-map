"""Airspace, GNSS, and maritime disruption overlay.

Public FAA, EASA, MARAD, UKMTO, and ReliefWeb pages are fetched with short
timeouts. Any failure leaves the structured baseline in place. Results are
cached in memory so the map page never waits on these sources. No model API
is called.
"""

from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Blueprint, jsonify

ROOT = Path(__file__).resolve().parent
CACHE_TTL_SECONDS = 15 * 60
FETCH_TIMEOUT = (3, 6)
REVIEWED = "2026-11-27T00:00:00Z"
HEADERS = {
    "User-Agent": "GeoRiskIntel/1.0 (public disruption layer)",
    "Accept": "application/rss+xml, application/xml, application/json, text/html;q=0.8, */*;q=0.5",
}
DISCLAIMER = (
    "Public-source situational awareness for travel, insurance, and maritime desks. "
    "Not a clearance, NOTAM, or war-risk determination. Verify the cited authority before flight or transit."
)
GEOMETRY_NOTE = (
    "Illustrative corridor for situational awareness. Not an official FIR boundary or navigational warning geometry."
)
AIR_GUIDANCE = (
    "Check the current NOTAM and the operator conflict-zone procedure before dispatch. "
    "This overlay is not a clearance to enter the FIR."
)
SEA_GUIDANCE = (
    "Follow the cited UKMTO, MARAD, or ReCAAP instructions, file the required reports, "
    "and confirm cover with the insurer. This marker is not a routing order."
)
GPS_IMPACT = (
    "GNSS position, ADS-B, and RNP approaches may be unreliable or spoofed inside this ring. "
    "Cross-check IRS and conventional navaids, tell ATC, and do not treat a stable GPS track as proof of position."
)

AVIATION_RE = re.compile(
    r"\b(airspace|notams?|no-fly|nofly|overflights?|kicz|czib|firs?|flight information region|"
    r"airports?|aviation|runways?|civil aviation)\b",
    re.I,
)
GPS_RE = re.compile(r"\b(gps|gnss|jamming|jammed|spoof(?:ing|ed)?|navigation interference)\b", re.I)
MARITIME_RE = re.compile(
    r"\b(maritime|vessels?|tankers?|piracy|hijack(?:ed|ing)?|ukmto|marad|shipping|chokepoints?|"
    r"bab el-mandeb|bab al-mandab|hormuz|gulf of guinea|malacca|black sea|red sea|gulf of aden|"
    r"boarding|armed robbery|sea of azov|ports?|harbou?rs?|straits?)\b",
    re.I,
)

EASA_URL = "https://www.easa.europa.eu/en/domains/air-operations/czibs"
EASA_FEED = "https://www.easa.europa.eu/en/domains/air-operations/czibs/feed.xml"
FAA_URL = "https://www.faa.gov/air_traffic/publications/us_restrictions"
MARAD_URL = "https://www.maritime.dot.gov/msci-advisories"
UKMTO_URL = "https://www.ukmto.org/"
RELIEF_URL = (
    "https://api.reliefweb.int/v1/reports?appname=georiskintel&preset=latest&limit=12"
    "&query[value]=airspace%20OR%20%22conflict%20zone%22%20OR%20piracy%20OR%20%22maritime%20security%22"
    "&fields[include][]=title&fields[include][]=url&fields[include][]=date.created"
)
GPS_SOURCE = "Structured baseline of publicly reported GNSS interference corridors (EASA SIB 2022-02; monitoring context such as GPSJam). Not a live measurement."
GPS_URL = "https://gpsjam.org/"

disruption_bp = Blueprint("disruption", __name__)
_lock_state = {"at": 0.0, "payload": None}
_guard = threading.Lock()


def _zone(**kwargs) -> dict:
    kwargs.setdefault("origin", "baseline")
    kwargs.setdefault("geometry_note", GEOMETRY_NOTE)
    return kwargs


AIRSPACE_BASELINE = [
    _zone(
        id="ukraine",
        name="Ukraine FIRs",
        fir="UKBV / UKDV / UKFV / UKOV / UKLV",
        region="Russia & Ukraine",
        severity="high",
        corridor="Ukraine FIRs",
        match=["ukraine", "ukbv", "ukdv", "kyiv", "kiev"],
        center=[49.0, 31.4],
        polygon=[[52.3, 24.1], [52.2, 33.2], [49.2, 40.1], [47.0, 39.5], [45.2, 36.6], [44.4, 34.1], [45.3, 32.5], [46.1, 30.2], [48.4, 22.2], [51.5, 23.6]],
        updated_at="2026-07-24T00:00:00Z",
        source="EASA CZIB-2022-01; FAA conflict-zone restrictions",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2022-01 remains active for the airspace of Ukraine, and FAA conflict-zone restrictions apply to US civil aviation. Treat these FIRs as a no-overflight advisory until the current NOTAM says otherwise.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="russia-west",
        name="Russian Federation airspace (western FIRs)",
        fir="UUWV / URRV / USTV and the wider Russian Federation CZIB",
        region="Russia & Ukraine",
        severity="high",
        corridor="Western Russian FIRs",
        match=["russia", "russian", "moscow fir", "rostov"],
        center=[54.0, 38.0],
        polygon=[[60.2, 28.0], [60.0, 50.0], [52.0, 48.5], [45.5, 40.5], [43.6, 39.5], [44.2, 36.5], [47.2, 38.2], [51.5, 30.0], [56.0, 27.8]],
        updated_at="2026-07-24T00:00:00Z",
        source="EASA CZIB-2025-01 (airspace of the Russian Federation)",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2025-01 is active for the airspace of the Russian Federation. The polygon highlights the western FIRs used by typical Europe–Asia routings. The bulletin is broader than the drawn area.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="belarus",
        name="Minsk FIR",
        fir="UMMV",
        region="Russia & Ukraine",
        severity="high",
        corridor="Minsk FIR",
        match=["belarus", "minsk", "ummv"],
        center=[53.7, 27.9],
        polygon=[[56.15, 23.6], [56.1, 28.2], [55.6, 30.8], [53.0, 32.6], [51.6, 30.3], [51.5, 23.7]],
        updated_at=REVIEWED,
        source="FAA KICZ NOTAM A0003/22 (Minsk FIR prohibition for certain US civil flights)",
        source_url=FAA_URL,
        advisory_summary="The FAA public restrictions list still carries KICZ NOTAM A0003/22, a prohibition on certain US civil flights in the Minsk FIR (UMMV). Confirm the NOTAM before any entry.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="syria",
        name="Damascus FIR",
        fir="OSTT",
        region="Middle East",
        severity="high",
        corridor="Damascus FIR",
        match=["syria", "damascus", "ostt"],
        center=[35.0, 38.4],
        polygon=[[37.3, 35.9], [37.2, 42.3], [34.5, 41.2], [32.4, 38.5], [32.6, 35.9], [34.6, 35.7]],
        updated_at="2026-11-16T00:00:00Z",
        source="EASA CZIB-2017-03; FAA conflict-zone restrictions",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2017-03 remains active for the airspace of Syria (revised 16 Nov 2026). FAA conflict-zone restrictions also apply to US civil aviation.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="iraq",
        name="Baghdad FIR",
        fir="ORBB",
        region="Middle East",
        severity="high",
        corridor="Baghdad FIR",
        match=["iraq", "baghdad", "orbb"],
        center=[33.0, 43.5],
        polygon=[[37.3, 39.0], [37.4, 46.0], [35.0, 46.1], [31.2, 47.8], [29.1, 48.5], [29.1, 46.5], [30.1, 40.6], [33.3, 38.8]],
        updated_at="2026-11-16T00:00:00Z",
        source="EASA CZIB-2026-05; FAA SFAR 77",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2026-05 is active for the airspace of Iraq. FAA SFAR 77 prohibits certain flights in the Baghdad FIR (ORBB).",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="iran",
        name="Tehran FIR",
        fir="OIIX",
        region="Middle East",
        severity="high",
        corridor="Tehran FIR",
        match=["iran", "tehran", "oiix"],
        center=[32.5, 53.5],
        polygon=[[39.7, 44.4], [39.3, 48.0], [37.5, 54.8], [37.2, 59.4], [31.0, 61.2], [25.4, 61.2], [25.6, 57.8], [26.8, 54.0], [27.8, 51.6], [30.0, 48.0], [33.5, 46.0], [36.5, 45.0], [39.2, 44.6]],
        updated_at="2026-11-16T00:00:00Z",
        source="EASA CZIB-2026-04; FAA SFAR 117",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2026-04 is active for the airspace of Iran. FAA SFAR 117 prohibits certain flights in the Tehran FIR (OIIX) for US civil aviation.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="persian-gulf-air",
        name="Persian Gulf and Gulf of Oman airspace",
        fir="Overwater corridors adjacent to OIIX / OMAE / OPKR",
        region="Middle East",
        severity="high",
        corridor="Persian Gulf and Gulf of Oman",
        match=["persian gulf", "gulf of oman"],
        center=[26.6, 54.2],
        polygon=[[29.8, 48.2], [29.2, 56.4], [26.6, 57.6], [24.2, 59.2], [23.2, 58.6], [24.5, 52.5], [26.6, 50.2], [27.6, 48.6]],
        updated_at="2026-11-16T00:00:00Z",
        source="EASA CZIB-2026-07",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2026-07 is active for the airspace of the Persian Gulf and the Gulf of Oman (revised 16 Nov 2026). Read it with the Iran, Iraq, and Hormuz maritime advisories.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="saudi-arabia",
        name="Airspace of Saudi Arabia",
        fir="OEJD / OERK",
        region="Middle East",
        severity="high",
        corridor="Saudi Arabia FIRs",
        match=["saudi"],
        center=[24.2, 44.5],
        polygon=[[32.1, 37.2], [31.0, 42.0], [28.5, 47.8], [27.0, 50.0], [24.2, 51.3], [22.0, 55.6], [16.6, 47.2], [16.5, 42.8], [21.0, 39.2], [27.8, 35.0], [29.4, 34.9], [31.3, 37.0]],
        updated_at="2026-11-16T00:00:00Z",
        source="EASA CZIB-2026-09",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2026-09 is active for the airspace of Saudi Arabia (revised 16 Nov 2026, valid through 31 Jan 2027 on the public list). Read the bulletin before overflights or tech stops.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="jordan",
        name="Airspace of Jordan",
        fir="OJAC",
        region="Middle East",
        severity="high",
        corridor="Amman FIR",
        match=["jordan", "amman", "ojac"],
        center=[31.2, 36.8],
        polygon=[[33.3, 35.0], [33.4, 39.3], [29.2, 39.3], [29.2, 34.9]],
        updated_at="2026-11-16T00:00:00Z",
        source="EASA CZIB-2026-08",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2026-08 is active for the airspace of Jordan (revised 16 Nov 2026). Check the bulletin and airport NOTAMs before scheduling.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="lebanon",
        name="Airspace of Lebanon",
        fir="OLBA",
        region="Middle East",
        severity="high",
        corridor="Beirut FIR",
        match=["lebanon", "beirut", "olba"],
        center=[33.9, 35.8],
        polygon=[[34.7, 35.1], [34.7, 36.6], [33.05, 36.6], [33.05, 35.1]],
        updated_at="2026-11-16T00:00:00Z",
        source="EASA CZIB-2026-06",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2026-06 is active for the airspace of Lebanon (revised 16 Nov 2026). Beirut operations have been interrupted during this conflict cycle. Use the current NOTAM.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="yemen",
        name="Sana'a FIR",
        fir="OYSC",
        region="Middle East",
        severity="high",
        corridor="Sana'a FIR",
        match=["yemen", "sana", "oysc"],
        center=[15.5, 47.6],
        polygon=[[17.5, 42.4], [17.4, 52.6], [15.6, 53.1], [12.6, 52.2], [12.6, 43.4], [14.6, 42.5]],
        updated_at="2026-09-30T00:00:00Z",
        source="EASA CZIB-2017-07; FAA conflict-zone restrictions",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2017-07 remains active for the Sana'a FIR. FAA conflict-zone restrictions apply to US civil aviation over Yemen.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="libya",
        name="Tripoli FIR",
        fir="HLLL",
        region="Africa",
        severity="high",
        corridor="Tripoli FIR",
        match=["libya", "tripoli", "hlll"],
        center=[27.0, 17.2],
        polygon=[[33.1, 9.4], [33.0, 24.9], [30.2, 25.1], [22.0, 25.0], [19.5, 24.0], [20.2, 10.0], [23.5, 9.5]],
        updated_at="2026-07-24T00:00:00Z",
        source="EASA CZIB-2017-02; FAA SFAR 112",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2017-02 remains active for the airspace of Libya. FAA SFAR 112 prohibits certain flights in Libyan territory and airspace.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="sudan",
        name="Khartoum FIR",
        fir="HSSS",
        region="Africa",
        severity="high",
        corridor="Khartoum FIR",
        match=["sudan", "khartoum", "hsss"],
        center=[15.6, 30.2],
        polygon=[[22.0, 24.0], [22.0, 38.5], [18.0, 38.6], [14.0, 36.5], [10.0, 34.0], [9.5, 24.0]],
        updated_at="2026-07-24T00:00:00Z",
        source="EASA CZIB-2023-01",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2023-01 remains active for the airspace of Sudan. Commercial access to Khartoum is not a normal planning assumption.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="somalia",
        name="Mogadishu FIR",
        fir="HCSM",
        region="Africa",
        severity="high",
        corridor="Mogadishu FIR",
        match=["somalia", "mogadishu", "hcsm"],
        center=[6.2, 46.2],
        polygon=[[11.8, 43.0], [11.5, 51.3], [8.0, 50.8], [1.6, 45.2], [-1.6, 41.6], [2.0, 41.0], [10.6, 42.6]],
        updated_at="2026-09-30T00:00:00Z",
        source="EASA CZIB-2017-05",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2017-05 remains active for the airspace of Somalia. Overflight and airport acceptance both need a current risk acceptance.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="mali",
        name="Airspace of Mali",
        fir="GABS / DRRR portions covering Mali",
        region="Africa",
        severity="high",
        corridor="Mali",
        match=["mali", "bamako"],
        center=[17.6, -2.0],
        polygon=[[25.0, -12.2], [25.0, 4.2], [15.0, 4.0], [10.2, 1.0], [10.4, -12.2]],
        updated_at="2026-09-30T00:00:00Z",
        source="EASA CZIB-2017-01; FAA Mali advisory",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2017-01 remains active for the airspace of Mali. The FAA also publishes a security advisory for US civil aviation in Mali.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="afghanistan",
        name="Kabul FIR",
        fir="OAKX",
        region="Afghanistan",
        severity="high",
        corridor="Kabul FIR",
        match=["afghanistan", "kabul", "oakx"],
        center=[33.8, 66.2],
        polygon=[[38.5, 60.5], [38.4, 74.8], [29.4, 71.2], [29.5, 60.8]],
        updated_at="2026-07-24T00:00:00Z",
        source="EASA CZIB-2017-08; FAA SFAR 119",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2017-08 remains active for Afghanistan. FAA SFAR 119 prohibits certain flights in the Kabul FIR (OAKX).",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="pakistan-west",
        name="Pakistan — Balochistan and Khyber Pakhtunkhwa",
        fir="OPLR / OPKR portions",
        region="South Asia",
        severity="high",
        corridor="Western and northern Pakistan",
        match=["balochistan", "baluchistan", "khyber", "pakhtunkhwa"],
        center=[31.2, 67.5],
        polygon=[[36.8, 69.2], [36.4, 74.2], [33.6, 73.8], [31.2, 70.2], [28.4, 66.2], [25.4, 62.2], [28.6, 61.6], [31.4, 66.4], [34.6, 67.6]],
        updated_at="2026-07-24T00:00:00Z",
        source="EASA CZIB-2018-02",
        source_url=EASA_URL,
        advisory_summary="EASA CZIB-2018-02 remains active for Balochistan and Khyber Pakhtunkhwa. It is not a nationwide Pakistan closure. The polygon is an illustration of those provinces.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="north-korea",
        name="Pyongyang FIR",
        fir="ZKKP",
        region="APAC",
        severity="high",
        corridor="Pyongyang FIR",
        match=["north korea", "dprk", "pyongyang", "zkkp"],
        center=[40.2, 127.2],
        polygon=[[43.0, 124.2], [43.0, 130.7], [37.7, 130.4], [37.6, 124.3]],
        updated_at=REVIEWED,
        source="FAA SFAR 79 / KICZ Pyongyang FIR prohibition",
        source_url=FAA_URL,
        advisory_summary="FAA SFAR 79 prohibits certain flights in the Pyongyang FIR (ZKKP). The public FAA restrictions list still carries the KICZ pointer.",
        operational_guidance=AIR_GUIDANCE,
    ),
    _zone(
        id="haiti",
        name="Airspace of Haiti",
        fir="MTEG",
        region="Global",
        severity="high",
        corridor="Haiti",
        match=["haiti"],
        center=[19.0, -72.8],
        polygon=[[20.1, -74.6], [20.1, -71.6], [18.0, -71.6], [18.0, -74.8]],
        updated_at=REVIEWED,
        source="FAA KICZ prohibition on certain flights in Haiti",
        source_url=FAA_URL,
        advisory_summary="The FAA public restrictions list includes a KICZ prohibition on certain flights in the territory and airspace of Haiti. US operators need the current NOTAM.",
        operational_guidance=AIR_GUIDANCE,
    ),
]

GPS_BASELINE = [
    _zone(
        id="east-med-gnss",
        name="Eastern Mediterranean",
        region="Middle East",
        severity="high",
        corridor="Israel, Lebanon, Cyprus approaches",
        match=["eastern mediterranean", "cyprus", "israel", "lebanon"],
        center=[33.6, 34.2],
        radius_km=220,
        updated_at=REVIEWED,
        source=GPS_SOURCE,
        source_url=GPS_URL,
        summary="Repeated public reports of GNSS jamming and spoofing on the eastern Mediterranean approaches, including Cyprus, Israel, and Lebanon corridors.",
        operational_impact=GPS_IMPACT,
        operational_guidance=GPS_IMPACT,
    ),
    _zone(
        id="black-sea-gnss",
        name="Black Sea and Crimea",
        region="Russia & Ukraine",
        severity="high",
        corridor="Black Sea",
        match=["black sea", "crimea", "sevastopol"],
        center=[44.4, 34.2],
        radius_km=240,
        updated_at=REVIEWED,
        source=GPS_SOURCE,
        source_url=GPS_URL,
        summary="The Black Sea and Crimea remain a widely reported GNSS interference area tied to the Ukraine war.",
        operational_impact=GPS_IMPACT,
        operational_guidance=GPS_IMPACT,
    ),
    _zone(
        id="baltic-gnss",
        name="Baltic and Kaliningrad",
        region="UK & Europe",
        severity="high",
        corridor="Southern Baltic",
        match=["baltic", "kaliningrad", "gulf of finland"],
        center=[55.2, 20.2],
        radius_km=180,
        updated_at=REVIEWED,
        source=GPS_SOURCE,
        source_url=GPS_URL,
        summary="Baltic approaches and the Kaliningrad area are a standing GNSS interference hotspot in public monitoring.",
        operational_impact=GPS_IMPACT,
        operational_guidance=GPS_IMPACT,
    ),
    _zone(
        id="hormuz-gnss",
        name="Strait of Hormuz GNSS",
        region="Middle East",
        severity="high",
        corridor="Strait of Hormuz",
        match=["hormuz", "persian gulf"],
        center=[26.6, 56.3],
        radius_km=190,
        updated_at=REVIEWED,
        source=GPS_SOURCE,
        source_url=GPS_URL,
        summary="The Strait of Hormuz and nearby Gulf waters have recurring public reports of GNSS interference affecting ships and aircraft.",
        operational_impact=GPS_IMPACT,
        operational_guidance=GPS_IMPACT,
    ),
    _zone(
        id="iraq-syria-gnss",
        name="Northern Iraq and northeast Syria",
        region="Middle East",
        severity="high",
        corridor="Iraq–Syria border",
        match=["northern iraq", "erbil", "northeast syria"],
        center=[36.3, 41.8],
        radius_km=170,
        updated_at=REVIEWED,
        source=GPS_SOURCE,
        source_url=GPS_URL,
        summary="Northern Iraq and northeast Syria are a known GNSS jamming corridor on public flight-monitoring plots.",
        operational_impact=GPS_IMPACT,
        operational_guidance=GPS_IMPACT,
    ),
    _zone(
        id="red-sea-gnss",
        name="Southern Red Sea GNSS",
        region="Maritime",
        severity="medium",
        corridor="Southern Red Sea",
        match=["red sea", "bab el-mandeb", "bab al-mandab"],
        center=[14.8, 42.6],
        radius_km=160,
        updated_at=REVIEWED,
        source=GPS_SOURCE,
        source_url=GPS_URL,
        summary="Southern Red Sea transits have intermittent public GNSS interference reports. Treat navigation cross-checks as normal procedure, not an exception.",
        operational_impact=GPS_IMPACT,
        operational_guidance=GPS_IMPACT,
    ),
]

MARITIME_BASELINE = [
    _zone(
        id="bab-el-mandeb",
        name="Southern Red Sea and Bab el-Mandeb",
        chokepoint="Bab el-Mandeb",
        region="Maritime",
        threat_level="high",
        severity="high",
        corridor="Southern Red Sea / Bab el-Mandeb",
        match=["bab el-mandeb", "bab al-mandab", "bab el mandeb", "mandeb", "southern red sea", "red sea"],
        center=[14.6, 42.4],
        coords=[14.6, 42.4],
        polygon=[[18.2, 39.4], [16.6, 42.4], [14.2, 43.5], [12.5, 45.6], [11.5, 44.8], [12.4, 42.8], [14.8, 41.2], [17.2, 39.0]],
        updated_at="2026-09-23T00:00:00Z",
        source="US MARAD advisory 2026-013; UKMTO voluntary reporting area",
        source_url=MARAD_URL,
        summary="MARAD advisory 2026-013 (effective 23 Sep 2026) covers Houthi attacks on commercial vessels in the Red Sea, Bab el-Mandeb, Gulf of Aden, Arabian Sea, and Somali Basin. UKMTO reporting remains in force.",
        operational_guidance=SEA_GUIDANCE,
    ),
    _zone(
        id="gulf-of-aden",
        name="Gulf of Aden",
        chokepoint="Gulf of Aden",
        region="Maritime",
        threat_level="high",
        severity="high",
        corridor="Gulf of Aden",
        match=["gulf of aden", "aden", "somali basin"],
        center=[12.4, 48.0],
        coords=[12.4, 48.0],
        polygon=[[13.4, 44.8], [14.4, 51.8], [11.2, 52.2], [10.6, 45.0]],
        updated_at="2026-09-23T00:00:00Z",
        source="US MARAD advisories 2026-013 and 2026-010; UKMTO",
        source_url=MARAD_URL,
        summary="The Gulf of Aden sits inside the same MARAD 2026-013 threat picture as Bab el-Mandeb, and MARAD 2026-010 still flags piracy, armed robbery, and kidnapping risk across the Gulf of Aden, Arabian Sea, and Indian Ocean approaches.",
        operational_guidance=SEA_GUIDANCE,
    ),
    _zone(
        id="hormuz",
        name="Strait of Hormuz and Gulf of Oman",
        chokepoint="Strait of Hormuz",
        region="Maritime",
        threat_level="high",
        severity="high",
        corridor="Strait of Hormuz / Gulf of Oman",
        match=["hormuz", "gulf of oman", "persian gulf"],
        center=[26.5, 56.6],
        coords=[26.5, 56.6],
        polygon=[[27.6, 55.4], [27.8, 57.6], [25.2, 58.2], [24.8, 56.4], [26.0, 55.2]],
        updated_at="2026-09-09T00:00:00Z",
        source="US MARAD advisory 2026-011; UKMTO",
        source_url=MARAD_URL,
        summary="MARAD advisory 2026-011 (effective 9 Sep 2026) covers attacks on commercial vessels in the Persian Gulf, Strait of Hormuz, and Gulf of Oman. UKMTO position reporting applies in the voluntary reporting area.",
        operational_guidance=SEA_GUIDANCE,
    ),
    _zone(
        id="black-sea",
        name="Black Sea and Sea of Azov",
        chokepoint="Black Sea",
        region="Maritime",
        threat_level="high",
        severity="high",
        corridor="Black Sea / Sea of Azov",
        match=["black sea", "sea of azov", "azov"],
        center=[43.6, 34.2],
        coords=[43.6, 34.2],
        polygon=[[46.8, 27.5], [46.6, 38.5], [43.5, 39.8], [41.0, 39.5], [41.0, 28.2], [43.4, 27.6]],
        updated_at="2026-09-23T00:00:00Z",
        source="US MARAD advisory 2026-012",
        source_url=MARAD_URL,
        summary="MARAD advisory 2026-012 (effective 23 Sep 2026) covers military combat operations in the Black Sea and the Sea of Azov. War-risk, coastal-state, and insurer conditions all apply.",
        operational_guidance=SEA_GUIDANCE,
    ),
    _zone(
        id="gulf-of-guinea",
        name="Gulf of Guinea",
        chokepoint="Gulf of Guinea",
        region="Maritime",
        threat_level="medium",
        severity="medium",
        corridor="Gulf of Guinea",
        match=["gulf of guinea"],
        center=[3.4, 2.2],
        coords=[3.4, 2.2],
        polygon=[[6.4, -4.5], [5.8, 8.8], [1.5, 9.2], [-0.5, 7.5], [0.8, -3.5], [4.2, -7.5]],
        updated_at="2026-06-24T00:00:00Z",
        source="US MARAD advisory 2026-009",
        source_url=MARAD_URL,
        summary="MARAD advisory 2026-009 (effective 24 Jun 2026) covers piracy, armed robbery, and kidnapping for ransom in the Gulf of Guinea. The drawn area is the threat basin, not a narrow strait.",
        operational_guidance=SEA_GUIDANCE,
    ),
    _zone(
        id="malacca",
        name="Malacca and Singapore Straits",
        chokepoint="Malacca Strait",
        region="Maritime",
        threat_level="medium",
        severity="medium",
        corridor="Malacca / Singapore Straits",
        match=["malacca", "singapore strait"],
        center=[3.2, 101.2],
        coords=[3.2, 101.2],
        polygon=[[6.2, 97.5], [5.0, 99.0], [3.2, 100.8], [1.4, 103.2], [1.1, 104.6], [1.8, 104.5], [2.6, 103.0], [4.2, 100.4], [6.0, 98.4]],
        updated_at=REVIEWED,
        source="ReCAAP ISC standing strait advisory; structured baseline",
        source_url="https://www.recaap.org/",
        summary="The Malacca and Singapore Straits stay on the desk as a robbery and theft corridor under ReCAAP reporting. They are not on the current MARAD high-threat list used for this baseline.",
        operational_guidance="Keep the usual anti-boarding watch, report to the coastal authority and ReCAAP, and do not treat this medium marker as a war-risk closure. " + SEA_GUIDANCE,
    ),
]


def classify_text(text: str) -> set[str]:
    """Tag aviation, GNSS, and maritime wording. Word boundaries avoid Mali/Somalia collisions."""
    blob = text or ""
    tags = set()
    if AVIATION_RE.search(blob):
        tags.add("aviation")
    if GPS_RE.search(blob):
        tags.add("gps")
    if MARITIME_RE.search(blob):
        tags.add("maritime")
    return tags


def mentions(text: str, needles: list[str]) -> bool:
    blob = (text or "").lower()
    for needle in needles:
        if re.search(r"\b" + re.escape(needle.lower()) + r"\b", blob):
            return True
    return False


def point_in_polygon(lat: float | None, lng: float | None, polygon: list) -> bool:
    if lat is None or lng is None or not polygon or len(polygon) < 3:
        return False
    inside = False
    previous = len(polygon) - 1
    for index, point in enumerate(polygon):
        lat_i, lng_i = point[0], point[1]
        lat_j, lng_j = polygon[previous][0], polygon[previous][1]
        if ((lat_i > lat) != (lat_j > lat)) and (
            lng < (lng_j - lng_i) * (lat - lat_i) / ((lat_j - lat_i) or 1e-12) + lng_i
        ):
            inside = not inside
        previous = index
    return inside


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    chord = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(chord)))


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _coords(feature: dict) -> tuple[float | None, float | None]:
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lng, lat = _num(coords[0]), _num(coords[1])
        if lat is not None and lng is not None and abs(lat) > 90 and abs(lng) <= 90:
            lat, lng = lng, lat
        return lat, lng
    props = feature.get("properties") or {}
    return _num(props.get("lat")), _num(props.get("lng"))


def _plain(content) -> str:
    if isinstance(content, bytes):
        text = content.decode("utf-8", "replace")
    else:
        text = str(content or "")
    try:
        extracted = BeautifulSoup(text, "html.parser").get_text(" ")
    except Exception:
        extracted = re.sub(r"<[^>]+>", " ", text)
    return " ".join(extracted.split())


def _stamp_struct(value) -> str:
    if not value:
        return ""
    try:
        return datetime(*value[:6], tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _stamp_text(raw: str) -> str:
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    try:
        moment = parsedate_to_datetime(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        day = text[:10]
        return day + "T00:00:00Z"
    return ""


def _entry(title: str, summary: str, url: str, updated_at: str, kind: str) -> dict:
    return {
        "title": " ".join(title.split())[:240],
        "summary": " ".join(summary.split())[:400],
        "url": url or "",
        "updated_at": updated_at or "",
        "kind": kind,
    }


def parse_rss(content, kind: str) -> list[dict]:
    parsed = feedparser.parse(content)
    entries = []
    for entry in list(getattr(parsed, "entries", []) or [])[:20]:
        title = str(entry.get("title") or "")
        summary = re.sub(r"<[^>]+>", " ", str(entry.get("summary") or entry.get("description") or ""))
        if not title.strip():
            continue
        stamp = _stamp_struct(entry.get("published_parsed") or entry.get("updated_parsed"))
        if not stamp:
            stamp = _stamp_text(str(entry.get("published") or entry.get("updated") or ""))
        entries.append(_entry(title, summary, str(entry.get("link") or ""), stamp, kind))
    return entries


def parse_easa_html(content, kind: str) -> list[dict]:
    plain = _plain(content)
    found = re.findall(r"Airspace of [A-Za-z][A-Za-z0-9 '’\-]{2,80}", plain)
    entries = []
    seen = set()
    for title in found:
        cleaned = title.strip(" -")
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append(_entry(cleaned, "", EASA_URL, "", kind))
    return entries[:20]


def parse_faa(content, kind: str) -> list[dict]:
    plain = _plain(content)
    lowered = plain.lower()
    if "kicz" not in lowered and "sfar" not in lowered and "prohibition" not in lowered:
        return []
    labels = [
        "Ukraine", "Belarus", "Syria", "Iraq", "Iran", "Yemen", "Libya", "Somalia",
        "Sudan", "Mali", "Afghanistan", "North Korea", "Haiti", "Russia",
        "Saudi Arabia", "Lebanon", "Jordan", "Pakistan",
    ]
    entries = []
    for label in labels:
        if mentions(plain, [label]):
            entries.append(_entry(f"FAA public restrictions page lists {label}", "", FAA_URL, "", kind))
    for match in re.finditer(r"(?:SFAR|KICZ)\s*[A-Z0-9./-]{0,24}[^.]{0,120}", plain, re.I):
        title = " ".join(match.group(0).split())
        if len(title) > 24:
            entries.append(_entry(title, "", FAA_URL, "", kind))
    return entries[:24]


def parse_marad(content, kind: str) -> list[dict]:
    plain = _plain(content)
    entries = []
    for chunk in re.findall(r"20\d{2}-\d{3}-.{8,180}", plain)[:15]:
        entries.append(_entry(chunk, "", MARAD_URL, "", kind))
    if entries:
        return entries
    labels = [
        ("Red Sea and Bab el-Mandeb", ["bab el-mandeb", "bab el mandeb", "red sea"]),
        ("Strait of Hormuz", ["hormuz"]),
        ("Black Sea", ["black sea"]),
        ("Gulf of Guinea", ["gulf of guinea"]),
        ("Gulf of Aden", ["gulf of aden"]),
    ]
    for label, needles in labels:
        if mentions(plain, needles):
            entries.append(_entry(f"MARAD public advisory list mentions {label}", "", MARAD_URL, "", kind))
    return entries


def parse_ukmto(content, kind: str) -> list[dict]:
    plain = _plain(content)
    labels = [
        ("Bab el-Mandeb", ["bab el-mandeb", "bab el mandeb", "red sea"]),
        ("Strait of Hormuz", ["hormuz"]),
        ("Gulf of Aden", ["gulf of aden"]),
        ("Gulf of Guinea", ["gulf of guinea"]),
        ("Black Sea", ["black sea"]),
    ]
    entries = []
    for label, needles in labels:
        if mentions(plain, needles):
            entries.append(_entry(f"UKMTO public page mentions {label}", "", UKMTO_URL, "", kind))
    return entries


def parse_reliefweb(content, kind: str) -> list[dict]:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return []
    entries = []
    for row in list(payload.get("data") or [])[:12]:
        fields = row.get("fields") or {}
        title = str(fields.get("title") or "")
        if not title.strip():
            continue
        created = fields.get("date") or {}
        stamp = ""
        if isinstance(created, dict):
            stamp = _stamp_text(str(created.get("created") or ""))
        entries.append(_entry(title, "", str(fields.get("url") or ""), stamp, kind))
    return entries


PARSERS = {
    "rss": parse_rss,
    "easa_html": parse_easa_html,
    "faa": parse_faa,
    "marad": parse_marad,
    "ukmto": parse_ukmto,
    "reliefweb": parse_reliefweb,
}

FEEDS = (
    {"id": "easa-czib", "name": "EASA CZIB", "url": EASA_FEED, "kind": "airspace", "parser": "rss"},
    {"id": "easa-czib-page", "name": "EASA CZIB", "url": EASA_URL, "kind": "airspace", "parser": "easa_html"},
    {"id": "faa-kicz", "name": "FAA KICZ / SFAR", "url": FAA_URL, "kind": "airspace", "parser": "faa"},
    {"id": "marad-msci", "name": "US MARAD MSCI", "url": MARAD_URL, "kind": "maritime", "parser": "marad"},
    {"id": "ukmto", "name": "UKMTO", "url": UKMTO_URL, "kind": "maritime", "parser": "ukmto"},
    {"id": "reliefweb", "name": "ReliefWeb", "url": RELIEF_URL, "kind": "mixed", "parser": "reliefweb"},
)


def fetch_one(spec: dict) -> dict:
    base = {"id": spec["id"], "name": spec["name"], "url": spec["url"], "ok": False, "error": "", "entries": []}
    try:
        response = requests.get(spec["url"], headers=HEADERS, timeout=FETCH_TIMEOUT)
        response.raise_for_status()
        entries = PARSERS[spec["parser"]](response.content[:400_000], spec["kind"])
        if not isinstance(entries, list):
            entries = []
        base["ok"] = True
        base["entries"] = entries[:24]
        return base
    except Exception as exc:
        base["error"] = exc.__class__.__name__
        return base


def fetch_public_feeds() -> list[dict]:
    specs = list(FEEDS)
    results = []
    with ThreadPoolExecutor(max_workers=min(6, len(specs))) as pool:
        futures = [pool.submit(fetch_one, spec) for spec in specs]
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"id": "", "name": "", "url": "", "ok": False, "error": exc.__class__.__name__, "entries": []})
    return results


def load_local_incidents() -> list[dict]:
    found = []
    for name in ("hotspots.json", "archive.json"):
        path = ROOT / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        features = payload.get("features") if isinstance(payload, dict) else payload
        if isinstance(features, list):
            found.extend(features)
    return found


def normalize_incidents(raw) -> list[dict]:
    if isinstance(raw, dict):
        items = raw.get("features") or []
    else:
        items = raw or []
    normalized = []
    for item in list(items)[:400]:
        if not isinstance(item, dict):
            continue
        if "geometry" in item or "properties" in item:
            props = item.get("properties") or {}
            title = str(props.get("title") or "")
            summary = str(props.get("summary") or "")
            lat, lng = _coords(item)
            source = str(props.get("source") or props.get("source_name") or "")
            stamp = str(props.get("timestamp") or "")
        else:
            title = str(item.get("title") or "")
            summary = str(item.get("summary") or "")
            lat, lng = _num(item.get("lat")), _num(item.get("lng"))
            source = str(item.get("source") or "")
            stamp = str(item.get("timestamp") or item.get("updated_at") or "")
        text = f"{title} {summary}"
        normalized.append({
            "title": " ".join(title.split())[:240],
            "summary": " ".join(summary.split())[:400],
            "text": text,
            "lat": lat,
            "lng": lng,
            "source": source,
            "timestamp": stamp,
            "tags": classify_text(text),
        })
    return normalized


def _entry_kinds(entry: dict) -> set[str]:
    text = f"{entry.get('title') or ''} {entry.get('summary') or ''}"
    kinds = set()
    declared = entry.get("kind") or ""
    if declared in {"airspace", "maritime", "gps"}:
        kinds.add(declared)
    tags = classify_text(text)
    if "aviation" in tags:
        kinds.add("airspace")
    if "maritime" in tags:
        kinds.add("maritime")
    if "gps" in tags:
        kinds.add("gps")
    return kinds


def _apply_feeds(grouped: dict[str, list], feeds: list[dict]) -> None:
    seen = set()
    for feed in feeds:
        if not feed.get("ok"):
            continue
        for entry in feed.get("entries") or []:
            title = entry.get("title") or ""
            key = title.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            text = f"{title} {entry.get('summary') or ''}"
            hit = {
                "title": title[:220],
                "feed": feed.get("name") or feed.get("id") or "Public feed",
                "updated_at": entry.get("updated_at") or "",
                "url": entry.get("url") or feed.get("url") or "",
            }
            for kind in _entry_kinds(entry):
                for zone in grouped.get(kind) or []:
                    if mentions(text, zone.get("match") or []):
                        zone.setdefault("_hits", []).append(hit)


def _fold_hits(zone: dict) -> None:
    hits = zone.pop("_hits", [])
    unique = []
    seen = set()
    for hit in hits:
        if hit["title"] in seen:
            continue
        seen.add(hit["title"])
        unique.append(hit)
    if not unique:
        return
    zone["origin"] = "live"
    extra = " ".join(f"Live {hit['feed']}: {hit['title']}." for hit in unique[:2])
    summary = f"{zone.get('summary') or zone.get('advisory_summary') or ''} {extra}".strip()
    zone["summary"] = summary[:900]
    if "advisory_summary" in zone:
        zone["advisory_summary"] = zone["summary"]
    names = []
    for hit in unique:
        if hit["feed"] not in names:
            names.append(hit["feed"])
    zone["source"] = (zone.get("source") or "") + " | Live: " + ", ".join(names)
    zone["source"] = zone["source"][:420]
    for hit in unique:
        if hit["updated_at"] and hit["updated_at"] > (zone.get("updated_at") or ""):
            zone["updated_at"] = hit["updated_at"]
        if hit.get("url"):
            zone["source_url"] = hit["url"]


def _apply_incidents(airspace: list, gps: list, maritime: list, incidents: list[dict]) -> list[dict]:
    for zone in maritime:
        matched = []
        for incident in incidents:
            if "maritime" not in incident["tags"]:
                continue
            named = mentions(incident["text"], zone.get("match") or [])
            inside = point_in_polygon(incident["lat"], incident["lng"], zone.get("polygon") or [])
            if named or inside:
                matched.append(incident)
        zone["live_incident_count"] = len(matched)
        if matched:
            sample = "; ".join(item["title"] for item in matched[:2] if item["title"])
            note = f" Recent maritime OSINT matched to this corridor: {len(matched)}."
            if sample:
                note += " Latest: " + sample + "."
            zone["summary"] = (zone.get("summary") or "") + note
            zone["summary"] = zone["summary"][:900]
    for zone in airspace:
        count = 0
        for incident in incidents:
            if "aviation" not in incident["tags"]:
                continue
            if mentions(incident["text"], zone.get("match") or []) or point_in_polygon(incident["lat"], incident["lng"], zone.get("polygon") or []):
                count += 1
        if count:
            zone["summary"] = ((zone.get("summary") or zone.get("advisory_summary") or "") + f" Related aviation OSINT incidents: {count}.").strip()[:900]
            zone["advisory_summary"] = zone["summary"]
    extras = []
    for zone in gps:
        count = 0
        for incident in incidents:
            if "gps" not in incident["tags"]:
                continue
            named = mentions(incident["text"], zone.get("match") or [])
            near = False
            if incident["lat"] is not None and incident["lng"] is not None:
                near = haversine_km(incident["lat"], incident["lng"], zone["center"][0], zone["center"][1]) <= float(zone["radius_km"])
            if named or near:
                count += 1
        if count:
            zone["summary"] = (zone.get("summary") or "") + f" Local OSINT reports GNSS interference in this area ({count})."
            zone["summary"] = zone["summary"][:900]
    for incident in incidents:
        if "gps" not in incident["tags"] or incident["lat"] is None or incident["lng"] is None:
            continue
        covered = any(
            haversine_km(incident["lat"], incident["lng"], zone["center"][0], zone["center"][1]) <= float(zone["radius_km"])
            for zone in gps
        )
        if covered:
            continue
        extras.append(_zone(
            id=f"osint-gps-{len(extras) + 1}",
            name=incident["title"][:80] or "GNSS interference report",
            region="Global",
            severity="medium",
            corridor="Reported GNSS interference",
            match=[],
            center=[incident["lat"], incident["lng"]],
            radius_km=50,
            updated_at=incident["timestamp"] or REVIEWED,
            source=incident["source"] or "GeoRiskIntel OSINT classification",
            source_url=incident["source"] if str(incident["source"]).startswith("http") else GPS_URL,
            summary=incident["summary"] or incident["title"] or "Local OSINT incident tagged as GNSS interference.",
            operational_impact=GPS_IMPACT,
            operational_guidance=GPS_IMPACT,
            origin="live",
        ))
    return extras


def _publish_air(zone: dict) -> dict:
    summary = zone.get("advisory_summary") or zone.get("summary") or ""
    return {
        "id": zone["id"],
        "name": zone["name"],
        "fir": zone["fir"],
        "region": zone["region"],
        "severity": zone["severity"],
        "advisory_summary": summary,
        "summary": summary,
        "operational_guidance": zone.get("operational_guidance") or AIR_GUIDANCE,
        "corridor": zone.get("corridor") or zone["fir"],
        "source": zone.get("source") or "",
        "source_url": zone.get("source_url") or "",
        "updated_at": zone.get("updated_at") or REVIEWED,
        "center": list(zone["center"]),
        "polygon": [list(pair) for pair in zone["polygon"]],
        "origin": zone.get("origin") or "baseline",
        "geometry_note": zone.get("geometry_note") or GEOMETRY_NOTE,
    }


def _publish_gps(zone: dict) -> dict:
    impact = zone.get("operational_impact") or GPS_IMPACT
    return {
        "id": zone["id"],
        "name": zone["name"],
        "region": zone.get("region") or "",
        "severity": zone.get("severity") or "medium",
        "center": [zone["center"][0], zone["center"][1]],
        "radius_km": zone["radius_km"],
        "operational_impact": impact,
        "operational_guidance": zone.get("operational_guidance") or impact,
        "summary": zone.get("summary") or impact,
        "corridor": zone.get("corridor") or zone["name"],
        "source": zone.get("source") or GPS_SOURCE,
        "source_url": zone.get("source_url") or GPS_URL,
        "updated_at": zone.get("updated_at") or REVIEWED,
        "origin": zone.get("origin") or "baseline",
        "geometry_note": zone.get("geometry_note") or GEOMETRY_NOTE,
    }


def _publish_sea(zone: dict) -> dict:
    center = list(zone.get("coords") or zone["center"])
    return {
        "id": zone["id"],
        "name": zone["name"],
        "chokepoint": zone["chokepoint"],
        "region": zone.get("region") or "Maritime",
        "coords": center,
        "center": center,
        "polygon": [list(pair) for pair in zone["polygon"]],
        "threat_level": zone["threat_level"],
        "severity": zone.get("severity") or zone["threat_level"],
        "live_incident_count": int(zone.get("live_incident_count") or 0),
        "summary": zone.get("summary") or "",
        "operational_guidance": zone.get("operational_guidance") or SEA_GUIDANCE,
        "corridor": zone.get("corridor") or zone["chokepoint"],
        "source": zone.get("source") or "",
        "source_url": zone.get("source_url") or "",
        "updated_at": zone.get("updated_at") or REVIEWED,
        "origin": zone.get("origin") or "baseline",
        "geometry_note": zone.get("geometry_note") or GEOMETRY_NOTE,
    }


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(feeds=None, incidents=None) -> dict:
    if feeds is None:
        feeds = fetch_public_feeds()
    raw_incidents = load_local_incidents() if incidents is None else incidents
    airspace = copy.deepcopy(AIRSPACE_BASELINE)
    gps = copy.deepcopy(GPS_BASELINE)
    maritime = copy.deepcopy(MARITIME_BASELINE)
    for zone in airspace:
        zone["summary"] = zone.get("advisory_summary") or ""
    grouped = {"airspace": airspace, "gps": gps, "maritime": maritime}
    _apply_feeds(grouped, feeds or [])
    for zone in airspace + gps + maritime:
        _fold_hits(zone)
    extras = _apply_incidents(airspace, gps, maritime, normalize_incidents(raw_incidents))
    gps.extend(extras)
    rank = {"high": 0, "medium": 1, "low": 2}
    airspace.sort(key=lambda item: (rank.get(item.get("severity"), 9), item.get("name") or ""))
    gps.sort(key=lambda item: (rank.get(item.get("severity"), 9), item.get("name") or ""))
    maritime.sort(key=lambda item: (rank.get(item.get("threat_level"), 9), item.get("name") or ""))
    published_feeds = [
        {
            "id": feed.get("id") or "",
            "name": feed.get("name") or "",
            "url": feed.get("url") or "",
            "ok": bool(feed.get("ok")),
            "error": str(feed.get("error") or ""),
            "items": len(feed.get("entries") or []),
        }
        for feed in (feeds or [])
    ]
    return {
        "ok": True,
        "generated_at": _now_stamp(),
        "cached": False,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "disclaimer": DISCLAIMER,
        "feeds": published_feeds,
        "airspace": [_publish_air(zone) for zone in airspace],
        "gps_jamming": [_publish_gps(zone) for zone in gps],
        "maritime": [_publish_sea(zone) for zone in maritime],
    }


def clear_cache() -> None:
    with _guard:
        _lock_state["at"] = 0.0
        _lock_state["payload"] = None


def get_disruptions(now=None) -> dict:
    moment = time.monotonic() if now is None else float(now)
    with _guard:
        cached = _lock_state["payload"]
        if cached is not None and moment - _lock_state["at"] < CACHE_TTL_SECONDS:
            payload = copy.deepcopy(cached)
            payload["cached"] = True
            return payload
    fresh = build_payload()
    fresh["cached"] = False
    with _guard:
        _lock_state["payload"] = copy.deepcopy(fresh)
        _lock_state["at"] = moment
    return fresh


@disruption_bp.get("/api/disruptions")
def api_disruptions():
    try:
        payload = get_disruptions()
    except Exception:
        payload = build_payload(feeds=[], incidents=[])
        payload["cached"] = False
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response
