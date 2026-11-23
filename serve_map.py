#!/usr/bin/env python3
"""Serve the static map, the summary API, and live RSS/Telegram ingestion on port 8000.

POST /api/summary makes one xAI call (grok-3-mini, timeout 25, no live search).
GET /api/data returns live events, six region counts, the cached summary, and last_polled_at.
POST /api/data with {"view":"regional"} returns that same live payload for the regional overview.
POST /api/poll-now starts one ingest batch in the background and returns immediately.
The poll never writes hotspots.json or daily_brief.json.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import random
import re
import smtplib
import sqlite3
from difflib import SequenceMatcher
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

import fetch_x_osint as osint

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "summary_cache.json"
DB_PATH = os.environ.get("DB_PATH", "osint_events.db")
_db_file = Path(DB_PATH)
FEEDS_DB = _db_file if _db_file.is_absolute() else ROOT / _db_file
POLL_SECONDS = 120
BATCH_SIZE = 7
X_BATCH_SIZE = 8
RSS_BATCH_SIZE = 2
MAX_NEW_PER_CYCLE = 6
LIVE_WINDOW = timedelta(hours=72)
SIMILAR_TITLE_LIMIT = 40
SIMILAR_TITLE_RATIO = 0.65
FETCH_TIMEOUT = 6
FIRMS_TIMEOUT = 10
FIRMS_CACHE_SECONDS = 15 * 60
FIRMS_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}"
    "/VIIRS_NOAA21_NRT/25.0,12.0,63.0,38.0/1"
)
FIRMS_PLACEHOLDERS = {
    "your_32_character_nasa_map_key_here",
    "your_nasa_firms_map_key_here",
}
USER_AGENT = "osint_tracker"
_thermal_lock = threading.Lock()
_thermal_cache: dict = {"at": 0.0, "points": None}
# Configurable public feeds. Google News RSS stands in for Reuters and AP.
ALJAZEERA_ALL_FEED = "https://www.aljazeera.com/xml/rss/all.xml"
ALJAZEERA_MIDDLE_EAST_PAGE = "https://www.aljazeera.com/where/middle-east/"
RSS_FEEDS = [
    ("Al Jazeera Middle East", "https://www.aljazeera.com/xml/rss/middleeast.xml"),
    ("Al Jazeera", ALJAZEERA_ALL_FEED),
    ("BBC Middle East", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"),
    ("Reuters Middle East & Syria", "https://news.google.com/rss/search?q=site:reuters.com+(Middle+East+OR+Syria)&hl=en-US&gl=US&ceid=US:en"),
    ("AP Middle East & Syria", "https://news.google.com/rss/search?q=site:apnews.com+(Middle+East+OR+Syria)&hl=en-US&gl=US&ceid=US:en"),
    ("Enab Baladi", "https://english.enabbaladi.net/feed/"),
    ("Syria Direct", "https://syriadirect.org/feed/"),
    ("Shafaq English", "https://shafaq.com/en/rss"),
    ("Iraqi News", "https://www.iraqinews.com/feed/"),
    ("Rudaw English", "https://www.rudaw.net/english/rss"),
    ("Iran International", "https://www.iranintl.com/en/rss"),
    ("Arab News", "https://www.arabnews.com/rss.xml"),
    ("The National", "https://www.thenationalnews.com/arc/outboundfeeds/rss/"),
    ("Jordan Times", "https://www.jordantimes.com/rss.xml"),
    ("Dabanga Sudan", "https://www.dabangasudan.org/en/all-news/feed"),
    ("Sudan Tribune", "https://sudantribune.com/feed/"),
    ("ReliefWeb Sudan", "https://reliefweb.int/updates/rss.xml?primary_country=220"),
    ("Middle East Monitor", "https://www.middleeastmonitor.com/feed/"),
    ("UKMTO JMIC", "https://www.ukmto.org/partner-products/jmic-products"),
    ("MOFA Japan Anzen", "https://www.anzen.mofa.go.jp/"),
    ("Kurdistan24", "https://www.kurdistan24.net/en/rss"),
    ("ISW", "https://www.understandingwar.org/rss.xml"),
]
# These two pages are notices, not always RSS. Never fetch https://mofa.go.jp itself.
HTML_NOTICE_FEEDS = {
    "https://www.ukmto.org/partner-products/jmic-products": "UKMTO JMIC",
    "https://www.anzen.mofa.go.jp/": "MOFA Japan Anzen",
}
# Configurable public OSINT channels, read from the t.me/s preview page.
TELEGRAM_CHANNELS = [
    "conflictarchive",
    "Suriyak_maps",
    "Liveuamap",
    "hawran_osint",
    "npasyria",
    "SANANewsEnglish",
    "televisionsyria",
    "AlekhbariahSY",
    "SyrianArabNewsAgency",
    "sn4hr",
    "D24net",
    "SweidaGov1",
    "syrianinfowar",
    "OsintTv",
    "syriahr",
    "EnabBaladi",
    "OSINTdefender",
    "osint613",
    "OsintUpdates",
    "warmonitors",
    "gulfnewsUAE",
    "AlArabiya_Brk",
    "almasirah",
    "almanarnews",
    "SabereenNews22",
    "IranIntl_En",
    "rudaw_english",
    "shafaqnews",
    "almaalomah",
    "INA_Iraq",
    "kurdistan24",
    "IRNA_en",
    "Mehrnews_en",
    "presstv",
    "alarabiya_eng",
    "skynewsarabia_b",
    "gulfnews",
    "KUNA_EN",
    "BNA_EN",
    "RoyaNews",
    "alghadnews",
    "PetraNewsAgency",
    "AuroraIntel",
    "ClashReport",
    "Middle_East_Spectator",
    "QudsNen",
    "sudanwarmonitor",
    "darfur24",
    "sudannews24",
    "alrakoba_sudan",
    "SudanTribune",
]
# The first seven are the start of every rotation. Later names are added once.
_X_HANDLE_GROUPS = [
    ["moi_bahrain", "BDF_Bahrain", "kuwaitarmyghq", "MOInformation", "MOFAQatar_EN", "UK_MTO", "Alsakaniali"],
    [
        "CJFCSpox", "SaudiDCD", "kbsalsaud", "FaisalbinFarhan", "KKIASA", "ksagaca", "Spa_Eng", "SPAregions",
        "SaudiNews50", "saudiexpat", "Yah_Saree", "MMY1444", "FaresALhemyari", "abda_sharif", "Yemen_PM",
    ],
    [
        "MKhamenei_ir", "drpezeshkian", "mb_ghalibaf", "ejei_org", "araghchi", "IRIMFA_SPOX", "ir_rezaee",
        "mmohammadii61", "hatami_org", "Gharibabadi", "IRAN_in_KUWAIT", "smajid__moosavi", "iribnews_irib",
        "PressTV", "tasnimarabic1", "iranintl_en", "ReutersIran", "s_m_marandi", "IRNA_English", "FarsNews_Agency",
    ],
    ["IraqiPMO", "thenewregion", "RudawEnglish", "Kurdistan24", "shafaqnewsarb"],
    ["_MartinKelly_", "MBA_AlThani_", "majedalansari", "mofauae", "AnwarGargash"],
    ["syrianmofaex", "Levant_24_", "SANAEnOfficial", "Sana__gov", "AlekhbariahSY", "AsharqNewsSYR"],
    [
        "IDF", "idfonline", "IDFSpokesperson", "AvichayAdraee", "netanyahu", "IsraelPresident", "IsraelMFA",
        "israel_katz", "gadi_eisenkot", "gantzbe", "yairlapid", "NaftaliBennett", "AvigdorLiberman",
        "yairgolan10", "ILRedAlert", "manniefabian", "BarakRavid", "kaisos1987", "ME_Observer_",
        "LBpresidency", "NNALeb", "LebarmyOfficial", "KingAbdullahII", "usembassybeirut", "usembassyjordan",
        "LOrientLeJour",
    ],
    [
        "RTErdogan", "usambturkiye", "CENTCOM", "CENTCOMFarsi", "StateDeptSpox", "TravelGov", "RapidResponse47",
        "JDVance", "SecScottBessent", "SecRubio", "RepJoeWilson", "UN", "antonioguterres", "USUN", "USAinKSA",
        "USAbilAraby", "USAbehFarsi", "GermanyDiplo", "PorteParoleMEAE", "TheStudyofWar", "FCDOtravelGovUK",
        "TravelGoC", "SafeTravelNZ", "Smartraveller", "sentdefender", "InsiderGeo", "MATA_osint",
        "faytuksnetwork", "MenchOsint", "DefenceGeek", "WSJ", "uniindianews", "AlarabyTV", "AlHadath",
        "Reuters", "araReuters", "AFPar", "BBCArabic", "AP", "AFP", "anadoluagency", "BBCWorld", "CNN",
        "SkyNews", "AJEnglish", "aawsat_News", "timesofisrael", "Jerusalem_Post", "ynetnews",
    ],
    [
        "syr_television", "SyriacOSINT", "QalaatAlMudiq", "Charles_Lister", "GregoryPWaters", "OSINTWarfare",
        "Osinttechnical", "AfriMEOSINT", "AuroraIntel", "OSINTdefender", "IntelCrab", "clashreport",
        "IraqiOsint", "K24English", "ShafaqNews", "Tammuz_Intel", "IraqSecurity", "SudanWarMonitor",
        "DabangaSudan", "SudanTribune_EN", "PatrickHeinisc1", "vista_sudan",
    ],
]
EXCLUDED_SOURCE_HANDLES = {"sepah_news1", "yahya_saree", "tasnimnews_en", "irantimes72"}
X_HANDLES = []
_seen_x_handles = set()
for _group in _X_HANDLE_GROUPS:
    for _handle in _group:
        _key = _handle.lower()
        if _key in EXCLUDED_SOURCE_HANDLES or _key in _seen_x_handles:
            continue
        _seen_x_handles.add(_key)
        X_HANDLES.append(_handle)
del _group, _handle, _key, _seen_x_handles
BROAD_X_HANDLES = {handle.lower() for group in _X_HANDLE_GROUPS[:-1] for handle in group}
SUDAN_X_HANDLES = {
    "sudanwarmonitor",
    "dabangasudan",
    "sudantribune_en",
    "patrickheinisc1",
    "vista_sudan",
}
IRAQ_X_HANDLES = {
    "iraqiosint",
    "rudawenglish",
    "k24english",
    "shafaqnews",
    "tammuz_intel",
    "iraqsecurity",
    "iraqipmo",
    "thenewregion",
    "kurdistan24",
    "shafaqnewsarb",
}
# First HTTP 200 with valid RSS wins. A dead or rate-limited mirror tries the next one.
X_FEED_URLS = [
    "https://xcancel.com/{handle}/rss",
    "https://nitter.poast.org/{handle}/rss",
    "https://nitter.privacydev.net/{handle}/rss",
]
X_MIRROR_TIMEOUT = 5
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
X_RATE_LIMITS = {401, 403, 429, 503}
_start_lock = threading.Lock()
_poll_lock = threading.Lock()
_db_lock = threading.Lock()
_pace_lock = threading.Lock()
_poll_started = False
_pacing = False
_pace_pending = False
MODEL = "grok-3-mini"
TIMEOUT = 25
MAX_OUTPUT_TOKENS = 800
HOST = "0.0.0.0"
PORT = 8000
REGIONS = (
    "Gulf",
    "Saudi & Yemen",
    "Levant",
    "Iran & Iraq",
    "Maritime",
    "Sudan & Horn",
    "Other / International",
)
# Published geographic centers, (latitude, longitude). Longer names are matched first.
GEO_FALLBACKS = {
    "strait of hormuz": (26.5667, 56.2667),
    "bab el mandeb": (12.5833, 43.3333),
    "bab al mandab": (12.5833, 43.3333),
    "gulf of oman": (25.0, 58.0),
    "persian gulf": (26.5, 51.5),
    "arabian gulf": (26.5, 51.5),
    "red sea state": (19.6158, 37.2164),
    "red sea": (20.0, 38.5),
    "united arab emirates": (24.0, 54.0),
    "u a e": (24.0, 54.0),
    "saudi arabia": (25.0, 45.0),
    "port sudan": (19.6158, 37.2164),
    "el fasher": (13.6281, 25.3494),
    "el fashir": (13.6281, 25.3494),
    "al fashir": (13.6281, 25.3494),
    "el geneina": (13.45, 22.45),
    "el obeid": (13.1833, 30.2167),
    "wad madani": (14.4, 33.52),
    "wad medani": (14.4, 33.52),
    "khartoum": (15.5518, 32.5324),
    "omdurman": (15.6445, 32.4773),
    "nyala": (12.05, 24.8833),
    "kadugli": (11.0167, 29.7167),
    "kassala": (15.45, 36.4),
    "gedaref": (14.0333, 35.3833),
    "al qadarif": (14.0333, 35.3833),
    "atbara": (17.7, 33.9833),
    "dongola": (19.1761, 30.4839),
    "north darfur": (13.6281, 25.3494),
    "south darfur": (12.05, 24.8833),
    "west darfur": (13.45, 22.45),
    "central darfur": (12.9092, 23.4706),
    "east darfur": (11.4611, 26.1258),
    "north kordofan": (13.1833, 30.2167),
    "south kordofan": (11.0167, 29.7167),
    "west kordofan": (11.7128, 28.3464),
    "al jazirah": (14.4, 33.52),
    "blue nile": (11.7891, 34.3592),
    "white nile": (13.18, 32.74),
    "river nile": (17.5928, 33.9597),
    "rif dimashq": (33.5138, 36.3),
    "damascus": (33.5138, 36.2765),
    "aleppo": (36.2021, 37.1612),
    "idlib": (35.9306, 36.6317),
    "idleb": (35.9306, 36.6317),
    "latakia": (35.5317, 35.7953),
    "tartus": (34.889, 35.8866),
    "hama": (35.1318, 36.7552),
    "homs": (34.7324, 36.7137),
    "raqqa": (35.9594, 39.0094),
    "deir ez zor": (35.3359, 40.1408),
    "deir ezzor": (35.3359, 40.1408),
    "al hasakah": (36.5024, 40.7477),
    "hasakah": (36.5024, 40.7477),
    "daraa": (32.6189, 36.1021),
    "quneitra": (33.1259, 35.8244),
    "as suwayda": (32.7094, 36.5667),
    "suwayda": (32.7094, 36.5667),
    "sweida": (32.7094, 36.5667),
    "hormuz": (26.5667, 56.2667),
    "iran": (32.0, 53.0),
    "iraq": (33.0, 44.0),
    "jordan": (31.0, 36.0),
    "kuwait": (29.5, 47.75),
    "bahrain": (26.0, 50.55),
    "saudi": (25.0, 45.0),
    "uae": (24.0, 54.0),
    "qatar": (25.5, 51.25),
    "oman": (21.0, 57.0),
    "yemen": (15.0, 48.0),
    "syria": (35.0, 38.0),
    "lebanon": (33.8333, 35.8333),
    "israel": (31.5, 34.75),
    "gaza": (31.5017, 34.4668),
    "sudan": (15.0, 30.0),
    "darfur": (13.6281, 25.3494),
    "kordofan": (13.1833, 30.2167),
    "sennar": (13.569, 33.5672),
}
# Sudan places, (latitude, longitude). Longer names are matched first.
SUDAN_GEO_FALLBACKS = {
    "adre border crossing": (13.4667, 22.2000),
    "khartoum north": (15.6150, 32.5480),
    "khartoum bahri": (15.6150, 32.5480),
    "red sea state": (19.6158, 37.2164),
    "port sudan": (19.6158, 37.2164),
    "north darfur": (13.6279, 25.3494),
    "south darfur": (12.0489, 24.8807),
    "west darfur": (13.4526, 22.4472),
    "central darfur": (12.9096, 23.4741),
    "east darfur": (11.4619, 26.1258),
    "north kordofan": (13.1842, 30.2167),
    "south kordofan": (11.0111, 29.7183),
    "west kordofan": (11.7329, 28.3579),
    "al ubayyid": (13.1842, 30.2167),
    "el fasher": (13.6279, 25.3494),
    "al fashir": (13.6279, 25.3494),
    "el geneina": (13.4526, 22.4472),
    "geneina": (13.4526, 22.4472),
    "el obeid": (13.1842, 30.2167),
    "al obeid": (13.1842, 30.2167),
    "wad madani": (14.4012, 33.5199),
    "wad medani": (14.4012, 33.5199),
    "al jazirah": (14.4012, 33.5199),
    "al jazeera state": (14.4012, 33.5199),
    "al fulah": (11.7329, 28.3579),
    "ed daein": (11.4619, 26.1258),
    "river nile": (17.7022, 33.9864),
    "northern state": (19.1698, 30.4749),
    "al qadarif": (14.0349, 35.3834),
    "rapid support forces": (15.5000, 30.2000),
    "zalingei": (12.9096, 23.4741),
    "omdurman": (15.6445, 32.4777),
    "khartoum": (15.5007, 32.5599),
    "kadugli": (11.0111, 29.7183),
    "kassala": (15.4510, 36.4000),
    "gedaref": (14.0349, 35.3834),
    "dongola": (19.1698, 30.4749),
    "kordofan": (13.1842, 30.2167),
    "sennar": (13.5691, 33.5672),
    "nyala": (12.0489, 24.8807),
    "atbara": (17.7022, 33.9864),
    "sinja": (13.5691, 33.5672),
    "bahri": (15.6150, 32.5480),
    "darfur": (13.6279, 25.3494),
    "adre": (13.4667, 22.2000),
    "sudan": (15.5000, 30.2000),
    "rsf": (15.5000, 30.2000),
    "saf": (15.5000, 30.2000),
    "الخرطوم بحري": (15.6150, 32.5480),
    "أم درمان": (15.6445, 32.4777),
    "ام درمان": (15.6445, 32.4777),
    "بورتسودان": (19.6158, 37.2164),
    "بورت سودان": (19.6158, 37.2164),
    "ولاية البحر الأحمر": (19.6158, 37.2164),
    "شمال دارفور": (13.6279, 25.3494),
    "جنوب دارفور": (12.0489, 24.8807),
    "غرب دارفور": (13.4526, 22.4472),
    "وسط دارفور": (12.9096, 23.4741),
    "شرق دارفور": (11.4619, 26.1258),
    "شمال كردفان": (13.1842, 30.2167),
    "جنوب كردفان": (11.0111, 29.7183),
    "غرب كردفان": (11.7329, 28.3579),
    "ولاية الجزيرة": (14.4012, 33.5199),
    "نهر النيل": (17.7022, 33.9864),
    "الولاية الشمالية": (19.1698, 30.4749),
    "معبر أدري": (13.4667, 22.2000),
    "الدعم السريع": (15.5000, 30.2000),
    "الخرطوم": (15.5007, 32.5599),
    "الفاشر": (13.6279, 25.3494),
    "زالنجي": (12.9096, 23.4741),
    "الجنينة": (13.4526, 22.4472),
    "الأبيض": (13.1842, 30.2167),
    "الابيض": (13.1842, 30.2167),
    "كادقلي": (11.0111, 29.7183),
    "الفولة": (11.7329, 28.3579),
    "ود مدني": (14.4012, 33.5199),
    "القضارف": (14.0349, 35.3834),
    "دارفور": (13.6279, 25.3494),
    "كردفان": (13.1842, 30.2167),
    "السودان": (15.5000, 30.2000),
    "نيالا": (12.0489, 24.8807),
    "كسلا": (15.4510, 36.4000),
    "سنجة": (13.5691, 33.5672),
    "سنار": (13.5691, 33.5672),
    "عطبرة": (17.7022, 33.9864),
    "دنقلا": (19.1698, 30.4749),
    "الضعين": (11.4619, 26.1258),
    "أدري": (13.4667, 22.2000),
}
_SUDAN_FALLBACK_ORDER = tuple(sorted(SUDAN_GEO_FALLBACKS, key=len, reverse=True))
# Iraq and Kurdistan places, (latitude, longitude). Longer names are matched first.
IRAQ_GEO_FALLBACKS = {
    "kurdistan regional government": (36.1911, 44.0092),
    "al qaim border crossing": (34.3917, 41.0717),
    "popular mobilization forces": (33.2232, 43.6793),
    "ain al asad air base": (33.7856, 42.4411),
    "al harir air base": (36.1911, 44.0092),
    "iraqi security forces": (33.2232, 43.6793),
    "hashd al shaabi": (33.2232, 43.6793),
    "popular mobilization": (33.2232, 43.6793),
    "baghdad airport": (33.3152, 44.3661),
    "jurf al sakhar": (32.8789, 44.1731),
    "jurf al sakhr": (32.8789, 44.1731),
    "balad air base": (34.6158, 43.6786),
    "umm qasr port": (30.5085, 47.7804),
    "kurdistan region": (36.1911, 44.0092),
    "iraqi kurdistan": (36.1911, 44.0092),
    "harir air base": (36.1911, 44.0092),
    "ain al asad": (33.7856, 42.4411),
    "al asad air base": (33.7856, 42.4411),
    "salah al din": (34.6158, 43.6786),
    "salah ad din": (34.6158, 43.6786),
    "city of hit": (33.7856, 42.4411),
    "hit anbar": (33.7856, 42.4411),
    "green zone": (33.3152, 44.3661),
    "al qaim": (34.3917, 41.0717),
    "umm qasr": (30.5085, 47.7804),
    "sulaymaniyah": (35.5558, 45.4351),
    "sulaimaniyah": (35.5558, 45.4351),
    "suleimaniyah": (35.5558, 45.4351),
    "fallujah": (33.3558, 43.7861),
    "kurdistan": (36.1911, 44.0092),
    "peshmerga": (36.1911, 44.0092),
    "pesmerge": (36.1911, 44.0092),
    "nineveh": (36.3489, 43.1577),
    "ninewa": (36.3489, 43.1577),
    "ninawa": (36.3489, 43.1577),
    "baqubah": (33.7486, 44.6436),
    "baquba": (33.7486, 44.6436),
    "shingal": (36.3209, 41.8754),
    "shengal": (36.3209, 41.8754),
    "sinjar": (36.3209, 41.8754),
    "singal": (36.3209, 41.8754),
    "silemani": (35.5558, 45.4351),
    "slemani": (35.5558, 45.4351),
    "baghdad": (33.3152, 44.3661),
    "kirkuk": (35.4681, 44.3922),
    "kerkuk": (35.4681, 44.3922),
    "ramadi": (33.3558, 43.7861),
    "diyala": (33.7486, 44.6436),
    "tikrit": (34.6158, 43.6786),
    "karbala": (32.6160, 44.0249),
    "kurdish": (36.1911, 44.0092),
    "asayish": (36.1911, 44.0092),
    "asayesh": (36.1911, 44.0092),
    "anbar": (33.7856, 42.4411),
    "mosul": (36.3489, 43.1577),
    "musil": (36.3489, 43.1577),
    "erbil": (36.1911, 44.0092),
    "hewler": (36.1911, 44.0092),
    "hawler": (36.1911, 44.0092),
    "duhok": (36.8669, 42.9503),
    "dohuk": (36.8669, 42.9503),
    "dahuk": (36.8669, 42.9503),
    "dihok": (36.8669, 42.9503),
    "zakho": (36.8669, 42.9503),
    "zaxo": (36.8669, 42.9503),
    "basra": (30.5085, 47.7804),
    "besra": (30.5085, 47.7804),
    "bexda": (33.3152, 44.3661),
    "najaf": (31.9996, 44.3267),
    "babil": (32.8789, 44.1731),
    "kurds": (36.1911, 44.0092),
    "iraqi": (33.2232, 43.6793),
    "iraqis": (33.2232, 43.6793),
    "hashd": (33.2232, 43.6793),
    "iraq": (33.2232, 43.6793),
    "pmf": (33.2232, 43.6793),
    "krg": (36.1911, 44.0092),
    "isf": (33.2232, 43.6793),
    "المنطقة الخضراء": (33.3152, 44.3661),
    "مطار بغداد": (33.3152, 44.3661),
    "قاعدة عين الأسد": (33.7856, 42.4411),
    "عين الأسد": (33.7856, 42.4411),
    "عين الاسد": (33.7856, 42.4411),
    "إقليم كردستان": (36.1911, 44.0092),
    "اقليم كردستان": (36.1911, 44.0092),
    "الحشد الشعبي": (33.2232, 43.6793),
    "الجيش العراقي": (33.2232, 43.6793),
    "جرف الصخر": (32.8789, 44.1731),
    "صلاح الدين": (34.6158, 43.6786),
    "قاعدة بلد": (34.6158, 43.6786),
    "السليمانية": (35.5558, 45.4351),
    "سلێمانی": (35.5558, 45.4351),
    "الفلوجة": (33.3558, 43.7861),
    "الرمادي": (33.3558, 43.7861),
    "نەینەوا": (36.3489, 43.1577),
    "نينوى": (36.3489, 43.1577),
    "کەرکووک": (35.4681, 44.3922),
    "كوردستان": (36.1911, 44.0092),
    "کوردستان": (36.1911, 44.0092),
    "كردستان": (36.1911, 44.0092),
    "پێشمەرگە": (36.1911, 44.0092),
    "بيشمركة": (36.1911, 44.0092),
    "البشمركة": (36.1911, 44.0092),
    "هەولێر": (36.1911, 44.0092),
    "بعقوبة": (33.7486, 44.6436),
    "الأنبار": (33.7856, 42.4411),
    "الانبار": (33.7856, 42.4411),
    "كربلاء": (32.6160, 44.0249),
    "کەربەلا": (32.6160, 44.0249),
    "أم قصر": (30.5085, 47.7804),
    "ام قصر": (30.5085, 47.7804),
    "الموصل": (36.3489, 43.1577),
    "مووسڵ": (36.3489, 43.1577),
    "أربيل": (36.1911, 44.0092),
    "اربيل": (36.1911, 44.0092),
    "بغداد": (33.3152, 44.3661),
    "بەغدا": (33.3152, 44.3661),
    "البصرة": (30.5085, 47.7804),
    "بەسرە": (30.5085, 47.7804),
    "كركوك": (35.4681, 44.3922),
    "شنگال": (36.3209, 41.8754),
    "سنجار": (36.3209, 41.8754),
    "ديالى": (33.7486, 44.6436),
    "دیالە": (33.7486, 44.6436),
    "تكريت": (34.6158, 43.6786),
    "النجف": (31.9996, 44.3267),
    "نەجەف": (31.9996, 44.3267),
    "دهۆک": (36.8669, 42.9503),
    "دهوك": (36.8669, 42.9503),
    "زاخۆ": (36.8669, 42.9503),
    "زاخو": (36.8669, 42.9503),
    "ئاسایش": (36.1911, 44.0092),
    "العراق": (33.2232, 43.6793),
    "عێراق": (33.2232, 43.6793),
    "بابل": (32.8789, 44.1731),
    "هيت": (33.7856, 42.4411),
    "هیت": (33.7856, 42.4411),
}
_IRAQ_FALLBACK_ORDER = tuple(sorted(IRAQ_GEO_FALLBACKS, key=len, reverse=True))
_KURDISH_FOLD = str.maketrans({
    "ê": "e", "Ê": "e", "î": "i", "Î": "i", "û": "u", "Û": "u",
    "ş": "s", "Ş": "s", "ç": "c", "Ç": "c",
    "ẍ": "x", "Ẍ": "x", "ḧ": "h", "Ḧ": "h",
    "ı": "i", "İ": "i",
})
_GEO_FALLBACK_ORDER = tuple(sorted(GEO_FALLBACKS, key=len, reverse=True))
_COUNTRY_STEMS = {
    "iran", "iraq", "syria", "yemen", "sudan", "oman", "qatar", "kuwait",
    "bahrain", "jordan", "israel", "saudi", "lebanon",
}
_COUNTRY_OR_SEA = {
    "iran", "iraq", "jordan", "kuwait", "bahrain", "saudi", "saudi arabia",
    "uae", "united arab emirates", "qatar", "oman", "yemen", "syria", "lebanon",
    "israel", "gaza", "sudan", "red sea", "bab el mandeb", "bab al mandab",
    "strait of hormuz", "hormuz", "persian gulf", "arabian gulf", "gulf of oman",
}
RECENT_COORD_WINDOW = timedelta(days=7)

SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "regional_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "region": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["region", "note"],
            },
        },
    },
    "required": ["summary", "regional_notes"],
}


def region_name(value: object) -> str:
    text = " ".join(str(value or "").split())
    if text in REGIONS:
        return text
    return "Other / International"


_OSINT_MARKERS = (
    "osintdefender",
    "auroraintel",
    "clashreport",
    "warmonitors",
    "osint613",
    "osintupdates",
    "osinttv",
    "osintwarfare",
    "osinttechnical",
    "afrimeosint",
    "syriacosint",
    "sentdefender",
    "intelcrab",
    "conflictarchive",
    "suriyak",
    "liveuamap",
    "hawranosint",
    "syrianinfowar",
    "qalaatalmudiq",
    "middleeastspectator",
    "sudanwarmonitor",
)


def source_identity(kind: str = "", name: str = "", url: str = "") -> str:
    """Outlet name, Telegram/X handle, and host. Article paths are left out."""
    parts = [str(kind or ""), str(name or "")]
    text = str(url or "")
    telegram = re.search(r"t\.me/([^/?#]+)", text, re.IGNORECASE)
    if telegram:
        parts.append(telegram.group(1))
    social = re.search(r"(?:x\.com|twitter\.com)/([^/?#]+)", text, re.IGNORECASE)
    if social and social.group(1).lower() not in {"i", "intent", "share", "search", "status"}:
        parts.append(social.group(1))
    host = re.search(r"https?://([^/?#]+)", text, re.IGNORECASE)
    if host:
        parts.append(host.group(1))
    return " ".join(parts)


_LEAD_HANDLES = {
    "sentdefender", "faytuksnetwork", "menchosint", "me_observer_", "insidergeo", "mata_osint",
    "defencegeek", "_martinkelly_", "mmy1444", "smajid__moosavi",
}
_WIRE_NAMES = {
    "reuters", "arareuters", "reutersiran", "ap", "afp", "afpar", "bbc", "bbcarabic", "bbcworld",
    "cnn", "sky", "skynews", "anadolu", "anadoluagency", "aljazeera", "ajenglish", "alarabiya",
    "alarabytv", "alhadath", "asharq", "aawsat", "aawsat_news", "timesofisrael", "jerusalem_post",
    "ynet", "ynetnews", "rudaw", "rudawenglish", "kurdistan24", "k24english", "shafaq", "shafaqnews",
    "shafaqnewsarb", "nna", "nnaleb", "thenewregion", "wsj", "uniindianews", "lorientlejour",
    "iranintl", "iranintl_en", "middleeastmonitor", "thestudyofwar", "understandingwar", "isw",
    "levant_24_", "levant24",
}
_OFFICIAL_HANDLES = {
    "moi_bahrain", "bdf_bahrain", "kuwaitarmyghq", "moinformation", "mofaqatar_en", "uk_mto",
    "alsakaniali", "cjfcspox", "saudidcd", "kbsalsaud", "faisalbinfarhan", "kkiasa", "ksagaca",
    "yemen_pm", "mkhamenei_ir", "drpezeshkian", "mb_ghalibaf", "ejei_org", "araghchi", "irimfa_spox",
    "ir_rezaee", "mmohammadii61", "hatami_org", "gharibabadi", "iran_in_kuwait", "iraqipmo",
    "mba_althani_", "majedalansari", "mofauae", "anwargargash", "syrianmofaex", "idf", "idfonline",
    "idfspokesperson", "avichayadraee", "netanyahu", "israelpresident", "israelmfa", "israel_katz",
    "gadi_eisenkot", "gantzbe", "yairlapid", "naftalibennett", "avigdorliberman", "yairgolan10",
    "kaisos1987", "lbpresidency", "lebarmyofficial", "kingabdullahii", "usembassybeirut",
    "usembassyjordan", "rterdogan", "usambturkiye", "centcom", "centcomfarsi", "statedeptspox",
    "travelgov", "rapidresponse47", "jdvance", "secscottbessent", "secrubio", "repjoewilson",
    "un", "antonioguterres", "usun", "usainksa", "usabilaraby", "usabehfarsi", "germanydiplo",
    "porteparolemeae", "fcdottravelgovuk", "travelgoc", "safetravelnz", "smartraveller",
}
_STATE_LABELS = (
    ("presstv", "Press TV"),
    ("irna", "IRNA"),
    ("tasnim", "Tasnim"),
    ("farsnews", "Fars"),
    ("irib", "IRIB"),
    ("sana", "SANA"),
    ("alekhbariah", "Alekhbariah Syria"),
    ("almasirah", "Al Masirah"),
    ("sabanews", "Saba"),
)


def _identity_parts(kind: str = "", name: str = "", url: str = "") -> tuple[str, set[str], str]:
    raw = source_identity(kind, name, url).lower()
    tokens = set(re.findall(r"[a-z0-9_]+", raw))
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    return raw, tokens, compact


def _named(tokens: set[str], compact: str, names: set[str]) -> bool:
    for name in names:
        key = name.lower().strip("@")
        squashed = re.sub(r"[^a-z0-9]+", "", key)
        if key in tokens or (squashed and squashed in tokens):
            return True
        if len(squashed) >= 5 and squashed in compact:
            return True
    return False


def is_excluded_source(*parts: object) -> bool:
    """True for the blocked accounts. Yah_Saree is a different handle and stays."""
    tokens: set[str] = set()
    for part in parts:
        tokens.update(re.findall(r"[a-z0-9_]+", str(part or "").lower()))
    squashed = {token.replace("_", "") for token in tokens}
    for handle in EXCLUDED_SOURCE_HANDLES:
        if handle in tokens or handle.replace("_", "") in squashed:
            return True
    return False


def source_tier_for(kind: str = "", name: str = "", url: str = "") -> str:
    """Official, State Media, Lead, or Wire. OSINT and Regional Media are the leftovers."""
    raw, tokens, compact = _identity_parts(kind, name, url)
    if _named(tokens, compact, _LEAD_HANDLES):
        return "Lead (Unverified)"
    state_prefix = ("sana", "irna", "irib", "presstv", "tasnim", "alekhbariah", "almasirah", "farsnews")
    if any(token == prefix or token.startswith(prefix) for token in tokens for prefix in state_prefix):
        return "State Media"
    if "fars" in tokens or "saba" in tokens or "sabanews" in compact or "saba.ye" in raw:
        return "State Media"
    if any(host in raw for host in (
        "reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "cnn.com", "sky.com", "aa.com.tr",
        "aljazeera.com", "alarabiya.net", "aawsat.com", "timesofisrael.com", "rudaw.net",
        "kurdistan24.net", "shafaq.com", "nna-leb", "middleeastmonitor.com", "iranintl.com",
        "jpost.com", "ynetnews.com", "understandingwar.org", "lorientlejour.com",
    )):
        return "Wire / Outlet"
    if _named(tokens, compact, _WIRE_NAMES):
        return "Wire / Outlet"
    if "anzen.mofa.go.jp" in raw or "ukmto.org" in raw:
        return "Official"
    if _named(tokens, compact, _OFFICIAL_HANDLES):
        return "Official"
    if tokens & {"embassy", "centcom", "ukmto"} or "ministry" in tokens or "mofa" in tokens:
        return "Official"
    if "osint" in compact or any(marker in compact for marker in _OSINT_MARKERS):
        return "OSINT"
    return "Regional Media"


def claim_outlet(kind: str = "", name: str = "", url: str = "") -> str:
    """State media and ISW need the outlet named in the English title or summary."""
    raw, tokens, compact = _identity_parts(kind, name, url)
    if "understandingwar" in compact or "thestudyofwar" in compact or "isw" in tokens:
        return "ISW"
    if "fars" in tokens or "farsnews" in compact:
        return "Fars"
    if "saba" in tokens or "sabanews" in compact:
        return "Saba"
    for key, label in _STATE_LABELS:
        if key in ("farsnews", "sabanews"):
            continue
        if key in compact:
            return label
    return ""


def enemy_forces_source(kind: str = "", name: str = "", url: str = "") -> bool:
    _raw, _tokens, compact = _identity_parts(kind, name, url)
    return "alekhbariah" in compact or "nnaleb" in compact


_LATENESS = re.compile(
    r"\b(?:about\s+|some\s+|several\s+|a few\s+)?(?:\d+\s+)?(?:hours?|minutes?|days?)\s+later\b"
    r"|\blate(?:\s+on)?\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|last night|tonight|yesterday|today)\b",
    re.IGNORECASE,
)
_ENEMY_FORCES = re.compile(r"\benemy forces\b", re.IGNORECASE)
_HOUTHI = re.compile(r"\bHouthi(?:s)?\b", re.IGNORECASE)


def strip_lateness(text: str) -> str:
    cleaned = _LATENESS.sub("", str(text or ""))
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    return cleaned.strip(" ,;:-")


def apply_house_style(title: str, summary: str, kind: str = "", name: str = "", url: str = "") -> tuple[str, str]:
    """English house style. The original post text is left untouched."""
    next_title = strip_lateness(title)
    next_summary = strip_lateness(summary)
    next_title = _HOUTHI.sub("Ansarallah", next_title)
    next_summary = _HOUTHI.sub("Ansarallah", next_summary)
    if enemy_forces_source(kind, name, url):
        next_title = _ENEMY_FORCES.sub("Israeli forces", next_title)
        next_summary = _ENEMY_FORCES.sub("Israeli forces", next_summary)
    outlet = claim_outlet(kind, name, url)
    blob = f"{next_title} {next_summary}".lower()
    attributed = outlet and (outlet.lower() in blob or (outlet == "ISW" and "institute for the study of war" in blob))
    if outlet and not attributed:
        sentence = next_summary or next_title
        next_summary = f"{outlet}: {sentence}".strip()
    return next_title.strip(), next_summary.strip()


def backfill_source_tiers(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if "source_tier" not in columns:
        return
    rows = conn.execute(
        "SELECT id, source_url, source_name, source_kind, source_tier FROM items"
    ).fetchall()
    for row in rows:
        tier = source_tier_for(row["source_kind"], row["source_name"], row["source_url"])
        if tier != (row["source_tier"] or ""):
            conn.execute("UPDATE items SET source_tier = ? WHERE id = ?", (tier, row["id"]))


def load_features() -> list[dict]:
    try:
        features = [item for item in osint.read_collection(osint.HOTSPOTS) if isinstance(item, dict)]
    except RuntimeError as exc:
        print(f"{exc}. Using no live events.")
        features = []
    if features or not osint.ARCHIVE.exists() or osint.ARCHIVE.stat().st_size == 0:
        return features
    try:
        archived = [item for item in osint.read_collection(osint.ARCHIVE) if isinstance(item, dict)]
    except RuntimeError as exc:
        print(f"{exc}. Archive was not used.")
        return features
    return archived


def event_row(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    if not isinstance(props, dict):
        return None
    moment = osint.parse_timestamp(props.get("timestamp"))
    title = osint.one_sentence(props.get("title") or props.get("summary"))
    if not title and moment is None:
        return None
    return {
        "title": title[:240],
        "summary": osint.one_sentence(props.get("summary"))[:500],
        "region": region_name(props.get("region")),
        "category": osint.live_category(props.get("category")),
        "timestamp": osint.format_timestamp(moment) if moment else "",
        "severity": osint.live_severity(props.get("severity")),
        "governorate": osint.match_governorate(props.get("governorate"))
        or osint.match_governorate(props.get("location_name")),
        "verification_tier": osint.one_sentence(props.get("verification_tier"))[:80],
        "_ms": moment.timestamp() if moment else 0,
    }


def is_point(feature: dict) -> bool:
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates")
    if geometry.get("type") != "Point" or not isinstance(coords, list) or len(coords) < 2:
        return False
    return osint.valid_coord(coords[0], coords[1]) is not None


def live_events_from(features: list[dict]) -> list[dict]:
    rows = []
    for feature in features:
        row = event_row(feature)
        if row:
            rows.append(row)
    rows.sort(key=lambda item: item["_ms"], reverse=True)
    for row in rows:
        row.pop("_ms", None)
    return rows


def live_events() -> list[dict]:
    return live_events_from(combined_features())


def region_counts(events: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in REGIONS}
    for event in events:
        counts[region_name(event.get("region"))] += 1
    return counts


def governorate_counts(events: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in osint.SYRIA_GOVERNORATES}
    for event in events:
        name = event.get("governorate") or ""
        if name in counts:
            counts[name] += 1
    return counts


def within_live_window(feature: dict, now: datetime | None = None) -> bool:
    moment = osint.parse_timestamp((feature.get("properties") or {}).get("timestamp"))
    if moment is None:
        return False
    cutoff = (now or datetime.now(timezone.utc)) - LIVE_WINDOW
    return moment >= cutoff


def read_cache() -> dict | None:
    if not CACHE.exists() or CACHE.stat().st_size == 0:
        return None
    try:
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"summary_cache.json was not readable ({exc.__class__.__name__}).")
        return None
    if not isinstance(payload, dict):
        return None
    text = " ".join(str(payload.get("text") or "").split())
    if not text:
        return None
    notes = []
    for item in payload.get("regional_notes") or []:
        if not isinstance(item, dict):
            continue
        region = " ".join(str(item.get("region") or "").split())
        note = " ".join(str(item.get("note") or "").split())
        if region and note:
            notes.append({"region": region, "note": note})
    return {
        "generated_at": " ".join(str(payload.get("generated_at") or "").split()),
        "text": text,
        "regional_notes": notes,
    }


def write_cache(summary: dict) -> None:
    payload = {
        "generated_at": summary["generated_at"],
        "text": summary["text"],
        "regional_notes": summary["regional_notes"],
    }
    temporary = CACHE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(CACHE)


def ensure_feed_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            headline_hash TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_name TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT '',
            region TEXT,
            governorate TEXT,
            location_name TEXT,
            lat REAL,
            lng REAL,
            severity TEXT,
            category TEXT,
            corroboration_count INTEGER NOT NULL DEFAULT 1,
            published_at TEXT,
            ingested_at TEXT NOT NULL,
            original_text TEXT,
            source_tier TEXT
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    additions = {
        "source_name": "TEXT NOT NULL DEFAULT ''",
        "source_kind": "TEXT NOT NULL DEFAULT ''",
        "region": "TEXT",
        "governorate": "TEXT",
        "location_name": "TEXT",
        "lat": "REAL",
        "lng": "REAL",
        "severity": "TEXT",
        "category": "TEXT",
        "corroboration_count": "INTEGER NOT NULL DEFAULT 1",
        "published_at": "TEXT",
        "ingested_at": "TEXT",
        "headline_hash": "TEXT",
        "url_hash": "TEXT",
        "source_url": "TEXT",
        "title": "TEXT",
        "summary": "TEXT",
        "original_text": "TEXT",
        "source_tier": "TEXT",
    }
    for name, typedef in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {typedef}")
    backfill_source_tiers(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_headline_hash ON items(headline_hash)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_url_hash ON items(url_hash)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    init_db(conn)
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_cache (
            scope TEXT PRIMARY KEY,
            updated_at TEXT,
            summary TEXT
        )
        """
    )
    # Railway boots through app.py -> start_background() -> db_connect() -> ensure_feed_db() -> init_db().
    purge_spam_rows_once(conn)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(FEEDS_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    ensure_feed_db(conn)
    return conn


def normalize_title(title: str) -> str:
    text = str(title or "").lower()
    text = re.sub(r"[^a-z0-9\u0600-\u06ff]+", " ", text)
    return " ".join(text.split())


def headline_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()


def url_hash(url: str) -> str:
    return hashlib.sha256(" ".join(str(url or "").strip().lower().split()).encode("utf-8")).hexdigest()


def recent_titles(conn: sqlite3.Connection, limit: int = SIMILAR_TITLE_LIMIT) -> list[str]:
    return [str(row["title"]) for row in recent_story_rows(conn, limit)]


def recent_story_rows(conn: sqlite3.Connection, limit: int = SIMILAR_TITLE_LIMIT) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, title, original_text, source_name, source_kind FROM items "
        "WHERE title IS NOT NULL AND title != '' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def similar_to_recent(title: str, titles: list[str]) -> bool:
    left = " ".join(str(title or "").split())
    if not left:
        return False
    folded = left.casefold()
    for other in titles:
        right = " ".join(str(other or "").split())
        if not right:
            continue
        if SequenceMatcher(None, folded, right.casefold()).ratio() > SIMILAR_TITLE_RATIO:
            return True
    return False


def best_title_match(title: str, rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    left = " ".join(str(title or "").split())
    if not left:
        return None
    folded = left.casefold()
    best = None
    best_ratio = SIMILAR_TITLE_RATIO
    for row in rows:
        candidates = [row["title"] or ""]
        if "original_text" in row.keys() and row["original_text"]:
            candidates.append(row["original_text"])
        for candidate in candidates:
            right = " ".join(str(candidate or "").split())
            if not right:
                continue
            ratio = SequenceMatcher(None, folded, right.casefold()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = row
    return best


def same_channel(item: dict, row: sqlite3.Row) -> bool:
    incoming = source_label(item.get("source_kind") or "", item.get("source_name") or "")
    stored = source_label(row["source_kind"] or "", row["source_name"] or "")
    return incoming.casefold() == stored.casefold()


def seen_url(conn: sqlite3.Connection, source_url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM items WHERE source_url = ? OR url_hash = ? LIMIT 1",
        (source_url, url_hash(source_url)),
    ).fetchone()
    return row is not None


def bump_corroboration(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute(
        "UPDATE items SET corroboration_count = COALESCE(corroboration_count, 1) + 1 WHERE id = ?",
        (row_id,),
    )


def titles_near(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= 24 and len(right) >= 24 and (left in right or right in left):
        return True
    left_words, right_words = set(left.split()), set(right.split())
    if len(left_words) < 4 or len(right_words) < 4:
        return False
    return len(left_words & right_words) / len(left_words | right_words) >= 0.82


def source_label(kind: str, name: str) -> str:
    clean = str(name or "").strip()
    if " · " in clean:
        return clean
    if kind == "telegram":
        return f"Telegram · @{clean.lstrip('@')}"
    if kind == "x":
        return f"X · @{clean.lstrip('@')}"
    return f"RSS · {clean or 'feed'}"


def parse_when(value: object) -> datetime | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    parsed = osint.parse_timestamp(text)
    if parsed:
        return parsed
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def plain_text(value: object) -> str:
    raw = str(value or "")
    if "<" in raw and ">" in raw:
        raw = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return osint.one_sentence(raw)


def seen_item(conn: sqlite3.Connection, source_url: str, digest: str, title: str = "") -> bool:
    digest_url = url_hash(source_url)
    row = conn.execute(
        "SELECT 1 FROM items WHERE source_url = ? OR headline_hash = ? OR url_hash = ? LIMIT 1",
        (source_url, digest, digest_url),
    ).fetchone()
    if row is not None:
        return True
    normalized = normalize_title(title)
    if not normalized:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute(
        "SELECT title FROM items WHERE COALESCE(published_at, ingested_at, '') >= ?",
        (cutoff,),
    ).fetchall()
    return any(titles_near(normalized, normalize_title(item["title"])) for item in recent)


def read_last_polled(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM feed_meta WHERE key = 'last_polled_at'").fetchone()
    if not row:
        return None
    text = " ".join(str(row["value"] or "").split())
    return text or None


def write_last_polled(conn: sqlite3.Connection, stamp: str) -> None:
    write_meta(conn, "last_polled_at", stamp)


def write_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO feed_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def read_meta_int(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM feed_meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return 0
    try:
        return int(str(row["value"]).strip() or 0)
    except ValueError:
        return 0


def rotate_slice(items: list, index: int, size: int) -> list:
    if not items or size <= 0:
        return []
    count = min(size, len(items))
    start = index % len(items)
    return [items[(start + offset) % len(items)] for offset in range(count)]


def paced_get(url: str):
    global _pace_pending
    if _pacing:
        with _pace_lock:
            if _pace_pending:
                time.sleep(random.uniform(1.0, 2.5))
            _pace_pending = True
    return requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})


def feed_features() -> list[dict]:
    features = []
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM items WHERE lat IS NOT NULL AND lng IS NOT NULL ORDER BY published_at DESC"
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        lng = row["lng"]
        lat = row["lat"]
        if not osint.valid_coord(lng, lat):
            continue
        if is_excluded_source(row["source_name"], row["source_url"], row["source_kind"]):
            continue
        props = {
            "severity": osint.live_severity(row["severity"]),
            "title": row["title"],
            "summary": row["summary"] or row["title"],
            "timestamp": row["published_at"] or row["ingested_at"],
            "source": row["source_url"],
            "source_url": row["source_url"] or "",
            "source_tier": row["source_tier"] or source_tier_for(row["source_kind"], row["source_name"], row["source_url"]),
            "account": source_label(row["source_kind"], row["source_name"]),
            "handle": source_label(row["source_kind"], row["source_name"]),
            "source_handle": source_label(row["source_kind"], row["source_name"]),
            "source_label": source_label(row["source_kind"], row["source_name"]),
            "batch": "live_feed",
            "verification_tier": "Unverified Lead" if row["source_kind"] == "telegram" else "State-Affiliated / Single-Source",
            "region": region_name(row["region"]),
            "category": osint.live_category(row["category"]),
            "warning_number": None,
            "location_name": row["location_name"] or "",
            "corroboration_count": int(row["corroboration_count"] or 1),
            "original_text": row["original_text"] or "",
        }
        if row["governorate"]:
            props["governorate"] = row["governorate"]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": props,
            }
        )
    return features


def combined_features() -> list[dict]:
    features = []
    seen: set[str] = set()
    for feature in load_features() + feed_features():
        if not is_point(feature):
            continue
        source = str((feature.get("properties") or {}).get("source") or "")
        if source and source in seen:
            continue
        if source:
            seen.add(source)
        features.append(feature)
    features.sort(
        key=lambda feature: osint.parse_timestamp((feature.get("properties") or {}).get("timestamp"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return features


def parse_feed(content: bytes):
    """Parse bytes already fetched with timeout=6. A parse error is logged by the caller."""
    return feedparser.parse(content)


def looks_like_feed(content: bytes) -> bool:
    sample = content.lstrip()[:800].lower()
    return sample.startswith((b"<?xml", b"<rss", b"<feed")) or b"<rss" in sample or b"<feed" in sample


def notice_page(url: str) -> bool:
    cleaned = url.rstrip("/")
    return any(cleaned == candidate.rstrip("/") for candidate in HTML_NOTICE_FEEDS)


def html_notice_items(name: str, url: str, content: bytes) -> list[dict]:
    """Visible notice titles from one HTML page. A bad page is skipped."""
    try:
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        found = []
        seen = set()
        for node in soup.select("article h1, article h2, article h3, h2, h3, li a, article a"):
            text = " ".join(node.get_text(" ", strip=True).split())
            if not 25 <= len(text) <= 220:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            found.append(
                {
                    "title": text[:240],
                    "summary": text[:500],
                    "source_url": f"{url}#notice-{digest}",
                    "source_name": source_label("rss", name),
                    "source_kind": "rss",
                    "published_at": "",
                }
            )
            if len(found) >= 12:
                break
    except Exception as exc:
        print(f"RSS {name} failed: {exc.__class__.__name__}.")
        note_feed("rss", name, False, exc.__class__.__name__)
        return []
    if not found:
        print(f"RSS {name} failed: no notices.")
        note_feed("rss", name, False, "no notices")
        return []
    note_feed("rss", name, True, "")
    return found


_AJ_THEATER_RE = re.compile(
    r"\b(?:middle[\s-]east|iran(?:ian)?s?|tehran|irgc|persian gulf|arabian gulf|the gulf|"
    r"bahrain|kuwait|qatar|emirates|emirati|oman|saudi(?: arabia)?|yemeni?|houthis?|ansarallah|ansar allah|"
    r"israel(?:i)?s?|gaza|palestin(?:e|ian)s?|west bank|lebanon(?:ese)?|beirut|hezbollah|"
    r"jordan(?:ian)?s?|amman|syria(?:n)?s?|damascus|aleppo|idlib|iraq(?:i)?s?|baghdad|kurdistan|"
    r"hormuz|red sea|bab al-mandeb|bab el-mandeb|bab al-mandab|gulf of aden|gulf of oman|arabian sea|"
    r"sudan(?:ese)?|khartoum|darfur|kordofan|rsf)\b",
    re.IGNORECASE,
)
_AJ_SECURITY_RE = re.compile(
    r"\b(?:wars?|conflict|strikes?|struck|attacks?|attacked|hits?|missiles?|drones?|rockets?|"
    r"shelling|shelled|clash(?:es|ed)?|troops|military|army|navy|naval|sanctions?|"
    r"blockades?|ceasefire|cease-fire|truce|airstrikes?|air strikes?|explosions?|blasts?|"
    r"bombs?|bombings?|hostages?|militias?|militants?|intercept(?:ed|s|ion)?|"
    r"sea mines?|naval mines?|landmines?|mines|raids?|invasions?|offensives?|"
    r"killed|killing|wounded|casualt(?:y|ies)|shooting|artillery|warships?|"
    r"sieges?|bombard(?:ed|ment)?|assassinat(?:ed|ion)|shot down|downed|"
    r"air defen[cs]e|unrest|tankers?|shipping|seiz(?:e|es|ed|ure)|captured?|"
    r"ablaze|on fire|fighter jets?|security council)\b",
    re.IGNORECASE,
)
_LIVEBLOG_PATH_RE = re.compile(
    r"/news/liveblog/(\d{4})/(\d{2})/(\d{2})/([a-z0-9-]+)",
    re.IGNORECASE,
)


def aljazeera_conflict_item(text: str) -> bool:
    """Middle East, Iran, Gulf, Levant, Yemen, maritime, or Sudan security news."""
    raw = str(text or "")
    if not raw.strip():
        return False
    theater = (
        _AJ_THEATER_RE.search(raw) is not None
        or sudan_mention(raw)
        or iraq_mention(raw)
        or mentions_syria_or_middle_east(raw)
    )
    return theater and _AJ_SECURITY_RE.search(raw) is not None


def browser_get(url: str):
    return requests.get(
        url,
        timeout=FETCH_TIMEOUT,
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/rss+xml,*/*",
        },
    )


def fetch_rss_feed(name: str, url: str) -> list[dict]:
    try:
        response = paced_get(url)
        response.raise_for_status()
        content = response.content or b""
    except Exception as exc:
        print(f"RSS {name} failed: {exc.__class__.__name__}.")
        note_feed("rss", name, False, exc.__class__.__name__)
        return []
    if notice_page(url) and not looks_like_feed(content):
        return html_notice_items(name, url, content)
    try:
        parsed = parse_feed(content)
    except Exception as exc:
        print(f"RSS {name} failed: {exc.__class__.__name__}.")
        note_feed("rss", name, False, exc.__class__.__name__)
        return []
    note_feed("rss", name, True, "")
    found = []
    for entry in (parsed.entries or [])[:20]:
        title = plain_text(getattr(entry, "title", ""))
        link = plain_text(getattr(entry, "link", ""))
        if not title or not link.startswith("http"):
            continue
        summary = plain_text(getattr(entry, "summary", "") or getattr(entry, "description", "") or title)
        if url == ALJAZEERA_ALL_FEED and "/news/liveblog/" in link:
            continue
        if url == ALJAZEERA_ALL_FEED and not aljazeera_conflict_item(f"{title} {summary}"):
            continue
        published = getattr(entry, "published", "") or getattr(entry, "updated", "")
        found.append(
            {
                "title": title[:240],
                "summary": (summary or title)[:500],
                "source_url": link,
                "source_name": source_label("rss", name),
                "source_kind": "rss",
                "published_at": published,
            }
        )
    return found


def newest_aljazeera_liveblog(text: str) -> str:
    """Newest /news/liveblog/YYYY/MM/DD/ link. Equal dates keep the first one."""
    best = ""
    best_key = ("", "", "")
    for match in _LIVEBLOG_PATH_RE.finditer(str(text or "").replace("\\/", "/")):
        key = (match.group(1), match.group(2), match.group(3))
        if key <= best_key:
            continue
        best_key = key
        slug = match.group(4).lower().strip("-")
        best = f"https://www.aljazeera.com/news/liveblog/{key[0]}/{key[1]}/{key[2]}/{slug}"
    return best


def discover_aljazeera_liveblog() -> str:
    """Look in the all-news RSS, then the Middle East page. No link means skip quietly."""
    for url in (ALJAZEERA_ALL_FEED, ALJAZEERA_MIDDLE_EAST_PAGE):
        try:
            response = browser_get(url)
            response.raise_for_status()
        except Exception as exc:
            print(f"Al Jazeera liveblog lookup failed: {exc.__class__.__name__}.")
            continue
        found = newest_aljazeera_liveblog(response.text)
        if found:
            return found
    return ""


def aljazeera_amp_url(page_url: str) -> str:
    marker = "https://www.aljazeera.com/news/liveblog/"
    amp = "https://www.aljazeera.com/amp/news/liveblog/"
    if page_url.startswith(amp):
        return page_url
    if page_url.startswith(marker):
        return amp + page_url[len(marker):]
    return page_url


def parse_aljazeera_liveblog(html_text: str, page_url: str) -> list[dict]:
    """Top 5 newest timestamped updates. One liveblog becomes one item per update."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    nodes = soup.select("#live-blog-updates li[data-sort-time]")
    if not nodes:
        nodes = soup.select("li[id^=live-blog-updates-item]")
    ranked = []
    for node in nodes:
        raw_sort = str(node.get("data-sort-time") or "")
        sort_value = int(raw_sort) if raw_sort.isdigit() else 0
        card = node.select_one(".card-live") or node
        heading_node = card.find(["h2", "h3"])
        heading = plain_text(heading_node.get_text(" ", strip=True) if heading_node else "")
        body_node = card.select_one(".wysiwyg") or card.select_one(".card-live__content")
        body = plain_text(body_node.get_text(" ", strip=True) if body_node else "")
        if not heading:
            continue
        item_id = str(node.get("id") or "").strip()
        if item_id:
            source_url = f"{page_url}#{item_id}"
        else:
            digest = hashlib.sha256(heading.encode("utf-8")).hexdigest()[:16]
            source_url = f"{page_url}#{digest}"
        published = ""
        if sort_value:
            published = osint.format_timestamp(datetime.fromtimestamp(sort_value, timezone.utc))
        ranked.append(
            (
                sort_value,
                {
                    "title": heading[:240],
                    "summary": (body or heading)[:500],
                    "source_url": source_url,
                    "source_name": "Al Jazeera Live",
                    "source_kind": "rss",
                    "published_at": published,
                },
            )
        )
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    found = []
    seen_heading = set()
    seen_url = set()
    seen_hash = set()
    for _sort, item in ranked:
        heading_key = " ".join(item["title"].lower().split())
        digest = hashlib.sha256(
            f"{heading_key}|{item['source_url']}|{item['summary']}".encode("utf-8")
        ).hexdigest()
        if heading_key in seen_heading or item["source_url"] in seen_url or digest in seen_hash:
            continue
        seen_heading.add(heading_key)
        seen_url.add(item["source_url"])
        seen_hash.add(digest)
        found.append(item)
        if len(found) >= 5:
            break
    return found


def poll_aljazeera_liveblog() -> list[dict]:
    """Fetch the newest liveblog and return its latest updates. No liveblog is a quiet skip."""
    try:
        page_url = discover_aljazeera_liveblog()
    except Exception as exc:
        print(f"Al Jazeera liveblog failed: {exc.__class__.__name__}.")
        return []
    if not page_url:
        return []
    try:
        response = browser_get(page_url)
        response.raise_for_status()
        updates = parse_aljazeera_liveblog(response.text, page_url)
        if not updates:
            amp = browser_get(aljazeera_amp_url(page_url))
            amp.raise_for_status()
            updates = parse_aljazeera_liveblog(amp.text, page_url)
    except Exception as exc:
        print(f"Al Jazeera liveblog failed: {exc.__class__.__name__}.")
        return []
    if not updates:
        return []
    kept = []
    for item in updates:
        blob = spam_post_text(item.get("title"), item.get("summary"), "", item.get("source_url"))
        if is_spam_post(blob):
            print(f"Dropped spam before save: {str(item.get('title') or '')[:80]}")
            continue
        kept.append(item)
    return kept


def fetch_rss_candidates() -> list[dict]:
    found = []
    with ThreadPoolExecutor(max_workers=len(RSS_FEEDS) or 1) as pool:
        futures = [pool.submit(fetch_rss_feed, name, url) for name, url in RSS_FEEDS]
        for future in futures:
            found.extend(future.result())
    return found


def fetch_telegram_channel(channel: str) -> list[dict]:
    url = f"https://t.me/s/{channel}"
    try:
        response = paced_get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        print(f"Telegram {channel} failed: {exc.__class__.__name__}.")
        note_feed("telegram", channel, False, exc.__class__.__name__)
        return []
    found = []
    for wrap in soup.select(".tgme_widget_message")[:20]:
        post = " ".join(str(wrap.get("data-post") or "").split())
        node = wrap.select_one(".tgme_widget_message_text")
        if not post or node is None:
            continue
        text = plain_text(node.get_text(" ", strip=True))
        if not text:
            continue
        when = ""
        clock = wrap.select_one("time")
        if clock is not None:
            when = clock.get("datetime") or ""
        found.append(
            {
                "title": text[:180],
                "summary": text[:500],
                "source_url": f"https://t.me/{post}",
                "source_name": source_label("telegram", channel),
                "source_kind": "telegram",
                "published_at": when,
            }
        )
    note_feed("telegram", channel, True, "")
    return found


def mentions_syria_or_middle_east(text: str) -> bool:
    lowered = text.lower()
    latin = (
        "syria", "syrian", "middle east", "middle-east", "aleppo", "damascus", "idlib", "idleb",
        "homs", "hama", "latakia", "tartus", "raqqa", "deir ez-zor", "deir ezzor", "hasakah",
        "daraa", "suwayda", "sweida", "quneitra", "rif dimashq",
    )
    if any(term in lowered for term in latin):
        return True
    return any(term in text for term in ("سوريا", "سورية", "الشرق الأوسط", "الشرق الاوسط", "حلب", "دمشق", "إدلب", "حمص"))


def mentions_sudan_or_horn(text: str) -> bool:
    if sudan_mention(text):
        return True
    lowered = text.lower()
    latin = ("horn of africa", "eritrea", "ethiopia", "djibouti", "somalia", "south sudan")
    if any(term in lowered for term in latin):
        return True
    return "القرن الأفريقي" in text or "القرن الافريقي" in text


def x_handle_from_item(item: dict) -> str:
    url = str(item.get("source_url") or "")
    match = re.search(r"(?:x|twitter)\.com/([^/?#]+)", url, re.IGNORECASE)
    if match and match.group(1).lower() not in {"i", "intent", "share", "search"}:
        return match.group(1)
    name = str(item.get("source_name") or "")
    if "·" in name:
        return name.split("·", 1)[1].strip().lstrip("@")
    return ""


def mentions_broad_theater(text: str) -> bool:
    """Gulf, Levant, Iran, maritime, Sudan, or Iraq. Used for the widened X sweep."""
    if mentions_syria_or_middle_east(text) or mentions_sudan_or_horn(text) or iraq_mention(text):
        return True
    lowered = text.lower()
    latin = (
        "bahrain", "kuwait", "qatar", "uae", "emirates", "oman", "saudi", "yemen", "houthi",
        "ansarallah", "iran", "tehran", "israel", "gaza", "palestine", "west bank", "lebanon",
        "beirut", "hezbollah", "jordan", "amman", "hormuz", "red sea", "bab el-mandeb",
        "bab al-mandab", "ukmto", "jmic", "gulf of oman", "persian gulf", "arabian sea",
        "turkey", "turkiye", "ankara",
    )
    if any(term in lowered for term in latin):
        return True
    return any(term in text for term in (
        "البحرين", "الكويت", "قطر", "الإمارات", "الامارات", "عمان", "السعود", "اليمن", "الحوث",
        "أنصار الله", "انصار الله", "إيران", "ايران", "إسرائيل", "اسرائيل", "غزة", "فلسطين",
        "لبنان", "بيروت", "حزب الله", "الأردن", "الاردن", "هرمز", "البحر الأحمر", "تركيا",
    ))


def keep_x_post(handle: str, text: str) -> bool:
    """Syria and Middle East posts stay. Sudan and Iraq accounts also keep their own regions."""
    if is_excluded_source(handle):
        return False
    if mentions_syria_or_middle_east(text):
        return True
    lowered = handle.lower()
    if lowered in SUDAN_X_HANDLES and mentions_sudan_or_horn(text):
        return True
    if lowered in IRAQ_X_HANDLES and iraq_mention(text):
        return True
    return lowered in BROAD_X_HANDLES and mentions_broad_theater(text)


def canonical_x_url(handle: str, link: str, guid: str) -> str:
    match = re.search(r"/status/(\d+)", f"{link} {guid}")
    if match:
        return f"https://x.com/{handle}/status/{match.group(1)}"
    cleaned = plain_text(link)
    return cleaned if cleaned.startswith("http") else ""


def fetch_x_handle(handle: str) -> list[dict]:
    """Try X mirrors in order. Only the final result is recorded as online or failing."""
    last_error = "no entries"
    for template in X_FEED_URLS:
        url = template.format(handle=handle)
        host = url.split("/")[2]
        try:
            response = requests.get(
                url,
                timeout=X_MIRROR_TIMEOUT,
                headers={"User-Agent": BROWSER_USER_AGENT},
            )
        except Exception as exc:
            last_error = exc.__class__.__name__
            print(f"X {handle} via {host} failed: {last_error}.")
            continue
        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}"
            print(f"X {handle} via {host} failed: {last_error}.")
            continue
        try:
            parsed = parse_feed(response.content)
        except Exception as exc:
            last_error = exc.__class__.__name__
            print(f"X {handle} via {host} failed: {last_error}.")
            continue
        if getattr(parsed, "bozo", False) and not parsed.entries:
            last_error = "invalid rss"
            print(f"X {handle} via {host} failed: invalid rss.")
            continue
        if not parsed.entries:
            last_error = "no entries"
            print(f"X {handle} via {host} failed: no entries.")
            continue
        found = []
        for entry in parsed.entries[:20]:
            title = plain_text(getattr(entry, "title", ""))
            summary = plain_text(getattr(entry, "summary", "") or getattr(entry, "description", "") or title)
            blob = f"{title} {summary}"
            if not title or not keep_x_post(handle, blob):
                continue
            source_url = canonical_x_url(handle, getattr(entry, "link", ""), getattr(entry, "id", "") or getattr(entry, "guid", ""))
            if not source_url:
                continue
            published = getattr(entry, "published", "") or getattr(entry, "updated", "")
            found.append(
                {
                    "title": title[:240],
                    "summary": (summary or title)[:500],
                    "source_url": source_url,
                    "source_name": source_label("x", handle),
                    "source_kind": "x",
                    "published_at": published,
                }
            )
        note_feed("x", handle, True, "")
        return found
    note_feed("x", handle, False, last_error)
    return []


def fetch_x_candidates() -> list[dict]:
    found = []
    with ThreadPoolExecutor(max_workers=min(4, len(X_HANDLES) or 1)) as pool:
        futures = [pool.submit(fetch_x_handle, handle) for handle in X_HANDLES]
        for future in futures:
            found.extend(future.result())
    return found


def fetch_telegram_candidates() -> list[dict]:
    found = []
    with ThreadPoolExecutor(max_workers=len(TELEGRAM_CHANNELS) or 1) as pool:
        futures = [pool.submit(fetch_telegram_channel, channel) for channel in TELEGRAM_CHANNELS]
        for future in futures:
            found.extend(future.result())
    return found


_FOREIGN_SCRIPT = re.compile(
    r"[\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"
    r"\u0900-\u097F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]"
)
_ENGLISH_STOPS = (" the ", " and ", " of ", " to ", " in ", " for ", " with ", " from ", " on ", " said ")
_OTHER_LANGUAGE_MARKERS = (
    " le ", " la ", " les ", " des ", " une ", " dans ", " est ",
    " los ", " las ", " del ", " una ", " que ",
    " und ", " der ", " die ", " nicht ",
)


def needs_english(text: object) -> bool:
    """True when a title or summary is still Arabic or another non-English language."""
    sample = " ".join(str(text or "").split())
    if not sample:
        return False
    latin = len(re.findall(r"[A-Za-z]", sample))
    foreign = len(_FOREIGN_SCRIPT.findall(sample))
    if foreign >= 3 and foreign > latin:
        return True
    words = re.findall(r"[A-Za-z\u00C0-\u024F']+", sample)
    if len(words) < 4:
        return False
    lowered = f" {sample.casefold()} "
    english_hits = sum(1 for token in _ENGLISH_STOPS if token in lowered)
    if english_hits >= 2:
        return False
    accented = re.search(r"[\u00C0-\u024F]", sample) is not None
    other_language = any(token in lowered for token in _OTHER_LANGUAGE_MARKERS)
    return (accented or other_language) and english_hits == 0


def original_post_text(item: dict) -> str:
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    chosen = summary if len(summary) >= len(title) else title
    return chosen[:2000]


def geo_blob(*parts: object) -> str:
    text = " ".join(str(part or "") for part in parts).lower().replace("'", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _arabic_keyword(keyword: str) -> bool:
    return any("\u0600" <= char <= "\u06FF" for char in keyword)


def match_sudan_fallback(text: str) -> tuple[str, float, float] | None:
    """Longest Sudan place, including Arabic names. None when the text names none of them."""
    raw = str(text or "")
    padded = f" {geo_blob(raw)} "
    tokens = set(padded.split())
    for keyword in _SUDAN_FALLBACK_ORDER:
        if _arabic_keyword(keyword):
            if keyword in raw:
                lat, lng = SUDAN_GEO_FALLBACKS[keyword]
                return keyword, lat, lng
            continue
        if " " in keyword:
            if f" {keyword} " in padded:
                lat, lng = SUDAN_GEO_FALLBACKS[keyword]
                return keyword, lat, lng
            continue
        if keyword in tokens:
            lat, lng = SUDAN_GEO_FALLBACKS[keyword]
            return keyword, lat, lng
    return None


def _latin_place_text(text: str) -> str:
    return geo_blob(str(text or "").translate(_KURDISH_FOLD))


def match_iraq_fallback(text: str) -> tuple[str, float, float] | None:
    """Longest Iraq or Kurdistan place, including Arabic and Kurdish names."""
    raw = str(text or "")
    padded = f" {_latin_place_text(raw)} "
    tokens = set(padded.split())
    for keyword in _IRAQ_FALLBACK_ORDER:
        if _arabic_keyword(keyword):
            if keyword in raw:
                lat, lng = IRAQ_GEO_FALLBACKS[keyword]
                return keyword, lat, lng
            continue
        if keyword == "hit":
            continue
        if " " in keyword:
            if f" {keyword} " in padded:
                lat, lng = IRAQ_GEO_FALLBACKS[keyword]
                return keyword, lat, lng
            continue
        if keyword in tokens:
            lat, lng = IRAQ_GEO_FALLBACKS[keyword]
            return keyword, lat, lng
    return None


def iraq_mention(text: str) -> bool:
    """Iraq, Kurdistan, a listed place, or an Iraqi or Kurdish security term."""
    return match_iraq_fallback(text) is not None


def sudan_mention(text: str) -> bool:
    """Sudan, RSF, SAF, Darfur, Kordofan, or a listed city."""
    if match_sudan_fallback(text):
        return True
    raw = str(text or "")
    return "الدعم السريع" in raw or "القوات المسلحة السودانية" in raw


def match_geo_fallback(text: str) -> tuple[str, float, float] | None:
    """Longest place keyword in the text, or None when nothing in GEO_FALLBACKS matches."""
    padded = f" {geo_blob(text)} "
    tokens = padded.split()
    for keyword in _GEO_FALLBACK_ORDER:
        if " " in keyword:
            if f" {keyword} " in padded:
                lat, lng = GEO_FALLBACKS[keyword]
                return keyword, lat, lng
            continue
        if keyword in tokens or (
            keyword in _COUNTRY_STEMS
            and any(token.startswith(keyword) and 0 < len(token) - len(keyword) <= 4 for token in tokens)
        ):
            lat, lng = GEO_FALLBACKS[keyword]
            return keyword, lat, lng
    return None


def fallback_offset(event_id: object) -> tuple[float, float]:
    digest = hashlib.sha256(str(event_id or "event").encode("utf-8")).digest()
    lat_shift = (digest[0] - 128) / 128 * 0.04
    lng_shift = (digest[1] - 128) / 128 * 0.04
    return lat_shift, lng_shift


def offset_fallback(lat: float, lng: float, event_id: object) -> tuple[float, float] | None:
    lat_shift, lng_shift = fallback_offset(event_id)
    checked = osint.valid_coord(lng + lng_shift, lat + lat_shift)
    if checked is None:
        return None
    return checked[1], checked[0]


def _near_center(lat: float, lng: float, center: tuple[float, float], tolerance: float = 0.25) -> bool:
    return abs(lat - center[0]) <= tolerance and abs(lng - center[1]) <= tolerance


def placed_coordinates(item: dict, placed: dict) -> tuple[float | None, float | None]:
    """Use a real model point. Otherwise a place-list center with a stable tiny offset."""
    coords = osint.valid_coord(placed.get("lng"), placed.get("lat"))
    raw_blob = " ".join(
        str(part or "")
        for part in (
            placed.get("location_name"),
            placed.get("governorate"),
            item.get("title"),
            item.get("summary"),
            item.get("original_text"),
        )
    )
    event_id = item.get("source_url") or item.get("id") or item.get("title")
    if not coords and not sudan_mention(raw_blob):
        iraq_hit = match_iraq_fallback(raw_blob)
        if iraq_hit:
            shifted = offset_fallback(iraq_hit[1], iraq_hit[2], event_id)
            if shifted:
                return shifted
    if not coords:
        sudan_hit = match_sudan_fallback(raw_blob)
        if sudan_hit:
            shifted = offset_fallback(sudan_hit[1], sudan_hit[2], event_id)
            if shifted:
                return shifted
    blob = geo_blob(
        placed.get("location_name"),
        placed.get("governorate"),
        item.get("title"),
        item.get("summary"),
        item.get("original_text"),
    )
    hit = match_geo_fallback(blob)
    if coords and hit and hit[0] in _COUNTRY_OR_SEA and _near_center(coords[1], coords[0], (hit[1], hit[2])):
        shifted = offset_fallback(hit[1], hit[2], event_id)
        if shifted:
            return shifted
    if coords:
        return coords[1], coords[0]
    if hit:
        shifted = offset_fallback(hit[1], hit[2], event_id)
        if shifted:
            return shifted
    return None, None


def backfill_coordinates(now: datetime | None = None) -> int:
    """Fill recent null or 0,0 coordinates from GEO_FALLBACKS. Rows without a keyword stay unmapped."""
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - RECENT_COORD_WINDOW
    updated = 0
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT id, title, summary, original_text, location_name, governorate, source_url, "
                "published_at, ingested_at FROM items "
                "WHERE lat IS NULL OR lng IS NULL OR lat = 0 OR lng = 0"
            ).fetchall()
            for row in rows:
                stamp = parse_when(row["published_at"]) or parse_when(row["ingested_at"])
                if stamp is None or stamp < cutoff:
                    continue
                raw = " ".join(
                    str(part or "")
                    for part in (row["location_name"], row["governorate"], row["title"], row["summary"], row["original_text"])
                )
                hit = match_sudan_fallback(raw)
                if hit is None and not sudan_mention(raw):
                    hit = match_iraq_fallback(raw)
                if hit is None:
                    hit = match_geo_fallback(geo_blob(raw))
                if not hit:
                    continue
                shifted = offset_fallback(hit[1], hit[2], row["id"])
                if not shifted:
                    continue
                conn.execute(
                    "UPDATE items SET lat = ?, lng = ? WHERE id = ?",
                    (shifted[0], shifted[1], row["id"]),
                )
                updated += 1
            if updated:
                conn.commit()
                print(f"Backfilled coordinates for {updated} recent event(s).")
        finally:
            conn.close()
    return updated


def coordinate_counts(now: datetime | None = None) -> tuple[int, int]:
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - LIVE_WINDOW
    mapped = 0
    total = 0
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT lat, lng, published_at, ingested_at FROM items"
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        stamp = parse_when(row["published_at"]) or parse_when(row["ingested_at"])
        if stamp is None or stamp < cutoff:
            continue
        total += 1
        if osint.valid_coord(row["lng"], row["lat"]):
            mapped += 1
    return mapped, total


def backfill_sudan_rows() -> int:
    """Give existing Sudan rows coordinates and move Other / International Sudan rows into Sudan & Horn."""
    updated = 0
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT id, title, summary, original_text, location_name, governorate, region, lat, lng "
                "FROM items"
            ).fetchall()
            for row in rows:
                blob = " ".join(
                    str(part or "")
                    for part in (
                        row["location_name"],
                        row["governorate"],
                        row["title"],
                        row["summary"],
                        row["original_text"],
                    )
                )
                mentioned = sudan_mention(blob)
                if not mentioned and (row["region"] or "") != "Sudan & Horn":
                    continue
                missing = row["lat"] is None or row["lng"] is None or row["lat"] == 0 or row["lng"] == 0
                other = (row["region"] or "") in ("", "Other / International")
                if not missing and not other:
                    continue
                new_region = "Sudan & Horn" if mentioned or (row["region"] or "") == "Sudan & Horn" else row["region"]
                if other and not mentioned:
                    continue
                new_lat, new_lng = row["lat"], row["lng"]
                if missing and mentioned:
                    hit = match_sudan_fallback(blob)
                    shifted = offset_fallback(hit[1], hit[2], row["id"]) if hit else None
                    if shifted:
                        new_lat, new_lng = shifted
                if new_region == row["region"] and new_lat == row["lat"] and new_lng == row["lng"]:
                    continue
                conn.execute(
                    "UPDATE items SET region = ?, lat = ?, lng = ? WHERE id = ?",
                    (new_region, new_lat, new_lng, row["id"]),
                )
                updated += 1
            if updated:
                conn.commit()
                print(f"Backfilled {updated} Sudan event(s).")
        finally:
            conn.close()
    return updated


def backfill_iraq_rows() -> int:
    """Fill Iraq and Kurdistan coordinates and move Other / International rows into Iran & Iraq.
    Sudan rows are left unchanged."""
    updated = 0
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT id, title, summary, original_text, location_name, governorate, region, lat, lng "
                "FROM items"
            ).fetchall()
            for row in rows:
                if (row["region"] or "") == "Sudan & Horn":
                    continue
                blob = " ".join(
                    str(part or "")
                    for part in (
                        row["location_name"],
                        row["governorate"],
                        row["title"],
                        row["summary"],
                        row["original_text"],
                    )
                )
                if sudan_mention(blob) or not iraq_mention(blob):
                    continue
                missing = row["lat"] is None or row["lng"] is None or row["lat"] == 0 or row["lng"] == 0
                other = (row["region"] or "") in ("", "Other / International")
                if not missing and not other:
                    continue
                new_lat, new_lng = row["lat"], row["lng"]
                if missing:
                    hit = match_iraq_fallback(blob)
                    shifted = offset_fallback(hit[1], hit[2], row["id"]) if hit else None
                    if shifted:
                        new_lat, new_lng = shifted
                if "Iran & Iraq" == row["region"] and new_lat == row["lat"] and new_lng == row["lng"]:
                    continue
                conn.execute(
                    "UPDATE items SET region = ?, lat = ?, lng = ? WHERE id = ?",
                    ("Iran & Iraq", new_lat, new_lng, row["id"]),
                )
                updated += 1
            if updated:
                conn.commit()
                print(f"Backfilled {updated} Iraq event(s).")
        finally:
            conn.close()
    return updated


def drop_excluded_items() -> int:
    """Remove rows from the blocked accounts. Yah_Saree rows stay."""
    removed = 0
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute("SELECT id, source_name, source_url, source_kind FROM items").fetchall()
            ids = [
                row["id"]
                for row in rows
                if is_excluded_source(row["source_name"], row["source_url"], row["source_kind"])
            ]
            if ids:
                conn.executemany("DELETE FROM items WHERE id = ?", [(item_id,) for item_id in ids])
                conn.commit()
                removed = len(ids)
                print(f"Dropped {removed} excluded source item(s).")
        finally:
            conn.close()
    return removed


_SPAM_BOUNDARY_TERMS = (
    "dating", "single girls", "meet singles", "hookup", "escort", "onlyfans",
    "adult", "romance", "matchmaking", "bride", "flirt", "chat with girls",
    "sugar daddy", "tinder", "bumble", "badoo", "casino", "betting", "1xbet",
    "promo code", "crypto signal", "forex",
)
_SPAM_BOUNDARY_RE = re.compile(
    "|".join(
        r"\b" + r"\s+".join(re.escape(part) for part in term.split()) + r"\b"
        for term in _SPAM_BOUNDARY_TERMS
    ),
    re.IGNORECASE,
)
_SPAM_ARABIC_TERMS = ("زواج", "تعارف", "بنات", "دردشة")
_spam_purged = False


def is_spam_post(text: str) -> bool:
    """Dating, gambling, and promo spam. A place name by itself is not spam."""
    raw = str(text or "")
    if _SPAM_BOUNDARY_RE.search(raw):
        return True
    return any(token in raw for token in _SPAM_ARABIC_TERMS)


def spam_post_text(*parts: object) -> str:
    return " ".join(str(part or "") for part in parts)


def purge_spam_rows(conn: sqlite3.Connection) -> int:
    """Delete stored spam. The database file itself stays."""
    rows = conn.execute(
        "SELECT id, title, summary, original_text FROM items"
    ).fetchall()
    ids = [
        row["id"]
        for row in rows
        if is_spam_post(spam_post_text(row["title"], row["summary"], row["original_text"]))
    ]
    if not ids:
        return 0
    conn.executemany("DELETE FROM items WHERE id = ?", [(item_id,) for item_id in ids])
    print(f"Deleted {len(ids)} spam row(s) from the live database.")
    return len(ids)


def purge_spam_rows_once(conn: sqlite3.Connection) -> int:
    """Run the spam cleanup on the first database open of this process."""
    global _spam_purged
    if _spam_purged:
        return 0
    removed = purge_spam_rows(conn)
    _spam_purged = True
    return removed


def insert_item(item: dict, placed: dict, now: datetime) -> bool:
    if is_excluded_source(item.get("source_name"), item.get("source_url"), item.get("source_kind")):
        return False
    if is_spam_post(spam_post_text(item.get("title"), item.get("summary"), item.get("original_text"), item.get("source_url"))):
        print(f"Dropped spam before insert: {str(item.get('title') or '')[:80]}")
        return False
    styled_title, styled_summary = apply_house_style(
        item.get("title") or "",
        item.get("summary") or "",
        item.get("source_kind") or "",
        item.get("source_name") or "",
        item.get("source_url") or "",
    )
    item = dict(item)
    item["title"] = styled_title or item.get("title") or ""
    item["summary"] = styled_summary or item.get("summary") or ""
    blob = " ".join(
        str(part or "")
        for part in (
            placed.get("location_name"),
            placed.get("governorate"),
            item.get("title"),
            item.get("summary"),
            item.get("original_text"),
        )
    )
    if sudan_mention(blob):
        placed = dict(placed)
        placed["region"] = "Sudan & Horn"
    elif iraq_mention(blob):
        placed = dict(placed)
        placed["region"] = "Iran & Iraq"
    lat, lng = placed_coordinates(item, placed)
    source_url = item["source_url"]
    if lat is None or lng is None:
        print(f"Stored unmapped {source_url}: no place keyword.")
    moment = parse_when(item.get("published_at")) or now
    with _db_lock:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO items (
                    source_url, url_hash, headline_hash, title, summary, original_text, source_name, source_kind,
                    source_tier, region, governorate, location_name, lat, lng, severity, category,
                    corroboration_count, published_at, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_url,
                    url_hash(source_url),
                    headline_hash(item["title"]),
                    item["title"],
                    item.get("summary") or item["title"],
                    item.get("original_text") or "",
                    item.get("source_name") or "",
                    item.get("source_kind") or "",
                    source_tier_for(item.get("source_kind") or "", item.get("source_name") or "", source_url),
                    placed.get("region") or "Other / International",
                    placed.get("governorate") or "",
                    placed.get("location_name") or "",
                    lat,
                    lng,
                    osint.live_severity(placed.get("severity")),
                    osint.live_category(placed.get("category")),
                    1,
                    osint.format_timestamp(moment),
                    osint.format_timestamp(now),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()
    return True


def save_classified_item(item: dict, now: datetime) -> bool:
    """Classify one candidate and commit it before the next item."""
    if is_spam_post(spam_post_text(item.get("title"), item.get("summary"), item.get("original_text"), item.get("source_url"))):
        print(f"Dropped spam before classify: {str(item.get('title') or '')[:80]}")
        return False
    with _db_lock:
        conn = db_connect()
        try:
            if seen_url(conn, item["source_url"]):
                return False
            match = best_title_match(item["title"], recent_story_rows(conn))
            if match is not None:
                if same_channel(item, match):
                    print(f"Skipped near-duplicate before classify: {item['title'][:80]}")
                else:
                    bump_corroboration(conn, match["id"])
                    conn.commit()
                    print(f"Corroborated story {match['id']} from another channel: {item['title'][:80]}")
                return False
        finally:
            conn.close()
    if item.get("source_kind") == "x" and not keep_x_post(
        x_handle_from_item(item),
        f"{item.get('title') or ''} {item.get('summary') or ''}",
    ):
        return False
    original = original_post_text(item)
    placed = osint.locate_event(item["title"], location_name=item.get("summary") or item["title"])
    if placed.get("relevant") is False:
        print(f"Dropped non-news spam from classifier: {str(item.get('title') or '')[:80]}")
        return False
    stored = dict(item)
    stored["original_text"] = original
    english_title = osint.one_sentence(placed.get("title") or "")
    english_summary = osint.one_sentence(placed.get("summary") or "")
    if english_title and not needs_english(english_title):
        stored["title"] = english_title[:240]
    if english_summary and not needs_english(english_summary):
        stored["summary"] = english_summary[:500]
    stored["title"], stored["summary"] = apply_house_style(
        stored.get("title") or "",
        stored.get("summary") or "",
        stored.get("source_kind") or "",
        stored.get("source_name") or "",
        stored.get("source_url") or "",
    )
    return insert_item(stored, placed, now)


def store_new_items(candidates: list[dict], now: datetime, limit: int = MAX_NEW_PER_CYCLE) -> int:
    candidates.sort(
        key=lambda item: parse_when(item.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    inserted = 0
    for item in candidates:
        if inserted >= limit:
            break
        if save_classified_item(item, now):
            inserted += 1
    return inserted


def collect_fetched(calls: list) -> list[dict]:
    """Run one batch slice together so a slow channel cannot stall the others."""
    if not calls:
        return []
    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(fn) for fn in calls]
        for future in futures:
            try:
                found.extend(future.result() or [])
            except Exception as exc:
                print(f"Batch fetch failed: {exc.__class__.__name__}.")
    return found


def ingest_feeds() -> dict:
    """One rotated batch: 7 Telegram channels, 2 X handles, and 2 RSS feeds."""
    global _pacing, _pace_pending
    with _poll_lock:
        _pacing = True
        _pace_pending = False
        inserted = 0
        checked = 0
        batch_no = 1
        total_batches = 1
        try:
            with _db_lock:
                conn = db_connect()
                try:
                    batch_index = read_meta_int(conn, "batch_index")
                    x_index = read_meta_int(conn, "x_batch_index")
                    rss_index = read_meta_int(conn, "rss_batch_index")
                finally:
                    conn.close()
            telegram = rotate_slice(TELEGRAM_CHANNELS, batch_index, BATCH_SIZE)
            handles = rotate_slice(X_HANDLES, x_index, X_BATCH_SIZE)
            feeds = rotate_slice(RSS_FEEDS, rss_index, RSS_BATCH_SIZE)
            checked = len(telegram)
            total_batches = max(1, math.ceil(len(TELEGRAM_CHANNELS) / BATCH_SIZE))
            batch_no = (batch_index // BATCH_SIZE) % total_batches + 1
            now = datetime.now(timezone.utc)
            backfill_coordinates(now)
            backfill_sudan_rows()
            backfill_iraq_rows()
            drop_excluded_items()
            telegram_items = collect_fetched(
                [(lambda channel=channel: fetch_telegram_channel(channel)) for channel in telegram]
            )
            inserted += store_new_items(telegram_items, now, MAX_NEW_PER_CYCLE - inserted)
            for handle in handles:
                if inserted >= MAX_NEW_PER_CYCLE:
                    break
                try:
                    fetched = fetch_x_handle(handle)
                except Exception as exc:
                    print(f"X {handle} failed: {exc.__class__.__name__}.")
                    fetched = []
                inserted += store_new_items(fetched, now, MAX_NEW_PER_CYCLE - inserted)
            rss_items = collect_fetched(
                [(lambda name=name, url=url: fetch_rss_feed(name, url)) for name, url in feeds]
            )
            try:
                live_items = poll_aljazeera_liveblog()
            except Exception as exc:
                print(f"Al Jazeera liveblog failed: {exc.__class__.__name__}.")
                live_items = []
            if inserted < MAX_NEW_PER_CYCLE and live_items:
                inserted += store_new_items(live_items, now, MAX_NEW_PER_CYCLE - inserted)
            if inserted < MAX_NEW_PER_CYCLE:
                inserted += store_new_items(rss_items, now, MAX_NEW_PER_CYCLE - inserted)
            stamp = osint.format_timestamp(now)
            next_batch = (batch_index + BATCH_SIZE) % len(TELEGRAM_CHANNELS) if TELEGRAM_CHANNELS else 0
            next_x = (x_index + X_BATCH_SIZE) % len(X_HANDLES) if X_HANDLES else 0
            next_rss = (rss_index + RSS_BATCH_SIZE) % len(RSS_FEEDS) if RSS_FEEDS else 0
            with _db_lock:
                conn = db_connect()
                try:
                    write_meta(conn, "batch_index", str(next_batch))
                    write_meta(conn, "x_batch_index", str(next_x))
                    write_meta(conn, "rss_batch_index", str(next_rss))
                    write_last_polled(conn, stamp)
                    conn.commit()
                finally:
                    conn.close()
            print(
                f"[Poller] Batch {batch_no}/{total_batches} | Checked {checked} Telegram channels | "
                f"Ingested {inserted} new events"
            )
            return {"ok": True, "inserted": inserted, "last_polled_at": stamp}
        finally:
            _pacing = False


def run_poll() -> dict:
    return ingest_feeds()


def poll_loop() -> None:
    while True:
        try:
            ingest_feeds()
        except Exception as exc:
            print(f"Feed poll failed: {exc.__class__.__name__}.")
        time.sleep(POLL_SECONDS)


EMPTY_REGION_TEXT = "No significant incidents recorded in the past 24 hours."
SUMMARY_INTERVAL = 60 * 60
_summary_lock = threading.Lock()
_summary_started = False
_briefing_started = False
_threads_started = False
_briefing_slots: set[str] = set()
LONDON = ZoneInfo("Europe/London")
SUMMARY_24H_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overall": {"type": "string"},
        "regions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {name: {"type": "string"} for name in REGIONS},
            "required": list(REGIONS),
        },
    },
    "required": ["overall", "regions"],
}


def grouped_24h(now: datetime) -> dict[str, list[dict]]:
    cutoff = now - timedelta(hours=24)
    groups = {name: [] for name in REGIONS}
    for feature in combined_features():
        props = feature.get("properties") or {}
        moment = osint.parse_timestamp(props.get("timestamp"))
        if moment is None or moment < cutoff:
            continue
        groups[region_name(props.get("region"))].append(
            {
                "headline": osint.one_sentence(props.get("title") or props.get("summary"))[:240],
                "location": osint.one_sentence(props.get("location_name") or props.get("governorate"))[:120],
                "severity": osint.live_severity(props.get("severity")),
                "corroboration": int(props.get("corroboration_count") or 1),
            }
        )
    for name in groups:
        groups[name] = groups[name][:20]
    return groups


def save_summaries(overall: str, regions: dict[str, str], stamp: str) -> None:
    with _db_lock:
        conn = db_connect()
        try:
            rows = [("overall", stamp, overall)] + [(name, stamp, regions[name]) for name in REGIONS]
            conn.executemany(
                "INSERT INTO summary_cache (scope, updated_at, summary) VALUES (?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET updated_at = excluded.updated_at, summary = excluded.summary",
                rows,
            )
            conn.commit()
        finally:
            conn.close()


def read_summaries() -> dict:
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute("SELECT scope, updated_at, summary FROM summary_cache").fetchall()
        finally:
            conn.close()
    if not rows:
        return {"overall": "", "regions": {name: "" for name in REGIONS}, "updated_at": ""}
    regions = {name: EMPTY_REGION_TEXT for name in REGIONS}
    overall = EMPTY_REGION_TEXT
    updated = ""
    for row in rows:
        text = " ".join(str(row["summary"] or "").split())
        scope = row["scope"]
        if scope == "overall" and text:
            overall = text
        elif scope in regions and text:
            regions[scope] = text
        stamp = " ".join(str(row["updated_at"] or "").split())
        if stamp > updated:
            updated = stamp
    return {"overall": overall, "regions": regions, "updated_at": updated}


def summaries_due() -> bool:
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT updated_at FROM summary_cache WHERE scope = 'overall'").fetchone()
        finally:
            conn.close()
    if not row or not str(row["updated_at"] or "").strip():
        return True
    moment = osint.parse_timestamp(row["updated_at"])
    if moment is None:
        return True
    return datetime.now(timezone.utc) - moment >= timedelta(minutes=60)


def call_24h_summary(grouped: dict[str, list[dict]], now: datetime, key: str) -> tuple[str, dict[str, str]]:
    prompt = (
        "Write a 24-hour geopolitical summary in English. Use only the grouped incidents below. "
        "Do not use live search and do not add events that are not listed. "
        "Use the terms Ansarallah, coalition (Saudi Arabia), government forces, and truce. "
        "Attribute state media and ISW assessments to the outlet. "
        "Do not add timing or lateness markers such as late or hours later.\n"
        f"As of {osint.format_timestamp(now)} UTC.\n"
        "overall is 2-4 sentences for the whole theater.\n"
        "regions has one short paragraph for each of these keys exactly: "
        + ", ".join(REGIONS)
        + ".\n"
        "If a region's list is empty, its text must be exactly: "
        + EMPTY_REGION_TEXT
        + "\n\n"
        + json.dumps(grouped, ensure_ascii=False)
    )
    response = osint.make_client(key).responses.create(
        model=MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "summary_24h",
                "schema": SUMMARY_24H_SCHEMA,
                "strict": True,
            }
        },
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout=TIMEOUT,
    )
    text = osint.response_text(response)
    if not text:
        raise RuntimeError("xAI response did not include output text")
    payload = osint.parse_json_object(text)
    overall = strip_lateness(" ".join(str(payload.get("overall") or "").split()))
    if not overall:
        raise RuntimeError("xAI response had no overall summary")
    raw_regions = payload.get("regions") if isinstance(payload.get("regions"), dict) else {}
    regions = {}
    for name in REGIONS:
        note = strip_lateness(" ".join(str(raw_regions.get(name) or "").split()))
        if not grouped.get(name) or not note:
            note = EMPTY_REGION_TEXT
        regions[name] = note
    return overall, regions


def generate_24h_summaries() -> dict:
    """One grok-3-mini call. A missing key stores the fallback sentence and does not edit hotspot or brief files."""
    with _summary_lock:
        now = datetime.now(timezone.utc)
        grouped = grouped_24h(now)
        stamp = osint.format_timestamp(now)
        key = osint.read_api_key()
        fallback = False
        if not key:
            print(
                "XAI_API_KEY is missing. Wrote fallback 24h summaries. "
                "daily_brief.json and hotspots.json were not changed."
            )
            overall, regions = EMPTY_REGION_TEXT, {name: EMPTY_REGION_TEXT for name in REGIONS}
            fallback = True
        else:
            try:
                overall, regions = call_24h_summary(grouped, now, key)
            except Exception as exc:
                print(
                    f"24h summary failed: {osint.public_error(exc, key)}. "
                    "Wrote fallback sentences. daily_brief.json and hotspots.json were not changed."
                )
                overall, regions = EMPTY_REGION_TEXT, {name: EMPTY_REGION_TEXT for name in REGIONS}
                fallback = True
        save_summaries(overall, regions, stamp)
        payload = {"overall": overall, "regions": regions, "updated_at": stamp, "fallback": fallback}
        print(f"Saved 24h summaries at {stamp}.")
        return payload


def summary_loop() -> None:
    try:
        if summaries_due():
            generate_24h_summaries()
    except Exception as exc:
        print(f"24h summary failed: {exc.__class__.__name__}.")
    while True:
        time.sleep(SUMMARY_INTERVAL)
        try:
            generate_24h_summaries()
        except Exception as exc:
            print(f"24h summary failed: {exc.__class__.__name__}.")


def start_summary_daemon() -> None:
    global _summary_started
    with _start_lock:
        if _summary_started:
            return
        _summary_started = True
    threading.Thread(target=summary_loop, name="summary-24h", daemon=True).start()


def start_poller() -> None:
    global _poll_started
    with _start_lock:
        if _poll_started:
            return
        _poll_started = True
    threading.Thread(target=poll_loop, name="feed-poller", daemon=True).start()


def firms_key() -> str:
    osint.load_dotenv(osint.ROOT / ".env")
    return (os.environ.get("FIRMS_MAP_KEY") or "").strip()


def firms_unconfigured(key: str) -> bool:
    cleaned = key.strip()
    if not cleaned or cleaned in FIRMS_PLACEHOLDERS:
        return True
    return cleaned.lower().startswith("your_")


def parse_firms_csv(text: str) -> list[dict]:
    points = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue

        def number(name: str) -> float | None:
            raw = " ".join(str(row.get(name) or "").split())
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        points.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "bright_ti4": number("bright_ti4"),
                "frp": number("frp"),
                "acq_date": " ".join(str(row.get("acq_date") or "").split()),
                "acq_time": " ".join(str(row.get("acq_time") or "").split()),
                "confidence": " ".join(str(row.get("confidence") or "").split()),
            }
        )
    return points


def thermal_payload() -> dict:
    key = firms_key()
    if firms_unconfigured(key):
        return {
            "ok": False,
            "configured": False,
            "status": (
                "NASA FIRMS is not configured. Add a real FIRMS_MAP_KEY to .env. "
                "Satellite heat was not requested."
            ),
            "points": [],
        }
    now = time.monotonic()
    with _thermal_lock:
        cached = _thermal_cache.get("points")
        cached_at = float(_thermal_cache.get("at") or 0)
    if isinstance(cached, list) and now - cached_at < FIRMS_CACHE_SECONDS:
        return {"ok": True, "configured": True, "status": "cached", "points": cached}
    try:
        response = requests.get(
            FIRMS_URL.format(key=key),
            timeout=FIRMS_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        points = parse_firms_csv(response.text)
    except Exception as exc:
        print(f"FIRMS fetch failed: {exc.__class__.__name__}.")
        return {
            "ok": False,
            "configured": True,
            "status": "NASA FIRMS could not be reached.",
            "points": [],
        }
    with _thermal_lock:
        _thermal_cache["at"] = time.monotonic()
        _thermal_cache["points"] = points
    return {"ok": True, "configured": True, "status": "ok", "points": points}


_feed_health_lock = threading.Lock()
_feed_health: dict[str, dict] = {}


def note_feed(kind: str, name: str, ok: bool, error: str = "") -> None:
    key = f"{kind}:{name}"
    with _feed_health_lock:
        _feed_health[key] = {
            "ok": bool(ok),
            "error": "" if ok else " ".join(str(error or "error").split())[:160],
        }


def feed_health_payload() -> dict:
    catalog = [("telegram", channel) for channel in TELEGRAM_CHANNELS]
    catalog.extend(("x", handle) for handle in X_HANDLES)
    catalog.extend(("rss", name) for name, _url in RSS_FEEDS)
    with _feed_health_lock:
        recorded = dict(_feed_health)
    failing = []
    online = 0
    for kind, name in catalog:
        row = recorded.get(f"{kind}:{name}")
        if row and row.get("ok"):
            online += 1
            continue
        if row and not row.get("ok"):
            failing.append(
                {
                    "kind": kind,
                    "name": name,
                    "label": source_label(kind, name),
                    "error": row.get("error") or "error",
                }
            )
    return {"online": online, "total": len(catalog), "failing": failing}


def trend_label(percent: int) -> dict:
    if percent > 0:
        return {"percent": percent, "direction": "up", "label": f"▲ +{percent}%"}
    if percent < 0:
        return {"percent": percent, "direction": "down", "label": f"▼ {percent}%"}
    return {"percent": 0, "direction": "flat", "label": "0%"}


def region_escalation(now: datetime | None = None) -> dict[str, dict]:
    """Compare each region's latest UTC day with its 7-day daily average."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    latest_day = moment.date()
    start_day = latest_day - timedelta(days=6)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
    series = {name: [0] * 7 for name in REGIONS}
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT region, published_at, ingested_at FROM items "
                "WHERE COALESCE(published_at, ingested_at, '') >= ?",
                (start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        stamp = parse_when(row["published_at"]) or parse_when(row["ingested_at"])
        if stamp is None:
            continue
        stamp = stamp.astimezone(timezone.utc)
        offset = (stamp.date() - start_day).days
        if offset < 0 or offset > 6:
            continue
        series[region_name(row["region"])][offset] += 1
    trends = {}
    for name in REGIONS:
        days = series[name]
        baseline = sum(days) / 7
        latest = days[-1]
        if baseline <= 0:
            percent = 100 if latest > 0 else 0
        else:
            percent = int(round((latest - baseline) / baseline * 100))
        trends[name] = trend_label(percent)
    return trends


def data_payload() -> dict:
    now = datetime.now(timezone.utc)
    features = [feature for feature in combined_features() if within_live_window(feature, now)]
    events = live_events_from(features)
    with _db_lock:
        conn = db_connect()
        try:
            last_polled_at = read_last_polled(conn)
        finally:
            conn.close()
    mapped_count, total_count = coordinate_counts(now)
    return {
        "events": events,
        "features": features,
        "counts_by_region": region_counts(events),
        "counts_by_governorate": governorate_counts(events),
        "summary": read_cache(),
        "summaries": read_summaries(),
        "last_polled_at": last_polled_at,
        "feed_health": feed_health_payload(),
        "region_trends": region_escalation(now),
        "briefing_window": format_briefing_window(*briefing_window("daily", now)),
        "mapped_count": mapped_count,
        "total_count": total_count,
    }


def fallback_summary(counts: dict[str, int]) -> dict:
    parts = [f"{name} {counts[name]}" for name in REGIONS]
    notes = [
        {"region": name, "note": f"{name}: {counts[name]} live incident(s)."}
        for name in REGIONS
    ]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "text": "Analysis is unavailable. Live incident counts: " + ", ".join(parts) + ".",
        "regional_notes": notes,
        "fallback": True,
    }


def summary_prompt(events: list[dict], counts: dict[str, int], now: datetime) -> str:
    sample = events[:40]
    return (
        "Write a concise real-time geopolitical situation summary in English. "
        "Use only the live incidents and counts below. Do not use live search and do not add events "
        "that are not in this list. "
        "Use the terms Ansarallah, coalition (Saudi Arabia), government forces, and truce. "
        "Attribute state media and ISW assessments to the outlet. "
        "Do not add timing or lateness markers such as late or hours later.\n"
        f"As of {osint.format_timestamp(now)} UTC.\n"
        "Cover " + ", ".join(REGIONS) + ".\n"
        "summary is 2-4 sentences for the whole theater.\n"
        "regional_notes is one short sentence per region whose count is above zero. "
        "Use the region name exactly. Omit regions with a count of 0.\n\n"
        + json.dumps({"counts_by_region": counts, "events": sample}, ensure_ascii=False)
    )


def call_summary(client, prompt: str, now: datetime) -> dict:
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "geopolitical_summary",
                "schema": SUMMARY_SCHEMA,
                "strict": True,
            }
        },
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout=TIMEOUT,
    )
    text = osint.response_text(response)
    if not text:
        raise RuntimeError("xAI response did not include output text")
    payload = osint.parse_json_object(text)
    summary = strip_lateness(" ".join(str(payload.get("summary") or "").split()))
    if not summary:
        raise RuntimeError("xAI response had no summary")
    notes = []
    for item in payload.get("regional_notes") or []:
        if not isinstance(item, dict):
            continue
        region = " ".join(str(item.get("region") or "").split())
        note = strip_lateness(" ".join(str(item.get("note") or "").split()))
        if region and note:
            notes.append({"region": region, "note": note})
    return {
        "generated_at": osint.format_timestamp(now),
        "text": summary,
        "regional_notes": notes,
        "fallback": False,
    }


def refresh_summary() -> dict:
    return generate_24h_summaries()


def blocked_static(name: str) -> bool:
    db_name = Path(DB_PATH).name
    return name == ".env" or name == db_name or name.endswith(".sqlite") or name.endswith(".db")


def thermal_response() -> dict:
    try:
        return thermal_payload()
    except Exception as exc:
        print(f"GET /api/thermal failed: {exc.__class__.__name__}.")
        return {
            "ok": False,
            "configured": False,
            "status": "Satellite heat is unavailable.",
            "points": [],
        }


def data_response() -> dict:
    try:
        return data_payload()
    except Exception as exc:
        print(f"GET /api/data failed: {exc.__class__.__name__}")
        return {
            "events": [],
            "features": [],
            "counts_by_region": {name: 0 for name in REGIONS},
            "counts_by_governorate": {name: 0 for name in osint.SYRIA_GOVERNORATES},
            "summary": read_cache(),
            "summaries": {"overall": "", "regions": {name: "" for name in REGIONS}, "updated_at": ""},
            "last_polled_at": None,
            "region_trends": {name: trend_label(0) for name in REGIONS},
            "briefing_window": "",
            "mapped_count": 0,
            "total_count": 0,
        }


def data_post_response(raw: bytes) -> dict:
    """Return the live payload. {"view":"regional"} selects the regional overview."""
    view = ""
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {}
    if isinstance(body, dict):
        view = str(body.get("view") or "").strip().lower()
    payload = data_response()
    if view == "regional":
        payload["view"] = "regional"
    return payload


def report_response(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    report_type = str(payload.get("type") or "").strip().lower() if isinstance(payload, dict) else ""
    try:
        sent = generate_and_email_periodic_report(report_type)
    except Exception as exc:
        print(f"POST /api/report failed: {exc.__class__.__name__}.")
        sent = False
    return {"ok": True, "type": report_type, "result": "sent" if sent else "skipped"}


def _poll_background() -> None:
    try:
        ingest_feeds()
    except Exception as exc:
        print(f"POST /api/poll-now failed: {exc.__class__.__name__}.")


def poll_response() -> dict:
    threading.Thread(target=_poll_background, name="poll-now", daemon=True).start()
    return {"status": "started"}


def summary_response() -> dict:
    try:
        summary = refresh_summary()
    except Exception as exc:
        print(f"POST /api/summary failed: {exc.__class__.__name__}. Returned a fallback summary.")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {
            "overall": EMPTY_REGION_TEXT,
            "regions": {name: EMPTY_REGION_TEXT for name in REGIONS},
            "updated_at": stamp,
            "fallback": True,
        }
    return {"summaries": summary, "fallback": bool(summary.get("fallback"))}


def apply_english_fields(title: str, summary: str, translated: dict) -> tuple[str, str] | None:
    new_title = osint.one_sentence(translated.get("title"))[:240]
    new_summary = osint.one_sentence(translated.get("summary"))[:500]
    updated_title = title
    updated_summary = summary
    changed = False
    if needs_english(title) and new_title and not needs_english(new_title):
        updated_title = new_title
        changed = True
    if needs_english(summary) and new_summary and not needs_english(new_summary):
        updated_summary = new_summary
        changed = True
    if not changed:
        return None
    return updated_title, updated_summary or updated_title


def translate_existing_rows() -> int:
    """Rewrite stored non-English titles and summaries. Leaves rows unchanged when the key is missing."""
    key = osint.read_api_key()
    if not key:
        print("XAI_API_KEY is missing. Translation of existing non-English rows was skipped.")
        return 0
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute("SELECT id, title, summary, original_text FROM items").fetchall()
        finally:
            conn.close()
    updated = 0
    for row in rows:
        title = str(row["title"] or "")
        summary = str(row["summary"] or "")
        if not needs_english(title) and not needs_english(summary):
            continue
        translated = osint.translate_post(title, summary)
        if not translated:
            continue
        applied = apply_english_fields(title, summary, translated)
        if applied is None:
            print(f"Translation left item {row['id']} unchanged.")
            continue
        english_title, english_summary = applied
        original = str(row["original_text"] or "").strip()
        if not original:
            original = original_post_text({"title": title, "summary": summary})
        with _db_lock:
            conn = db_connect()
            try:
                conn.execute(
                    "UPDATE items SET title = ?, summary = ?, original_text = ? WHERE id = ?",
                    (english_title, english_summary, original, row["id"]),
                )
                conn.commit()
            finally:
                conn.close()
        updated += 1
        print(f"Translated item {row['id']} into English.")
    return updated


def start_translation_pass() -> None:
    threading.Thread(target=translate_existing_rows, name="translate-existing", daemon=True).start()


def start_background() -> None:
    """Open the database and start ingest plus the London mailer once per process."""
    global _threads_started
    with _start_lock:
        if _threads_started:
            return
        with _db_lock:
            conn = db_connect()
            conn.close()
        backfill_coordinates()
        backfill_sudan_rows()
        backfill_iraq_rows()
        drop_excluded_items()
        _threads_started = True
    start_translation_pass()
    start_poller()
    start_summary_daemon()
    start_briefing_daemon()


class MapHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _route(self) -> str:
        return self.path.split("?", 1)[0]

    def _blocked(self) -> bool:
        return blocked_static(self._route().rsplit("/", 1)[-1])

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self._blocked():
            self.send_error(404)
            return
        route = self._route()
        if route == "/api/thermal":
            self._json(thermal_response())
            return
        if route == "/api/data":
            self._json(data_response())
            return
        super().do_GET()

    def do_POST(self) -> None:
        route = self._route()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 1_000_000)) if length else b""
        if route == "/api/data":
            self._json(data_post_response(raw))
            return
        if route == "/api/report":
            self._json(report_response(raw))
            return
        if route == "/api/poll-now":
            self._json(poll_response())
            return
        if route != "/api/summary":
            self.send_error(404)
            return
        self._json(summary_response())

    def log_message(self, fmt: str, *args) -> None:
        super().log_message(fmt, *args)


def severity_color(value: object) -> str:
    rank = str(value or "").strip().lower()
    if rank == "high":
        return "#ff4d6d"
    if rank == "low":
        return "#19d3b8"
    return "#ffb703"


def dark_map_png() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (800, 480), (10, 17, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_period_map_png(events: list[dict]) -> bytes:
    """Draw the briefing map. A tile failure still returns a dark PNG."""
    try:
        from staticmap import CircleMarker, StaticMap

        canvas = StaticMap(
            800,
            480,
            url_template="https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
            tile_request_timeout=8,
            delay_between_retries=0,
        )
        for event in events:
            lng = event.get("lng")
            lat = event.get("lat")
            if lng is None or lat is None:
                continue
            canvas.add_marker(CircleMarker((float(lng), float(lat)), severity_color(event.get("severity")), 8))
        image = canvas.render(zoom=4, center=(48.0, 27.5))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        print(f"Briefing map tiles failed: {exc.__class__.__name__}. Using a dark fallback image.")
        return dark_map_png()


def briefing_window(report_type: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    moment = now.astimezone(LONDON) if now is not None else datetime.now(LONDON)
    end = moment.replace(hour=7, minute=0, second=0, microsecond=0)
    if moment < end:
        end -= timedelta(days=1)
    if report_type == "daily":
        start = end - timedelta(days=1)
    elif report_type == "weekly":
        thursday = (end - timedelta(days=(end.weekday() - 3) % 7)).replace(
            hour=7, minute=0, second=0, microsecond=0
        )
        start = thursday - timedelta(days=7) if thursday == end else thursday
    elif report_type == "monthly":
        first = end.replace(day=1, hour=7, minute=0, second=0, microsecond=0)
        if first == end:
            start = (first - timedelta(days=1)).replace(day=1, hour=7, minute=0, second=0, microsecond=0)
        else:
            start = first
    else:
        raise ValueError(report_type)
    return start, end


def format_briefing_window(start: datetime, end: datetime) -> str:
    pattern = "%d %b %Y, %H:%M"
    return f"{start.strftime(pattern)} → {end.strftime(pattern)} Europe/London"


def event_source(props: dict) -> str:
    labeled = str(props.get("source_label") or "")
    if " · " in labeled:
        return labeled
    handle = str(props.get("source_handle") or props.get("handle") or props.get("account") or "").lstrip("@")
    kind = str(props.get("source_kind") or "")
    if kind:
        return source_label(kind, handle)
    if handle:
        return f"X · @{handle}"
    return "Live"


def period_events(start: datetime, end: datetime) -> list[dict]:
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    found = []
    for feature in combined_features():
        props = feature.get("properties") or {}
        moment = osint.parse_timestamp(props.get("timestamp"))
        if moment is None or moment < start_utc or moment >= end_utc:
            continue
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        lng = lat = None
        if isinstance(coords, list) and len(coords) >= 2:
            placed = osint.valid_coord(coords[0], coords[1])
            if placed:
                lng, lat = placed
        found.append(
            {
                "title": osint.one_sentence(props.get("title") or props.get("summary"))[:240],
                "summary": osint.one_sentence(props.get("summary") or props.get("title"))[:500],
                "location_name": osint.one_sentence(props.get("location_name"))[:160],
                "governorate": osint.match_governorate(props.get("governorate"))
                or osint.match_governorate(props.get("location_name")),
                "region": region_name(props.get("region")),
                "severity": osint.live_severity(props.get("severity")),
                "corroboration": int(props.get("corroboration_count") or 1),
                "source": event_source(props),
                "lat": lat,
                "lng": lng,
                "timestamp": moment,
            }
        )
    found.sort(key=lambda item: item["timestamp"], reverse=True)
    return found


def smtp_settings() -> dict | None:
    osint.load_dotenv(osint.ROOT / ".env")
    host = (os.environ.get("SMTP_HOST") or "").strip()
    sender = (os.environ.get("SMTP_FROM") or "").strip()
    recipient = (os.environ.get("SMTP_TO") or "").strip()
    if not host or not sender or not recipient:
        return None
    try:
        port = int((os.environ.get("SMTP_PORT") or "587").strip() or "587")
    except ValueError:
        port = 587
    return {
        "host": host,
        "port": port,
        "user": (os.environ.get("SMTP_USER") or "").strip(),
        "password": os.environ.get("SMTP_PASSWORD") or "",
        "sender": sender,
        "recipient": recipient,
    }


BRIEFING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overall": {"type": "string"},
        "regions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {name: {"type": "string"} for name in REGIONS},
            "required": list(REGIONS),
        },
        "syria": {"type": "string"},
    },
    "required": ["overall", "regions", "syria"],
}


def briefing_narrative(events: list[dict], report_type: str, start: datetime, end: datetime) -> dict:
    fallback = {
        "overall": "Analysis is unavailable for this briefing window.",
        "regions": {name: "No regional summary is available." for name in REGIONS},
        "syria": "No Syria field summary is available.",
    }
    key = osint.read_api_key()
    if not key:
        print(
            "XAI_API_KEY is missing. The briefing will use a short fallback. "
            "daily_brief.json and hotspots.json were not changed."
        )
        return fallback
    sample = [
        {
            "title": item["title"],
            "location": item["location_name"],
            "region": item["region"],
            "governorate": item["governorate"],
            "severity": item["severity"],
            "corroboration": item["corroboration"],
        }
        for item in events[:40]
    ]
    prompt = (
        "Write an English OSINT briefing from only the incidents below. "
        "Do not use live search and do not add events that are not listed. "
        "Use the terms Ansarallah, coalition (Saudi Arabia), government forces, and truce. "
        "Attribute state media and ISW assessments to the outlet. "
        "Do not add timing or lateness markers such as late or hours later.\n"
        f"{report_type} window {format_briefing_window(start, end)}.\n"
        "overall is 2-4 sentences. regions has one short paragraph for each region key. "
        "syria is a short field summary of Syrian governorates mentioned.\n\n"
        + json.dumps({"regions": list(REGIONS), "incidents": sample}, ensure_ascii=False)
    )
    try:
        response = osint.make_client(key).responses.create(
            model=MODEL,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "periodic_briefing",
                    "schema": BRIEFING_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=MAX_OUTPUT_TOKENS,
            timeout=TIMEOUT,
        )
        text = osint.response_text(response)
        payload = osint.parse_json_object(text) if text else None
        if not isinstance(payload, dict):
            raise RuntimeError("xAI response was not an object")
        overall = strip_lateness(" ".join(str(payload.get("overall") or "").split())) or fallback["overall"]
        raw_regions = payload.get("regions") if isinstance(payload.get("regions"), dict) else {}
        regions = {}
        for name in REGIONS:
            regions[name] = strip_lateness(" ".join(str(raw_regions.get(name) or "").split())) or fallback["regions"][name]
        syria = strip_lateness(" ".join(str(payload.get("syria") or "").split())) or fallback["syria"]
        return {"overall": overall, "regions": regions, "syria": syria}
    except Exception as exc:
        print(f"Briefing summary failed: {osint.public_error(exc, key)}. Using a short fallback.")
        return fallback


def briefing_html(report_type: str, start: datetime, end: datetime, events: list[dict], narrative: dict) -> str:
    counts = {name: 0 for name in REGIONS}
    governorates = {name: 0 for name in osint.SYRIA_GOVERNORATES}
    for event in events:
        counts[region_name(event.get("region"))] += 1
        if event.get("governorate") in governorates:
            governorates[event["governorate"]] += 1
    widest = max(counts.values() or [1]) or 1
    bars = []
    for name in REGIONS:
        width = round(counts[name] / widest * 100)
        bars.append(
            "<div style=\"margin:8px 0;\">"
            f"<div style=\"color:#d5e4e8;font-size:13px;\">{html.escape(name)} {counts[name]}</div>"
            "<div style=\"background:#1c2229;border-radius:999px;height:10px;\">"
            f"<div style=\"width:{width}%;height:10px;border-radius:999px;background:linear-gradient(90deg,#F3C64F 0%,#e09f3e 55%,#ff6b6b 100%);\"></div>"
            "</div></div>"
        )
    region_notes = []
    for name in REGIONS:
        region_notes.append(
            "<h3 style=\"color:#F3C64F;font-size:15px;margin:16px 0 6px;\">"
            f"{html.escape(name)}</h3><p style=\"margin:0;\">{html.escape(narrative['regions'][name])}</p>"
        )
    gov_lines = "".join(
        f"<li>{html.escape(name)}: {count}</li>" for name, count in governorates.items()
    )
    highlighted = [
        item for item in events if str(item.get("severity") or "").lower() == "high" or int(item.get("corroboration") or 1) > 1
    ]
    highlighted = highlighted[:8]
    if highlighted:
        incident_html = []
        for item in highlighted:
            if item.get("lat") is None:
                coords = "Coordinates unavailable"
            else:
                coords = f"{item['lat']:.4f}, {item['lng']:.4f}"
            summary_line = ""
            if str(item.get("summary") or "").strip():
                summary_line = f"{html.escape(item.get('summary') or '')}<br>"
            incident_html.append(
                "<div style=\"border-top:1px solid #1c2229;padding:8px 0;\">"
                f"<strong>{html.escape(item.get('title') or 'Incident')}</strong><br>"
                f"{summary_line}"
                f"{html.escape(item.get('location_name') or 'Location unavailable')}<br>"
                f"{html.escape(coords)}<br>"
                "<span style=\"display:inline-block;margin-top:4px;color:#F3C64F;border:1px solid #F3C64F;"
                f"border-radius:999px;padding:1px 8px;font-size:12px;\">{html.escape(item.get('source') or 'Live')}</span>"
                "</div>"
            )
        incidents = "".join(incident_html)
    else:
        incidents = "<p>No high-severity or corroborated incidents in this window.</p>"
    title = f"{report_type.upper()} OSINT INTELLIGENCE BRIEFING"
    return (
        "<html><body style=\"margin:0;background:#07090b;color:#e7eef0;font-family:Segoe UI,sans-serif;\">"
        "<div style=\"max-width:720px;margin:0 auto;padding:24px;\">"
        + briefing_banner()
        + "<div style=\"background:#0c1014;border:1px solid #1c2229;border-radius:12px;padding:16px 18px;\">"
        f"<h1 style=\"color:#F3C64F;font-size:20px;margin:0 0 8px;\">{html.escape(title)}</h1>"
        f"<p style=\"margin:0 0 12px;color:#d5e4e8;\">{html.escape(format_briefing_window(start, end))}</p>"
        "<img src=\"cid:incident_map\" alt=\"Incident map\" style=\"width:100%;border-radius:12px;\" />"
        "<h2 style=\"color:#F3C64F;font-size:16px;\">Regions</h2>"
        + "".join(bars)
        + f"<h2 style=\"color:#F3C64F;font-size:16px;\">Overall</h2><p>{html.escape(narrative['overall'])}</p>"
        + "".join(region_notes)
        + "<h2 style=\"color:#F3C64F;font-size:16px;\">Syria</h2>"
        + f"<p>{html.escape(narrative['syria'])}</p><ul>{gov_lines}</ul>"
        + "<h2 style=\"color:#F3C64F;font-size:16px;\">High-severity and corroborated incidents</h2>"
        + incidents
        + "</div></div></body></html>"
    )


def logo_path() -> Path:
    return ROOT / "static" / "logo.png"


def briefing_banner() -> str:
    if logo_path().is_file():
        return (
            "<div style=\"background:#000000;text-align:center;padding:14px 12px;\">"
            "<img src=\"cid:georisk_logo\" alt=\"GeoRiskIntel\" height=\"34\" "
            "style=\"height:34px;display:block;margin:0 auto;\" />"
            "</div>"
        )
    return (
        "<div style=\"background:#000000;text-align:center;padding:14px 12px;"
        "color:#F3C64F;font-size:22px;font-weight:700;letter-spacing:0.04em;\">GeoRiskIntel</div>"
    )


def briefing_subject(report_type: str, start: datetime, end: datetime) -> str:
    label = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}[report_type]
    return f"[GeoRiskIntel] {label} Geopolitical & OSINT Briefing — {format_briefing_window(start, end)}"


def send_briefing_message(subject: str, body: str, png: bytes, settings: dict) -> bool:
    message = MIMEMultipart("related")
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = settings["recipient"]
    message.attach(MIMEText(body, "html", "utf-8"))
    if logo_path().is_file():
        logo = MIMEImage(logo_path().read_bytes(), _subtype="png")
        logo.add_header("Content-ID", "<georisk_logo>")
        logo.add_header("Content-Disposition", "inline", filename="logo.png")
        message.attach(logo)
    image = MIMEImage(png, _subtype="png")
    image.add_header("Content-ID", "<incident_map>")
    image.add_header("Content-Disposition", "inline", filename="incident_map.png")
    message.attach(image)
    try:
        if settings["port"] == 465:
            smtp = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30)
        else:
            smtp = smtplib.SMTP(settings["host"], settings["port"], timeout=30)
        with smtp:
            smtp.ehlo()
            if settings["port"] != 465:
                smtp.starttls()
                smtp.ehlo()
            if settings["user"]:
                smtp.login(settings["user"], settings["password"])
            smtp.send_message(message)
    except Exception as exc:
        print(f"Briefing email failed: {exc.__class__.__name__}.")
        return False
    return True


def generate_and_email_periodic_report(report_type: str) -> bool:
    kind = str(report_type or "").strip().lower()
    if kind not in {"daily", "weekly", "monthly"}:
        print(f"Unknown briefing type. Skipped sending.")
        return False
    settings = smtp_settings()
    if settings is None:
        print(f"SMTP is not configured. Skipped sending the {kind} OSINT briefing.")
        return False
    start, end = briefing_window(kind)
    events = period_events(start, end)
    png = render_period_map_png(events)
    narrative = briefing_narrative(events, kind, start, end)
    body = briefing_html(kind, start, end, events, narrative)
    subject = briefing_subject(kind, start, end)
    sent = send_briefing_message(subject, body, png, settings)
    if sent:
        print(f"Sent the {kind} OSINT briefing. daily_brief.json and hotspots.json were not changed.")
    return sent


def briefing_already_sent(slot: str) -> bool:
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT value FROM feed_meta WHERE key = ?", (slot,)).fetchone()
        finally:
            conn.close()
    return bool(row and str(row["value"]) == "sent")


def mark_briefing_sent(slot: str) -> None:
    with _db_lock:
        conn = db_connect()
        try:
            write_meta(conn, slot, "sent")
            conn.commit()
        finally:
            conn.close()


def maybe_send_briefings() -> None:
    now = datetime.now(LONDON)
    if not (now.hour == 7 and now.minute < 5):
        return
    day = now.strftime("%Y-%m-%d")
    jobs = [("daily", f"briefing-daily-{day}")]
    if now.weekday() == 3:
        jobs.append(("weekly", f"briefing-weekly-{day}"))
    if now.day == 1:
        jobs.append(("monthly", f"briefing-monthly-{now.strftime('%Y-%m')}"))
    for report_type, slot in jobs:
        if slot in _briefing_slots or briefing_already_sent(slot):
            _briefing_slots.add(slot)
            continue
        _briefing_slots.add(slot)
        try:
            sent = generate_and_email_periodic_report(report_type)
        except Exception as exc:
            print(f"Briefing {report_type} failed: {exc.__class__.__name__}.")
            sent = False
        if sent:
            mark_briefing_sent(slot)


def briefing_loop() -> None:
    while True:
        try:
            maybe_send_briefings()
        except Exception as exc:
            print(f"Briefing scheduler failed: {exc.__class__.__name__}.")
        time.sleep(20)


def start_briefing_daemon() -> None:
    global _briefing_started
    with _start_lock:
        if _briefing_started:
            return
        _briefing_started = True
    threading.Thread(target=briefing_loop, name="briefing-mail", daemon=True).start()


def main() -> None:
    start_background()
    server = ThreadingHTTPServer((HOST, PORT), MapHandler)
    print(f"Serving map and summary API on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
