#!/usr/bin/env python3
"""Serve the static map, the summary API, and live RSS/Telegram ingestion on port 8000.

POST /api/summary makes one xAI call (grok-3-mini, timeout 25, no live search)
for the completed Europe/London 07:00–07:00 window already stored in the database.
GET /api/data returns live events, six region counts, the cached summary, and last_polled_at.
POST /api/data with {"view":"regional"} returns that same live payload for the regional overview.
POST /api/poll-now starts one ingest batch in the background and returns immediately.
GET /api/daily-chart returns the 07:00 Europe/London incident chart.
GET /api/daily-chart.png returns that chart as a PNG.
Ingest never writes hotspots.json. The 07:00 summary writes daily_brief.json only after Grok returns.
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
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, unquote
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

import fetch_x_osint as osint

mimetypes.add_type("application/manifest+json", ".webmanifest")

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
FETCH_TIMEOUT = 10
FEED_TIMEOUT = 10
FEED_ACCEPT = "application/rss+xml, application/xml, text/xml, */*;q=0.8"
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
    ("Al Arabiya English", "https://english.alarabiya.net/tools/rss"),
    ("The National", "https://www.thenationalnews.com/arc/outboundfeeds/rss/"),
    ("Jordan Times", "https://www.jordantimes.com/rss.xml"),
    ("Dabanga Sudan", "https://www.dabangasudan.org/en/all-news/feed"),
    ("Sudan Tribune", "https://sudantribune.com/feed/"),
    ("Radio Dabanga", "https://www.dabangasudan.org/en/feed"),
    ("Darfur24", "https://darfur24.com/en/feed/"),
    ("Al Taghyeer", "https://www.altaghyeer.info/en/feed"),
    ("ReliefWeb Sudan", "https://reliefweb.int/updates/rss.xml?primary_country=220"),
    ("ReliefWeb Sudan JSON", "https://api.reliefweb.int/v1/reports?appname=georiskglobal&filter[field]=country.name&filter[value]=Sudan&limit=20"),
    ("Middle East Monitor", "https://www.middleeastmonitor.com/feed/"),
    ("UKMTO JMIC", "https://www.ukmto.org/partner-products/jmic-products"),
    ("MOFA Japan Anzen", "https://www.anzen.mofa.go.jp/"),
    ("Kurdistan24", "https://www.kurdistan24.net/en/rss"),
    ("The New Region", "https://thenewregion.com/feed"),
    ("ISW", "https://www.understandingwar.org/rss.xml"),
    ("Militia Spotlight", "https://www.washingtoninstitute.org/policy-analysis/series/militia-spotlight/feed"),
    ("Bellingcat", "https://www.bellingcat.com/feed/"),
    ("Syriac Press", "https://syriacpress.com/feed/"),
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
    "Daraa24_News",
    "horanfree",
    "Suwayda24",
    "DeirEzzor24",
    "EuphratesPost",
    "northpressagency",
    "damascusv011",
    "AleppoTodayTV",
    "syrianmoi",
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
    "AsharqNewsBrk",
    "almasirah",
    "almanarnews",
    "SabereenNews22",
    "IranIntl_En",
    "rudaw_english",
    "Rudaw_arabic",
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
# Hyper-local Syrian channels. Their posts stay Syria unless the text names another country.
SYRIA_LOCAL_CHANNELS = {
    "daraa24_news",
    "horanfree",
    "suwayda24",
    "deirezzor24",
    "euphratespost",
    "northpressagency",
    "damascusv011",
    "aleppotodaytv",
    "syrianmoi",
}
# Iraq and Kurdistan channels added on top of the ones already polled.
IRAQ_LOCAL_CHANNELS = {"rudaw_arabic"}
IRAQ_RSS_NAMES = {"the new region", "militia spotlight"}
SYRIA_RSS_NAMES = {"syriac press"}
# The first seven are the start of every rotation. Later names are added once.
_X_HANDLE_GROUPS = [
    ["moi_bahrain", "BDF_Bahrain", "kuwaitarmyghq", "MOInformation", "MOFAQatar_EN", "UK_MTO", "Alsakaniali"],
    [
        "CJFCSpox", "SaudiDCD", "kbsalsaud", "FaisalbinFarhan", "KKIASA", "ksagaca", "Spa_Eng", "SPAregions",
        "SaudiNews50", "saudiexpat", "KSAMOFA", "modgovksa", "MOISaudiArabia", "AlArabiya_Brk",
        "AlArabiya_Eng", "AsharqNewsBrk", "ArabNews", "Yah_Saree", "MMY1444", "FaresALhemyari",
        "abda_sharif", "Yemen_PM",
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
        "Reuters", "araReuters", "AFPar", "BBCArabic", "AP", "anadoluagency", "BBCWorld", "CNN",
        "SkyNews", "AJEnglish", "aawsat_News", "timesofisrael", "Jerusalem_Post", "ynetnews",
        "criticalthreats", "ACLEDINFO", "bellingcat", "ELINTNews", "Conflicts", "Osint613",
        "ReutersWorld", "UNDSS",
    ],
    [
        "syr_television", "SyriacOSINT", "QalaatAlMudiq", "Charles_Lister", "GregoryPWaters", "OSINTWarfare",
        "Osinttechnical", "AfriMEOSINT", "AuroraIntel", "OSINTdefender", "IntelCrab", "clashreport",
        "IraqiOsint", "K24English", "ShafaqNews", "Tammuz_Intel", "IraqSecurity",
        "AymennJawad", "obretix", "CalibreObscura", "SyriaHR", "EnabBaladi", "Syria_Direct",
        "SyriacPress", "Joshua_Landis", "Hamdi_Malik", "MichaelPKnights", "Shelly_Kittleson",
        "tamerbadawi1", "ShafaqNewsEng", "Rudaw_English", "kurdistan_24",
        "EnabBaladi_Eng", "syriadirect", "syrianmoi", "isfmoisy", "SyriaCivilDef",
        "SyriaCivilDefe", "OCHA_Syria", "UNinSyria", "SDF_Syria", "farhad_shami", "hawarnews",
        "kurdistan24tv", "k24_Kurmanci", "arabick24", "iraqiagency", "UNIraq", "IKRPresident",
        "USCGERBIL", "ReutersAfrica", "BBCAfrica", "RadioDabanga", "AyinSudan", "UNOCHA_Sudan",
        "UN_Sudan", "UNHCRinSudan", "IOMSudan", "iomchad", "SudanTribune_AR", "SDN154",
        "SUNA_AGENCY_EN", "SUNA_AGENCY",
        "SudanWarMonitor",
        "DabangaSudan", "SudanTribune_EN", "PatrickHeinisc1", "vista_sudan",
    ],
]
EXCLUDED_SOURCE_HANDLES = {"sepah_news1", "yahya_saree", "tasnimnews_en", "irantimes72", "afp"}
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
    "reutersafrica",
    "bbcafrica",
    "radiodabanga",
    "ayinsudan",
    "unocha_sudan",
    "un_sudan",
    "unhcrinsudan",
    "iomsudan",
    "iomchad",
    "sudantribune_ar",
    "sdn154",
    "suna_agency_en",
    "suna_agency",
}
SUDAN_RSS_NAMES = {"khartoum err"}
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
    "hamdi_malik",
    "michaelpknights",
    "shelly_kittleson",
    "tamerbadawi1",
    "shafaqnewseng",
    "rudaw_english",
    "kurdistan_24",
    "kurdistan24tv",
    "k24_kurmanci",
    "arabick24",
    "iraqiagency",
    "uniraq",
    "ikrpresident",
    "uscgerbil",
}
SYRIA_X_HANDLES = {
    "qalaatalmudiq",
    "aymennjawad",
    "charles_lister",
    "obretix",
    "calibreobscura",
    "syriahr",
    "enabbaladi",
    "syria_direct",
    "syriacpress",
    "joshua_landis",
    "enabbaladi_eng",
    "syriadirect",
    "asharqnewssyr",
    "sanaenofficial",
    "syrianmofaex",
    "syrianmoi",
    "isfmoisy",
    "syriacivildef",
    "syriacivildefe",
    "ocha_syria",
    "uninsyria",
    "sdf_syria",
    "farhad_shami",
    "hawarnews",
}
SAUDI_OFFICIAL_X = {
    "cjfcspox",
    "saudidcd",
    "kbsalsaud",
    "faisalbinfarhan",
    "kkiasa",
    "ksagaca",
    "spa_eng",
    "sparegions",
    "saudinews50",
    "saudiexpat",
    "ksamofa",
    "modgovksa",
    "moisaudiarabia",
}
SAUDI_MEDIA_X = {
    "alarabiya_brk",
    "alarabiya_eng",
    "asharqnewsbrk",
    "arabnews",
}
SAUDI_X_HANDLES = SAUDI_OFFICIAL_X | SAUDI_MEDIA_X
SAUDI_RSS_NAMES = {"arab news", "al arabiya english"}
# Country desks. Cross-cutting wires stay Dynamic and follow the incident.
_DESK_META = (
    ("Syria", "Syria", "Syria"),
    ("Iraq", "Iraq", "Iraq"),
    ("Saudi Arabia", "Saudi Arabia", "Saudi Arabia"),
    ("Sudan & Chad", "Sudan", "Sudan"),
    ("Iran", "Iran", "Iran"),
    ("Gulf States", "", ""),
    ("Neighbor States", "", ""),
    ("Maritime & Chokepoints", "Maritime", "Maritime"),
    ("Cross-Cutting Wires", "Dynamic", "Dynamic"),
)
_DESK_HANDLES = {
    "Syria": {
        "conflictarchive", "suriyak_maps", "hawran_osint", "npasyria", "sananewsenglish",
        "televisionsyria", "alekhbariahsy", "syrianarabnewsagency", "sn4hr", "d24net",
        "sweidagov1", "daraa24_news", "horanfree", "suwayda24", "deirezzor24", "euphratespost",
        "northpressagency", "damascusv011", "aleppotodaytv", "syrianmoi", "syrianinfowar",
        "syriahr", "enabbaladi", "syrianmofaex", "levant_24_", "sanaenofficial", "sana__gov",
        "asharqnewssyr", "syr_television", "syriacosint", "qalaatalmudiq", "charles_lister",
        "gregorypwaters", "aymennjawad", "obretix", "calibreobscura", "syria_direct",
        "syriacpress", "joshua_landis", "enabbaladi_eng", "syriadirect", "isfmoisy",
        "syriacivildef", "syriacivildefe", "ocha_syria", "uninsyria", "sdf_syria",
        "farhad_shami", "hawarnews",
    },
    "Iraq": {
        "rudaw_english", "rudaw_arabic", "shafaqnews", "almaalomah", "ina_iraq", "kurdistan24",
        "sabereennews22", "iraqipmo", "thenewregion", "rudawenglish", "shafaqnewsarb",
        "iraqiosint", "k24english", "tammuz_intel", "iraqsecurity", "hamdi_malik",
        "michaelpknights", "shelly_kittleson", "tamerbadawi1", "shafaqnewseng", "kurdistan_24",
        "kurdistan24tv", "k24_kurmanci", "arabick24", "iraqiagency", "uniraq", "ikrpresident",
        "uscgerbil",
    },
    "Saudi Arabia": set(SAUDI_X_HANDLES) | {
        "alarabiya_brk", "alarabiya_eng", "asharqnewsbrk", "almasirah", "yah_saree", "mmy1444",
        "faresalhemyari", "abda_sharif", "yemen_pm", "usainksa", "aawsat_news", "alhadath",
    },
    "Sudan & Chad": set(SUDAN_X_HANDLES) | {
        "darfur24", "sudannews24", "alrakoba_sudan", "sudantribune",
    },
    "Iran": {
        "mkhamenei_ir", "drpezeshkian", "mb_ghalibaf", "ejei_org", "araghchi", "irimfa_spox",
        "ir_rezaee", "mmohammadii61", "hatami_org", "gharibabadi", "iran_in_kuwait",
        "smajid__moosavi", "iribnews_irib", "presstv", "tasnimarabic1", "iranintl_en",
        "reutersiran", "s_m_marandi", "irna_english", "farsnews_agency", "iranintl_en",
        "irna_en", "mehrnews_en",
    },
    "Gulf States": {
        "moi_bahrain", "bdf_bahrain", "kuwaitarmyghq", "moinformation", "mofaqatar_en",
        "alsakaniali", "mba_althani_", "majedalansari", "mofauae", "anwargargash",
        "gulfnewsuae", "gulfnews", "kuna_en", "bna_en",
    },
    "Neighbor States": {
        "idf", "idfonline", "idfspokesperson", "avichayadraee", "netanyahu", "israelpresident",
        "israelmfa", "israel_katz", "gadi_eisenkot", "gantzbe", "yairlapid", "naftalibennett",
        "avigdorliberman", "yairgolan10", "ilredalert", "manniefabian", "barakravid",
        "kaisos1987", "lbpresidency", "nnaleb", "lebarmyofficial", "kingabdullahii",
        "usembassybeirut", "usembassyjordan", "lorientlejour", "timesofisrael", "jerusalem_post",
        "ynetnews", "almanarnews", "royanews", "alghadnews", "petranewsagency", "qudsnen",
    },
    "Maritime & Chokepoints": {"uk_mto"},
    "Cross-Cutting Wires": set(),
}
_HANDLE_COUNTRY = {
    "moi_bahrain": "Bahrain", "bdf_bahrain": "Bahrain", "bna_en": "Bahrain",
    "kuwaitarmyghq": "Kuwait", "moinformation": "Kuwait", "kuna_en": "Kuwait",
    "mofaqatar_en": "Qatar", "mba_althani_": "Qatar", "majedalansari": "Qatar",
    "mofauae": "UAE", "anwargargash": "UAE", "gulfnewsuae": "UAE", "gulfnews": "UAE",
    "alsakaniali": "Oman",
    "yah_saree": "Yemen", "yemen_pm": "Yemen", "mmy1444": "Yemen", "faresalhemyari": "Yemen",
    "abda_sharif": "Yemen", "almasirah": "Yemen",
    "idf": "Israel", "idfonline": "Israel", "idfspokesperson": "Israel", "avichayadraee": "Israel",
    "netanyahu": "Israel", "israelpresident": "Israel", "israelmfa": "Israel", "israel_katz": "Israel",
    "gadi_eisenkot": "Israel", "gantzbe": "Israel", "yairlapid": "Israel", "naftalibennett": "Israel",
    "avigdorliberman": "Israel", "yairgolan10": "Israel", "ilredalert": "Israel",
    "manniefabian": "Israel", "barakravid": "Israel", "timesofisrael": "Israel",
    "jerusalem_post": "Israel", "ynetnews": "Israel",
    "kaisos1987": "Lebanon", "lbpresidency": "Lebanon", "nnaleb": "Lebanon",
    "lebarmyofficial": "Lebanon", "lorientlejour": "Lebanon", "almanarnews": "Lebanon",
    "usembassybeirut": "Lebanon",
    "kingabdullahii": "Jordan", "usembassyjordan": "Jordan", "royanews": "Jordan",
    "alghadnews": "Jordan", "petranewsagency": "Jordan",
    "qudsnen": "Palestine",
    "iomchad": "Chad",
    "usainksa": "Saudi Arabia",
}
_RSS_DESK = {
    "https://english.enabbaladi.net/feed/": "Syria",
    "https://syriadirect.org/feed/": "Syria",
    "https://syriacpress.com/feed/": "Syria",
    "https://shafaq.com/en/rss": "Iraq",
    "https://www.iraqinews.com/feed/": "Iraq",
    "https://www.rudaw.net/english/rss": "Iraq",
    "https://www.kurdistan24.net/en/rss": "Iraq",
    "https://thenewregion.com/feed": "Iraq",
    "https://www.washingtoninstitute.org/policy-analysis/series/militia-spotlight/feed": "Iraq",
    "https://www.iranintl.com/en/rss": "Iran",
    "https://www.arabnews.com/rss.xml": "Saudi Arabia",
    "https://english.alarabiya.net/tools/rss": "Saudi Arabia",
    "https://www.dabangasudan.org/en/all-news/feed": "Sudan & Chad",
    "https://www.dabangasudan.org/en/feed": "Sudan & Chad",
    "https://sudantribune.com/feed/": "Sudan & Chad",
    "https://darfur24.com/en/feed/": "Sudan & Chad",
    "https://www.altaghyeer.info/en/feed": "Sudan & Chad",
    "https://reliefweb.int/updates/rss.xml?primary_country=220": "Sudan & Chad",
    "https://api.reliefweb.int/v1/reports?appname=georiskglobal&filter[field]=country.name&filter[value]=Sudan&limit=20": "Sudan & Chad",
    "https://khartoumerr.org/feed/": "Sudan & Chad",
    "https://khartoumerr.org/": "Sudan & Chad",
    "https://www.thenationalnews.com/arc/outboundfeeds/rss/": "Gulf States",
    "https://www.jordantimes.com/rss.xml": "Neighbor States",
    "https://www.ukmto.org/partner-products/jmic-products": "Maritime & Chokepoints",
    "https://www.aljazeera.com/xml/rss/middleeast.xml": "Cross-Cutting Wires",
    "https://www.aljazeera.com/xml/rss/all.xml": "Cross-Cutting Wires",
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml": "Cross-Cutting Wires",
    "https://news.google.com/rss/search?q=site:reuters.com+(Middle+East+OR+Syria)&hl=en-US&gl=US&ceid=US:en": "Cross-Cutting Wires",
    "https://news.google.com/rss/search?q=site:apnews.com+(Middle+East+OR+Syria)&hl=en-US&gl=US&ceid=US:en": "Cross-Cutting Wires",
    "https://www.middleeastmonitor.com/feed/": "Cross-Cutting Wires",
    "https://www.anzen.mofa.go.jp/": "Cross-Cutting Wires",
    "https://www.understandingwar.org/rss.xml": "Cross-Cutting Wires",
    "https://www.bellingcat.com/feed/": "Cross-Cutting Wires",
}


def _closest_desk(label: str) -> str:
    """A polled source missing from the desk lists stays on the nearest desk."""
    blob = str(label or "").lower()
    rules = (
        ("khartoum", "Sudan & Chad"), ("darfur", "Sudan & Chad"), ("sudan", "Sudan & Chad"),
        ("chad", "Sudan & Chad"), ("syria", "Syria"), ("idlib", "Syria"), ("aleppo", "Syria"),
        ("kurd", "Iraq"), ("rudaw", "Iraq"), ("shafaq", "Iraq"), ("iraq", "Iraq"),
        ("iran", "Iran"), ("irna", "Iran"), ("presstv", "Iran"),
        ("yemen", "Saudi Arabia"), ("houthi", "Saudi Arabia"), ("saudi", "Saudi Arabia"),
        ("bahrain", "Gulf States"), ("kuwait", "Gulf States"), ("qatar", "Gulf States"),
        ("emirates", "Gulf States"), ("uae", "Gulf States"), ("oman", "Gulf States"),
        ("ukmto", "Maritime & Chokepoints"), ("jmic", "Maritime & Chokepoints"),
        ("hormuz", "Maritime & Chokepoints"),
        ("israel", "Neighbor States"), ("lebanon", "Neighbor States"), ("jordan", "Neighbor States"),
        ("gaza", "Neighbor States"), ("palestine", "Neighbor States"),
    )
    for needle, desk in rules:
        if needle in blob:
            return desk
    return "Cross-Cutting Wires"


def _build_country_registry() -> tuple[dict, ...]:
    """One desk each. Handles and feed URLs are not repeated across desks."""
    desks = {
        name: {"desk": name, "region": region, "default_country": country, "x": [], "telegram": [], "rss": []}
        for name, region, country in _DESK_META
    }
    assigned: dict[str, str] = {}
    seen_urls: set[str] = set()

    def claim_handle(desk_name: str, kind: str, handle: str) -> None:
        key = handle.lower()
        if not key or key in EXCLUDED_SOURCE_HANDLES:
            return
        owner = assigned.get(key)
        if owner and owner != desk_name:
            return
        assigned[key] = desk_name
        bucket = desks[desk_name][kind]
        if key not in {item.lower() for item in bucket}:
            bucket.append(handle)

    explicit = {}
    for desk_name, handles in _DESK_HANDLES.items():
        for handle in handles:
            explicit.setdefault(handle.lower(), desk_name)
    for handle in X_HANDLES:
        desk_name = explicit.get(handle.lower()) or _closest_desk(handle)
        claim_handle(desk_name, "x", handle)
    for channel in TELEGRAM_CHANNELS:
        desk_name = explicit.get(channel.lower()) or _closest_desk(channel)
        claim_handle(desk_name, "telegram", channel)
    for name, url in list(RSS_FEEDS) + [
        ("Khartoum Err", "https://khartoumerr.org/feed/"),
        ("Khartoum Err", "https://khartoumerr.org/"),
    ]:
        key = url.strip()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        desk_name = _RSS_DESK.get(key) or _closest_desk(f"{name} {url}")
        desks[desk_name]["rss"].append((name, key))
    return tuple(desks[name] for name, _region, _country in _DESK_META)


COUNTRY_SOURCE_REGISTRY = _build_country_registry()
_DESK_BY_NAME = {desk["desk"]: desk for desk in COUNTRY_SOURCE_REGISTRY}
_DESK_BY_HANDLE = {}
_DESK_BY_URL = {}
for _desk in COUNTRY_SOURCE_REGISTRY:
    for _kind in ("x", "telegram"):
        for _handle in _desk[_kind]:
            _DESK_BY_HANDLE.setdefault(_handle.lower(), _desk)
    for _name, _url in _desk["rss"]:
        _DESK_BY_URL.setdefault(_url, _desk)
del _desk, _kind, _handle, _name, _url
COUNTRY_TAGS = (
    "Syria", "Iraq", "Saudi Arabia", "Sudan", "Chad", "Iran", "Lebanon", "Israel",
    "Palestine", "Jordan", "Yemen", "Bahrain", "Kuwait", "Qatar", "UAE", "Oman",
    "Red Sea / Maritime", "Strait of Hormuz / Gulf", "Other",
)
COUNTRY_CENTERS = {
    "Syria": (33.5138, 36.2765),
    "Iraq": (33.3152, 44.3661),
    "Saudi Arabia": (24.7136, 46.6753),
    "Sudan": (15.5007, 32.5599),
    "Chad": (12.1348, 15.0557),
    "Iran": (35.6892, 51.3890),
    "Lebanon": (33.8938, 35.5018),
    "Israel": (31.7683, 35.2137),
    "Palestine": (31.9522, 35.2332),
    "Jordan": (31.9454, 35.9284),
    "Yemen": (15.3694, 44.1910),
    "Bahrain": (26.0667, 50.5577),
    "Kuwait": (29.3759, 47.9774),
    "Qatar": (25.2854, 51.5310),
    "UAE": (24.4539, 54.3773),
    "Oman": (23.5880, 58.3829),
    "Red Sea / Maritime": (20.0, 38.5),
    "Strait of Hormuz / Gulf": (26.5667, 56.2667),
    "Other": (26.5, 47.0),
}
_SEA_COUNTRY_NAMES = frozenset({"Red Sea / Maritime", "Strait of Hormuz / Gulf", "Maritime"})
# First HTTP 200 with valid RSS wins. A dead or rate-limited mirror tries the next one.
X_FEED_URLS = [
    "https://xcancel.com/{handle}/rss",
    "https://nitter.poast.org/{handle}/rss",
    "https://nitter.privacydev.net/{handle}/rss",
]
X_MIRROR_TIMEOUT = 10
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
    "Syria",
    "Lebanon",
    "Israel",
    "Palestine",
    "Jordan",
    "Iraq",
    "Iran",
    "Saudi Arabia",
    "Yemen",
    "Sudan",
    "Chad",
    "Bahrain",
    "Kuwait",
    "Qatar",
    "UAE",
    "Oman",
    "Maritime",
)
GULF_COUNTRIES = ("Bahrain", "Kuwait", "Qatar", "UAE", "Oman")
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
    "adre crossing": (13.4667, 22.2000),
    "tine crossing": (15.0167, 22.7833),
    "khartoum": (15.5007, 32.5599),
    "omdurman": (15.6445, 32.4777),
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
    "quneitra": (33.1256, 35.8244),
    "as suwayda": (32.7094, 36.5667),
    "suwayda": (32.7094, 36.5667),
    "sweida": (32.7094, 36.5667),
    "jaber nasib crossing": (32.5594, 36.2244),
    "dabousiya crossing": (34.6361, 36.0211),
    "quneitra bir ajam": (33.1256, 35.8244),
    "palmyra tadmur": (34.5600, 38.2842),
    "al bukamal": (34.4514, 40.9186),
    "abu kamal": (34.4514, 40.9186),
    "al mayadin": (35.0183, 40.4517),
    "tal tamer": (36.6553, 40.3694),
    "tal tamr": (36.6553, 40.3694),
    "bir ajam": (33.1256, 35.8244),
    "beit jinn": (33.3144, 35.8797),
    "qamishli": (37.0522, 41.2317),
    "qamishlo": (37.0522, 41.2317),
    "palmyra": (34.5600, 38.2842),
    "tadmur": (34.5600, 38.2842),
    "zabadani": (33.7247, 36.0972),
    "manbij": (36.5281, 37.9549),
    "baniyas": (35.1819, 35.9486),
    "al tanf": (33.5089, 38.6531),
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
    "adre crossing": (13.4667, 22.2000),
    "tine crossing": (15.0167, 22.7833),
    "khartoum north": (15.6333, 32.5333),
    "khartoum bahri": (15.6333, 32.5333),
    "red sea state": (19.6158, 37.2164),
    "port sudan": (19.6158, 37.2164),
    "north darfur": (13.6281, 25.3494),
    "south darfur": (12.0500, 24.8833),
    "west darfur": (13.4500, 22.4500),
    "central darfur": (12.9096, 23.4741),
    "east darfur": (11.4619, 26.1258),
    "north kordofan": (13.1833, 30.2167),
    "south kordofan": (11.0167, 29.7167),
    "west kordofan": (11.7329, 28.3579),
    "al ubayyid": (13.1833, 30.2167),
    "el fasher": (13.6281, 25.3494),
    "al fashir": (13.6281, 25.3494),
    "el geneina": (13.4500, 22.4500),
    "geneina": (13.4500, 22.4500),
    "el obeid": (13.1833, 30.2167),
    "al obeid": (13.1833, 30.2167),
    "wad madani": (14.4000, 33.5167),
    "wad medani": (14.4000, 33.5167),
    "al jazirah": (14.4000, 33.5167),
    "al jazeera state": (14.4000, 33.5167),
    "al fulah": (11.7329, 28.3579),
    "ed daein": (11.4619, 26.1258),
    "river nile": (17.7022, 33.9864),
    "northern state": (19.1698, 30.4749),
    "al qadarif": (14.0333, 35.3833),
    "rapid support forces": (15.5000, 30.2000),
    "zalingei": (12.9096, 23.4741),
    "omdurman": (15.6445, 32.4777),
    "khartoum": (15.5007, 32.5599),
    "kadugli": (11.0167, 29.7167),
    "kassala": (15.4500, 36.4000),
    "gedaref": (14.0333, 35.3833),
    "dongola": (19.1698, 30.4749),
    "kordofan": (13.1833, 30.2167),
    "sennar": (13.5500, 33.6000),
    "kosti": (13.1667, 32.6667),
    "al maliha": (15.1333, 25.9667),
    "el malha": (15.1333, 25.9667),
    "el maliha": (15.1333, 25.9667),
    "mellit": (14.1167, 25.8167),
    "nyala": (12.0500, 24.8833),
    "atbara": (17.7022, 33.9864),
    "sinja": (13.5691, 33.5672),
    "bahri": (15.6333, 32.5333),
    "darfur": (13.6281, 25.3494),
    "adre": (13.4667, 22.2000),
    "sudan": (15.5000, 30.2000),
    "rsf": (15.5000, 30.2000),
    "saf": (15.5000, 30.2000),
    "الخرطوم بحري": (15.6333, 32.5333),
    "بحري": (15.6333, 32.5333),
    "أم درمان": (15.6445, 32.4777),
    "ام درمان": (15.6445, 32.4777),
    "بورتسودان": (19.6158, 37.2164),
    "بورت سودان": (19.6158, 37.2164),
    "ولاية البحر الأحمر": (19.6158, 37.2164),
    "شمال دارفور": (13.6281, 25.3494),
    "جنوب دارفور": (12.0500, 24.8833),
    "غرب دارفور": (13.4500, 22.4500),
    "وسط دارفور": (12.9096, 23.4741),
    "شرق دارفور": (11.4619, 26.1258),
    "شمال كردفان": (13.1833, 30.2167),
    "جنوب كردفان": (11.0167, 29.7167),
    "غرب كردفان": (11.7329, 28.3579),
    "ولاية الجزيرة": (14.4000, 33.5167),
    "نهر النيل": (17.7022, 33.9864),
    "الولاية الشمالية": (19.1698, 30.4749),
    "معبر أدري": (13.4667, 22.2000),
    "الدعم السريع": (15.5000, 30.2000),
    "الخرطوم": (15.5007, 32.5599),
    "الفاشر": (13.6281, 25.3494),
    "زالنجي": (12.9096, 23.4741),
    "الجنينة": (13.4500, 22.4500),
    "الأبيض": (13.1833, 30.2167),
    "الابيض": (13.1833, 30.2167),
    "كادقلي": (11.0167, 29.7167),
    "الفولة": (11.7329, 28.3579),
    "ود مدني": (14.4000, 33.5167),
    "القضارف": (14.0333, 35.3833),
    "دارفور": (13.6281, 25.3494),
    "كردفان": (13.1833, 30.2167),
    "السودان": (15.5000, 30.2000),
    "نيالا": (12.0500, 24.8833),
    "كسلا": (15.4500, 36.4000),
    "سنجة": (13.5691, 33.5672),
    "سنار": (13.5500, 33.6000),
    "كوستي": (13.1667, 32.6667),
    "المليحة": (15.1333, 25.9667),
    "مليط": (14.1167, 25.8167),
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
# Longer place names match before the country center. Country keys are tried last.
SAUDI_GEO_FALLBACKS = {
    "king khalid international airport": (24.7136, 46.6753),
    "king abdulaziz international airport": (21.4858, 39.1925),
    "king abdulaziz port": (21.4858, 39.1925),
    "مطار الملك عبدالعزيز": (21.4858, 39.1925),
    "ميناء الملك عبدالعزيز": (21.4858, 39.1925),
    "المملكة العربية السعودية": (24.7136, 46.6753),
    "king khalid airport": (24.7136, 46.6753),
    "مطار الملك خالد": (24.7136, 46.6753),
    "eastern province": (26.4207, 50.0888),
    "khamis mushait": (18.3000, 42.7333),
    "khamis mushayt": (18.3000, 42.7333),
    "المنطقة الشرقية": (26.4207, 50.0888),
    "jeddah airport": (21.4858, 39.1925),
    "المدينة المنورة": (24.5247, 39.5692),
    "مكة المكرمة": (21.3891, 39.8579),
    "خميس مشيط": (18.3000, 42.7333),
    "saudi arabia": (24.7136, 46.6753),
    "ras tanura": (26.6436, 50.1592),
    "رأس تنورة": (26.6436, 50.1592),
    "راس تنورة": (26.6436, 50.1592),
    "king khalid": (24.7136, 46.6753),
    "السعودية": (24.7136, 46.6753),
    "dhahran": (26.4207, 50.0888),
    "dammam": (26.4207, 50.0888),
    "khobar": (26.4207, 50.0888),
    "abqaiq": (25.9371, 49.6776),
    "shaybah": (25.9371, 49.6776),
    "jubail": (27.0046, 49.6460),
    "yanbu": (24.0895, 38.0618),
    "yenbo": (24.0895, 38.0618),
    "jizan": (16.8892, 42.5511),
    "jazan": (16.8892, 42.5511),
    "najran": (17.4924, 44.1277),
    "abha": (18.3000, 42.7333),
    "asir": (18.3000, 42.7333),
    "tabuk": (28.3838, 36.5550),
    "neom": (28.3838, 36.5550),
    "makkah": (21.3891, 39.8579),
    "mecca": (21.3891, 39.8579),
    "madinah": (24.5247, 39.5692),
    "medina": (24.5247, 39.5692),
    "riyadh": (24.7136, 46.6753),
    "jeddah": (21.4858, 39.1925),
    "jiddah": (21.4858, 39.1925),
    "الظهران": (26.4207, 50.0888),
    "الدمام": (26.4207, 50.0888),
    "الخبر": (26.4207, 50.0888),
    "الجبيل": (27.0046, 49.6460),
    "نجران": (17.4924, 44.1277),
    "جازان": (16.8892, 42.5511),
    "جيزان": (16.8892, 42.5511),
    "ينبع": (24.0895, 38.0618),
    "شيبة": (25.9371, 49.6776),
    "بقيق": (25.9371, 49.6776),
    "عسير": (18.3000, 42.7333),
    "أبها": (18.3000, 42.7333),
    "ابها": (18.3000, 42.7333),
    "تبوك": (28.3838, 36.5550),
    "نيوم": (28.3838, 36.5550),
    "مكة": (21.3891, 39.8579),
    "الرياض": (24.7136, 46.6753),
    "جدة": (21.4858, 39.1925),
    "جده": (21.4858, 39.1925),
    "saudi": (24.7136, 46.6753),
}
_SAUDI_COUNTRY_KEYS = frozenset({
    "saudi", "saudi arabia", "السعودية", "المملكة العربية السعودية",
})
_SAUDI_FALLBACK_ORDER = tuple(sorted(
    SAUDI_GEO_FALLBACKS,
    key=lambda key: (key in _SAUDI_COUNTRY_KEYS, -len(key)),
))
_KURDISH_FOLD = str.maketrans({
    "ê": "e", "Ê": "e", "î": "i", "Î": "i", "û": "u", "Û": "u",
    "ş": "s", "Ş": "s", "ç": "c", "Ç": "c",
    "ẍ": "x", "Ẍ": "x", "ḧ": "h", "Ḧ": "h",
    "ı": "i", "İ": "i",
})
_LOCALITY_FALLBACKS = (
    "jaber nasib crossing",
    "dabousiya crossing",
    "quneitra bir ajam",
    "palmyra tadmur",
    "al bukamal",
    "abu kamal",
    "al mayadin",
    "tal tamer",
    "tal tamr",
    "bir ajam",
    "beit jinn",
    "qamishli",
    "qamishlo",
    "palmyra",
    "tadmur",
    "zabadani",
    "manbij",
    "baniyas",
    "al tanf",
)
_GEO_FALLBACK_ORDER = _LOCALITY_FALLBACKS + tuple(
    name for name in sorted(GEO_FALLBACKS, key=len, reverse=True) if name not in set(_LOCALITY_FALLBACKS)
)
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
GEO_PRECISION_EXACT = "Exact Site"
GEO_PRECISION_CITY = "City / Governorate"
GEO_PRECISION_COUNTRY = "Country / Sea Fallback"
GEO_PRECISIONS = (GEO_PRECISION_EXACT, GEO_PRECISION_CITY, GEO_PRECISION_COUNTRY)
AMPLIFY_WINDOW = timedelta(minutes=30)
_EXACT_SITE_RE = re.compile(
    r"\b(crossing|airport|airbase|air base|checkpoint|refinery|terminal|canal|naval base)\b",
    re.IGNORECASE,
)
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


def canonical_country(value: object) -> str:
    """A stored country. Sea labels fold into Maritime. Theater names do not."""
    text = " ".join(str(value or "").split())
    if text in _SEA_COUNTRY_NAMES:
        return "Maritime"
    if text in REGIONS or text == "Other":
        return text
    return ""


def region_name(value: object) -> str:
    """Region is the country. An old theater label is not a country."""
    return canonical_country(value)


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
    "cnn", "sky", "skynews", "anadolu", "anadoluagency", "aljazeera", "ajenglish",     "alarabiya",
    "alarabiya_brk", "alarabiya_eng", "alarabytv", "alhadath", "asharq", "asharqnewsbrk", "arabnews",
    "aawsat", "aawsat_news", "timesofisrael", "jerusalem_post",
    "ynet", "ynetnews", "rudaw", "rudawenglish", "kurdistan24", "k24english", "shafaq", "shafaqnews",
    "shafaqnewsarb", "nna", "nnaleb", "thenewregion", "wsj", "uniindianews", "lorientlejour",
    "iranintl", "iranintl_en", "middleeastmonitor", "thestudyofwar", "understandingwar", "isw",
    "levant_24_", "levant24", "televisionsyria",
}
_OFFICIAL_HANDLES = {
    "moi_bahrain", "bdf_bahrain", "kuwaitarmyghq", "moinformation", "mofaqatar_en", "uk_mto",
    "alsakaniali", "cjfcspox", "saudidcd", "kbsalsaud", "faisalbinfarhan", "kkiasa", "ksagaca",
    "ksamofa", "modgovksa", "moisaudiarabia",
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


def _token_named(tokens: set[str], names: set[str]) -> bool:
    """Exact handle token. Underscore spellings do not absorb the unpunctuated twin."""
    return any(name.lower().strip("@") in tokens for name in names)


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


_INSTITUTIONAL_HANDLES = {
    "thestudyofwar", "criticalthreats", "acledinfo", "bellingcat",
}
_INSTITUTIONAL_HOSTS = (
    "understandingwar.org", "criticalthreats.org", "acleddata.com",
    "bellingcat.com", "washingtoninstitute.org",
    "reliefweb.int", "api.reliefweb.int",
)
_SPECIALIST_HANDLES = {
    "qalaatalmudiq", "aymennjawad", "charles_lister", "obretix", "calibreobscura",
    "hamdi_malik", "michaelpknights", "shelly_kittleson", "tamerbadawi1", "joshua_landis",
}
_LOCAL_OUTLET_HANDLES = {
    "enabbaladi", "syria_direct", "syriacpress", "shafaqnewseng",
    "rudaw_english", "k24english", "kurdistan_24",
}
_LOCAL_OUTLET_HOSTS = ("enabbaladi.net", "syriadirect.org", "syriacpress.com")
_TIPOFF_HANDLES = {"elintnews", "intelcrab", "sentdefender", "osint613"}
_SOHR_HANDLES = {"syriahr", "sohr"}
_DIPLOMATIC_HANDLES = {
    "ocha_syria", "uninsyria", "uniraq", "unocha_sudan", "un_sudan", "unhcrinsudan",
    "iomsudan", "iomchad", "undss", "uscgerbil", "centcom", "reuters", "reutersworld",
    "reutersafrica", "bbcafrica", "ajenglish", "acledinfo",
}
_FIELD_DESK_HANDLES = {
    "enabbaladi_eng", "syriadirect", "asharqnewssyr", "syriacivildef", "syriacivildefe",
    "charles_lister", "shafaqnewseng", "shafaqnewsarb", "radiodabanga", "ayinsudan",
    "sudantribune_en", "sudantribune_ar", "sdn154",
}
_CLAIM_HANDLES = {
    "sanaenofficial", "syrianmofaex", "syrianmoi", "isfmoisy", "sdf_syria", "farhad_shami",
    "hawarnews", "rudawenglish", "k24english", "kurdistan24tv", "k24_kurmanci", "arabick24",
    "iraqiagency", "iraqipmo", "ikrpresident", "suna_agency_en", "suna_agency",
}
_CAUTION_HANDLES = {"syriahr", "iranintl_en"}
_SOHR_PHRASE = "according to SOHR, figures unverified"
_CASUALTY_WORDS = re.compile(
    r"\b(?:killed|killing|dead|deaths|death toll|wounded|casualt(?:y|ies))\b",
    re.IGNORECASE,
)


def source_tier_for(kind: str = "", name: str = "", url: str = "") -> str:
    """Official, State Media, Lead, or Wire. OSINT and Regional Media are the leftovers."""
    raw, tokens, compact = _identity_parts(kind, name, url)
    if _token_named(tokens, _CAUTION_HANDLES):
        return "Single-Source / Opposition Caution"
    if _token_named(tokens, _CLAIM_HANDLES):
        return "State / Party-Aligned Claim"
    if _token_named(tokens, _FIELD_DESK_HANDLES) or "khartoumerr.org" in raw:
        return "Independent / Field Desk"
    if _token_named(tokens, _DIPLOMATIC_HANDLES):
        return "UN / Wire / Diplomatic"
    if _token_named(tokens, _SOHR_HANDLES) or "sohr.org" in raw:
        return "SOHR"
    if "conflicts" in tokens or _token_named(tokens, _TIPOFF_HANDLES):
        return "Tip-off"
    if (
        _token_named(tokens, _INSTITUTIONAL_HANDLES)
        or any(host in raw for host in _INSTITUTIONAL_HOSTS)
        or "militiaspotlight" in compact
    ):
        return "Institutional / Research"
    if _token_named(tokens, _SPECIALIST_HANDLES):
        return "Specialist Analyst"
    if _token_named(tokens, _LOCAL_OUTLET_HANDLES) or any(host in raw for host in _LOCAL_OUTLET_HOSTS):
        return "Wire / Local Outlet"
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
        "sudantribune.com", "dabangasudan.org", "darfur24.com", "altaghyeer.info",
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
    """State media and research outlets need the institution named in the English summary."""
    raw, tokens, compact = _identity_parts(kind, name, url)
    if "bellingcat" in compact:
        return "Bellingcat"
    if "criticalthreat" in compact:
        return "Critical Threats"
    if "acledinfo" in compact or "acleddata" in compact:
        return "ACLED"
    if "washingtoninstitute" in compact or "militiaspotlight" in compact:
        return "Militia Spotlight"
    if "syriahr" in compact or "sohr" in tokens or "sohr.org" in raw:
        return "SOHR"
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
        current = row["source_tier"] or ""
        if row["source_kind"] == "sweep" and current in {"Wire / Outlet", "OSINT Sweep"}:
            continue
        tier = source_tier_for(row["source_kind"], row["source_name"], row["source_url"])
        if tier != current:
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
        "region": region_name(props.get("country") or props.get("region")),
        "category": threat_category_label(props.get("category"), props.get("threat_code")),
        "threat_code": props.get("threat_code"),
        "timestamp": osint.format_timestamp(moment) if moment else "",
        "severity": osint.live_severity(props.get("severity")),
        "governorate": osint.match_governorate(props.get("governorate"))
        or osint.match_governorate(props.get("location_name")),
        "country": str(props.get("country") or ""),
        "sub_region": str(props.get("sub_region") or ""),
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
        name = region_name(event.get("country") or event.get("region"))
        if name in counts:
            counts[name] += 1
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
        return True
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
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(CACHE)
    except OSError as exc:
        print(f"summary_cache.json was not written ({exc.__class__.__name__}).")


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
        "geo_precision": "TEXT",
        "country": "TEXT",
        "sub_region": "TEXT",
        "threat_code": "INTEGER",
        "actor": "TEXT",
        "system_type": "TEXT",
        "amplification_count": "INTEGER NOT NULL DEFAULT 0",
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS briefing_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_label TEXT NOT NULL DEFAULT '',
            generated_at TEXT NOT NULL,
            overall TEXT NOT NULL,
            regions TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Railway boots through app.py -> start_background() -> db_connect() -> ensure_feed_db() -> init_db().
    purge_spam_rows_once(conn)


def _open_feeds_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    ensure_feed_db(conn)
    return conn


def db_connect() -> sqlite3.Connection:
    """Open the live feed database, moving it off a read-only bundle when required."""
    global FEEDS_DB
    try:
        return _open_feeds_db(FEEDS_DB)
    except (OSError, sqlite3.OperationalError):
        from analytics_store import prefer_writable_file

        relocated = prefer_writable_file(Path(FEEDS_DB))
        try:
            same = relocated.resolve() == Path(FEEDS_DB).resolve()
        except OSError:
            same = relocated == Path(FEEDS_DB)
        if not same:
            FEEDS_DB = relocated
        return _open_feeds_db(FEEDS_DB)


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


def bump_amplification(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute(
        "UPDATE items SET amplification_count = COALESCE(amplification_count, 0) + 1 WHERE id = ?",
        (row_id,),
    )


def classify_precision(name: object) -> str:
    """Exact site, city or governorate, or a country/sea center."""
    label = " ".join(str(name or "").casefold().split())
    if not label:
        return GEO_PRECISION_CITY
    if label in _COUNTRY_OR_SEA or label in _COUNTRY_STEMS:
        return GEO_PRECISION_COUNTRY
    if label in _LOCALITY_FALLBACKS or _EXACT_SITE_RE.search(label):
        return GEO_PRECISION_EXACT
    return GEO_PRECISION_CITY


def story_text(item: object) -> str:
    if isinstance(item, sqlite3.Row):
        parts = [item["title"], item["summary"]]
        if "original_text" in item.keys():
            parts.append(item["original_text"])
        return " ".join(str(part or "") for part in parts)
    if isinstance(item, dict):
        return " ".join(str(item.get(key) or "") for key in ("title", "summary", "original_text"))
    return str(item or "")


def near_identical_text(left: object, right: object) -> bool:
    """True when two posts are the same text, not merely the same story."""
    folded_left = normalize_title(story_text(left))
    folded_right = normalize_title(story_text(right))
    if not folded_left or not folded_right:
        return False
    if folded_left == folded_right:
        return True
    if min(len(folded_left), len(folded_right)) >= 40 and (
        folded_left in folded_right or folded_right in folded_left
    ):
        return True
    return SequenceMatcher(None, folded_left, folded_right).ratio() >= 0.92


def amplification_original(conn: sqlite3.Connection, item: dict):
    """The stored pin whose text this post echoes inside 30 minutes, if any."""
    moment = parse_when(item.get("published_at")) or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    cutoff = (moment - AMPLIFY_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT id, title, summary, original_text, published_at, ingested_at FROM items "
        "WHERE COALESCE(published_at, ingested_at, '') >= ? ORDER BY id DESC LIMIT 80",
        (cutoff,),
    ).fetchall()
    for row in rows:
        other = parse_when(row["published_at"]) or parse_when(row["ingested_at"])
        if other is None:
            continue
        if other.tzinfo is None:
            other = other.replace(tzinfo=timezone.utc)
        if abs((other.astimezone(timezone.utc) - moment).total_seconds()) > AMPLIFY_WINDOW.total_seconds():
            continue
        if near_identical_text(item, row):
            return row
    return None


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


def feed_request_headers() -> dict[str, str]:
    """Browser headers for every outbound feed and OSINT HTTP request."""
    return {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": FEED_ACCEPT,
        "Accept-Language": "en",
    }


def paced_get(url: str):
    global _pace_pending
    if _pacing:
        with _pace_lock:
            if _pace_pending:
                time.sleep(random.uniform(1.0, 2.5))
            _pace_pending = True
    return requests.get(url, timeout=FEED_TIMEOUT, headers=feed_request_headers())


def feed_features() -> list[dict]:
    features = []
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM items ORDER BY published_at DESC"
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        lng = row["lng"]
        lat = row["lat"]
        point = osint.valid_coord(lng, lat)
        if is_excluded_source(row["source_name"], row["source_url"], row["source_kind"]):
            continue
        title, summary = visible_english(row["title"], row["summary"])
        props = {
            "severity": osint.live_severity(row["severity"]),
            "title": title or "Incident",
            "summary": summary or title or "",
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
            "region": region_name((row["country"] if "country" in row.keys() else "") or row["region"]),
            "category": threat_category_label(row["category"], row["threat_code"] if "threat_code" in row.keys() else None),
            "threat_code": row["threat_code"] if "threat_code" in row.keys() else None,
            "warning_number": None,
            "location_name": row["location_name"] or "",
            "corroboration_count": int(row["corroboration_count"] or 1),
            "amplification_count": int(row["amplification_count"] or 0) if "amplification_count" in row.keys() else 0,
            "original_text": row["original_text"] or "",
            "geo_precision": (row["geo_precision"] or "") if "geo_precision" in row.keys() else "",
            "country": (row["country"] or "") if "country" in row.keys() else "",
            "sub_region": (row["sub_region"] or "") if "sub_region" in row.keys() else "",
            "actor": (row["actor"] or "") if "actor" in row.keys() else "",
            "system_type": (row["system_type"] or "") if "system_type" in row.keys() else "",
        }
        if row["governorate"]:
            props["governorate"] = row["governorate"]
        feature = {
            "type": "Feature",
            "properties": props,
        }
        if point:
            feature["geometry"] = {"type": "Point", "coordinates": [point[0], point[1]]}
        features.append(feature)
    return features


def combined_features() -> list[dict]:
    features = []
    seen: set[str] = set()
    for feature in load_features() + feed_features():
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
    print(f"RSS {name}: {len(found)} raw items.")
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
    """Middle East security news, routed by the country where it happened."""
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
    return requests.get(url, timeout=FEED_TIMEOUT, headers=feed_request_headers())


def _reliefweb_text(value: object) -> str:
    if isinstance(value, dict):
        return plain_text(value.get("html") or value.get("value") or value.get("summary") or "")
    return plain_text(value or "")


def reliefweb_json_items(name: str, payload: object) -> list[dict]:
    """ReliefWeb `data` rows. A row with an unexpected shape is skipped."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    found = []
    for row in rows[:20]:
        try:
            if not isinstance(row, dict):
                continue
            fields = row.get("fields") if isinstance(row.get("fields"), dict) else row
            if not isinstance(fields, dict):
                continue
            title = _reliefweb_text(fields.get("title"))
            body = fields.get("body")
            if body in (None, ""):
                body = fields.get("summary")
            if body in (None, ""):
                body = fields.get("body-html")
            summary = _reliefweb_text(body) or title
            link = fields.get("url") or fields.get("url_alias") or ""
            if isinstance(link, dict):
                link = link.get("url") or ""
            link = plain_text(link)
            if link.startswith("/"):
                link = "https://reliefweb.int" + link
            date = fields.get("date")
            if isinstance(date, dict):
                published = str(date.get("created") or date.get("original") or "")
            else:
                published = str(date or "")
            if not title or not link.startswith("http"):
                continue
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
        except Exception:
            continue
    return found


def fetch_rss_feed(name: str, url: str) -> list[dict]:
    reliefweb = "api.reliefweb.int" in url.lower()
    try:
        if reliefweb:
            headers = feed_request_headers()
            headers["Accept"] = "application/json, " + FEED_ACCEPT
            response = requests.get(url, timeout=FEED_TIMEOUT, headers=headers)
        else:
            response = paced_get(url)
        response.raise_for_status()
        content = response.content or b""
    except Exception as exc:
        print(f"RSS {name} failed: {exc.__class__.__name__}.")
        note_feed("rss", name, False, exc.__class__.__name__)
        return []
    if reliefweb:
        try:
            payload = response.json()
        except Exception as exc:
            print(f"RSS {name} failed: {exc.__class__.__name__}.")
            note_feed("rss", name, False, exc.__class__.__name__)
            return []
        rows = payload.get("data") if isinstance(payload, dict) else None
        raw_count = len(rows) if isinstance(rows, list) else 0
        print(f"RSS {name}: {raw_count} raw items.")
        if not isinstance(rows, list):
            note_feed("rss", name, False, "unexpected shape")
            return []
        try:
            found = reliefweb_json_items(name, payload)
        except Exception as exc:
            print(f"RSS {name} failed: {exc.__class__.__name__}.")
            note_feed("rss", name, False, exc.__class__.__name__)
            return []
        note_feed("rss", name, True, "")
        return found
    if notice_page(url) and not looks_like_feed(content):
        return html_notice_items(name, url, content)
    try:
        parsed = parse_feed(content)
    except Exception as exc:
        print(f"RSS {name} failed: {exc.__class__.__name__}.")
        note_feed("rss", name, False, exc.__class__.__name__)
        return []
    raw_count = len(parsed.entries or [])
    print(f"RSS {name}: {raw_count} raw items.")
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


KHARTOUM_ERR_FEED = "https://khartoumerr.org/feed/"
KHARTOUM_ERR_HOME = "https://khartoumerr.org/"
_BLOCKED_SCRAPE_HOSTS = {"mofa.go.jp", "www.mofa.go.jp"}


def blocked_scrape_url(url: str) -> bool:
    """mofa.go.jp itself is never scraped. The Anzen notice page is a separate feed."""
    match = re.search(r"https?://([^/?#]+)", str(url or ""), re.IGNORECASE)
    if not match:
        return False
    return match.group(1).lower() in _BLOCKED_SCRAPE_HOSTS


def _khartoum_err_items(entries: list) -> list[dict]:
    found = []
    for entry in entries[:20]:
        title = plain_text(getattr(entry, "title", ""))
        link = plain_text(getattr(entry, "link", ""))
        if not title or not link.startswith("http") or blocked_scrape_url(link):
            continue
        summary = plain_text(getattr(entry, "summary", "") or getattr(entry, "description", "") or title)
        published = getattr(entry, "published", "") or getattr(entry, "updated", "")
        found.append(
            {
                "title": title[:240],
                "summary": (summary or title)[:500],
                "source_url": link,
                "source_name": source_label("rss", "Khartoum Err"),
                "source_kind": "rss",
                "published_at": published,
                "region": "Sudan & Horn",
            }
        )
    return found


def _khartoum_err_html(page: str) -> list[dict]:
    soup = BeautifulSoup(page or "", "html.parser")
    found = []
    seen = set()
    for node in soup.select("h2 a, h3 a, article a"):
        title = plain_text(node.get_text(" ", strip=True))
        link = plain_text(node.get("href") or "")
        if link.startswith("/"):
            link = "https://khartoumerr.org" + link
        if not title or not link.startswith("https://khartoumerr.org/") or link in seen:
            continue
        if blocked_scrape_url(link):
            continue
        seen.add(link)
        found.append(
            {
                "title": title[:240],
                "summary": title[:500],
                "source_url": link,
                "source_name": source_label("rss", "Khartoum Err"),
                "source_kind": "rss",
                "published_at": "",
                "region": "Sudan & Horn",
            }
        )
        if len(found) >= 20:
            break
    return found


def fetch_khartoum_err() -> list[dict]:
    """RSS first, then the homepage. Timeout 6. A parse failure is logged and skipped."""
    if blocked_scrape_url(KHARTOUM_ERR_FEED) or blocked_scrape_url(KHARTOUM_ERR_HOME):
        print("Khartoum Err skipped: blocked host.")
        return []
    try:
        response = requests.get(
            KHARTOUM_ERR_FEED,
            timeout=FEED_TIMEOUT,
            headers=feed_request_headers(),
        )
        response.raise_for_status()
        parsed = parse_feed(response.content or b"")
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise ValueError("invalid rss")
        if not parsed.entries:
            raise ValueError("no entries")
        items = _khartoum_err_items(parsed.entries)
        print(f"RSS Khartoum Err: {len(parsed.entries or [])} raw items.")
        note_feed("rss", "Khartoum Err", True, "")
        return items
    except Exception as exc:
        print(f"Khartoum Err RSS parse failed: {exc.__class__.__name__}.")
    try:
        response = requests.get(
            KHARTOUM_ERR_HOME,
            timeout=FEED_TIMEOUT,
            headers=feed_request_headers(),
        )
        response.raise_for_status()
        found = _khartoum_err_html(response.text)
        if not found:
            raise ValueError("no entries")
        print(f"RSS Khartoum Err: {len(found)} raw items.")
        note_feed("rss", "Khartoum Err", True, "")
        return found
    except Exception as exc:
        print(f"Khartoum Err parse failed: {exc.__class__.__name__}.")
        note_feed("rss", "Khartoum Err", False, exc.__class__.__name__)
        return []


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
        note_feed("liveblog", "Al Jazeera Live", False, exc.__class__.__name__)
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
        print(f"Al Jazeera Live: {len(updates)} raw items.")
    except Exception as exc:
        print(f"Al Jazeera liveblog failed: {exc.__class__.__name__}.")
        note_feed("liveblog", "Al Jazeera Live", False, exc.__class__.__name__)
        return []
    if not updates:
        note_feed("liveblog", "Al Jazeera Live", False, "no updates")
        return []
    note_feed("liveblog", "Al Jazeera Live", True, "")
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
        futures = {
            pool.submit(fetch_rss_feed, name, url): name
            for name, url in RSS_FEEDS
        }
        for future, name in futures.items():
            try:
                found.extend(future.result() or [])
            except Exception as exc:
                print(f"RSS {name} failed: {exc.__class__.__name__}.")
                note_feed("rss", name, False, exc.__class__.__name__)
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
    print(f"Telegram {channel}: {len(found)} raw items.")
    note_feed("telegram", channel, True, "")
    return found


def mentions_syria_or_middle_east(text: str) -> bool:
    lowered = text.lower()
    latin = (
        "syria", "syrian", "middle east", "middle-east", "aleppo", "damascus", "idlib", "idleb",
        "homs", "hama", "latakia", "tartus", "raqqa", "deir ez-zor", "deir ezzor", "hasakah",
        "daraa", "suwayda", "sweida", "quneitra", "rif dimashq", "palmyra", "tadmur",
        "al-bukamal", "abu kamal", "mayadin", "tal tamr", "tal tamer", "bir ajam", "beit jinn",
        "zabadani", "nasib", "dabousiya", "baniyas", "qamishli", "qamishlo", "manbij", "al-tanf",
    )
    if any(term in lowered for term in latin):
        return True
    return any(term in text for term in ("سوريا", "سورية", "الشرق الأوسط", "الشرق الاوسط", "حلب", "دمشق", "إدلب", "حمص"))


def mentions_chad_crossing(text: str) -> bool:
    """Chad and the named border crossings. Bare 'tine' is not a place match."""
    lowered = str(text or "").lower()
    if any(term in lowered for term in ("chad", "n'djamena", "ndjamena", "adre crossing", "tine crossing", "adré", "tiné")):
        return True
    return "تشاد" in str(text or "") or "نجامينا" in str(text or "")


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
    """Syria, Iraq, Iran, the Gulf states, Yemen, maritime, or Sudan. Used for the widened X sweep."""
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
    if lowered in SUDAN_X_HANDLES and (mentions_sudan_or_horn(text) or mentions_chad_crossing(text)):
        return True
    if lowered in IRAQ_X_HANDLES and iraq_mention(text):
        return True
    if lowered in SAUDI_OFFICIAL_X:
        return True
    if lowered in SAUDI_MEDIA_X and (saudi_mention(text) or mentions_broad_theater(text)):
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
                headers=feed_request_headers(),
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
        print(f"X {handle}: {len(parsed.entries)} raw items.")
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
        futures = {pool.submit(fetch_x_handle, handle): handle for handle in X_HANDLES}
        for future, handle in futures.items():
            try:
                found.extend(future.result() or [])
            except Exception as exc:
                print(f"X {handle} failed: {exc.__class__.__name__}.")
                note_feed("x", handle, False, exc.__class__.__name__)
    return found


def fetch_telegram_candidates() -> list[dict]:
    found = []
    with ThreadPoolExecutor(max_workers=len(TELEGRAM_CHANNELS) or 1) as pool:
        futures = {pool.submit(fetch_telegram_channel, channel): channel for channel in TELEGRAM_CHANNELS}
        for future, channel in futures.items():
            try:
                found.extend(future.result() or [])
            except Exception as exc:
                print(f"Telegram {channel} failed: {exc.__class__.__name__}.")
                note_feed("telegram", channel, False, exc.__class__.__name__)
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
    if re.search(r"[çêîûşÇÊÎÛŞ]", sample):
        return True
    accented = re.search(r"[\u00C0-\u024F]", sample) is not None
    other_language = any(token in lowered for token in _OTHER_LANGUAGE_MARKERS)
    return (accented or other_language) and english_hits == 0


def kurdish_handle_needs_english(handle: str, title: str, summary: str) -> bool:
    """Kurmanji and Sorani accounts stay in the translation queue when the text is not English."""
    if str(handle or "").lower() not in {"k24_kurmanci", "kurdistan24tv"}:
        return False
    sample = f" {title} {summary} ".casefold()
    if sum(1 for token in _ENGLISH_STOPS if token in sample) >= 2:
        return False
    return len(re.findall(r"[A-Za-z\u00C0-\u024F']+", sample)) >= 4 or bool(_FOREIGN_SCRIPT.search(sample))


def original_post_text(item: dict) -> str:
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    chosen = summary if len(summary) >= len(title) else title
    return chosen[:2000]


_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_translate_lock = threading.Lock()
_translation_key_noted = False


def contains_arabic(text: object) -> bool:
    """Arabic, Farsi, and Sorani Kurdish share these script blocks."""
    return _ARABIC_SCRIPT.search(str(text or "")) is not None


def _english_sentence(text: object, limit: int) -> str:
    cleaned = osint.one_sentence(text)[:limit]
    if cleaned and not contains_arabic(cleaned):
        return cleaned
    return ""


def visible_english(title: object, summary: object) -> tuple[str, str]:
    """Ticker, brief, live wire, and email copy. Arabic stays out of these fields."""
    clean_title = _english_sentence(title, 240)
    clean_summary = _english_sentence(summary, 500)
    if not clean_title:
        clean_title = clean_summary
    if not clean_summary:
        clean_summary = clean_title
    return clean_title, clean_summary


def _grok_english(title: str, summary: str, key: str) -> dict | None:
    """One grok-3-mini JSON translation. A failure leaves the caller to keep the previous text."""
    prompt = (
        "Translate this post into clear English. "
        "Arabic, Farsi, Kurdish, and French must become English. "
        "title is a concise English headline. "
        "summary is 1-2 clear English sentences. "
        "Preserve place names, governorates, and unit names. "
        "Do not add facts. If the post is already English, keep it in English.\n\n"
        f"Title: {osint.one_sentence(title)}\n"
        f"Summary: {osint.one_sentence(summary) or osint.one_sentence(title)}"
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["title", "summary"],
    }
    try:
        response = osint.make_client(key).responses.create(
            model=osint.GEO_MODEL,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "english_headline",
                    "schema": schema,
                    "strict": True,
                }
            },
            max_output_tokens=400,
            timeout=getattr(osint, "API_TIMEOUT", 25),
        )
        text = osint.response_text(response)
        payload = osint.parse_json_object(text) if text else None
    except Exception as exc:
        print(f"Translation failed: {osint.public_error(exc, key)}.")
        return None
    if not isinstance(payload, dict):
        print("Translation failed: response was not an object.")
        return None
    return payload


THREAT_LABELS = {
    1: "Small Arms Fire (SAF)",
    2: "Indirect Fire (IDF) and Air Strikes",
    3: "Suicide / Vehicle-Borne Improvised Explosive Device (S/VBIED)",
    4: "Improvised Explosive Device (IED)",
    5: "Explosively Formed Projectile (EFP)",
    6: "Unexploded Ordnance (UXO)",
    7: "Kidnap",
    8: "Road Traffic Collision (RTC)",
    9: "Civil Unrest",
    10: "Trauma / Casualty",
    11: "Detention / Arrest (Checkpoints)",
    12: "Miscellaneous",
}
ADVISORY_LABEL = "Other / Advisory"
_THREAT_BY_LABEL = {label: code for code, label in THREAT_LABELS.items()}
_THREAT_PATTERNS = (
    (5, re.compile(r"\befps?\b|explosively formed", re.I)),
    (3, re.compile(r"\b(?:s/vbied|svbied|vbied)s?\b|suicide (?:bomb(?:er|ing)?|attack|vest)|vehicle[ -]borne|car bomb", re.I)),
    (4, re.compile(r"\bieds?\b|improvised explosive", re.I)),
    (6, re.compile(r"\buxo\b|unexploded", re.I)),
    (2, re.compile(r"indirect fire|air ?strikes?|airstrikes?|\bmortar\b|\bartillery\b|\bshelling\b|\bmissile\b|drone strike|\brocket\b", re.I)),
    (1, re.compile(r"small arms|\bgunfire\b|\bfirefight\b|\bshooting\b|\bclashes?\b", re.I)),
    (7, re.compile(r"kidnapp?(?:ing|ed)?|\babduction\b|\bhostage\b", re.I)),
    (8, re.compile(r"\brtc\b|road traffic|traffic collision|car crash|vehicle collision", re.I)),
    (9, re.compile(r"\briots?\b|\bprotests?\b|\bdemonstration\b|civil unrest|\bunrest\b", re.I)),
    (11, re.compile(r"\bcheckpoints?\b|\bdetention\b|\barrest(?:ed|s)?\b|\bdetained\b", re.I)),
    (10, re.compile(r"\bcasualt(?:y|ies)\b|\bwounded\b|\bkilled\b|\btrauma\b|\binjured\b", re.I)),
)
_ADVISORY_RE = re.compile(
    r"\b(?:notam|weather (?:warning|advisory)|flight (?:notice|cancellation|advisory)|"
    r"embassy advisory|diplomatic (?:note|advisory)|travel advisory)\b",
    re.I,
)
_MISC_RE = re.compile(
    r"border crossings?|\bpower\b|\bfuel\b|blackout|electricity|airspace|no-fly|\bnotams?\b|"
    r"ministry directives?|ministerial (?:order|directive)|infrastructure",
    re.I,
)
_OLD_THREAT_CODES = {
    "SFA": 1,
    "ACT": 2,
    "VC": 10,
    "CU": 9,
    "KINETIC/MILITARY": 2,
    "SECURITY/CLASHES": 1,
}
_OLD_ADVISORY = {"HS", "OD", "MARITIME", "POLITICAL/OTHER", "OTHER / ADVISORY", "MISCELLANEOUS"}


def classify_threat(text: object, previous: object = None) -> tuple[int | None, str]:
    """Map text onto the single best of threat codes 1-11, or 12 Miscellaneous.

    Codes 1-11 keep their existing patterns and win over 12. Category 12 is the
    old Other / Advisory bucket plus operational, security, infrastructure, or
    diplomatic news that matches none of 1-11. An empty result means the text
    named none of those; callers still leave spam unstored.
    """
    blob = str(text or "")
    prev = " ".join(str(previous or "").split())
    for code, pattern in _THREAT_PATTERNS:
        if pattern.search(blob) or pattern.search(prev):
            return code, THREAT_LABELS[code]
    if prev in _THREAT_BY_LABEL:
        return _THREAT_BY_LABEL[prev], prev
    if prev.isdigit() and int(prev) in THREAT_LABELS:
        code = int(prev)
        return code, THREAT_LABELS[code]
    old = prev.upper()
    if old in _OLD_THREAT_CODES:
        code = _OLD_THREAT_CODES[old]
        return code, THREAT_LABELS[code]
    if (
        old in _OLD_ADVISORY
        or _ADVISORY_RE.search(blob) or _ADVISORY_RE.search(prev)
        or _MISC_RE.search(blob) or _MISC_RE.search(prev)
    ):
        return 12, THREAT_LABELS[12]
    return None, ""


def assigned_threat(text: object, previous: object = None) -> tuple[int, str]:
    """Single best of 1-11, otherwise 12 Miscellaneous."""
    code, label = classify_threat(text, previous)
    if code:
        return code, label
    return 12, THREAT_LABELS[12]


def threat_category_label(category: object, threat_code: object = None) -> str:
    try:
        code = int(threat_code)
    except (TypeError, ValueError):
        code = None
    if code in THREAT_LABELS:
        return THREAT_LABELS[code]
    _code, label = classify_threat("", category)
    return label


def _apply_threat(stored: dict, located: dict) -> dict:
    blob = " ".join(
        str(part or "")
        for part in (
            stored.get("title"), stored.get("summary"), stored.get("original_text"),
            located.get("title"), located.get("summary"), located.get("category"),
        )
    )
    if is_spam_post(blob):
        stored["relevant"] = False
        return stored
    code, label = assigned_threat(blob, located.get("category") or stored.get("category"))
    stored["category"] = label
    stored["threat_code"] = code
    return stored


def classify_and_translate_event(item: dict, placed: dict | None = None) -> dict:
    """English title and summary via grok-3-mini. Raw text stays in original_text.

    The stored category is the single best of threat codes 1-11, or 12 Miscellaneous.
    Spam stays relevant false and is not given a threat code.
    A missing xAI key leaves title and summary unchanged and does not wipe the row.
    Arabic is never written into title or summary when a key is present.
    """
    stored = dict(item)
    original = " ".join(str(stored.get("original_text") or "").split())
    if not original:
        original = original_post_text(stored)
    stored["original_text"] = original[:2000]
    incoming_title = str(stored.get("title") or "")
    incoming_summary = str(stored.get("summary") or "")
    located = placed or {}
    title = _english_sentence(located.get("title"), 240)
    summary = _english_sentence(located.get("summary"), 500)
    key = get_xai_api_key()
    if not key:
        global _translation_key_noted
        if not _translation_key_noted:
            print("XAI_API_KEY is missing. Non-English text uses the public translation fallback.")
            _translation_key_noted = True
        try:
            import feed_quality

            if feed_quality.needs_translation(incoming_title) or feed_quality.needs_translation(original):
                translated = feed_quality.translate_text(original or incoming_title)
                if translated and not contains_arabic(translated):
                    stored["title"] = translated[:240]
                    stored["summary"] = (feed_quality.translate_text(incoming_summary) or translated)[:500]
                    stored["original_text"] = (original or incoming_title)[:2000]
                    return route_event_country(_apply_threat(stored, located), located)
        except Exception as exc:
            print(f"Translation fallback failed: {exc.__class__.__name__}.")
        if title:
            stored["title"] = title
        if summary:
            stored["summary"] = summary
        return route_event_country(_apply_threat(stored, located), located)
    if not title or not summary:
        first = _grok_english(original or incoming_title, incoming_summary or incoming_title, key)
        if first:
            title = _english_sentence(first.get("title"), 240) or title
            summary = _english_sentence(first.get("summary"), 500) or summary
    if contains_arabic(title) or contains_arabic(summary) or not title or not summary:
        second = _grok_english(original or incoming_title, summary or incoming_summary or incoming_title, key)
        if second:
            title = _english_sentence(second.get("title"), 240) or title
            summary = _english_sentence(second.get("summary"), 500) or summary
    if title and not contains_arabic(title):
        stored["title"] = title
    if summary and not contains_arabic(summary):
        stored["summary"] = summary
    elif stored.get("title") and not contains_arabic(stored.get("title")):
        stored["summary"] = stored["title"]
    return route_event_country(_apply_threat(stored, located), located)


_LATIN_FOLD = str.maketrans({
    "á": "a", "à": "a", "â": "a", "ä": "a", "ã": "a",
    "é": "e", "è": "e", "ë": "e",
    "í": "i", "ì": "i", "ï": "i",
    "ó": "o", "ò": "o", "ô": "o", "ö": "o",
    "ú": "u", "ù": "u", "ü": "u",
    "ý": "y", "ñ": "n",
})


def geo_blob(*parts: object) -> str:
    text = " ".join(str(part or "") for part in parts).lower().replace("'", " ")
    text = text.translate(_KURDISH_FOLD).translate(_LATIN_FOLD)
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


def saudi_precision(keyword: str) -> str:
    """Airports and ports are exact sites. The country name stays a country fallback."""
    key = str(keyword or "")
    if key in _SAUDI_COUNTRY_KEYS:
        return "Country / Sea Fallback"
    if any(marker in key for marker in ("airport", "port", "مطار", "ميناء")):
        return "Exact Site"
    return classify_precision(key)


def match_saudi_fallback(text: str) -> tuple[str, float, float] | None:
    """Longest Saudi place. Country names match only when no city or site is named."""
    raw = str(text or "")
    padded = f" {geo_blob(raw)} "
    tokens = set(padded.split())
    for keyword in _SAUDI_FALLBACK_ORDER:
        if _arabic_keyword(keyword):
            if keyword in raw:
                lat, lng = SAUDI_GEO_FALLBACKS[keyword]
                return keyword, lat, lng
            continue
        if " " in keyword:
            if f" {keyword} " in padded:
                lat, lng = SAUDI_GEO_FALLBACKS[keyword]
                return keyword, lat, lng
            continue
        if keyword in tokens:
            lat, lng = SAUDI_GEO_FALLBACKS[keyword]
            return keyword, lat, lng
    return None


def saudi_mention(text: str) -> bool:
    """A listed Saudi city, site, or the country name."""
    return match_saudi_fallback(text) is not None


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


_IRAQ_COUNTRY_KEYS = {"iraq", "العراق", "عێراق"}
_SUDAN_COUNTRY_KEYS = {"sudan", "rsf", "saf", "السودان"}
_SUDAN_NON_PLACE = _SUDAN_COUNTRY_KEYS | {"rapid support forces", "الدعم السريع"}


def match_sudan_place(text: str) -> tuple[str, float, float] | None:
    """Longest Sudan city or governorate. Country and actor names are not pins."""
    raw = str(text or "")
    padded = f" {geo_blob(raw)} "
    tokens = set(padded.split())
    for keyword in _SUDAN_FALLBACK_ORDER:
        if keyword in _SUDAN_NON_PLACE:
            continue
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
_CHAD_KEYS = {
    "adre", "adre crossing", "adre border crossing", "tine", "tine crossing",
    "ndjamena", "n djamena", "chad", "تشاد", "أدري", "ادري",
}
_GEO_CITY_COUNTRY = {
    "rif dimashq": "Syria", "damascus": "Syria", "aleppo": "Syria", "idlib": "Syria", "idleb": "Syria",
    "latakia": "Syria", "tartus": "Syria", "hama": "Syria", "homs": "Syria", "raqqa": "Syria",
    "deir ez zor": "Syria", "deir ezzor": "Syria", "al hasakah": "Syria", "hasakah": "Syria",
    "daraa": "Syria", "quneitra": "Syria", "as suwayda": "Syria", "suwayda": "Syria", "sweida": "Syria",
    "jaber nasib crossing": "Syria", "dabousiya crossing": "Syria", "quneitra bir ajam": "Syria",
    "palmyra tadmur": "Syria", "al bukamal": "Syria", "abu kamal": "Syria", "al mayadin": "Syria",
    "tal tamer": "Syria", "tal tamr": "Syria", "bir ajam": "Syria", "beit jinn": "Syria",
    "qamishli": "Syria", "qamishlo": "Syria", "palmyra": "Syria", "tadmur": "Syria",
    "zabadani": "Syria", "manbij": "Syria", "baniyas": "Syria", "al tanf": "Syria",
    "port sudan": "Sudan", "el fasher": "Sudan", "el fashir": "Sudan", "al fashir": "Sudan",
    "el geneina": "Sudan", "el obeid": "Sudan", "wad madani": "Sudan", "wad medani": "Sudan",
    "khartoum": "Sudan", "omdurman": "Sudan", "nyala": "Sudan", "kadugli": "Sudan",
    "kassala": "Sudan", "gedaref": "Sudan", "al qadarif": "Sudan", "atbara": "Sudan",
    "dongola": "Sudan", "darfur": "Sudan", "kordofan": "Sudan", "sennar": "Sudan",
    "north darfur": "Sudan", "south darfur": "Sudan", "west darfur": "Sudan",
    "central darfur": "Sudan", "east darfur": "Sudan", "north kordofan": "Sudan",
    "south kordofan": "Sudan", "west kordofan": "Sudan", "al jazirah": "Sudan",
    "blue nile": "Sudan", "white nile": "Sudan", "river nile": "Sudan",
    "strait of hormuz": "Strait of Hormuz / Gulf", "hormuz": "Strait of Hormuz / Gulf",
    "gulf of oman": "Strait of Hormuz / Gulf", "persian gulf": "Strait of Hormuz / Gulf",
    "arabian gulf": "Strait of Hormuz / Gulf",
    "bab el mandeb": "Red Sea / Maritime", "bab al mandab": "Red Sea / Maritime",
    "red sea": "Red Sea / Maritime",
    "gaza": "Palestine",
}
_COUNTRY_NAME_ORDER = tuple(sorted((
    ("strait of hormuz", "Strait of Hormuz / Gulf"),
    ("bab el-mandeb", "Red Sea / Maritime"),
    ("bab el mandeb", "Red Sea / Maritime"),
    ("bab al-mandab", "Red Sea / Maritime"),
    ("bab al mandab", "Red Sea / Maritime"),
    ("persian gulf", "Strait of Hormuz / Gulf"),
    ("arabian gulf", "Strait of Hormuz / Gulf"),
    ("gulf of oman", "Strait of Hormuz / Gulf"),
    ("united arab emirates", "UAE"),
    ("saudi arabia", "Saudi Arabia"),
    ("red sea", "Red Sea / Maritime"),
    ("palestine", "Palestine"),
    ("west bank", "Palestine"),
    ("jerusalem", "Israel"),
    ("tel aviv", "Israel"),
    ("nabatieh", "Lebanon"),
    ("beirut", "Lebanon"),
    ("lebanon", "Lebanon"),
    ("amman", "Jordan"),
    ("bahrain", "Bahrain"),
    ("kuwait", "Kuwait"),
    ("israel", "Israel"),
    ("jordan", "Jordan"),
    ("qatar", "Qatar"),
    ("oman", "Oman"),
    ("yemen", "Yemen"),
    ("syria", "Syria"),
    ("sudan", "Sudan"),
    ("iran", "Iran"),
    ("iraq", "Iraq"),
    ("chad", "Chad"),
    ("gaza", "Palestine"),
    ("hormuz", "Strait of Hormuz / Gulf"),
    ("ukmto", "Red Sea / Maritime"),
    ("jmic", "Red Sea / Maritime"),
    ("uae", "UAE"),
    ("rsf", "Sudan"),
    ("السعودية", "Saudi Arabia"),
    ("سوريا", "Syria"),
    ("العراق", "Iraq"),
    ("إيران", "Iran"),
    ("ايران", "Iran"),
    ("لبنان", "Lebanon"),
    ("الأردن", "Jordan"),
    ("الاردن", "Jordan"),
    ("اليمن", "Yemen"),
    ("السودان", "Sudan"),
    ("تشاد", "Chad"),
    ("فلسطين", "Palestine"),
    ("غزة", "Palestine"),
    ("إسرائيل", "Israel"),
    ("اسرائيل", "Israel"),
    ("البحرين", "Bahrain"),
    ("الكويت", "Kuwait"),
    ("قطر", "Qatar"),
    ("عمان", "Oman"),
    ("الإمارات", "UAE"),
    ("الامارات", "UAE"),
), key=lambda item: len(item[0]), reverse=True))


def region_for_country(country: str) -> str:
    """Region and country are the same label."""
    return canonical_country(country) or "Other"


def desk_for_item(item: dict) -> dict | None:
    """The source desk, or None when the item is not in the registry."""
    handle = channel_handle(item).lower()
    if not handle:
        handle = x_handle_from_item(item).lower()
    if handle and handle in _DESK_BY_HANDLE:
        return _DESK_BY_HANDLE[handle]
    url = str(item.get("source_url") or "")
    name = str(item.get("source_name") or "").lower()
    if "khartoumerr.org" in url or "khartoum err" in name:
        return _DESK_BY_NAME.get("Sudan & Chad")
    for feed_url, desk in _DESK_BY_URL.items():
        if feed_url and feed_url in url:
            return desk
    return None


def incident_country(text: str) -> str:
    """The country the text places the event in. A city beats a country name."""
    raw = str(text or "")
    iraq = match_iraq_fallback(raw)
    if iraq and iraq[0] not in _IRAQ_COUNTRY_KEYS:
        return "Iraq"
    sudan = match_sudan_fallback(raw)
    if sudan and sudan[0] in _CHAD_KEYS:
        return "Chad"
    if sudan and sudan[0] not in _SUDAN_COUNTRY_KEYS:
        return "Sudan"
    saudi = match_saudi_fallback(raw)
    if saudi and saudi[0] not in _SAUDI_COUNTRY_KEYS:
        return "Saudi Arabia"
    hit = match_geo_fallback(geo_blob(raw))
    if hit and hit[0] in _GEO_CITY_COUNTRY:
        return _GEO_CITY_COUNTRY[hit[0]]
    padded = f" {geo_blob(raw)} "
    tokens = set(padded.split())
    for keyword, country in _COUNTRY_NAME_ORDER:
        if _arabic_keyword(keyword):
            if keyword in raw:
                return country
            continue
        if " " in keyword:
            if f" {keyword} " in padded:
                return country
            continue
        if keyword in tokens or (len(keyword) >= 5 and keyword in padded):
            return country
    return ""


_SUB_REGION_RULES = tuple(sorted((
    ("khartoum bahri", "Khartoum / Omdurman"),
    ("khartoum north", "Khartoum / Omdurman"),
    ("eastern province", "Eastern Province"),
    ("ras tanura", "Eastern Province"),
    ("red sea coast", "Red Sea Coast"),
    ("sulaymaniyah", "Kurdistan Region (KRI)"),
    ("rif dimashq", "Damascus / Rif Dimashq"),
    ("rural damascus", "Damascus / Rif Dimashq"),
    ("deir ez zor", "Deir ez-Zor / Hasakah"),
    ("deir ezzor", "Deir ez-Zor / Hasakah"),
    ("deir al zor", "Deir ez-Zor / Hasakah"),
    ("al hasakah", "Deir ez-Zor / Hasakah"),
    ("umm qasr", "Basra / Southern Iraq"),
    ("el fasher", "Darfur"),
    ("el geneina", "Darfur"),
    ("fallujah", "Anbar / Western Iraq"),
    ("dhahran", "Eastern Province"),
    ("dammam", "Eastern Province"),
    ("khobar", "Eastern Province"),
    ("abqaiq", "Eastern Province"),
    ("shaybah", "Eastern Province"),
    ("jubail", "Eastern Province"),
    ("jeddah", "Red Sea Coast"),
    ("yanbu", "Red Sea Coast"),
    ("jizan", "Southern Border (Jizan/Najran)"),
    ("jazan", "Southern Border (Jizan/Najran)"),
    ("najran", "Southern Border (Jizan/Najran)"),
    ("riyadh", "Riyadh"),
    ("kurdistan", "Kurdistan Region (KRI)"),
    ("erbil", "Kurdistan Region (KRI)"),
    ("duhok", "Kurdistan Region (KRI)"),
    ("zakho", "Kurdistan Region (KRI)"),
    ("baghdad", "Baghdad / Central"),
    ("ramadi", "Anbar / Western Iraq"),
    ("anbar", "Anbar / Western Iraq"),
    ("basra", "Basra / Southern Iraq"),
    ("omdurman", "Khartoum / Omdurman"),
    ("khartoum", "Khartoum / Omdurman"),
    ("darfur", "Darfur"),
    ("nyala", "Darfur"),
    ("adre", "Adré / Tiné Border"),
    ("tine", "Adré / Tiné Border"),
    ("ndjamena", "Adré / Tiné Border"),
    ("damascus", "Damascus / Rif Dimashq"),
    ("quneitra", "Quneitra / Daraa"),
    ("daraa", "Quneitra / Daraa"),
    ("hasakah", "Deir ez-Zor / Hasakah"),
    ("أربيل", "Kurdistan Region (KRI)"),
    ("بغداد", "Baghdad / Central"),
    ("البصرة", "Basra / Southern Iraq"),
    ("الأنبار", "Anbar / Western Iraq"),
    ("الرياض", "Riyadh"),
    ("جدة", "Red Sea Coast"),
    ("الخرطوم", "Khartoum / Omdurman"),
    ("دارفور", "Darfur"),
    ("دمشق", "Damascus / Rif Dimashq"),
    ("درعا", "Quneitra / Daraa"),
    ("دير الزور", "Deir ez-Zor / Hasakah"),
    ("الحسكة", "Deir ez-Zor / Hasakah"),
), key=lambda item: len(item[0]), reverse=True))


def sub_region_for(text: str, country: str = "") -> str:
    """A desk sub-region when the text names one. Empty when it does not."""
    raw = str(text or "")
    padded = f" {geo_blob(raw)} "
    tokens = set(padded.split())
    for keyword, label in _SUB_REGION_RULES:
        if _arabic_keyword(keyword):
            if keyword in raw:
                return label
            continue
        if " " in keyword:
            if f" {keyword} " in padded:
                return label
            continue
        if keyword in tokens:
            return label
    return ""


def country_from_coordinates(lat: object, lng: object) -> str:
    """Nearest listed country center when the point sits close to one."""
    point = osint.valid_coord(lng, lat)
    if point is None:
        return ""
    lng_v, lat_v = point
    best = ""
    best_distance = 1.6
    for name, center in COUNTRY_CENTERS.items():
        if name == "Other":
            continue
        distance = ((lat_v - center[0]) ** 2 + (lng_v - center[1]) ** 2) ** 0.5
        if distance < best_distance:
            best_distance = distance
            best = name
    return best


def resolve_item_country(item: dict, blob: str) -> str:
    """Country where the incident happened. The source desk is used only when the text names no place."""
    named = incident_country(blob)
    if named:
        return canonical_country(named) or named
    desk = desk_for_item(item)
    handle = channel_handle(item).lower()
    home = ""
    if handle in _HANDLE_COUNTRY:
        home = _HANDLE_COUNTRY[handle]
    elif desk and desk.get("default_country") not in ("", "Dynamic"):
        home = desk["default_country"]
    if home:
        return canonical_country(home) or home
    return "Other"


def resolve_item_region(item: dict, placed: dict, blob: str) -> str:
    """Region equals the country of the incident."""
    country = resolve_item_country(item, blob)
    if country and country != "Other":
        return country
    named = canonical_country(placed.get("country") or placed.get("region"))
    return named or "Other"


def route_event_country(stored: dict, located: dict) -> dict:
    """Set country from the incident. Use the source desk only when no place is named."""
    blob = " ".join(
        str(part or "")
        for part in (
            located.get("location_name"), located.get("governorate"),
            stored.get("title"), stored.get("summary"), stored.get("original_text"),
            located.get("title"), located.get("summary"),
        )
    )
    country = resolve_item_country(stored, blob)
    if country in ("", "Other"):
        coord = country_from_coordinates(located.get("lat") or stored.get("lat"), located.get("lng") or stored.get("lng"))
        country = canonical_country(coord) or country or "Other"
    stored["country"] = country
    stored["region"] = country if country != "Other" else "Other"
    return stored


def _desk_place_hit(country: str, text: str) -> tuple[str, float, float] | None:
    if country == "Iraq":
        return match_iraq_fallback(text)
    if country == "Sudan":
        hit = match_sudan_place(text)
        if hit and hit[0] in _CHAD_KEYS:
            return None
        return hit
    if country == "Chad":
        hit = match_sudan_fallback(text)
        if hit and hit[0] in _CHAD_KEYS:
            return hit
        return None
    if country == "Saudi Arabia":
        return match_saudi_fallback(text)
    if country == "Syria":
        hit = match_geo_fallback(geo_blob(text))
        if hit and _GEO_CITY_COUNTRY.get(hit[0]) == "Syria":
            return hit
        return None
    if country in {"Red Sea / Maritime", "Strait of Hormuz / Gulf"}:
        hit = match_geo_fallback(geo_blob(text))
        if hit and _GEO_CITY_COUNTRY.get(hit[0]) == country:
            return hit
        return None
    hit = match_geo_fallback(geo_blob(text))
    if hit and _GEO_CITY_COUNTRY.get(hit[0]) == country:
        return hit
    return None


def desk_anchor_point(item: dict, raw_blob: str, coords, event_id) -> tuple[float, float, str] | None:
    """Pin a country desk to that country unless the text names an external event."""
    desk = desk_for_item(item)
    if not desk:
        return None
    home = desk.get("default_country") or ""
    if home in ("", "Dynamic"):
        return None
    named = incident_country(raw_blob)
    if named and named != home:
        return None
    hit = _desk_place_hit(home, raw_blob)
    if hit:
        if coords and not (hit[0] in _COUNTRY_OR_SEA or hit[0] in _SAUDI_COUNTRY_KEYS or hit[0] in _SUDAN_COUNTRY_KEYS or hit[0] in _IRAQ_COUNTRY_KEYS):
            return None
        shifted = offset_fallback(hit[1], hit[2], event_id)
        if not shifted:
            return None
        if home == "Saudi Arabia":
            return shifted[0], shifted[1], saudi_precision(hit[0])
        return shifted[0], shifted[1], classify_precision(hit[0])
    if coords:
        return None
    center = COUNTRY_CENTERS.get(home)
    if not center:
        return None
    shifted = offset_fallback(center[0], center[1], event_id)
    if not shifted:
        return None
    return shifted[0], shifted[1], "Country / Sea Fallback"


def placed_coordinates(item: dict, placed: dict) -> tuple[float | None, float | None, str]:
    """Use a real model point. Otherwise a place-list center with a stable tiny offset.

    The third value is a geo_precision label when GEO_FALLBACKS assigned the point.
    """
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
    anchored = desk_anchor_point(item, raw_blob, coords, event_id)
    if anchored:
        return anchored
    if not coords and not sudan_mention(raw_blob):
        iraq_hit = match_iraq_fallback(raw_blob)
        if iraq_hit:
            shifted = offset_fallback(iraq_hit[1], iraq_hit[2], event_id)
            if shifted:
                return shifted[0], shifted[1], classify_precision(iraq_hit[0])
    sudan_hit = match_sudan_place(raw_blob)
    sudan_center = COUNTRY_CENTERS.get("Sudan")
    near_sudan_centroid = bool(
        coords and sudan_center and _near_center(coords[1], coords[0], sudan_center)
    )
    if sudan_hit and (not coords or near_sudan_centroid):
        shifted = offset_fallback(sudan_hit[1], sudan_hit[2], event_id)
        if shifted:
            precision = classify_precision(sudan_hit[0])
            if precision != GEO_PRECISION_COUNTRY:
                precision = GEO_PRECISION_CITY
            return shifted[0], shifted[1], precision
    if not sudan_hit and near_sudan_centroid:
        return coords[1], coords[0], GEO_PRECISION_COUNTRY
    if not coords and not sudan_mention(raw_blob) and not iraq_mention(raw_blob):
        saudi_hit = match_saudi_fallback(raw_blob)
        if saudi_hit:
            shifted = offset_fallback(saudi_hit[1], saudi_hit[2], event_id)
            if shifted:
                return shifted[0], shifted[1], saudi_precision(saudi_hit[0])
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
            return shifted[0], shifted[1], classify_precision(hit[0])
    if coords:
        precision = ""
        if placed.get("_grok_coords"):
            precision = classify_precision(placed.get("location_name") or "")
        return coords[1], coords[0], precision
    if hit and hit[0] in _SUDAN_NON_PLACE:
        hit = None
    if hit:
        shifted = offset_fallback(hit[1], hit[2], event_id)
        if shifted:
            return shifted[0], shifted[1], classify_precision(hit[0])
    centroid = country_centroid_point(raw_blob, event_id)
    if centroid:
        lat, lng, country = centroid
        if not placed.get("country") or str(placed.get("country")) in {"Other", "Other / International"}:
            placed["country"] = country
            placed["region"] = country
        return lat, lng, GEO_PRECISION_COUNTRY
    return None, None, ""


def country_centroid_point(text: str, event_id: object) -> tuple[float, float, str] | None:
    """Country or sea center from a place keyword. Gulf States is not a stored country."""
    named = incident_country(text)
    if not named and "maritime" in geo_blob(text):
        named = "Red Sea / Maritime"
    if not named:
        return None
    country = canonical_country(named)
    if not country or country == "Other":
        return None
    if named in COUNTRY_CENTERS:
        center = COUNTRY_CENTERS[named]
    elif country in COUNTRY_CENTERS:
        center = COUNTRY_CENTERS[country]
    elif country == "Maritime":
        center = COUNTRY_CENTERS["Red Sea / Maritime"]
    else:
        return None
    shifted = offset_fallback(center[0], center[1], event_id)
    if not shifted:
        return None
    return shifted[0], shifted[1], country


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
                hit = match_sudan_place(raw)
                if hit is None and not sudan_mention(raw):
                    hit = match_iraq_fallback(raw)
                if hit is None and not sudan_mention(raw) and not iraq_mention(raw):
                    hit = match_saudi_fallback(raw)
                if hit is None:
                    hit = match_geo_fallback(geo_blob(raw))
                    if hit and hit[0] in _SUDAN_NON_PLACE:
                        hit = None
                if not hit:
                    centroid = country_centroid_point(raw, row["id"])
                    if not centroid:
                        continue
                    conn.execute(
                        "UPDATE items SET lat = ?, lng = ?, geo_precision = ?, "
                        "country = CASE WHEN country IS NULL OR country = '' OR country = 'Other' THEN ? ELSE country END, "
                        "region = CASE WHEN region IS NULL OR region = '' OR region = 'Other' "
                        "OR region = 'Other / International' THEN ? ELSE region END "
                        "WHERE id = ?",
                        (centroid[0], centroid[1], GEO_PRECISION_COUNTRY, centroid[2], centroid[2], row["id"]),
                    )
                    updated += 1
                    continue
                shifted = offset_fallback(hit[1], hit[2], row["id"])
                if not shifted:
                    continue
                precision = saudi_precision(hit[0]) if hit[0] in SAUDI_GEO_FALLBACKS else classify_precision(hit[0])
                conn.execute(
                    "UPDATE items SET lat = ?, lng = ?, geo_precision = ? WHERE id = ?",
                    (shifted[0], shifted[1], precision, row["id"]),
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
                    hit = match_sudan_place(blob)
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


def backfill_country_tags() -> int:
    """Fill country and sub_region when the row is blank or Other. A specific country stays."""
    updated = 0
    with _db_lock:
        conn = db_connect()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            if "country" not in columns:
                return 0
            rows = conn.execute(
                "SELECT id, title, summary, original_text, location_name, governorate, "
                "source_name, source_kind, source_url, lat, lng, country FROM items "
                "WHERE country IS NULL OR TRIM(country) = '' OR country = 'Other'"
            ).fetchall()
            for row in rows:
                stored = str(row["country"] or "").strip()
                if stored and stored != "Other":
                    continue
                blob = " ".join(
                    str(part or "")
                    for part in (
                        row["location_name"], row["governorate"], row["title"],
                        row["summary"], row["original_text"],
                    )
                )
                item = {
                    "source_name": row["source_name"],
                    "source_kind": row["source_kind"],
                    "source_url": row["source_url"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "original_text": row["original_text"],
                }
                country = resolve_item_country(item, blob)
                if country in ("", "Other"):
                    country = country_from_coordinates(row["lat"], row["lng"]) or country or "Other"
                sub_region = sub_region_for(blob, country)
                if "sub_region" in columns:
                    conn.execute(
                        "UPDATE items SET country = ?, sub_region = ? WHERE id = ? "
                        "AND (country IS NULL OR TRIM(country) = '' OR country = 'Other')",
                        (country, sub_region, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE items SET country = ? WHERE id = ? "
                        "AND (country IS NULL OR TRIM(country) = '' OR country = 'Other')",
                        (country, row["id"]),
                    )
                updated += 1
            if updated:
                conn.commit()
                print(f"Backfilled country tags for {updated} event(s).")
        finally:
            conn.close()
    return updated


def _country_from_source_name(source_name: object) -> str:
    handle = str(source_name or "").strip().lstrip("@").lower()
    if "·" in handle:
        handle = handle.split("·", 1)[1].strip()
    if handle in _HANDLE_COUNTRY:
        return _HANDLE_COUNTRY[handle]
    desk = _DESK_BY_HANDLE.get(handle)
    if desk and desk.get("default_country") not in ("", "Dynamic", None):
        return str(desk["default_country"])
    return ""


def backfill_levant_routes() -> int:
    """Retag Levant rows from the place, the coordinates, and the source name."""
    updated = 0
    with _db_lock:
        conn = db_connect()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            if "country" not in columns:
                return 0
            rows = conn.execute(
                "SELECT id, location_name, lat, lng, source_name, region, country FROM items "
                "WHERE region = 'Levant' OR country = 'Levant'"
            ).fetchall()
            for row in rows:
                named = incident_country(str(row["location_name"] or ""))
                country = canonical_country(named) or named
                if not country:
                    country = canonical_country(country_from_coordinates(row["lat"], row["lng"]))
                if not country:
                    country = canonical_country(_country_from_source_name(row["source_name"]))
                if not country:
                    stored = canonical_country(row["country"])
                    country = stored if stored and stored != "Other" else "Other"
                region = country
                if str(row["region"] or "") == region and str(row["country"] or "") == country:
                    continue
                conn.execute(
                    "UPDATE items SET region = ?, country = ? WHERE id = ?",
                    (region, country, row["id"]),
                )
                updated += 1
            if updated:
                conn.commit()
                print(f"Retagged {updated} Levant event(s) by country.")
        finally:
            conn.close()
    return updated


SYRIA_GOVERNORATE_COORDS = {
    "Damascus": (33.5138, 36.2765),
    "Rif Dimashq": (33.6500, 36.5500),
    "Aleppo": (36.2021, 37.1343),
    "Idlib": (35.9306, 36.6339),
    "Latakia": (35.5317, 35.7901),
    "Tartus": (34.8959, 35.8867),
    "Homs": (34.7324, 36.7137),
    "Hama": (35.1318, 36.7578),
    "Deir ez-Zor": (35.3359, 40.1408),
    "Raqqa": (35.9594, 39.0079),
    "Al-Hasakah": (36.5072, 40.7485),
    "Daraa": (32.6189, 36.1021),
    "Quneitra": (33.1256, 35.8244),
    "As-Suwayda": (32.7090, 36.5662),
}
SYRIA_GOVERNORATE_KEYWORDS = {
    "Rif Dimashq": (
        "damascus countryside", "rural damascus", "rif dimashq",
        "ريف دمشق", "الزبداني", "zabadani",
    ),
    "Deir ez-Zor": (
        "deir ez-zor", "deir ez zor", "deir ezzor", "deir al-zor", "deir al zor", "deir al-zour",
        "al mayadin", "al bukamal", "abu kamal", "دير الزور", "الميادين", "البوكمال",
    ),
    "Al-Hasakah": (
        "al-hasakah", "al hasakah", "hasakah", "tal tamer", "tal tamr", "qamishli", "qamishlo",
        "الحسكة", "القامشلي",
    ),
    "As-Suwayda": ("as-suwayda", "as suwayda", "suwayda", "sweida", "السويداء"),
    "Quneitra": ("quneitra bir ajam", "quneitra", "bir ajam", "beit jinn", "القنيطرة"),
    "Latakia": ("latakia", "lattakia", "اللاذقية"),
    "Damascus": ("damascus", "دمشق"),
    "Aleppo": ("aleppo", "manbij", "حلب", "منبج"),
    "Idlib": ("idlib", "idleb", "إدلب", "ادلب"),
    "Tartus": ("tartous", "tartus", "baniyas", "طرطوس", "بانياس"),
    "Homs": ("palmyra tadmur", "dabousiya crossing", "palmyra", "tadmur", "homs", "al tanf", "حمص", "تدمر"),
    "Hama": ("hama", "حماة"),
    "Raqqa": ("raqqa", "الرقة"),
    "Daraa": ("jaber nasib crossing", "daraa", "درعا"),
}
_SYRIA_GOVERNORATE_ORDER = tuple(sorted(
    ((keyword, name) for name, keywords in SYRIA_GOVERNORATE_KEYWORDS.items() for keyword in keywords),
    key=lambda item: len(item[0]),
    reverse=True,
))
_MATCHED_SITE_PRECISION = {GEO_PRECISION_EXACT, GEO_PRECISION_CITY}


def _syria_row_scope(item: dict, country: str) -> bool:
    """Syria rows, and Syria-desk rows that were not placed in another country."""
    if country == "Syria":
        return True
    desk = desk_for_item(item)
    return bool(desk and desk.get("desk") == "Syria" and country in ("", "Other"))


def _syria_keyword_hit(record: dict) -> str:
    """Longer place names win. Rural Damascus is Rif Dimashq, not Damascus."""
    raw = " ".join(
        str(record.get(key) or "")
        for key in ("governorate", "location_name", "title", "summary")
    )
    padded = f" {geo_blob(raw)} "
    tokens = set(padded.split())
    for keyword, name in _SYRIA_GOVERNORATE_ORDER:
        if _arabic_keyword(keyword):
            if keyword in raw:
                return name
            continue
        folded = geo_blob(keyword)
        if " " in folded:
            if f" {folded} " in padded:
                return name
            continue
        if folded in tokens:
            return name
    return ""


def _exact_syria_coordinates(record: dict) -> bool:
    if osint.valid_coord(record.get("lng"), record.get("lat")) is None:
        return False
    precision = str(record.get("geo_precision") or "")
    if precision in ("", "Country / Sea Fallback", "Damascus Default Fallback"):
        return False
    return True


def _damascus_default_point(event_id: object) -> tuple[float, float]:
    digest = hashlib.sha256(str(event_id or "event").encode("utf-8")).digest()
    lat_shift = (digest[0] - 128) / 128 * 0.015
    lng_shift = (digest[1] - 128) / 128 * 0.015
    return 33.5138 + lat_shift, 36.2765 + lng_shift


def resolve_syria_governorate(record: dict) -> dict:
    """Match a Syrian governorate. Unmatched Syria rows fall back to Damascus."""
    country = str(record.get("country") or "")
    if country != "Syria" and not _syria_row_scope(record, country):
        return {}
    name = _syria_keyword_hit(record)
    event_id = record.get("id") or record.get("source_url") or record.get("title")
    if name:
        result = {"governorate": name, "sub_region": name}
        if not _exact_syria_coordinates(record):
            center = SYRIA_GOVERNORATE_COORDS[name]
            shifted = offset_fallback(center[0], center[1], event_id)
            if shifted:
                result["lat"] = shifted[0]
                result["lng"] = shifted[1]
                result["geo_precision"] = GEO_PRECISION_CITY
        return result
    result = {"governorate": "Damascus", "sub_region": "Damascus"}
    if _exact_syria_coordinates(record):
        return result
    lat, lng = _damascus_default_point(event_id)
    result["lat"] = lat
    result["lng"] = lng
    result["geo_precision"] = "Damascus Default Fallback"
    return result


def backfill_syria_governorates() -> int:
    """Match Syria rows to a governorate. Unmatched rows use the Damascus fallback."""
    updated = 0
    with _db_lock:
        conn = db_connect()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            if "country" not in columns:
                return 0
            rows = conn.execute(
                "SELECT id, title, summary, location_name, governorate, sub_region, lat, lng, "
                "geo_precision, country, source_name, source_kind, source_url FROM items "
                "WHERE country = 'Syria'"
            ).fetchall()
            for row in rows:
                resolved = resolve_syria_governorate(dict(row))
                if not resolved:
                    continue
                same = (
                    str(row["governorate"] or "") == resolved["governorate"]
                    and str(row["sub_region"] or "") == resolved["sub_region"]
                    and ("lat" not in resolved or (row["lat"] == resolved["lat"] and row["lng"] == resolved["lng"]))
                    and ("geo_precision" not in resolved or str(row["geo_precision"] or "") == resolved["geo_precision"])
                )
                if same:
                    continue
                if "lat" in resolved:
                    conn.execute(
                        "UPDATE items SET governorate = ?, sub_region = ?, lat = ?, lng = ?, geo_precision = ? "
                        "WHERE id = ?",
                        (
                            resolved["governorate"], resolved["sub_region"], resolved["lat"], resolved["lng"],
                            resolved["geo_precision"], row["id"],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE items SET governorate = ?, sub_region = ? WHERE id = ?",
                        (resolved["governorate"], resolved["sub_region"], row["id"]),
                    )
                updated += 1
            if updated:
                conn.commit()
                print(f"Resolved {updated} Syria governorate(s).")
        finally:
            conn.close()
    return updated


def backfill_threat_codes() -> int:
    """Remap unlabeled rows onto threat codes 1-12. A stored code 1-11 stays.

    Syria rows with no code are filled with 1-12. Other / Advisory becomes 12
    unless the text matches an earlier code. Spam is left for the spam purge
    and is not labeled Miscellaneous.
    """
    updated = 0
    old_labels = {
        "ACT", "SFA", "VC", "CU", "HS", "OD",
        "Kinetic/Military", "Maritime", "Security/Clashes", "Political/Other",
        ADVISORY_LABEL, "Miscellaneous",
    }
    with _db_lock:
        conn = db_connect()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            if "threat_code" not in columns:
                return 0
            fields = ["id", "title", "summary", "original_text", "category", "threat_code"]
            for extra in ("country", "source_name", "source_kind", "source_url"):
                if extra in columns:
                    fields.append(extra)
            rows = conn.execute(
                f"SELECT {', '.join(fields)} FROM items "
                "WHERE threat_code IS NULL OR threat_code = 0 OR threat_code = '' "
                "OR category IN ('Other / Advisory', 'Political/Other', 'Miscellaneous')"
            ).fetchall()
            for row in rows:
                blob = " ".join(str(row[key] or "") for key in ("title", "summary", "original_text", "category"))
                if is_spam_post(blob):
                    continue
                existing = row["threat_code"]
                try:
                    existing_code = int(existing) if existing not in (None, "") else None
                except (TypeError, ValueError):
                    existing_code = None
                if existing_code in THREAT_LABELS:
                    label = THREAT_LABELS[existing_code]
                    if str(row["category"] or "") == label:
                        continue
                    conn.execute(
                        "UPDATE items SET category = ? WHERE id = ?",
                        (label, row["id"]),
                    )
                    updated += 1
                    continue
                code, label = classify_threat(blob, row["category"])
                country = str(row["country"] or "") if "country" in row.keys() else ""
                syria = _syria_row_scope(
                    {
                        "source_name": row["source_name"] if "source_name" in row.keys() else "",
                        "source_kind": row["source_kind"] if "source_kind" in row.keys() else "",
                        "source_url": row["source_url"] if "source_url" in row.keys() else "",
                    },
                    country,
                )
                legacy = str(row["category"] or "") in old_labels or str(row["category"] or "").upper() in _OLD_ADVISORY
                if not code and (syria or legacy):
                    code, label = 12, THREAT_LABELS[12]
                if not code:
                    continue
                conn.execute(
                    "UPDATE items SET threat_code = ?, category = ? WHERE id = ?",
                    (code, label, row["id"]),
                )
                updated += 1
            if updated:
                conn.commit()
                print(f"Remapped {updated} threat code(s).")
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
    """Dating, gambling, promo, bare-link, and empty posts. A place name by itself is not spam."""
    raw = str(text or "")
    if _SPAM_BOUNDARY_RE.search(raw):
        return True
    if any(token in raw for token in _SPAM_ARABIC_TERMS):
        return True
    try:
        import feed_quality

        return feed_quality.is_noise(raw)
    except Exception:
        return False


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


def channel_handle(item: dict) -> str:
    name = str(item.get("source_name") or "")
    if "·" in name:
        name = name.split("·", 1)[1]
    return name.strip().lstrip("@").lower()


def coverage_region(item: dict, placed: dict) -> dict:
    """Pin the event to the country it names. A source desk fills in only when the text names none."""
    placed = dict(placed)
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
    country = resolve_item_country(item, blob)
    if country in ("", "Other") and item.get("source_kind") == "sweep":
        country = "Iraq" if iraq_mention(blob) else "Syria"
    if country:
        placed["country"] = country
        placed["region"] = country
    return placed


def item_source_tier(item: dict, source_url: str) -> str:
    if item.get("source_kind") == "sweep":
        explicit = str(item.get("source_tier") or "").strip()
        if explicit in {"Wire / Outlet", "OSINT Sweep"}:
            return explicit
    return source_tier_for(item.get("source_kind") or "", item.get("source_name") or "", source_url)


def tagged_actor(existing: object, text: str) -> str:
    """Keep a classified actor. Fill SAF or RSF only when the actor is still blank."""
    current = " ".join(str(existing or "").split())
    if current and current != "Unattributed":
        return current
    return osint.coerce_actor("", text)


def tagged_system(existing: object, text: str) -> str:
    """Keep a classified system. Fill drone or artillery tags only when none is set."""
    current = " ".join(str(existing or "").split())
    if current:
        return current
    return osint.coerce_system("", text)


def backfill_actor_system() -> int:
    """Fill blank actors and systems from the text. Threat codes and specific actors stay."""
    updated = 0
    with _db_lock:
        conn = db_connect()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            if "actor" not in columns or "system_type" not in columns:
                return 0
            text_fields = [name for name in ("title", "summary", "original_text", "location_name") if name in columns]
            rows = conn.execute(
                "SELECT id, actor, system_type, "
                + ", ".join(text_fields)
                + " FROM items WHERE actor IS NULL OR actor = '' OR actor = 'Unattributed' "
                "OR system_type IS NULL OR system_type = ''"
            ).fetchall()
            for row in rows:
                blob = " ".join(str(row[name] or "") for name in text_fields)
                old_actor = row["actor"] or ""
                old_system = row["system_type"] or ""
                actor = tagged_actor(old_actor, blob)
                system = tagged_system(old_system, blob)
                if actor == old_actor and system == old_system:
                    continue
                if not old_actor and actor == "Unattributed" and system == old_system:
                    continue
                conn.execute(
                    "UPDATE items SET actor = ?, system_type = ? WHERE id = ?",
                    (actor, system, row["id"]),
                )
                updated += 1
            if updated:
                conn.commit()
        finally:
            conn.close()
    if updated:
        print(f"Tagged actor or system on {updated} items.")
    return updated


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
    placed = dict(placed)
    placed["country"] = resolve_item_country(item, blob)
    placed["region"] = placed["country"] if placed["country"] and placed["country"] != "Other" else resolve_item_region(item, placed, blob)
    if placed["region"] == "Other":
        placed["country"] = "Other"
    placed["sub_region"] = sub_region_for(blob, placed["country"])
    lat, lng, fallback_precision = placed_coordinates(item, placed)
    if fallback_precision:
        placed = dict(placed)
        placed["geo_precision"] = fallback_precision
    elif placed.get("_grok_coords"):
        placed = dict(placed)
        placed["geo_precision"] = classify_precision(placed.get("location_name") or "")
    if is_spam_post(blob):
        print(f"Dropped spam before insert: {str(item.get('title') or '')[:80]}")
        return False
    threat_code, threat_label = assigned_threat(blob, item.get("category") or placed.get("category"))
    placed["category"] = threat_label
    placed["threat_code"] = threat_code
    if _syria_row_scope(item, placed.get("country") or ""):
        resolved = resolve_syria_governorate({
            "country": "Syria" if placed.get("country") in ("", "Other") else placed.get("country"),
            "governorate": placed.get("governorate") or "",
            "location_name": placed.get("location_name") or "",
            "title": item.get("title") or "",
            "summary": item.get("summary") or "",
            "lat": lat,
            "lng": lng,
            "geo_precision": placed.get("geo_precision") or "",
            "source_name": item.get("source_name") or "",
            "source_kind": item.get("source_kind") or "",
            "source_url": item.get("source_url") or "",
            "id": item.get("source_url") or item.get("title") or "",
        })
        if resolved:
            placed["governorate"] = resolved["governorate"]
            placed["sub_region"] = resolved["sub_region"]
            if "lat" in resolved:
                lat = resolved["lat"]
                lng = resolved["lng"]
                placed["geo_precision"] = resolved["geo_precision"]
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
                    corroboration_count, published_at, ingested_at, geo_precision, country, sub_region, threat_code,
                    actor, system_type, amplification_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    item_source_tier(item, source_url),
                    placed.get("region") or "Other / International",
                    placed.get("governorate") or "",
                    placed.get("location_name") or "",
                    lat,
                    lng,
                    osint.live_severity(placed.get("severity")),
                    placed.get("category") or "",
                    1,
                    osint.format_timestamp(moment),
                    osint.format_timestamp(now),
                    placed.get("geo_precision") or "",
                    placed.get("country") or "",
                    placed.get("sub_region") or "",
                    placed.get("threat_code"),
                    tagged_actor(placed.get("actor"), blob),
                    tagged_system(placed.get("system_type"), blob),
                    0,
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
            echo = amplification_original(conn, item)
            if echo is not None:
                bump_amplification(conn, echo["id"])
                conn.commit()
                print(f"Amplified story {echo['id']} inside 30 minutes: {str(item.get('title') or '')[:80]}")
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
    placed = osint.locate_event(
        item["title"],
        location_name=item.get("location_name") or item.get("summary") or item["title"],
        lat=item.get("lat"),
        lng=item.get("lng"),
        region=item.get("region"),
    )
    placed = coverage_region(item, placed)
    if placed.get("relevant") is False:
        print(f"Dropped non-news spam from classifier: {str(item.get('title') or '')[:80]}")
        return False
    stored = classify_and_translate_event(item, placed)
    if stored.get("relevant") is False or is_spam_post(
        spam_post_text(stored.get("title"), stored.get("summary"), stored.get("original_text"), stored.get("source_url"))
    ):
        print(f"Dropped non-news spam from classifier: {str(item.get('title') or '')[:80]}")
        return False
    if not str(stored.get("original_text") or "").strip():
        stored["original_text"] = original
    if get_xai_api_key() and (
        contains_arabic(stored.get("title")) or contains_arabic(stored.get("summary"))
    ):
        print("Translation did not return English. The item was not stored.")
        return False
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
            try:
                import global_feeds

                global_feeds.kick()
            except Exception as exc:
                print(f"Global feed refresh was not started: {exc.__class__.__name__}.")
            now = datetime.now(timezone.utc)
            backfill_coordinates(now)
            backfill_sudan_rows()
            backfill_iraq_rows()
            backfill_country_tags()
            backfill_levant_routes()
            backfill_syria_governorates()
            backfill_threat_codes()
            backfill_actor_system()
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
                rss_items.extend(fetch_khartoum_err())
            except Exception as exc:
                print(f"Khartoum Err failed: {exc.__class__.__name__}.")
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
            try:
                schedule_brief_after_scrape()
            except Exception as exc:
                print(f"Geopolitical summary schedule failed: {exc.__class__.__name__}.")
            try:
                translate_arabic_rows()
            except Exception as exc:
                print(f"Arabic translation pass failed: {exc.__class__.__name__}.")


def run_poll() -> dict:
    return ingest_feeds()


def poll_loop() -> None:
    while True:
        try:
            ingest_feeds()
        except Exception as exc:
            print(f"Feed poll failed: {exc.__class__.__name__}.")
        time.sleep(POLL_SECONDS)


SWEEP_INTERVAL = 20 * 60
SWEEP_MODEL = "grok-3"
SWEEP_TIMEOUT = 25
SWEEP_URL = "https://api.x.ai/v1/chat/completions"
_sweep_started = False
_sweep_key_noted = False
_SWEEP_UNNAMED = {"", "osint sweep", "unknown", "unattributed", "n/a", "none", "null"}


def get_xai_api_key() -> str:
    """Return the xAI key. A missing key is an empty string and is never printed."""
    return osint.read_api_key()


def sweep_source_tier(name: object) -> str:
    named = " ".join(str(name or "").split())
    if named.lower() in _SWEEP_UNNAMED:
        return "OSINT Sweep"
    return "Wire / Outlet"


def stored_events_since(cutoff: datetime) -> list[dict]:
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT title, source_url, published_at FROM items ORDER BY id DESC LIMIT 400"
            ).fetchall()
        finally:
            conn.close()
    found = []
    for row in rows:
        moment = parse_event_time(row["published_at"])
        if moment is None or moment < cutoff:
            continue
        found.append({"title": str(row["title"] or "")[:180], "source_url": str(row["source_url"] or "")})
    return found[:80]


def parse_sweep_payload(text: str) -> list[dict]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def request_sweep_events(key: str, known: list[dict], now: datetime) -> list[dict]:
    governorates = ", ".join(osint.SYRIA_GOVERNORATES)
    prompt = (
        "Return strict JSON only. No markdown. Do not use tools or live search plugins. "
        "List breaking security, border, energy, or infrastructure incidents from the past 2 hours "
        "in Syria, covering all 14 governorates (" + governorates + "), and in Iraq or the "
        "Kurdistan Region. Omit anything already stored. Each event is one object in events. "
        "title and summary are concise English. location_name is the most specific place. "
        "governorate is a Syrian governorate or an empty string. "
        "country and region are the same and exactly one of: " + ", ".join(REGIONS) + ". "
        "Use the country where the incident happened. Do not use Levant or a theater bucket. "
        "lat and lng are decimal numbers for that place. severity is High, Medium, or Low. "
        "category is Kinetic/Military, Maritime, Security/Clashes, or Political/Other. "
        "source_name is the named wire or outlet, or an empty string when none is named. "
        "source_url is the original http(s) URL, or an empty string. "
        "published_at is an ISO-8601 UTC timestamp inside the past 2 hours. "
        "Do not invent an incident that is not a real reported event. "
        "If there is nothing new, return {\"events\": []}.\n"
        f"As of {osint.format_timestamp(now)} UTC.\n"
        "Already stored:\n"
        + json.dumps(known, ensure_ascii=False)
    )
    response = requests.post(
        SWEEP_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": SWEEP_MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Reply with one JSON object and nothing else."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=SWEEP_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    content = ""
    choices = body.get("choices") if isinstance(body, dict) else None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = str(message.get("content") or "")
    return parse_sweep_payload(content)


def sweep_item(event: dict, now: datetime) -> dict | None:
    title = osint.one_sentence(event.get("title") or "")[:240]
    summary = osint.one_sentence(event.get("summary") or title)[:500]
    if not title:
        return None
    published = parse_event_time(event.get("published_at"))
    if published is not None and published < now - timedelta(hours=2, minutes=5):
        return None
    place_blob = f"{title} {summary} {event.get('location_name') or ''}"
    country = incident_country(place_blob) or ("Iraq" if iraq_mention(place_blob) else "")
    country = canonical_country(country) or country
    if not country and not mentions_syria_or_middle_east(place_blob):
        return None
    if not country:
        country = "Syria"
    region = country
    source_name = " ".join(str(event.get("source_name") or "").split())
    source_url = str(event.get("source_url") or "").strip()
    if not source_url.startswith("http"):
        source_url = "https://osint-sweep.local/event/" + headline_hash(title)
    lat = event.get("lat")
    lng = event.get("lng")
    try:
        lat = float(lat) if lat is not None and lat != "" else None
        lng = float(lng) if lng is not None and lng != "" else None
    except (TypeError, ValueError):
        lat = lng = None
    return {
        "title": title,
        "summary": summary or title,
        "location_name": osint.one_sentence(event.get("location_name") or "")[:160],
        "source_url": source_url,
        "source_name": source_name or "OSINT Sweep",
        "source_kind": "sweep",
        "source_tier": sweep_source_tier(source_name),
        "published_at": osint.format_timestamp(published or now),
        "region": region,
        "lat": lat,
        "lng": lng,
        "original_text": "",
    }


def run_grok_sweep() -> int:
    """One Syria and Iraq sweep. A missing key skips the call and does not delete rows."""
    global _sweep_key_noted
    key = get_xai_api_key()
    if not key:
        if not _sweep_key_noted:
            print("XAI_API_KEY is missing. Syria and Iraq live sweep skipped. Rows and hotspots were not changed.")
            _sweep_key_noted = True
        return 0
    now = datetime.now(timezone.utc)
    try:
        events = request_sweep_events(key, stored_events_since(now - timedelta(hours=2)), now)
    except Exception as exc:
        print(f"Syria and Iraq live sweep failed: {osint.public_error(exc, key)}.")
        return 0
    inserted = 0
    for event in events:
        item = sweep_item(event, now)
        if item is None:
            continue
        if is_spam_post(spam_post_text(item.get("title"), item.get("summary"), "", item.get("source_url"))):
            print(f"Dropped spam before sweep insert: {item['title'][:80]}")
            continue
        try:
            if save_classified_item(item, now):
                inserted += 1
        except Exception as exc:
            print(f"Sweep insert failed: {exc.__class__.__name__}.")
    if inserted:
        print(f"Syria and Iraq live sweep ingested {inserted} event(s).")
    return inserted


def sweep_loop() -> None:
    while True:
        try:
            run_grok_sweep()
        except Exception as exc:
            print(f"Syria and Iraq live sweep failed: {exc.__class__.__name__}.")
        time.sleep(SWEEP_INTERVAL)


def start_sweep_daemon() -> None:
    global _sweep_started
    with _start_lock:
        if _sweep_started:
            return
        _sweep_started = True
    threading.Thread(target=sweep_loop, name="grok-sweep", daemon=True).start()


EMPTY_REGION_TEXT = "No significant incidents recorded in the past 24 hours."
EMPTY_WINDOW_TEXT = "No significant incidents recorded in this 07:00–07:00 window."
SUMMARY_INTERVAL = 60 * 60
SCRAPE_HOURS = (3, 7, 11, 15, 19, 23)
ROLLING_HOURS = (3, 11, 15, 19, 23)
OFFICIAL_HOUR = 7
BRIEF_CYCLE = timedelta(hours=4)
_summary_lock = threading.Lock()
_brief_refresh_lock = threading.Lock()
_summary_started = False
_briefing_started = False
_threads_started = False
_startup_brief_checked = False
_briefing_slots: set[str] = set()
LONDON = ZoneInfo("Europe/London")
BRIEF_PATH = ROOT / "daily_brief.json"
CLIENT_PROFILES = ROOT / "client_profiles.json"
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def london_cycle(now: datetime | None = None, view: str = "completed") -> tuple[datetime, datetime]:
    """Europe/London 07:00 windows.

    Completed, when now is at or after 07:00: yesterday 07:00:00 through today 07:00:00.
    Completed, when now is before 07:00: two days ago 07:00:00 through yesterday 07:00:00.
    Active: the open cycle from the latest 07:00:00 through now (today 07:00 when that hour has passed).
    """
    moment = now.astimezone(LONDON) if now is not None else datetime.now(LONDON)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LONDON)
    else:
        moment = moment.astimezone(LONDON)
    today_seven = moment.replace(hour=7, minute=0, second=0, microsecond=0)
    if str(view or "completed").strip().lower() == "active":
        start = today_seven if moment >= today_seven else today_seven - timedelta(days=1)
        if start > moment:
            start = today_seven - timedelta(days=1)
        return start, moment
    end = today_seven if moment >= today_seven else today_seven - timedelta(days=1)
    return end - timedelta(days=1), end


def _in_cycle(moment: datetime, start: datetime, end: datetime) -> bool:
    stamp = moment.astimezone(timezone.utc)
    return start.astimezone(timezone.utc) <= stamp < end.astimezone(timezone.utc)


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
        name = region_name(props.get("country") or props.get("region"))
        if name not in groups:
            continue
        groups[name].append(
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
    record = None
    for row in rows:
        scope = row["scope"]
        if scope == "__geo_record__":
            try:
                parsed = json.loads(row["summary"] or "")
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                record = parsed
            continue
        text = " ".join(str(row["summary"] or "").split())
        if scope == "overall" and text:
            overall = text
        elif scope in regions and text:
            regions[scope] = text
        stamp = " ".join(str(row["updated_at"] or "").split())
        if stamp > updated:
            updated = stamp
    payload = {"overall": overall, "regions": regions, "updated_at": updated}
    if not record:
        return payload
    if str(record.get("overall") or "").strip():
        payload["overall"] = " ".join(str(record["overall"]).split())
    raw_regions = record.get("regions") if isinstance(record.get("regions"), dict) else {}
    for name in REGIONS:
        note = " ".join(str(raw_regions.get(name) or "").split())
        if note:
            payload["regions"][name] = note
    payload["window_label"] = " ".join(str(record.get("window_label") or "").split())
    generated = " ".join(str(record.get("generated_at") or "").split())
    if generated:
        payload["generated_at"] = generated
        payload["updated_at"] = generated
    try:
        payload["source_count"] = int(record.get("source_count") or 0)
    except (TypeError, ValueError):
        payload["source_count"] = 0
    return payload


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


def _clean_summary_text(text: str) -> str:
    return " ".join(_HOUTHI.sub("Ansarallah", strip_lateness(text)).split())


def _summary_record(title: str, summary: str, moment: datetime, region: str, severity: str, category: str, location: str, source_name: str, source_tier: str, corroboration_count: object = 1) -> dict:
    try:
        count = int(corroboration_count or 1)
    except (TypeError, ValueError):
        count = 1
    return {
        "timestamp": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": region_name(region),
        "severity": osint.live_severity(severity),
        "category": threat_category_label(category) if str(category or "").strip() else "",
        "location_name": osint.one_sentence(location)[:160],
        "title": osint.one_sentence(title or summary)[:240],
        "summary": osint.one_sentence(summary or title)[:500],
        "source_name": osint.one_sentence(source_name)[:160],
        "source_tier": osint.one_sentence(source_tier)[:80],
        "corroboration_count": count if count > 0 else 1,
    }


def _record_stamp(record: dict) -> datetime | None:
    return parse_event_time(record.get("timestamp"))


def window_records(start: datetime, end: datetime) -> list[dict]:
    """Rows already in osint_events.db or hotspots.json whose timestamps fall inside the window."""
    found: list[dict] = []
    seen: set[str] = set()

    def add(record: dict | None, moment: datetime | None) -> None:
        if not record or moment is None or not _in_cycle(moment, start, end):
            return
        key = f"{record['timestamp']}|{record['title'].lower()}"
        if key in seen:
            return
        seen.add(key)
        found.append(record)

    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT title, summary, region, severity, category, location_name, "
                "source_name, source_kind, source_url, source_tier, corroboration_count, "
                "published_at, ingested_at FROM items"
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        if is_excluded_source(row["source_name"], row["source_url"], row["source_kind"]):
            continue
        moment = parse_event_time(row["published_at"]) or parse_event_time(row["ingested_at"])
        stored = row["source_tier"] or ""
        computed = source_tier_for(row["source_kind"], row["source_name"], row["source_url"])
        if row["source_kind"] == "sweep" and stored in {"Wire / Outlet", "OSINT Sweep"}:
            tier = stored
        else:
            tier = computed or stored
        name = row["source_name"] or source_label(row["source_kind"] or "", "")
        english_title, english_summary = visible_english(row["title"], row["summary"])
        if not english_title:
            continue
        add(
            _summary_record(
                english_title,
                english_summary,
                moment,
                row["region"],
                row["severity"],
                row["category"],
                row["location_name"],
                name,
                tier,
                row["corroboration_count"],
            ) if moment else None,
            moment,
        )
    try:
        features = load_features()
    except Exception as exc:
        print(f"Hotspot read failed: {exc.__class__.__name__}.")
        features = []
    for feature in features:
        props = feature.get("properties") or {}
        if is_excluded_source(props.get("source_name"), props.get("source"), props.get("source_handle")):
            continue
        moment = parse_event_time(props.get("timestamp"))
        source_name = props.get("source_name") or props.get("source_label") or props.get("source_handle") or ""
        computed = source_tier_for("", source_name, props.get("source_url") or props.get("source") or "")
        stored = str(props.get("source_tier") or "")
        tier = stored if stored in {"Wire / Outlet", "OSINT Sweep"} and computed == "Regional Media" else (computed or stored)
        english_title, english_summary = visible_english(props.get("title"), props.get("summary"))
        if not english_title:
            continue
        add(
            _summary_record(
                english_title,
                english_summary,
                moment,
                props.get("region"),
                props.get("severity"),
                props.get("category"),
                props.get("location_name") or props.get("location"),
                source_name,
                tier,
                props.get("corroboration_count"),
            ) if moment else None,
            moment,
        )
    found.sort(key=lambda item: item["timestamp"], reverse=True)
    return found


def _top_records(records: list[dict]) -> list[dict]:
    def sort_key(item: dict) -> tuple:
        moment = _record_stamp(item) or datetime.min.replace(tzinfo=timezone.utc)
        return (_SEVERITY_RANK.get(str(item.get("severity") or "").lower(), 1), -moment.timestamp())

    return sorted(records, key=sort_key)


def _sohr_record(record: dict) -> bool:
    blob = f"{record.get('source_name') or ''} {record.get('source_tier') or ''}".lower()
    return str(record.get("source_tier") or "") == "SOHR" or "syriahr" in blob or "sohr" in re.findall(r"[a-z0-9_]+", blob)


def _single_tipoff(record: dict) -> bool:
    if str(record.get("source_tier") or "") != "Tip-off":
        return False
    try:
        count = int(record.get("corroboration_count") or 1)
    except (TypeError, ValueError):
        count = 1
    return count <= 1


def apply_summary_caveats(text: str, records: list[dict]) -> str:
    """Keep SOHR casualty wording and single-source tip-offs out of the confirmed voice."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned or cleaned in {EMPTY_WINDOW_TEXT, EMPTY_REGION_TEXT}:
        return cleaned
    if any(_sohr_record(item) for item in records) and _CASUALTY_WORDS.search(cleaned) and _SOHR_PHRASE not in cleaned.lower():
        cleaned = cleaned.rstrip(".") + f". Casualty figures are cited {_SOHR_PHRASE}."
    if any(_single_tipoff(item) for item in records) and "unverified tip-off" not in cleaned.lower():
        cleaned = cleaned.rstrip(".") + ". A single-source aggregator post is an unverified tip-off and is not confirmed."
    if any(str(item.get("source_tier") or "") == "State / Party-Aligned Claim" for item in records) and "until a wire" not in cleaned.lower():
        cleaned = cleaned.rstrip(".") + ". State and party-aligned accounts are claims until a wire, UN agency, or independent outlet confirms them."
    if any("iranintl" in f"{item.get('source_name') or ''} {item.get('source_tier') or ''}".lower() for item in records) and "opposition framing" not in cleaned.lower():
        cleaned = cleaned.rstrip(".") + ". Iran International is attributed as opposition framing."
    return cleaned


def deterministic_window_summary(records: list[dict], start: datetime, end: datetime) -> tuple[str, dict[str, str]]:
    """Panel text from the highest-severity stored events. No network call."""
    grouped: dict[str, list[dict]] = {name: [] for name in REGIONS}
    for item in records:
        name = region_name(item.get("country") or item.get("region"))
        if name in grouped:
            grouped[name].append(item)
    label = format_briefing_window(start, end)

    def cited(item: dict) -> str:
        place = item.get("location_name") or item.get("region") or "location unavailable"
        tier = item.get("source_tier") or "unattributed"
        source = item.get("source_name") or "unnamed source"
        title = item.get("title") or item.get("summary") or "Incident"
        return f"{title} ({place}; {item.get('severity') or 'Medium'}; {tier}; {source})"

    if not records:
        overall = EMPTY_WINDOW_TEXT
    else:
        lead = _top_records(records)[0]
        overall = (
            f"During {label}, {len(records)} recorded incident"
            f"{'' if len(records) == 1 else 's'} are on file. "
            f"The leading recorded item is {cited(lead)}."
        )
    regions = {}
    for name in REGIONS:
        chosen = _top_records(grouped[name])[:3]
        if not chosen:
            regions[name] = EMPTY_WINDOW_TEXT
        else:
            regions[name] = f"{name}: " + " ".join(cited(item) for item in chosen)
    overall = apply_summary_caveats(overall, records)
    regions = {
        name: apply_summary_caveats(text, grouped[name])
        for name, text in regions.items()
    }
    return overall, regions


def _grouped_records(records: list[dict]) -> dict[str, list[dict]]:
    grouped = {name: [] for name in REGIONS}
    for item in records:
        name = region_name(item.get("country") or item.get("region"))
        if name not in grouped:
            continue
        grouped[name].append(
            {
                key: item.get(key) if key == "corroboration_count" else (item.get(key) or "")
                for key in (
                    "timestamp", "region", "severity", "category", "location_name",
                    "title", "summary", "source_name", "source_tier", "corroboration_count",
                )
            }
        )
    return grouped


def call_window_summary(records: list[dict], start: datetime, end: datetime, key: str) -> tuple[str, dict[str, str]]:
    grouped = _grouped_records(records)
    prompt = (
        "Write the Emerald house geopolitical summary in English. "
        "Use only the recorded incidents below. Do not use live search, web search, or X search. "
        "Do not add events, casualties, actors, or locations that are not in this list.\n"
        f"Window: {format_briefing_window(start, end)}. "
        "This is a Europe/London 07:00–07:00 cycle already stored in the event database.\n"
        "Attribute each point with its source_name and source tier. Use these tier labels when they apply: "
        "Official, State Media, Wire / Outlet, Wire / Local Outlet, Lead (Unverified), "
        "Institutional / Research, Specialist Analyst, SOHR, and Tip-off.\n"
        "Institutional / Research is attributed to the institution: TheStudyofWar is the Institute for the Study of War, "
        "criticalthreats is Critical Threats, ACLEDINFO is ACLED, bellingcat is Bellingcat, "
        "and Militia Spotlight is the Washington Institute for Near East Policy.\n"
        "Specialist Analyst is a named analyst, not official confirmation. "
        "Wire / Local Outlet is a local outlet.\n"
        "When a casualty figure comes from SOHR or SyriaHR, the sentence must include exactly: "
        "according to SOHR, figures unverified.\n"
        "A Tip-off item is an aggregator post. When corroboration_count is missing or not greater than 1, "
        "do not describe that post as confirmed. Call it an unverified tip-off.\n"
        "Also use these tier labels when they apply: UN / Wire / Diplomatic, Independent / Field Desk, "
        "State / Party-Aligned Claim, and Single-Source / Opposition Caution.\n"
        "State / Party-Aligned Claim items are claims. Attribute them as claims until a wire, UN agency, "
        "or independent outlet confirms them.\n"
        "Hedge casualty figures from syriahr. Attribute IranIntl_En as opposition framing, not as confirmed fact.\n"
        "When a sentence states likelihood, use only this calibrated scale: "
        "remote chance, highly unlikely, unlikely, realistic possibility, likely, highly likely, almost certain. "
        "Do not assign a likelihood the records do not support.\n"
        "Emerald house style: judgment first, then the recorded evidence, short sentences, no rhetoric. "
        "Use the terms Ansarallah, coalition (Saudi Arabia), government forces, and truce. "
        "Do not add timing or lateness markers such as late or hours later.\n"
        "overall is 2-4 sentences for the whole theater.\n"
        "regions has one short paragraph for each of these keys exactly: "
        + ", ".join(REGIONS)
        + ".\n"
        "If a region's list is empty, its text must be exactly: "
        + EMPTY_WINDOW_TEXT
        + "\n\n"
        + json.dumps({"source_count": len(records), "regions": grouped}, ensure_ascii=False)
    )
    response = osint.make_client(key).responses.create(
        model=MODEL,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "geopolitical_summary",
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
    overall = _clean_summary_text(str(payload.get("overall") or ""))
    if not overall:
        raise RuntimeError("xAI response had no overall summary")
    raw_regions = payload.get("regions") if isinstance(payload.get("regions"), dict) else {}
    regions = {}
    for name in REGIONS:
        note = _clean_summary_text(str(raw_regions.get(name) or ""))
        if not grouped.get(name) or not note:
            note = EMPTY_WINDOW_TEXT
        regions[name] = note
    overall = apply_summary_caveats(overall, records)
    regions = {
        name: apply_summary_caveats(text, [item for item in records if region_name(item.get("region")) == name])
        for name, text in regions.items()
    }
    return overall, regions


def _window_end_token(end: datetime) -> str:
    return end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def completed_summary_ready(now: datetime | None = None) -> bool:
    _start, end = london_cycle(now, "completed")
    expected = _window_end_token(end)
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT value FROM feed_meta WHERE key = 'geo_summary_window_end'").fetchone()
        finally:
            conn.close()
    return bool(row and str(row["value"] or "") == expected)


def _save_geo_record(record: dict, write_brief: bool) -> None:
    save_summaries(record["overall"], record["regions"], record["generated_at"])
    blob = json.dumps(record, ensure_ascii=False)
    with _db_lock:
        conn = db_connect()
        try:
            conn.execute(
                "INSERT INTO summary_cache (scope, updated_at, summary) VALUES (?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET updated_at = excluded.updated_at, summary = excluded.summary",
                ("__geo_record__", record["generated_at"], blob),
            )
            conn.commit()
        finally:
            conn.close()
    if write_brief:
        payload = {
            "window_label": record["window_label"],
            "generated_at": record["generated_at"],
            "source_count": record["source_count"],
            "overall": record["overall"],
            "regions": {name: record["regions"][name] for name in REGIONS},
        }
        temporary = BRIEF_PATH.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(BRIEF_PATH)
        except OSError as exc:
            print(f"daily_brief.json was not written ({exc.__class__.__name__}).")
    if not record.get("window_end"):
        return
    with _db_lock:
        conn = db_connect()
        try:
            write_meta(conn, "geo_summary_window_end", str(record["window_end"]))
            conn.commit()
        finally:
            conn.close()


def london_now(now: datetime | None = None) -> datetime:
    moment = now.astimezone(LONDON) if now is not None else datetime.now(LONDON)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LONDON)
    return moment.astimezone(LONDON)


def latest_scrape_hour(now: datetime | None = None) -> int:
    """The Europe/London scrape hour that has most recently come due."""
    moment = london_now(now)
    passed = [hour for hour in SCRAPE_HOURS if hour <= moment.hour]
    return passed[-1] if passed else SCRAPE_HOURS[-1]


def brief_cycle_slot(now: datetime | None = None) -> str:
    moment = london_now(now)
    return f"brief-cycle-{moment.strftime('%Y-%m-%d')}-{moment.hour:02d}"


def brief_generated_at() -> datetime | None:
    """Cached brief time. The file wins, then the SQLite summary cache."""
    try:
        if BRIEF_PATH.exists() and BRIEF_PATH.stat().st_size:
            payload = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                moment = osint.parse_timestamp(payload.get("generated_at"))
                if moment is not None:
                    return moment
    except (OSError, json.JSONDecodeError) as exc:
        print(f"daily_brief.json was not readable ({exc.__class__.__name__}).")
    stored = read_summaries()
    return osint.parse_timestamp(stored.get("generated_at") or stored.get("updated_at"))


def brief_is_stale(now: datetime | None = None) -> bool:
    moment = brief_generated_at()
    if moment is None:
        return True
    current = london_now(now).astimezone(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return current - moment.astimezone(timezone.utc) >= BRIEF_CYCLE


def _summary_payload(record: dict) -> dict:
    return {
        "window_label": record["window_label"],
        "generated_at": record["generated_at"],
        "source_count": record["source_count"],
        "overall": record["overall"],
        "regions": record["regions"],
        "updated_at": record["generated_at"],
        "fallback": bool(record.get("fallback")),
    }


def generate_geopolitical_summary(view: str = "completed", force: bool = False, now: datetime | None = None) -> dict:
    """Summarize stored SQLite events. completed is the official 07:00 window. rolling is the last 24 hours."""
    requested = str(view or "").strip().lower()
    kind = "rolling" if requested == "rolling" else ("active" if requested == "active" else "completed")
    with _summary_lock:
        moment = london_now(now)
        if kind == "rolling":
            end = moment
            start = end - timedelta(hours=24)
        else:
            start, end = london_cycle(moment, kind)
        if kind == "completed" and not force and completed_summary_ready(moment):
            stored = read_summaries()
            stored["fallback"] = False
            return stored
        records = window_records(start, end)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = get_xai_api_key()
        fallback = False
        if not key:
            print(
                "XAI_API_KEY is missing. Filled the geopolitical summary from stored events. "
                "hotspots.json was not changed."
            )
            overall, regions = deterministic_window_summary(records, start, end)
            fallback = True
        else:
            try:
                if kind == "rolling":
                    overall, regions = call_24h_summary(_grouped_records(records), moment, key)
                else:
                    overall, regions = call_window_summary(records, start, end, key)
            except Exception as exc:
                print(
                    f"Geopolitical summary failed: {osint.public_error(exc, key)}. "
                    "Filled the summary from stored events. hotspots.json was not changed."
                )
                overall, regions = deterministic_window_summary(records, start, end)
                fallback = True
        if not str(overall or "").strip():
            print("Geopolitical summary was empty. daily_brief.json and hotspots.json were not changed.")
            stored = read_summaries()
            stored["fallback"] = True
            return stored
        record = {
            "window_label": format_briefing_window(start, end),
            "window_end": _window_end_token(end) if kind == "completed" else "",
            "generated_at": stamp,
            "source_count": len(records),
            "overall": overall,
            "regions": regions,
            "fallback": fallback,
        }
        if kind == "active":
            return _summary_payload(record)
        _save_geo_record(record, write_brief=True)
        if kind == "rolling":
            print(f"Saved the rolling 24-hour summary at {stamp} ({len(records)} source records).")
        elif kind == "completed":
            print(f"Saved the completed geopolitical summary at {stamp} ({len(records)} source records).")
        return _summary_payload(record)


def ensure_completed_summary() -> None:
    try:
        if completed_summary_ready():
            return
        generate_geopolitical_summary("completed")
    except Exception as exc:
        print(f"Geopolitical summary failed: {exc.__class__.__name__}.")


def schedule_completed_summary() -> None:
    if completed_summary_ready():
        return
    threading.Thread(target=ensure_completed_summary, name="geo-summary", daemon=True).start()


def save_briefing_archive(record: dict) -> None:
    """Keep the official 07:00 briefing. Existing brief files are left in place."""
    overall = " ".join(str(record.get("overall") or "").split())
    if not overall:
        return
    regions = record.get("regions") if isinstance(record.get("regions"), dict) else {}
    generated = " ".join(str(record.get("generated_at") or "").split())
    with _db_lock:
        conn = db_connect()
        try:
            conn.execute(
                "INSERT INTO briefing_archive (window_label, generated_at, overall, regions, source_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    " ".join(str(record.get("window_label") or "").split()),
                    generated,
                    overall,
                    json.dumps({name: regions.get(name) or "" for name in REGIONS}, ensure_ascii=False),
                    int(record.get("source_count") or 0),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def client_profile_recipients() -> list[str]:
    """Emails from client_profiles.json. A missing file adds nobody."""
    if not CLIENT_PROFILES.exists() or CLIENT_PROFILES.stat().st_size == 0:
        return []
    try:
        payload = json.loads(CLIENT_PROFILES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"client_profiles.json was not readable ({exc.__class__.__name__}).")
        return []
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        rows = payload.get("profiles") or payload.get("clients") or []
    found = []
    for row in rows:
        if isinstance(row, str):
            email = row.strip()
        elif isinstance(row, dict):
            email = str(row.get("email") or row.get("recipient") or "").strip()
        else:
            email = ""
        if email and "@" in email:
            found.append(email)
    return found


def report_recipients() -> list[str]:
    """REPORT_RECIPIENTS, then client profiles, then SMTP_TO when neither is set."""
    found = []
    raw = os.environ.get("REPORT_RECIPIENTS") or ""
    for part in re.split(r"[,;\s]+", raw):
        email = part.strip()
        if email and "@" in email:
            found.append(email)
    found.extend(client_profile_recipients())
    if not found:
        fallback = (os.environ.get("SMTP_TO") or "").strip()
        if fallback and "@" in fallback:
            found.append(fallback)
    unique = []
    seen = set()
    for email in found:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(email)
    return unique


def run_brief_refresh(moment: datetime, startup: bool) -> None:
    """Regenerate after a scrape. Email and the archive run only for the 07:00 cycle."""
    mode_hour = latest_scrape_hour(moment) if startup else moment.hour
    if mode_hour == OFFICIAL_HOUR:
        record = generate_geopolitical_summary("completed", force=True, now=moment)
        save_briefing_archive(record)
        if moment.hour == OFFICIAL_HOUR:
            send_official_daily_email(moment)
        return
    if mode_hour not in ROLLING_HOURS:
        return
    generate_geopolitical_summary("rolling", force=True, now=moment)


def schedule_brief_after_scrape() -> None:
    """One refresh on startup when the cached brief is older than 4 hours, then once per London scrape hour."""
    global _startup_brief_checked
    if not _brief_refresh_lock.acquire(blocking=False):
        return
    moment = london_now()
    startup = False
    if not _startup_brief_checked:
        _startup_brief_checked = True
        startup = brief_is_stale(moment)
    scheduled = moment.hour in SCRAPE_HOURS and not briefing_already_sent(brief_cycle_slot(moment))
    if not startup and not scheduled:
        _brief_refresh_lock.release()
        return

    def work() -> None:
        try:
            run_brief_refresh(moment, startup=startup and not scheduled)
            if moment.hour in SCRAPE_HOURS:
                mark_briefing_sent(brief_cycle_slot(moment))
        except Exception as exc:
            print(f"Brief refresh after scrape failed: {exc.__class__.__name__}.")
        finally:
            _brief_refresh_lock.release()

    threading.Thread(target=work, name="brief-after-scrape", daemon=True).start()


def summary_loop() -> None:
    while True:
        try:
            ensure_completed_summary()
        except Exception as exc:
            print(f"Geopolitical summary failed: {exc.__class__.__name__}.")
        time.sleep(SUMMARY_INTERVAL)


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
            headers=feed_request_headers(),
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
    """Record the final result for one feed. Do not call this for a dead mirror
    when a later mirror may still succeed."""
    key = f"{kind}:{name}"
    clean_error = "" if ok else " ".join(str(error or "error").split())[:160]
    readable = source_label(kind, name) if kind in {"telegram", "x", "rss"} else str(name)
    checked = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _feed_health_lock:
        _feed_health[key] = {
            "source_name": readable,
            "type": kind,
            "status": "active" if ok else "offline",
            "last_checked": checked,
            "last_error": clean_error,
            "ok": bool(ok),
            "error": clean_error,
        }


def note_feed_mirrors(kind: str, name: str, attempts: list[tuple[bool, str]]) -> None:
    """First successful mirror wins. Earlier failures never leave the feed offline."""
    last_error = "no entries"
    for ok, error in attempts:
        if ok:
            note_feed(kind, name, True, "")
            return
        if error:
            last_error = error
    note_feed(kind, name, False, last_error)


def feed_health_payload() -> dict:
    catalog = feed_catalog()
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
                    "label": (row or {}).get("source_name") or source_label(kind, name),
                    "error": row.get("error") or "error",
                }
            )
    failing_sources = [{"name": item["label"], "error": item["error"]} for item in failing]
    return {
        "online": online,
        "total": len(catalog),
        "onlineCount": online,
        "totalCount": len(catalog),
        "failing": failing,
        "failingSources": failing_sources,
    }


def feed_catalog() -> list[tuple[str, str]]:
    catalog = [("telegram", channel) for channel in TELEGRAM_CHANNELS]
    catalog.extend(("x", handle) for handle in X_HANDLES)
    catalog.extend(("rss", name) for name, _url in RSS_FEEDS)
    catalog.append(("liveblog", "Al Jazeera Live"))
    return catalog


def feed_status_payload() -> dict:
    """Active versus offline feeds. Unchecked sources are neither."""
    with _feed_health_lock:
        recorded = dict(_feed_health)
    feeds = []
    for kind, name in feed_catalog():
        row = recorded.get(f"{kind}:{name}") or {}
        status = str(row.get("status") or "")
        if status not in {"active", "offline"}:
            status = "unchecked"
        feeds.append(
            {
                "source_name": row.get("source_name") or (
                    source_label(kind, name) if kind in {"telegram", "x", "rss"} else name
                ),
                "type": kind,
                "status": status,
                "last_checked": row.get("last_checked") or "",
                "last_error": row.get("last_error") or "",
            }
        )
    try:
        import global_feeds

        feeds.extend(global_feeds.status_rows())
    except Exception as exc:
        print(f"Global feed status was not added: {exc.__class__.__name__}.")
    active = [item for item in feeds if item["status"] == "active"]
    offline = [item for item in feeds if item["status"] == "offline"]
    standby = [item for item in feeds if item["status"] == "standby"]
    return {
        "feeds": feeds,
        "active": active,
        "offline": offline,
        "online": len(active),
        "offline_count": len(offline),
        "standby": standby,
        "total": len(feeds),
        "onlineCount": len(active),
        "totalCount": len(feeds),
        "failingSources": [
            {"name": item["source_name"], "error": item["last_error"] or "error"}
            for item in offline
        ],
    }


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
        name = region_name((row["country"] if "country" in row.keys() else "") or row["region"])
        if name in series:
            series[name][offset] += 1
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


def _annotate_live_features(features: list[dict]) -> list[dict]:
    """Drop noise and fill coordinates so each remaining alert can be pinned."""
    try:
        import feed_quality
    except Exception:
        return features
    prepared = []
    for feature in features:
        try:
            annotated = feed_quality.annotate_feature(feature)
        except Exception:
            annotated = feature
        if annotated and (annotated.get("geometry") or {}).get("type") == "Point":
            prepared.append(annotated)
    return prepared


def data_payload() -> dict:
    now = datetime.now(timezone.utc)
    features = [feature for feature in combined_features() if within_live_window(feature, now)]
    try:
        import global_feeds

        global_feeds.kick()
        features.extend(global_feeds.features())
    except Exception as exc:
        print(f"Global feeds were not added: {exc.__class__.__name__}.")
    features = _annotate_live_features(features)
    events = live_events_from(features)
    with _db_lock:
        conn = db_connect()
        try:
            last_polled_at = read_last_polled(conn)
        finally:
            conn.close()
    mapped_count, total_count = coordinate_counts(now)
    print(f"Data API incidents: {len(features)}.")
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
    return generate_geopolitical_summary("completed", force=True)


def blocked_static(name: str) -> bool:
    db_name = Path(DB_PATH).name
    hidden = {
        ".env",
        db_name,
        "enquiries.jsonl",
        "advisory_crosscheck_cache.json",
        "global_feed_cache.json",
        "data_snapshot_cache.json",
        "news_stream_cache.json",
        "translation_cache.json",
    }
    return name in hidden or name.endswith(".sqlite") or name.endswith(".db") or name.startswith(".")


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


DATA_SNAPSHOT_PATH = ROOT / "data_snapshot_cache.json"
_snapshot_lock = threading.Lock()
_snapshot_refreshing = False
_snapshot_payload: dict | None = None
_snapshot_at = 0.0


def _load_snapshot() -> dict | None:
    global _snapshot_payload, _snapshot_at
    with _snapshot_lock:
        if isinstance(_snapshot_payload, dict):
            return _snapshot_payload
    try:
        payload = json.loads(DATA_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    with _snapshot_lock:
        _snapshot_payload = payload
        _snapshot_at = time.time()
    return payload


def _store_snapshot(payload: dict) -> None:
    global _snapshot_payload, _snapshot_at
    with _snapshot_lock:
        _snapshot_payload = payload
        _snapshot_at = time.time()
    try:
        temporary = DATA_SNAPSHOT_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(DATA_SNAPSHOT_PATH)
    except (OSError, TypeError):
        pass


def _refresh_snapshot() -> None:
    global _snapshot_refreshing
    with _snapshot_lock:
        if _snapshot_refreshing:
            return
        _snapshot_refreshing = True
    try:
        _store_snapshot(data_payload())
    except Exception as exc:
        print(f"Data snapshot refresh failed: {exc.__class__.__name__}.")
    finally:
        with _snapshot_lock:
            _snapshot_refreshing = False


def _schedule_snapshot(max_age: float = 20.0) -> None:
    with _snapshot_lock:
        fresh = isinstance(_snapshot_payload, dict) and time.time() - _snapshot_at < max_age
        busy = _snapshot_refreshing
    if fresh or busy:
        return
    threading.Thread(target=_refresh_snapshot, name="data-snapshot", daemon=True).start()


def data_response() -> dict:
    """Return the last snapshot immediately. A refresh runs in the background."""
    cached = _load_snapshot()
    if isinstance(cached, dict):
        _schedule_snapshot()
        return cached
    try:
        payload = data_payload()
        _store_snapshot(payload)
        return payload
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


def country_incidents_response(country: str, area: str = "", days: int = 7) -> dict:
    """Public 7-day incidents for one country, plus the advisory cross-check."""
    from country_incidents import build_report

    features = []
    try:
        features.extend((data_response().get("features") or []))
    except Exception as exc:
        print(f"Country incidents could not read the map snapshot: {exc.__class__.__name__}.")
    try:
        features.extend(feed_features())
    except Exception as exc:
        print(f"Country incidents could not read stored rows: {exc.__class__.__name__}.")
    try:
        import global_feeds

        features.extend(global_feeds.features())
    except Exception as exc:
        print(f"Country incidents could not read global feeds: {exc.__class__.__name__}.")
    report = build_report(features, country, area, days)
    try:
        import platform_phase1

        checked = platform_phase1.crosscheck_payload(country)
        report["advisories"] = checked.get("sources") or {}
        report["advisory_note"] = checked.get("note") or ""
    except Exception as exc:
        print(f"Country advisory cross-check failed: {exc.__class__.__name__}.")
        report["advisories"] = {}
        report["advisory_note"] = ""
    return report


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


def apply_english_fields(title: str, summary: str, translated: dict, force: bool = False) -> tuple[str, str] | None:
    new_title = osint.one_sentence(translated.get("title"))[:240]
    new_summary = osint.one_sentence(translated.get("summary"))[:500]
    updated_title = title
    updated_summary = summary
    changed = False
    if (force or needs_english(title)) and new_title and not needs_english(new_title):
        updated_title = new_title
        changed = True
    if (force or needs_english(summary)) and new_summary and not needs_english(new_summary):
        updated_summary = new_summary
        changed = True
    if not changed:
        return None
    return updated_title, updated_summary or updated_title


def translate_arabic_rows() -> int:
    """Rewrite stored rows whose title or summary still contains Arabic.

    A missing xAI key leaves every row unchanged. A failed translation does not blank the row.
    """
    if not get_xai_api_key():
        print("XAI_API_KEY is missing. Arabic titles and summaries were left unchanged.")
        return 0
    if not _translate_lock.acquire(blocking=False):
        return 0
    try:
        with _db_lock:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT id, title, summary, original_text, source_name, source_kind, source_url FROM items"
                ).fetchall()
            finally:
                conn.close()
        updated = 0
        for row in rows:
            title = str(row["title"] or "")
            summary = str(row["summary"] or "")
            if not contains_arabic(title) and not contains_arabic(summary):
                continue
            original = str(row["original_text"] or "").strip()
            if not original:
                original = original_post_text({"title": title, "summary": summary})
            result = classify_and_translate_event(
                {
                    "title": title,
                    "summary": summary,
                    "original_text": original,
                    "source_name": row["source_name"],
                    "source_kind": row["source_kind"],
                    "source_url": row["source_url"],
                }
            )
            english_title, english_summary = apply_house_style(
                result.get("title") or "",
                result.get("summary") or "",
                row["source_kind"] or "",
                row["source_name"] or "",
                row["source_url"] or "",
            )
            if (
                not english_title
                or contains_arabic(english_title)
                or contains_arabic(english_summary)
            ):
                print(f"Translation left item {row['id']} unchanged.")
                continue
            kept_original = str(result.get("original_text") or original).strip() or original
            with _db_lock:
                conn = db_connect()
                try:
                    conn.execute(
                        "UPDATE items SET title = ?, summary = ?, original_text = ? WHERE id = ?",
                        (english_title, english_summary or english_title, kept_original, row["id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
            updated += 1
            print(f"Translated item {row['id']} into English.")
        return updated
    finally:
        _translate_lock.release()


def translate_existing_rows() -> int:
    """Startup pass. Leaves rows unchanged when the xAI key is missing."""
    return translate_arabic_rows()


def start_translation_pass() -> None:
    threading.Thread(target=translate_existing_rows, name="translate-existing", daemon=True).start()


def _api_json(payload: dict) -> bytes:
    try:
        import ppv

        if ppv.is_sensitive(ppv.request_route()):
            payload = ppv.restrict(payload)
    except Exception:
        pass
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


EXPORT_FIELDS = (
    "title", "summary", "timestamp", "region", "severity", "category",
    "actor", "system_type", "geo_precision", "location_name", "governorate",
    "lat", "lng", "source", "source_tier", "corroboration_count", "amplification_count",
)
_EXPORT_WINDOWS = {"2h": timedelta(hours=2), "24h": timedelta(hours=24), "7d": timedelta(days=7)}


def _query_value(query: str, key: str, default: str = "all") -> str:
    values = parse_qs(query or "", keep_blank_values=False).get(key) or []
    text = " ".join(str(values[0] if values else "").split())
    return text or default


def feature_in_window(feature: dict, window_id: str, now: datetime | None = None) -> bool:
    if window_id in ("", "all"):
        return True
    window = _EXPORT_WINDOWS.get(window_id)
    if window is None:
        return True
    moment = osint.parse_timestamp((feature.get("properties") or {}).get("timestamp"))
    if moment is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = (now or datetime.now(timezone.utc)) - moment.astimezone(timezone.utc)
    if age < timedelta(minutes=-5):
        return False
    return age <= window


def filtered_features(query: str, now: datetime | None = None) -> list[dict]:
    """Live pins matching the map's region, severity, and time-window filters."""
    region = _query_value(query, "region")
    severity = _query_value(query, "severity").lower()
    window_id = _query_value(query, "time")
    chosen = []
    for feature in combined_features():
        props = feature.get("properties") or {}
        if region != "all" and str(props.get("region") or "") != region:
            continue
        if severity != "all" and str(props.get("severity") or "").lower() != severity:
            continue
        if not feature_in_window(feature, window_id, now):
            continue
        chosen.append(feature)
    return chosen


def export_record(feature: dict) -> dict:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
    precision = str(props.get("geo_precision") or "")
    if precision not in GEO_PRECISIONS:
        precision = classify_precision(props.get("location_name") or props.get("governorate") or "")
    lng = coords[0] if len(coords) > 0 else ""
    lat = coords[1] if len(coords) > 1 else ""
    return {
        "title": props.get("title") or "",
        "summary": props.get("summary") or "",
        "timestamp": props.get("timestamp") or "",
        "region": props.get("region") or "",
        "severity": props.get("severity") or "",
        "category": props.get("category") or "",
        "actor": props.get("actor") or "",
        "system_type": props.get("system_type") or "",
        "geo_precision": precision,
        "location_name": props.get("location_name") or "",
        "governorate": props.get("governorate") or "",
        "lat": lat if lat is not None else "",
        "lng": lng if lng is not None else "",
        "source": props.get("source") or "",
        "source_tier": props.get("source_tier") or "",
        "corroboration_count": props.get("corroboration_count") or 1,
        "amplification_count": props.get("amplification_count") or 0,
    }


def export_csv_bytes(query: str) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for feature in filtered_features(query):
        writer.writerow(export_record(feature))
    return buffer.getvalue().encode("utf-8")


def export_geojson_bytes(query: str) -> bytes:
    features = []
    for feature in filtered_features(query):
        record = export_record(feature)
        props = dict(feature.get("properties") or {})
        props["geo_precision"] = record["geo_precision"]
        props["actor"] = record["actor"]
        props["system_type"] = record["system_type"]
        props["amplification_count"] = record["amplification_count"]
        features.append({
            "type": "Feature",
            "geometry": feature.get("geometry"),
            "properties": props,
        })
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False).encode("utf-8")


ADVISORY_CACHE_PATH = ROOT / "advisory_cache.json"
ADVISORY_TTL_SECONDS = 8 * 60 * 60
US_ADVISORY_URL = "https://cadataapi.state.gov/api/TravelAdvisories"
UK_ADVISORY_INDEX = "https://www.gov.uk/api/content/foreign-travel-advice"
ADVISORY_LEVELS = {
    4: ("Level 4: Do Not Travel / Advise Against All Travel", "#dc2626"),
    3: ("Level 3: Reconsider Travel / Advise Against All But Essential Travel", "#ea580c"),
    2: ("Level 2: Exercise Increased Caution / Regional Warnings", "#eab308"),
    1: ("Level 1: Exercise Normal Precautions", "#0d9488"),
    0: ("Unrated / No Data", "#1e293b"),
}
UK_ALERT_LEVEL = {
    "avoid_all_travel_to_whole_country": 4,
    "avoid_all_but_essential_travel_to_whole_country": 3,
    "avoid_all_travel_to_parts": 2,
    "avoid_all_but_essential_travel_to_parts": 2,
}
ADVISORY_ALIASES = {
    "united states": "USA",
    "united states of america": "USA",
    "usa": "USA",
    "uk": "GBR",
    "united kingdom": "GBR",
    "great britain": "GBR",
    "russia": "RUS",
    "russian federation": "RUS",
    "syria": "SYR",
    "syrian arab republic": "SYR",
    "iran": "IRN",
    "south korea": "KOR",
    "republic of korea": "KOR",
    "north korea": "PRK",
    "czechia": "CZE",
    "czech republic": "CZE",
    "laos": "LAO",
    "myanmar": "MMR",
    "burma": "MMR",
    "cote d ivoire": "CIV",
    "ivory coast": "CIV",
    "democratic republic of the congo": "COD",
    "democratic republic of congo": "COD",
    "dr congo": "COD",
    "republic of the congo": "COG",
    "republic of congo": "COG",
    "palestine": "PSE",
    "gaza": "PSE",
    "west bank": "PSE",
    "the west bank": "PSE",
    "uae": "ARE",
    "united arab emirates": "ARE",
    "venezuela": "VEN",
    "vietnam": "VNM",
    "viet nam": "VNM",
    "tanzania": "TZA",
    "united republic of tanzania": "TZA",
    "kosovo": "XKX",
    "taiwan": "TWN",
    "turkey": "TUR",
    "turkiye": "TUR",
    "eswatini": "SWZ",
    "swaziland": "SWZ",
    "cabo verde": "CPV",
    "cape verde": "CPV",
    "timor leste": "TLS",
    "east timor": "TLS",
    "north macedonia": "MKD",
    "czech republic": "CZE",
}
_WARNING_RULES = (
    ("terrorism", ("terrorism", "terrorist")),
    ("armed conflict", ("armed conflict", "armed clashes")),
    ("civil unrest", ("civil unrest", "unrest")),
    ("kidnapping", ("kidnapping", "kidnap")),
)
_advisory_lock = threading.Lock()
_advisory_memory: dict = {"at": 0.0, "payload": None}
_country_index_cache: dict | None = None


def _advisory_norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _country_indexes() -> tuple[dict[str, str], dict[str, dict]]:
    """Name and ISO indexes from the vendored World Countries file."""
    global _country_index_cache
    if _country_index_cache is not None:
        return _country_index_cache["by_name"], _country_index_cache["by_iso3"]
    by_name: dict[str, str] = {}
    by_iso3: dict[str, dict] = {}
    path = ROOT / "static" / "world-countries.geojson"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"World countries file was not readable ({exc.__class__.__name__}).")
        payload = {}
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        iso3 = str(props.get("ISO_A3") or "").upper()
        admin = str(props.get("ADMIN") or "")
        if not iso3 or not admin:
            continue
        by_iso3[iso3] = {
            "name": admin,
            "iso2": str(props.get("ISO_A2") or "").upper(),
            "iso3": iso3,
        }
        by_name[_advisory_norm(admin)] = iso3
    for alias, iso3 in ADVISORY_ALIASES.items():
        if iso3 in by_iso3:
            by_name[_advisory_norm(alias)] = iso3
    _country_index_cache = {"by_name": by_name, "by_iso3": by_iso3}
    return by_name, by_iso3


def _strip_advisory_html(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(text).split())


def _advisory_warnings(text: str) -> list[str]:
    lowered = text.lower()
    found = []
    for label, keys in _WARNING_RULES:
        if any(key in lowered for key in keys):
            found.append(label)
    return found


def _blank_advisory(meta: dict) -> dict:
    label, color = ADVISORY_LEVELS[0]
    admin = meta["name"]
    iso3 = meta["iso3"]
    map_country = "UAE" if iso3 == "ARE" else admin if admin in REGIONS else admin
    return {
        "name": admin,
        "iso2": meta.get("iso2") or "",
        "iso3": iso3,
        "level": 0,
        "label": label,
        "color": color,
        "summary": "",
        "url": "",
        "updated": "",
        "warnings": [],
        "source": "",
        "mapCountry": map_country,
    }


def _apply_advisory(bucket: dict[str, dict], iso3: str, level: int, summary: str, url: str, updated: str, source: str) -> None:
    _by_name, by_iso3 = _country_indexes()
    meta = by_iso3.get(iso3)
    if not meta or level not in ADVISORY_LEVELS or level == 0:
        return
    label, color = ADVISORY_LEVELS[level]
    current = bucket.get(iso3) or _blank_advisory(meta)
    warnings = list(dict.fromkeys(list(current.get("warnings") or []) + _advisory_warnings(summary)))
    if level > int(current.get("level") or 0) or not current.get("summary"):
        current["summary"] = summary[:700]
        current["url"] = url
        current["updated"] = updated
        current["source"] = source
    elif source and source not in str(current.get("source") or ""):
        current["source"] = (str(current.get("source") or "") + " / " + source).strip(" /")
    current["level"] = max(int(current.get("level") or 0), level)
    current["label"], current["color"] = ADVISORY_LEVELS[current["level"]]
    current["warnings"] = warnings
    bucket[iso3] = current


def _advisory_request_headers() -> dict[str, str]:
    headers = feed_request_headers()
    headers["Accept"] = "application/json, " + FEED_ACCEPT
    return headers


def _xml_local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _advisory_xml_rows(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    rows = []
    for node in root:
        if _xml_local(node.tag) != "Rss":
            continue
        row = {}
        for child in list(node):
            row[_xml_local(child.tag)] = "".join(child.itertext())
        rows.append(row)
    return rows


def _advisory_get_json(url: str):
    response = requests.get(url, timeout=FEED_TIMEOUT, headers=_advisory_request_headers())
    response.raise_for_status()
    sample = (response.content or b"").lstrip()[:1]
    if sample == b"<":
        return _advisory_xml_rows(response.content)
    return response.json()


def _ingest_us_advisories(bucket: dict[str, dict]) -> int:
    payload = _advisory_get_json(US_ADVISORY_URL)
    rows = payload if isinstance(payload, list) else []
    by_name, _by_iso3 = _country_indexes()
    kept = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("Title") or "")
        match = re.search(r"level\s*([1-4])", title, re.IGNORECASE)
        if not match:
            continue
        country_name = title.split(" - ")[0].strip()
        iso3 = by_name.get(_advisory_norm(country_name))
        if not iso3:
            continue
        summary = _strip_advisory_html(row.get("Summary"))
        _apply_advisory(
            bucket,
            iso3,
            int(match.group(1)),
            summary,
            str(row.get("Link") or ""),
            str(row.get("Updated") or row.get("Published") or ""),
            "US State Department",
        )
        kept += 1
    print(f"US travel advisories: {len(rows)} raw items, {kept} matched.")
    return kept


def _uk_level(statuses: list) -> int:
    levels = [UK_ALERT_LEVEL[str(item)] for item in statuses if str(item) in UK_ALERT_LEVEL]
    if not levels:
        return 1
    return max(levels)


def _ingest_uk_advisories(bucket: dict[str, dict]) -> int:
    index = _advisory_get_json(UK_ADVISORY_INDEX)
    children = ((index.get("links") or {}).get("children") or []) if isinstance(index, dict) else []
    pages = []
    for child in children:
        if not isinstance(child, dict):
            continue
        url = str(child.get("api_url") or "")
        if not url and str(child.get("api_path") or "").startswith("/"):
            url = "https://www.gov.uk" + str(child.get("api_path"))
        if url:
            pages.append(url)
    by_name, _by_iso3 = _country_indexes()
    kept = 0

    def one(url: str) -> dict | None:
        try:
            return _advisory_get_json(url)
        except Exception as exc:
            print(f"UK travel advice page failed: {exc.__class__.__name__}.")
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        for page in pool.map(one, pages):
            if not isinstance(page, dict):
                continue
            details = page.get("details") or {}
            country = (details.get("country") or {}).get("name") or ""
            iso3 = by_name.get(_advisory_norm(country))
            if not iso3:
                continue
            parts = details.get("parts") or []
            body = ""
            if isinstance(parts, list) and parts:
                body = _strip_advisory_html((parts[0] or {}).get("body"))
            _apply_advisory(
                bucket,
                iso3,
                _uk_level(list(details.get("alert_status") or [])),
                body or _strip_advisory_html(page.get("description")),
                str(page.get("web_url") or ""),
                str(page.get("public_updated_at") or details.get("updated_at") or ""),
                "UK FCDO",
            )
            kept += 1
    print(f"UK travel advisories: {len(pages)} raw items, {kept} matched.")
    return kept


def _read_advisory_disk() -> dict | None:
    if not ADVISORY_CACHE_PATH.exists() or ADVISORY_CACHE_PATH.stat().st_size == 0:
        return None
    try:
        payload = json.loads(ADVISORY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Travel advisory cache was not readable ({exc.__class__.__name__}).")
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _store_advisories(payload: dict) -> None:
    global _advisory_memory
    _advisory_memory = {"at": time.time(), "payload": payload}
    try:
        temporary = ADVISORY_CACHE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(ADVISORY_CACHE_PATH)
    except OSError as exc:
        print(f"Travel advisory cache was not written ({exc.__class__.__name__}).")


def _advisory_age(payload: dict) -> float | None:
    stamp = str(payload.get("fetched_at") or "")
    moment = osint.parse_timestamp(stamp)
    if moment is None:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


def empty_travel_advisories() -> dict:
    return {
        "ok": True,
        "cached": False,
        "updated_at": None,
        "fetched_at": None,
        "sources": [],
        "countries": [],
        "note": "unrated",
    }


def travel_advisories_payload() -> dict:
    """Official US and UK travel levels. A failed refresh keeps the last cache."""
    try:
        with _advisory_lock:
            memory = _advisory_memory.get("payload")
            if isinstance(memory, dict) and time.time() - float(_advisory_memory.get("at") or 0) < ADVISORY_TTL_SECONDS:
                return memory
            disk = _read_advisory_disk()
            if isinstance(disk, dict):
                age = _advisory_age(disk)
                if age is not None and age < ADVISORY_TTL_SECONDS:
                    _advisory_memory["at"] = time.time()
                    _advisory_memory["payload"] = disk
                    return disk
            bucket: dict[str, dict] = {}
            sources = []
            try:
                if _ingest_us_advisories(bucket):
                    sources.append("US State Department")
            except Exception as exc:
                print(f"US travel advisories failed: {exc.__class__.__name__}.")
            try:
                if _ingest_uk_advisories(bucket):
                    sources.append("UK FCDO")
            except Exception as exc:
                print(f"UK travel advisories failed: {exc.__class__.__name__}.")
            if bucket:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                payload = {
                    "ok": True,
                    "cached": False,
                    "updated_at": now,
                    "fetched_at": now,
                    "sources": sources,
                    "countries": sorted(bucket.values(), key=lambda item: item["name"]),
                    "note": "",
                }
                _store_advisories(payload)
                print(f"Travel advisories: {len(payload['countries'])} countries.")
                return payload
            if isinstance(disk, dict) and disk.get("countries"):
                stale = dict(disk)
                stale["ok"] = True
                stale["cached"] = True
                stale["note"] = "cached"
                return stale
            if isinstance(memory, dict) and memory.get("countries"):
                stale = dict(memory)
                stale["cached"] = True
                stale["note"] = "cached"
                return stale
            print("Travel advisories: no cache, countries left unrated.")
            return empty_travel_advisories()
    except Exception as exc:
        print(f"Travel advisories failed: {exc.__class__.__name__}.")
        disk = _read_advisory_disk()
        if isinstance(disk, dict):
            disk = dict(disk)
            disk["ok"] = True
            disk["cached"] = True
            return disk
        return empty_travel_advisories()



ASSET_CACHE_PATH = ROOT / "country_assets_cache.json"
ASSET_TTL_SECONDS = 24 * 60 * 60
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_asset_lock = threading.Lock()
_asset_memory: dict[str, dict] = {}
_fcdo_slug_cache: dict = {"at": 0.0, "slugs": None}
_EU_ISO = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}
# Search centers only. Facilities themselves come from Overpass or the FCDO page.
_ASSET_HUBS = {
    "syria": (("Damascus", 33.5138, 36.2765), ("Aleppo", 36.2021, 37.1343)),
    "iraq": (("Baghdad", 33.3152, 44.3661), ("Erbil", 36.1911, 44.0092), ("Basra", 30.5081, 47.7835)),
    "saudi arabia": (("Riyadh", 24.7136, 46.6753), ("Jeddah", 21.5433, 39.1728), ("Dammam", 26.4207, 50.0888)),
    "sudan": (("Khartoum", 15.5007, 32.5599), ("Port Sudan", 19.6158, 37.2164)),
    "chad": (("N'Djamena", 12.1348, 15.0557),),
    "iran": (("Tehran", 35.6892, 51.3890), ("Isfahan", 32.6546, 51.6680)),
    "lebanon": (("Beirut", 33.8938, 35.5018),),
    "israel": (("Jerusalem", 31.7683, 35.2137), ("Tel Aviv", 32.0853, 34.7818)),
    "palestine": (("Ramallah", 31.9038, 35.2034), ("Gaza", 31.5017, 34.4668)),
    "jordan": (("Amman", 31.9454, 35.9284),),
    "yemen": (("Sanaa", 15.3694, 44.1910), ("Aden", 12.7855, 45.0187)),
    "bahrain": (("Manama", 26.2235, 50.5876),),
    "kuwait": (("Kuwait City", 29.3759, 47.9774),),
    "qatar": (("Doha", 25.2854, 51.5310),),
    "united arab emirates": (("Abu Dhabi", 24.4539, 54.3773), ("Dubai", 25.2048, 55.2708)),
    "oman": (("Muscat", 23.5880, 58.3829),),
}
_MEDEVAC_NAIROBI = {
    "sudan", "chad", "south sudan", "ethiopia", "eritrea", "somalia", "kenya", "uganda",
    "tanzania", "democratic republic of the congo", "rwanda", "burundi", "djibouti",
}
_MEDEVAC_DUBAI = {
    "saudi arabia", "united arab emirates", "uae", "oman", "qatar", "bahrain", "kuwait",
    "yemen", "iran",
}
_MEDEVAC_AMMAN = {"syria", "lebanon", "jordan", "israel", "palestine", "iraq"}
_BRITISH_MISSION_RE = re.compile(
    r"British\s+(?:Embassy|High Commission|Consulate(?:-General| General)?)"
    r"(?:\s+in\s+[A-Z][A-Za-z'’.\-]+(?:\s+[A-Z][A-Za-z'’.\-]+){0,3})?"
)


def _blank_assets(country: str, iso2: str = "", iso3: str = "") -> dict:
    return {
        "ok": True,
        "country": country,
        "iso2": iso2,
        "iso3": iso3,
        "cached": False,
        "fetched_at": None,
        "contacts_available": False,
        "missions": [],
        "hospitals": [],
        "airports": [],
        "police": "",
        "ambulance": "",
        "emergency_note": "Local police and ambulance numbers are not listed.",
        "medical_note": "",
    }


def _asset_identity(raw: str) -> dict:
    text = unquote(str(raw or "")).strip()
    norm = _advisory_norm(text)
    if norm in {"uae"}:
        norm = "united arab emirates"
    by_name, by_iso3 = _country_indexes()
    iso3 = ""
    if len(text) == 3 and text.upper() in by_iso3:
        iso3 = text.upper()
    else:
        iso3 = by_name.get(norm) or by_name.get(_advisory_norm(text)) or ""
    if iso3 and iso3 in by_iso3:
        meta = by_iso3[iso3]
        iso2 = str(meta.get("iso2") or "")
        if iso2 in {"-99", "-1"}:
            iso2 = ""
        return {"name": meta["name"], "iso2": iso2, "iso3": iso3, "key": iso3, "norm": _advisory_norm(meta["name"])}
    return {"name": text or "Unknown", "iso2": "", "iso3": "", "key": norm or "unknown", "norm": norm}


def _medevac_hub(norm: str) -> str:
    if norm in _MEDEVAC_NAIROBI:
        return "Nairobi"
    if norm in _MEDEVAC_DUBAI:
        return "Dubai"
    if norm in _MEDEVAC_AMMAN:
        return "Amman"
    return "London"


def _medical_note_for(level: int, norm: str) -> str:
    if int(level or 0) >= 3:
        return (
            "Local trauma care may not meet international standards. "
            f"Air-ambulance medevac to {_medevac_hub(norm)} should be arranged before travel."
        )
    return "Confirm the facility can take trauma before relying on it."


def _cached_advisory_level(iso3: str) -> int:
    if not iso3:
        return 0
    payload = _advisory_memory.get("payload")
    if not isinstance(payload, dict):
        payload = _read_advisory_disk()
    if not isinstance(payload, dict):
        return 0
    for row in payload.get("countries") or []:
        if isinstance(row, dict) and str(row.get("iso3") or "") == iso3:
            try:
                return int(row.get("level") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _read_asset_disk() -> dict:
    if not ASSET_CACHE_PATH.exists() or ASSET_CACHE_PATH.stat().st_size == 0:
        return {}
    try:
        payload = json.loads(ASSET_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Country asset cache was not readable ({exc.__class__.__name__}).")
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_asset_disk(store: dict) -> None:
    try:
        temporary = ASSET_CACHE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
        temporary.replace(ASSET_CACHE_PATH)
    except OSError as exc:
        print(f"Country asset cache was not written ({exc.__class__.__name__}).")


def _asset_cache_get(key: str) -> dict | None:
    memory = _asset_memory.get(key)
    if isinstance(memory, dict):
        return memory
    disk = _read_asset_disk().get(key)
    if isinstance(disk, dict):
        _asset_memory[key] = disk
        return disk
    return None


def _asset_cache_put(key: str, payload: dict) -> None:
    _asset_memory[key] = payload
    store = _read_asset_disk()
    store[key] = payload
    _write_asset_disk(store)


def _asset_fresh(payload: dict) -> bool:
    stamp = str(payload.get("fetched_at") or "")
    moment = osint.parse_timestamp(stamp)
    if moment is None:
        return False
    return (datetime.now(timezone.utc) - moment).total_seconds() < ASSET_TTL_SECONDS


def _overpass_headers() -> dict[str, str]:
    headers = feed_request_headers()
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


def _overpass(query: str) -> dict:
    response = requests.post(OVERPASS_URL, data={"data": query}, headers=_overpass_headers(), timeout=FEED_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("overpass")
    return payload


def _osm_point(element: dict) -> tuple[float, float] | None:
    lat = element.get("lat")
    lng = element.get("lon")
    if lat is None or lng is None:
        center = element.get("center") or {}
        lat = center.get("lat")
        lng = center.get("lon")
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        return None
    return lat_f, lng_f


def _http_website(tags: dict) -> str:
    for key in ("website", "contact:website", "url"):
        value = str(tags.get(key) or "").strip()
        if value.startswith("http://") or value.startswith("https://"):
            return value
    return ""


def _osm_phone(tags: dict) -> str:
    return str(tags.get("phone") or tags.get("contact:phone") or "").strip()


def _osm_address(tags: dict) -> str:
    parts = [
        " ".join(part for part in (str(tags.get("addr:housenumber") or "").strip(), str(tags.get("addr:street") or "").strip()) if part),
        str(tags.get("addr:city") or tags.get("addr:district") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def _asset_hubs(identity: dict) -> list[tuple[str, float, float]]:
    hubs = list(_ASSET_HUBS.get(identity.get("norm") or "", ()))
    if hubs:
        return hubs[:3]
    iso2 = identity.get("iso2") or ""
    if not re.fullmatch(r"[A-Z]{2}", iso2):
        return []
    query = (
        '[out:json][timeout:8];\n'
        f'area["ISO3166-1"="{iso2}"]["admin_level"="2"]->.a;\n'
        'node["capital"="yes"](area.a);\n'
        "out tags;"
    )
    payload = _overpass(query)
    found = []
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        point = _osm_point(element)
        tags = element.get("tags") or {}
        name = str(tags.get("name:en") or tags.get("name") or "").strip()
        if point and name:
            found.append((name, point[0], point[1]))
    return found[:2]


def _around_query(filters: tuple[str, ...], hubs: list[tuple[str, float, float]], radius: int) -> str:
    lines = []
    for _name, lat, lng in hubs:
        for item in filters:
            lines.append(f"  nwr[{item}](around:{radius},{lat:.4f},{lng:.4f});")
    return "[out:json][timeout:8];\n(\n" + "\n".join(lines) + "\n);\nout center tags;"


def _mission_group(tags: dict, name: str) -> str:
    country = str(tags.get("country") or "").upper()
    blob = name.lower()
    if country in {"GB", "UK"} or "british" in blob or "united kingdom" in blob:
        return "uk"
    if country in {"US", "USA"} or "united states" in blob or "american embassy" in blob:
        return "us"
    if "united nations" in blob or country == "UN":
        return "un"
    if country in _EU_ISO or "european union" in blob:
        return "eu"
    return "other"


def _overpass_missions(hubs: list[tuple[str, float, float]]) -> list[dict]:
    if not hubs:
        return []
    payload = _overpass(_around_query(('"amenity"="embassy"', '"diplomatic"="embassy"', '"diplomatic"="consulate"'), hubs, 30000))
    found = []
    seen = set()
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        name = str(tags.get("name:en") or tags.get("name") or "").strip()
        point = _osm_point(element)
        if not name or not point:
            continue
        key = (name.lower(), round(point[0], 3), round(point[1], 3))
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "name": name,
            "kind": "consulate" if str(tags.get("diplomatic") or "") == "consulate" else "embassy",
            "address": _osm_address(tags),
            "phone": _osm_phone(tags),
            "website": _http_website(tags),
            "lat": point[0],
            "lng": point[1],
            "source": "OpenStreetMap",
            "group": _mission_group(tags, name),
            "consular": False,
        })
    order = {"uk": 0, "us": 1, "eu": 2, "un": 3, "other": 4}
    found.sort(key=lambda item: (order.get(item["group"], 9), item["name"]))
    preferred = [item for item in found if item["group"] != "other"]
    others = [item for item in found if item["group"] == "other"]
    chosen = preferred[:10]
    if len(chosen) < 4:
        chosen.extend(others[: max(0, 8 - len(chosen))])
    return chosen


def _hospital_score(tags: dict) -> int:
    score = 0
    if str(tags.get("emergency") or "").lower() == "yes":
        score += 4
    if _osm_phone(tags):
        score += 2
    if _http_website(tags):
        score += 2
    return score


def _overpass_hospitals(hubs: list[tuple[str, float, float]]) -> list[dict]:
    if not hubs:
        return []
    payload = _overpass(_around_query(('"amenity"="hospital"',), hubs, 25000))
    found = []
    seen = set()
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        name = str(tags.get("name:en") or tags.get("name") or "").strip()
        point = _osm_point(element)
        if not name or not point:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append((
            _hospital_score(tags),
            {
                "name": name,
                "city": str(tags.get("addr:city") or tags.get("addr:district") or "").strip(),
                "emergency": str(tags.get("emergency") or "").lower() == "yes",
                "phone": _osm_phone(tags),
                "website": _http_website(tags),
                "lat": point[0],
                "lng": point[1],
                "source": "OpenStreetMap",
            },
        ))
    found.sort(key=lambda item: (-item[0], item[1]["name"]))
    return [item[1] for item in found[:5]]


def _skip_private_strip(tags: dict) -> bool:
    kind = str(tags.get("aerodrome") or "").lower()
    if kind in {"private", "private_strip"}:
        return True
    icao = str(tags.get("icao") or tags.get("ref:icao") or "").strip()
    iata = str(tags.get("iata") or tags.get("ref:iata") or "").strip()
    if str(tags.get("access") or "").lower() == "private" and not icao and not iata:
        return True
    return False


def _overpass_airports(hubs: list[tuple[str, float, float]]) -> list[dict]:
    if not hubs:
        return []
    payload = _overpass(_around_query(('"aeroway"="aerodrome"', '"aeroway"="helipad"'), hubs, 70000))
    found = []
    seen = set()
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if _skip_private_strip(tags):
            continue
        name = str(tags.get("name:en") or tags.get("name") or "").strip()
        point = _osm_point(element)
        if not name or not point:
            continue
        aeroway = str(tags.get("aeroway") or "")
        kind = "Helipad" if aeroway == "helipad" else "Airport"
        icao = str(tags.get("icao") or tags.get("ref:icao") or "").strip()
        iata = str(tags.get("iata") or tags.get("ref:iata") or "").strip()
        key = (kind, name.lower(), round(point[0], 3), round(point[1], 3))
        if key in seen:
            continue
        seen.add(key)
        rank = 0 if icao or iata else 1
        if kind == "Helipad":
            rank += 2
        if str(tags.get("aerodrome") or "").lower() == "international":
            rank -= 1
        found.append((rank, {
            "name": name,
            "icao": icao,
            "iata": iata,
            "type": kind,
            "city": str(tags.get("addr:city") or "").strip(),
            "phone": _osm_phone(tags),
            "lat": point[0],
            "lng": point[1],
            "source": "OpenStreetMap",
        }))
    found.sort(key=lambda item: (item[0], item[1]["name"]))
    return [item[1] for item in found[:8]]


def _fcdo_slugs() -> dict[str, str]:
    if isinstance(_fcdo_slug_cache.get("slugs"), dict) and time.time() - float(_fcdo_slug_cache.get("at") or 0) < ASSET_TTL_SECONDS:
        return _fcdo_slug_cache["slugs"]
    payload = _advisory_get_json(UK_ADVISORY_INDEX)
    slugs: dict[str, str] = {}
    children = ((payload.get("links") or {}).get("children") or []) if isinstance(payload, dict) else []
    for child in children:
        if not isinstance(child, dict):
            continue
        title = str(child.get("title") or "")
        path = str(child.get("base_path") or child.get("api_path") or "")
        slug = path.rstrip("/").split("/")[-1].strip().lower()
        if title and slug and slug not in {"foreign-travel-advice", "api"}:
            slugs[_advisory_norm(title)] = slug
    _fcdo_slug_cache["at"] = time.time()
    _fcdo_slug_cache["slugs"] = slugs
    return slugs


def _fcdo_slug_for(identity: dict) -> str:
    slugs = _fcdo_slugs()
    norm = identity.get("norm") or ""
    if norm in slugs:
        return slugs[norm]
    if norm == "united arab emirates" and "united arab emirates" in slugs:
        return slugs["united arab emirates"]
    return ""


def _parse_emergency_numbers(text: str) -> tuple[str, str]:
    police = ""
    ambulance = ""
    match = re.search(r"emergency services[\s\S]{0,700}", text, re.I)
    block = match.group(0) if match else text[:800]
    combined = re.search(r"Telephone\s*:\s*([0-9]{2,4})\s*\(([^)]+)\)", block, re.I)
    if combined:
        number, kinds = combined.group(1), combined.group(2).lower()
        if "police" in kinds:
            police = number
        if "ambulance" in kinds:
            ambulance = number
    for label, target in (("ambulance", "ambulance"), ("police", "police")):
        found = re.search(rf"{label}\s*:\s*([0-9]{{2,4}})\b", block, re.I)
        if not found:
            continue
        if target == "ambulance" and not ambulance:
            ambulance = found.group(1)
        if target == "police" and not police:
            police = found.group(1)
    return police, ambulance


def _parse_uk_mission(page: dict) -> tuple[dict | None, str, str]:
    details = page.get("details") or {}
    parts = details.get("parts") or []
    chunks = []
    for part in parts:
        if isinstance(part, dict):
            chunks.append(str(part.get("body") or ""))
    html_body = "\n".join(chunks)
    text = BeautifulSoup(html_body, "html.parser").get_text("\n", strip=True)
    police, ambulance = _parse_emergency_numbers(text)
    mission_name = ""
    for match in _BRITISH_MISSION_RE.finditer(text):
        candidate = " ".join(match.group(0).split())
        if re.search(r"\bin the UK\b|\bin London\b", candidate, re.I):
            continue
        mission_name = candidate
        break
    phone = ""
    consular = False
    london = re.search(r"call the FCDO in London on\s+(\+?\d[\d\s().\-]{8,}\d)", text, re.I)
    marked = re.search(r"Telephone\s*:\s*(\+?\d[\d\s().\-]{6,}\d)\s*\((24\s*hours?|24/7)\)", text, re.I)
    if marked:
        phone = " ".join(marked.group(1).split())
        consular = True
    elif london:
        phone = " ".join(london.group(1).split())
        consular = True
    if not mission_name and phone and re.search(r"FCDO in London", text, re.I):
        mission_name = "FCDO in London"
    if not mission_name:
        return None, police, ambulance
    website = str(page.get("web_url") or "").strip()
    if website and not website.startswith("http"):
        website = ""
    return {
        "name": mission_name or "British mission",
        "kind": "embassy",
        "address": "",
        "phone": phone,
        "website": website,
        "lat": None,
        "lng": None,
        "source": "UK FCDO",
        "group": "uk",
        "consular": consular,
    }, police, ambulance


def _fetch_uk_mission(identity: dict) -> tuple[dict | None, str, str]:
    slug = _fcdo_slug_for(identity)
    if not slug:
        return None, "", ""
    page = _advisory_get_json(f"https://www.gov.uk/api/content/foreign-travel-advice/{slug}")
    if not isinstance(page, dict):
        return None, "", ""
    country = ((page.get("details") or {}).get("country") or {}).get("name") or ""
    if country and _advisory_norm(country) != identity.get("norm"):
        return None, "", ""
    return _parse_uk_mission(page)


def _merge_uk_with_osm(uk: dict | None, missions: list[dict]) -> list[dict]:
    if not uk:
        return missions
    kept = []
    for item in missions:
        blob = item["name"].lower()
        if item.get("group") == "uk" or "british" in blob or "united kingdom" in blob:
            if uk.get("lat") is None and item.get("lat") is not None:
                uk["lat"] = item["lat"]
                uk["lng"] = item["lng"]
            if not uk.get("address") and item.get("address"):
                uk["address"] = item["address"]
            if not uk.get("phone") and item.get("phone"):
                uk["phone"] = item["phone"]
            continue
        kept.append(item)
    return [uk] + kept


def _emergency_note(police: str, ambulance: str) -> str:
    if police and ambulance:
        return ""
    missing = []
    if not police:
        missing.append("police")
    if not ambulance:
        missing.append("ambulance")
    return "Local " + " and ".join(missing) + " number is not listed." if len(missing) == 1 else "Local police and ambulance numbers are not listed."


def country_assets_payload(country: str) -> dict:
    """Embassies, hospitals, and airports for one country. Failed upstreams keep the last cache."""
    identity = _asset_identity(country)
    blank = _blank_assets(identity["name"], identity.get("iso2") or "", identity.get("iso3") or "")
    blank["medical_note"] = _medical_note_for(_cached_advisory_level(identity.get("iso3") or ""), identity.get("norm") or "")
    if not identity.get("key") or identity["key"] == "unknown":
        return blank
    try:
        with _asset_lock:
            cached = _asset_cache_get(identity["key"])
            if isinstance(cached, dict) and _asset_fresh(cached):
                fresh = dict(cached)
                fresh["cached"] = True
                fresh["ok"] = True
                return fresh
            previous = cached if isinstance(cached, dict) else {}
            hubs: list[tuple[str, float, float]] = []
            try:
                hubs = _asset_hubs(identity)
            except Exception as exc:
                print(f"Country asset hubs failed for {identity['key']}: {exc.__class__.__name__}.")
                hubs = list(_ASSET_HUBS.get(identity.get("norm") or "", ()))
            uk_mission = None
            police = ""
            ambulance = ""
            uk_ok = False
            try:
                uk_mission, police, ambulance = _fetch_uk_mission(identity)
                uk_ok = True
            except Exception as exc:
                print(f"UK mission lookup failed for {identity['key']}: {exc.__class__.__name__}.")
            osm_missions: list[dict] = []
            missions_ok = False
            try:
                osm_missions = _overpass_missions(hubs)
                missions_ok = True
            except Exception as exc:
                print(f"Embassy lookup failed for {identity['key']}: {exc.__class__.__name__}.")
            hospitals: list[dict] = []
            hospitals_ok = False
            try:
                hospitals = _overpass_hospitals(hubs)
                hospitals_ok = True
            except Exception as exc:
                print(f"Hospital lookup failed for {identity['key']}: {exc.__class__.__name__}.")
            airports: list[dict] = []
            airports_ok = False
            try:
                airports = _overpass_airports(hubs)
                airports_ok = True
            except Exception as exc:
                print(f"Airport lookup failed for {identity['key']}: {exc.__class__.__name__}.")
            if not uk_ok:
                police = str(previous.get("police") or "")
                ambulance = str(previous.get("ambulance") or "")
                old_uk = next((item for item in previous.get("missions") or [] if item.get("source") == "UK FCDO"), None)
                uk_mission = old_uk
            if not missions_ok:
                osm_missions = [item for item in previous.get("missions") or [] if item.get("source") != "UK FCDO"]
            if not hospitals_ok:
                hospitals = list(previous.get("hospitals") or [])
            if not airports_ok:
                airports = list(previous.get("airports") or [])
            any_ok = uk_ok or missions_ok or hospitals_ok or airports_ok
            if not any_ok:
                if previous:
                    stale = dict(previous)
                    stale["ok"] = True
                    stale["cached"] = True
                    stale["contacts_available"] = True
                    return stale
                blank["contacts_available"] = False
                return blank
            payload = _blank_assets(identity["name"], identity.get("iso2") or "", identity.get("iso3") or "")
            payload["contacts_available"] = True
            payload["cached"] = False
            payload["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload["missions"] = _merge_uk_with_osm(uk_mission, osm_missions)
            payload["hospitals"] = hospitals
            payload["airports"] = airports
            payload["police"] = police
            payload["ambulance"] = ambulance
            payload["emergency_note"] = _emergency_note(police, ambulance)
            payload["medical_note"] = _medical_note_for(_cached_advisory_level(identity.get("iso3") or ""), identity.get("norm") or "")
            _asset_cache_put(identity["key"], payload)
            return payload
    except Exception as exc:
        print(f"Country assets failed for {identity.get('key')}: {exc.__class__.__name__}.")
        cached = _asset_cache_get(identity.get("key") or "")
        if isinstance(cached, dict):
            stale = dict(cached)
            stale["ok"] = True
            stale["cached"] = True
            return stale
        return blank



CALENDAR_PATH = ROOT / "operational_calendar.json"
CALENDAR_CACHE_PATH = ROOT / "country_calendar_cache.json"
CALENDAR_TTL_SECONDS = 24 * 60 * 60
NAGER_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{iso2}"
_calendar_lock = threading.Lock()
_calendar_memory: dict[str, dict] = {}
_calendar_file_cache: dict | None = None
_CAL_GOV = ("GOV / EMBASSY / BANK CLOSURE", "#eab308", "embassy/bank closure", "Expect embassy and bank closures. Confirm visas 48 hours ahead.")
_CAL_MOVE = ("MOVEMENT / CURFEW IMPACT", "#ea580c", "movement impact", "Expect slower roads and possible curfews. Confirm routes a day ahead.")
_CAL_FLASH = ("HIGH FLASHPOINT / PROTEST RISK", "#dc2626", "protest risk", "Avoid central squares and confirm visas 48 hours ahead.")


def _empty_calendar(iso2: str, today: date | None = None) -> dict:
    day = today or datetime.now(timezone.utc).date()
    return {"ok": True, "iso2": iso2, "today": day.isoformat(), "cached": False, "events": []}


def _known_iso2(iso2: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}", iso2):
        return False
    _by_name, by_iso3 = _country_indexes()
    return any(str((meta or {}).get("iso2") or "") == iso2 for meta in by_iso3.values())


def _calendar_dictionary() -> dict:
    global _calendar_file_cache
    if isinstance(_calendar_file_cache, dict):
        return _calendar_file_cache
    try:
        payload = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Operational calendar file was not readable ({exc.__class__.__name__}).")
        payload = {}
    _calendar_file_cache = payload if isinstance(payload, dict) else {}
    return _calendar_file_cache


def _calendar_public_event(item: dict) -> dict:
    return {
        "date": item["date"],
        "name": item["name"],
        "tier": item["tier"],
        "color": item["color"],
        "impact": item.get("impact") or "",
        "note": item.get("note") or "",
    }


def _local_calendar_events(iso2: str, start: date, end: date) -> list[dict]:
    data = _calendar_dictionary()
    events = []
    for item in data.get("anniversaries") or []:
        if iso2 not in (item.get("iso2") or []):
            continue
        for year in (start.year, end.year):
            try:
                moment = date(year, int(item["month"]), int(item["day"]))
            except (TypeError, ValueError):
                continue
            if start <= moment <= end:
                row = dict(item)
                row["date"] = moment.isoformat()
                events.append(_calendar_public_event(row))
    for item in data.get("observances") or []:
        if iso2 not in (item.get("iso2") or []):
            continue
        try:
            moment = date.fromisoformat(str(item.get("date") or ""))
        except ValueError:
            continue
        if start <= moment <= end:
            events.append(_calendar_public_event(item))
    return events


def _nager_tier(name: str) -> tuple[str, str, str, str]:
    if re.search(r"\b(revolution|uprising|intifada)\b", name, re.I):
        return _CAL_FLASH
    if re.search(r"\b(ramadan|eid al-fitr|eid al-adha|eid ul-fitr|eid ul-adha|ashura)\b", name, re.I):
        return _CAL_MOVE
    return _CAL_GOV


def _nager_headers() -> dict[str, str]:
    headers = feed_request_headers()
    headers["Accept"] = "application/json, text/plain, */*"
    return headers


def _nager_holidays(iso2: str, year: int) -> list[dict]:
    response = requests.get(NAGER_URL.format(year=year, iso2=iso2), headers=_nager_headers(), timeout=FEED_TIMEOUT)
    if response.status_code == 204 or not response.content:
        return []
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []
    events = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("localName") or "").strip()
        raw = str(item.get("date") or "")
        if not name or not raw:
            continue
        try:
            date.fromisoformat(raw)
        except ValueError:
            continue
        tier, color, impact, note = _nager_tier(name)
        events.append({
            "date": raw,
            "name": name,
            "tier": tier,
            "color": color,
            "impact": impact,
            "note": note,
        })
    return events


def _calendar_overlaps(left: dict, right: dict) -> bool:
    if left.get("date") != right.get("date"):
        return False
    a = _advisory_norm(left.get("name"))
    b = _advisory_norm(right.get("name"))
    return bool(a and b and (a in b or b in a))


def _merge_calendar(local: list[dict], remote: list[dict]) -> list[dict]:
    merged = list(local)
    for item in remote:
        if any(_calendar_overlaps(item, existing) for existing in merged):
            continue
        merged.append(item)
    merged.sort(key=lambda item: (item.get("date") or "", item.get("name") or ""))
    return merged


def _read_calendar_disk() -> dict:
    if not CALENDAR_CACHE_PATH.exists() or CALENDAR_CACHE_PATH.stat().st_size == 0:
        return {}
    try:
        payload = json.loads(CALENDAR_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Country calendar cache was not readable ({exc.__class__.__name__}).")
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_calendar_disk(store: dict) -> None:
    try:
        temporary = CALENDAR_CACHE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
        temporary.replace(CALENDAR_CACHE_PATH)
    except OSError as exc:
        print(f"Country calendar cache was not written ({exc.__class__.__name__}).")


def country_calendar_payload(iso2: str, today: date | None = None) -> dict:
    """Public holidays and fixed anniversaries from today through 90 days."""
    code = str(iso2 or "").strip().upper()
    day = today or datetime.now(timezone.utc).date()
    if not _known_iso2(code):
        return _empty_calendar(code, day)
    end = day + timedelta(days=90)
    try:
        with _calendar_lock:
            cached = _calendar_memory.get(code)
            if not isinstance(cached, dict):
                cached = _read_calendar_disk().get(code)
                if isinstance(cached, dict):
                    _calendar_memory[code] = cached
            if isinstance(cached, dict) and cached.get("today") == day.isoformat():
                moment = osint.parse_timestamp(str(cached.get("fetched_at") or ""))
                if moment is not None and (datetime.now(timezone.utc) - moment).total_seconds() < CALENDAR_TTL_SECONDS:
                    fresh = dict(cached)
                    fresh["cached"] = True
                    fresh["ok"] = True
                    return fresh
            local = _local_calendar_events(code, day, end)
            remote: list[dict] = []
            remote_ok = True
            for year in (day.year, end.year):
                try:
                    remote.extend(_nager_holidays(code, year))
                except Exception as exc:
                    remote_ok = False
                    print(f"Nager calendar failed for {code} {year}: {exc.__class__.__name__}.")
            if not remote_ok:
                if isinstance(cached, dict) and cached.get("events"):
                    stale = dict(cached)
                    stale["ok"] = True
                    stale["cached"] = True
                    stale["today"] = day.isoformat()
                    return stale
                remote = []
            remote = [item for item in remote if day.isoformat() <= str(item.get("date") or "") <= end.isoformat()]
            payload = _empty_calendar(code, day)
            payload["events"] = _merge_calendar(local, remote)
            payload["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _calendar_memory[code] = payload
            store = _read_calendar_disk()
            store[code] = payload
            _write_calendar_disk(store)
            return payload
    except Exception as exc:
        print(f"Country calendar failed for {code}: {exc.__class__.__name__}.")
        cached = _calendar_memory.get(code) or _read_calendar_disk().get(code)
        if isinstance(cached, dict):
            stale = dict(cached)
            stale["ok"] = True
            stale["cached"] = True
            return stale
        return _empty_calendar(code, day)


def handle_api(method: str, path: str, body: bytes = b"", authorization: str = "") -> tuple[int, str, bytes] | None:
    """Shared API dispatch for the built-in server and the Flask/Gunicorn entry."""
    route = path.split("?", 1)[0]
    query = path.split("?", 1)[1] if "?" in path else ""
    verb = str(method or "GET").upper()
    try:
        import ppv

        ppv.set_route(route)
        if ppv.current() and route in {"/api/export.csv", "/api/export.geojson"} and ppv.export_blocked(query):
            if route.endswith(".csv"):
                return 200, "text/csv; charset=utf-8", b"title,summary\n"
            return 200, "application/geo+json; charset=utf-8", b'{"type":"FeatureCollection","features":[]}'
        if ppv.current() and route in {"/api/daily-chart", "/api/daily-chart.png"}:
            if route.endswith(".png"):
                return 200, "image/png", dark_map_png()
            return 200, "application/json; charset=utf-8", _api_json(empty_daily_chart())
        if ppv.current() and route.startswith("/api/country-assets/"):
            asset_country = unquote(route[len("/api/country-assets/"):]).strip("/")
            if not ppv.matches({"country": asset_country}, ppv.current()):
                return 200, "application/json; charset=utf-8", _api_json({"ok": False, "message": "This pass does not include that country.", "assets": []})
    except Exception:
        pass
    if not route.startswith("/api/"):
        return None
    if route == "/api/briefings/publish" or route.startswith("/api/briefings/published"):
        try:
            import daily_ao_publisher

            routed = daily_ao_publisher.dispatch(verb, path, body, authorization=authorization)
        except Exception as exc:
            print(f"Daily AO publisher failed: {exc.__class__.__name__}.")
            routed = None
        if routed is not None:
            return routed
    if route in {"/api/ai/claude-status", "/api/risk-assessments/ai-draft", "/api/briefings/ai-draft"}:
        try:
            import claude_client

            routed = claude_client.dispatch(verb, path, body)
        except Exception as exc:
            print(f"Claude draft route failed: {exc.__class__.__name__}.")
            routed = None
        if routed is not None:
            return routed
    try:
        import platform_phase1

        routed = platform_phase1.dispatch(verb, path, body)
        if routed is not None:
            return routed
    except Exception as exc:
        print(f"Platform route failed: {exc.__class__.__name__}.")
        if route.startswith("/api/grok") or route.startswith("/api/advisories") or route.startswith("/api/enterprise"):
            return 200, "application/json; charset=utf-8", b'{"ok":false,"message":"That request could not be completed."}'
    try:
        if verb == "GET" and route == "/api/thermal":
            return 200, "application/json; charset=utf-8", _api_json(thermal_response())
        if verb == "GET" and route == "/api/data":
            return 200, "application/json; charset=utf-8", _api_json(data_response())
        if verb == "POST" and route == "/api/data":
            return 200, "application/json; charset=utf-8", _api_json(data_post_response(body))
        if verb == "GET" and route == "/api/feed-status":
            return 200, "application/json; charset=utf-8", _api_json(feed_status_payload())
        if verb == "GET" and route == "/api/travel-advisories":
            return 200, "application/json; charset=utf-8", _api_json(travel_advisories_payload())
        if verb == "GET" and route in {"/api/geo/catalog", "/api/geo/template", "/api/geo/summary", "/api/pricing/ao"}:
            import geo_split

            params = parse_qs(query, keep_blank_values=True)
            if route == "/api/geo/catalog":
                return 200, "application/json; charset=utf-8", _api_json(geo_split.catalog())
            if route == "/api/geo/template":
                country = (params.get("country") or [""])[0]
                return 200, "application/json; charset=utf-8", _api_json(geo_split.country_template(country))
            if route == "/api/pricing/ao":
                return 200, "application/json; charset=utf-8", _api_json(geo_split.quote_ao(
                    (params.get("mode") or ["country"])[0],
                    (params.get("count") or ["1"])[0],
                    (params.get("briefing") or ["none"])[0],
                    (params.get("currency") or ["gbp"])[0],
                    (params.get("billing") or ["monthly"])[0],
                ))
            region = (params.get("region") or [""])[0]
            country = (params.get("country") or [""])[0]
            features = []
            try:
                features = list((data_response().get("features") or []))
            except Exception as exc:
                print(f"Geopolitical summary could not read the map snapshot: {exc.__class__.__name__}.")
            try:
                import ppv

                grant = ppv.current()
                if grant:
                    features = [feature for feature in features if ppv.matches((feature.get("properties") if isinstance(feature, dict) else {}) or feature, grant)]
            except Exception:
                pass
            return 200, "application/json; charset=utf-8", _api_json(geo_split.compose_summary(features, region, country))
        if verb == "GET" and route == "/api/risk-assessments/country-incidents":
            params = parse_qs(query, keep_blank_values=True)
            country = (params.get("country") or [""])[0]
            area = (params.get("area") or params.get("city") or [""])[0]
            try:
                days = int((params.get("days") or ["7"])[0])
            except ValueError:
                days = 7
            return 200, "application/json; charset=utf-8", _api_json(country_incidents_response(country, area, days))
        if verb == "GET" and route == "/api/news-stream":
            import news_stream

            params = parse_qs(query, keep_blank_values=True)
            region = (params.get("region") or [""])[0]
            return 200, "application/json; charset=utf-8", _api_json(news_stream.payload(region))
        if verb == "GET" and route.startswith("/api/country-assets/"):
            country = unquote(route[len("/api/country-assets/"):]).strip("/")
            return 200, "application/json; charset=utf-8", _api_json(country_assets_payload(country))
        if verb == "GET" and route.startswith("/api/country-calendar/"):
            iso2 = unquote(route[len("/api/country-calendar/"):]).strip("/")
            return 200, "application/json; charset=utf-8", _api_json(country_calendar_payload(iso2))
        if verb == "GET" and route == "/api/daily-chart":
            return 200, "application/json; charset=utf-8", _api_json(daily_chart_response(query))
        if verb == "GET" and route == "/api/daily-chart.png":
            return 200, "image/png", daily_chart_png_response(query)
        if verb == "GET" and route == "/api/export.csv":
            return 200, "text/csv; charset=utf-8", export_csv_bytes(query)
        if verb == "GET" and route == "/api/export.geojson":
            return 200, "application/geo+json; charset=utf-8", export_geojson_bytes(query)
        if verb == "POST" and route == "/api/report":
            return 200, "application/json; charset=utf-8", _api_json(report_response(body))
        if verb == "POST" and route == "/api/poll-now":
            return 200, "application/json; charset=utf-8", _api_json(poll_response())
        if verb == "POST" and route == "/api/summary":
            return 200, "application/json; charset=utf-8", _api_json(summary_response())
    except Exception as exc:
        print(f"{verb} {route} failed: {exc.__class__.__name__}.")
        if route == "/api/daily-chart":
            return 200, "application/json; charset=utf-8", _api_json(empty_daily_chart())
        if route == "/api/daily-chart.png":
            return 200, "image/png", dark_map_png()
        if route == "/api/travel-advisories":
            return 200, "application/json; charset=utf-8", _api_json(empty_travel_advisories())
        if route.startswith("/api/country-assets/"):
            country = unquote(route[len("/api/country-assets/"):]).strip("/")
            return 200, "application/json; charset=utf-8", _api_json(_blank_assets(country or "Unknown"))
        if route.startswith("/api/country-calendar/"):
            iso2 = unquote(route[len("/api/country-calendar/"):]).strip("/")
            return 200, "application/json; charset=utf-8", _api_json(_empty_calendar(iso2))
    return None


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
        backfill_country_tags()
        backfill_levant_routes()
        backfill_syria_governorates()
        backfill_threat_codes()
        backfill_actor_system()
        drop_excluded_items()
        _threads_started = True
    try:
        import global_feeds

        global_feeds.start()
    except Exception as exc:
        print(f"Global feeds did not start: {exc.__class__.__name__}.")
    try:
        import news_stream

        news_stream.start()
    except Exception as exc:
        print(f"News stream did not start: {exc.__class__.__name__}.")
    start_translation_pass()
    start_poller()
    start_sweep_daemon()
    start_summary_daemon()
    start_briefing_daemon()


HOME_DEEP_LINK_KEYS = frozenset({
    "country", "region", "governorate", "lat", "lng", "zoom",
    "focusAsset", "showAssets", "basemap", "pass", "severity", "time",
})


def home_url_opens_the_map(target: str) -> bool:
    """True when a legacy homepage URL carries a live-map query."""
    query = target.split("?", 1)[1] if "?" in (target or "") else ""
    if not query:
        return False
    params = parse_qs(query, keep_blank_values=False)
    for key in HOME_DEEP_LINK_KEYS:
        if any(str(value).strip() for value in (params.get(key) or [])):
            return True
    return False


class MapHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        if not getattr(self, "_cache_control_set", False):
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

    def _sse(self, chunks) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for chunk in chunks:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self._blocked():
            self.send_error(404)
            return
        route = self._route()
        if route == "/" and home_url_opens_the_map(self.path):
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            location = "/map" + (("?" + query) if query else "")
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route in {"/sw.js", "/manifest.webmanifest"}:
            filename = "sw.js" if route == "/sw.js" else "manifest.webmanifest"
            path = ROOT / "static" / filename
            if not path.is_file():
                self.send_error(404)
                return
            payload = path.read_bytes()
            content_type = "application/javascript" if route == "/sw.js" else "application/manifest+json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if route == "/sw.js":
                self.send_header("Service-Worker-Allowed", "/")
                self.send_header("Cache-Control", "no-cache")
                self._cache_control_set = True
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            import platform_phase1

            page = platform_phase1.PAGES.get(self._route())
        except Exception:
            page = None
        if page:
            self.path = "/" + page
            super().do_GET()
            return
        handled = handle_api("GET", self.path)
        if handled:
            self._send(*handled)
            return
        super().do_GET()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 1_000_000)) if length else b""
        try:
            import platform_phase1

            if platform_phase1.is_stream_route(self._route()):
                self._sse(platform_phase1.stream_for(self._route(), raw))
                return
        except Exception as exc:
            print(f"Grok stream failed: {exc.__class__.__name__}.")
        handled = handle_api("POST", self.path, raw, authorization=self.headers.get("Authorization") or "")
        if handled:
            self._send(*handled)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:
        super().log_message(fmt, *args)


def severity_color(value: object) -> str:
    rank = str(value or "").strip().lower()
    if rank == "high":
        return "#E11D48"
    if rank == "low":
        return "#3B82F6"
    return "#D97706"


def dark_map_png() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (800, 480), (6, 10, 19))
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
            url_template="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
            tile_request_timeout=8,
            headers={"User-Agent": BROWSER_USER_AGENT},
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
    if report_type == "daily":
        return london_cycle(moment, "completed")
    end = moment.replace(hour=7, minute=0, second=0, microsecond=0)
    if moment < end:
        end -= timedelta(days=1)
    if report_type == "weekly":
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


def parse_event_time(value: object) -> datetime | None:
    """ISO, SQLite YYYY-MM-DD HH:MM:SS, or epoch seconds/milliseconds. Never raises."""
    try:
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, bool) or value is None:
            return None
        elif isinstance(value, (int, float)):
            moment = _epoch_to_datetime(float(value))
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
                moment = _epoch_to_datetime(float(text))
            else:
                if text.endswith(("Z", "z")):
                    text = text[:-1] + "+00:00"
                moment = datetime.fromisoformat(text)
        else:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _epoch_to_datetime(number: float) -> datetime:
    if not math.isfinite(number):
        raise ValueError("bad epoch")
    if abs(number) >= 100_000_000_000:
        number /= 1000.0
    return datetime.fromtimestamp(number, timezone.utc)


def period_events(start: datetime, end: datetime) -> list[dict]:
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    found = []
    for feature in combined_features():
        try:
            props = feature.get("properties") or {}
            moment = parse_event_time(props.get("timestamp"))
        except Exception:
            continue
        if moment is None or moment < start_utc or moment >= end_utc:
            continue
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        lng = lat = None
        if isinstance(coords, list) and len(coords) >= 2:
            placed = osint.valid_coord(coords[0], coords[1])
            if placed:
                lng, lat = placed
        title, summary = visible_english(props.get("title"), props.get("summary"))
        found.append(
            {
                "title": title or "Incident",
                "summary": summary,
                "location_name": osint.one_sentence(props.get("location_name"))[:160],
                "governorate": osint.match_governorate(props.get("governorate"))
                or osint.match_governorate(props.get("location_name")),
                "region": region_name(props.get("country") or props.get("region")),
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


_CHART_HOURS = list(range(7, 24)) + list(range(0, 7))
_CHART_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def chart_bounds(window: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Yesterday is the completed 07:00–07:00 window. Current is the open cycle through now."""
    moment = now.astimezone(LONDON) if now is not None else datetime.now(LONDON)
    if str(window or "").strip().lower() == "current":
        return london_cycle(moment, "active")
    return london_cycle(moment, "completed")


def format_chart_window(start: datetime, end: datetime) -> str:
    def piece(moment: datetime) -> str:
        return f"{moment.day} {_CHART_MONTHS[moment.month - 1]} {moment.strftime('%H:%M')}"

    zone = start.tzname() or "GMT"
    return f"{piece(start)} – {piece(end)} {zone}"


def _chart_buckets() -> list[dict]:
    return [{"hour_label": f"{hour:02d}:00", "high": 0, "medium": 0, "low": 0} for hour in _CHART_HOURS]


def empty_daily_chart(window: str = "yesterday", now: datetime | None = None) -> dict:
    label = ""
    try:
        start, end = chart_bounds(window, now)
        label = format_chart_window(start, end)
    except Exception:
        label = ""
    return {
        "window_label": label,
        "total_incidents": 0,
        "high_count": 0,
        "medium_count": 0,
        "low_count": 0,
        "hourly_buckets": _chart_buckets(),
        "region_counts": {name: 0 for name in REGIONS},
    }


def daily_chart(window: str = "yesterday", now: datetime | None = None) -> dict:
    try:
        kind = "current" if str(window or "").strip().lower() == "current" else "yesterday"
        start, end = chart_bounds(kind, now)
        buckets = {hour: {"high": 0, "medium": 0, "low": 0} for hour in _CHART_HOURS}
        regions = {name: 0 for name in REGIONS}
        high = medium = low = 0
        for event in period_events(start, end):
            try:
                stamp = event.get("timestamp")
                if not isinstance(stamp, datetime):
                    stamp = parse_event_time(stamp)
                if not isinstance(stamp, datetime):
                    continue
                rank = str(event.get("severity") or "").strip().lower()
                if rank == "high":
                    field = "high"
                    high += 1
                elif rank == "low":
                    field = "low"
                    low += 1
                else:
                    field = "medium"
                    medium += 1
                buckets[stamp.astimezone(LONDON).hour][field] += 1
                name = region_name(event.get("country") or event.get("region"))
                if name in regions:
                    regions[name] += 1
            except Exception:
                continue
        return {
            "window_label": format_chart_window(start, end),
            "total_incidents": high + medium + low,
            "high_count": high,
            "medium_count": medium,
            "low_count": low,
            "hourly_buckets": [
                {"hour_label": f"{hour:02d}:00", **buckets[hour]} for hour in _CHART_HOURS
            ],
            "region_counts": regions,
        }
    except Exception as exc:
        print(f"Daily chart failed: {exc.__class__.__name__}.")
        return empty_daily_chart(window, now)


def daily_chart_response(query: str = "") -> dict:
    window = "yesterday"
    try:
        raw = (parse_qs(query or "").get("window") or ["yesterday"])[0]
        window = "current" if str(raw).strip().lower() == "current" else "yesterday"
        chart = daily_chart(window)
        if not isinstance(chart, dict) or not chart.get("hourly_buckets"):
            return empty_daily_chart(window)
        return chart
    except Exception as exc:
        print(f"GET /api/daily-chart failed: {exc.__class__.__name__}.")
        return empty_daily_chart(window)


def daily_chart_png_response(query: str = "") -> bytes:
    try:
        png = render_daily_chart_png(daily_chart_response(query))
        if png:
            return png
    except Exception as exc:
        print(f"GET /api/daily-chart.png failed: {exc.__class__.__name__}.")
    return dark_map_png()


def _chart_font(size: int, bold: bool = False):
    from PIL import ImageFont

    path = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    if os.path.isfile(path):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_daily_chart_png(chart: dict) -> bytes:
    """High-DPI dark chart for the daily briefing. A draw failure returns no image."""
    try:
        from PIL import Image, ImageDraw

        scale = 2
        width, height = 840 * scale, 560 * scale
        image = Image.new("RGB", (width, height), (6, 10, 19))
        draw = ImageDraw.Draw(image)
        title_font = _chart_font(16 * scale, bold=True)
        body_font = _chart_font(12 * scale)
        small_font = _chart_font(9 * scale)
        gold = (212, 175, 55)
        muted = (148, 163, 184)
        high_color = (225, 29, 72)
        medium_color = (217, 119, 6)
        low_color = (59, 130, 246)
        track = (13, 20, 36)
        label = str(chart.get("window_label") or "")
        draw.text((20 * scale, 16 * scale), label, font=title_font, fill=gold)
        counts = (
            f"{int(chart.get('total_incidents') or 0)} incidents"
            f"    High {int(chart.get('high_count') or 0)}"
            f"    Medium {int(chart.get('medium_count') or 0)}"
            f"    Low {int(chart.get('low_count') or 0)}"
        )
        draw.text((20 * scale, 42 * scale), counts, font=body_font, fill=muted)
        buckets = list(chart.get("hourly_buckets") or _chart_buckets())
        if len(buckets) != 24:
            buckets = _chart_buckets()
        plot_left = 28 * scale
        plot_top = 78 * scale
        plot_width = 784 * scale
        plot_height = 210 * scale
        slot = plot_width / 24
        gap = 3 * scale
        max_stack = max(
            1,
            max(int(bucket.get("high") or 0) + int(bucket.get("medium") or 0) + int(bucket.get("low") or 0) for bucket in buckets),
        )
        for index, bucket in enumerate(buckets):
            x0 = plot_left + index * slot + gap / 2
            x1 = plot_left + (index + 1) * slot - gap / 2
            draw.rectangle((x0, plot_top, x1, plot_top + plot_height), fill=track)
            y = plot_top + plot_height
            for key, color in (("low", low_color), ("medium", medium_color), ("high", high_color)):
                count = int(bucket.get(key) or 0)
                if count <= 0:
                    continue
                bar_height = plot_height * count / max_stack
                draw.rectangle((x0, y - bar_height, x1, y), fill=color)
                y -= bar_height
            hour = str(bucket.get("hour_label") or "")[:2]
            if index % 2 == 0:
                draw.text((x0, plot_top + plot_height + 4 * scale), hour, font=small_font, fill=muted)
        legend_y = plot_top + plot_height + 28 * scale
        legend = (("High", high_color), ("Medium", medium_color), ("Low", low_color))
        cursor = 20 * scale
        for name, color in legend:
            draw.rectangle((cursor, legend_y, cursor + 12 * scale, legend_y + 12 * scale), fill=color)
            draw.text((cursor + 16 * scale, legend_y - 1 * scale), name, font=body_font, fill=muted)
            cursor += 110 * scale
        regions = chart.get("region_counts") if isinstance(chart.get("region_counts"), dict) else {}
        widest = max([1] + [int(regions.get(name) or 0) for name in REGIONS])
        row_top = legend_y + 28 * scale
        row_h = 22 * scale
        bar_left = 168 * scale
        bar_right = width - 48 * scale
        for offset, name in enumerate(REGIONS):
            y = row_top + offset * row_h
            count = int(regions.get(name) or 0)
            draw.text((20 * scale, y), name, font=small_font, fill=muted)
            draw.rectangle((bar_left, y + 3 * scale, bar_right, y + 11 * scale), fill=track)
            if count:
                span = (bar_right - bar_left) * count / widest
                draw.rectangle((bar_left, y + 3 * scale, bar_left + span, y + 11 * scale), fill=gold)
            draw.text((bar_right + 6 * scale, y), str(count), font=small_font, fill=gold)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        print(f"Daily chart image failed: {exc.__class__.__name__}.")
        return b""


def smtp_settings() -> dict | None:
    osint.load_dotenv(osint.ROOT / ".env")
    host = (os.environ.get("SMTP_HOST") or "").strip()
    sender = (os.environ.get("SMTP_FROM") or "").strip()
    recipient = (os.environ.get("SMTP_TO") or "").strip()
    if not recipient:
        for part in re.split(r"[,;\s]+", os.environ.get("REPORT_RECIPIENTS") or ""):
            if part.strip() and "@" in part:
                recipient = part.strip()
                break
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
    sample = []
    for item in events[:40]:
        title, summary = visible_english(item.get("title"), item.get("summary"))
        if not title:
            continue
        sample.append(
            {
                "title": title,
                "summary": summary,
                "location": item["location_name"],
                "region": item["region"],
                "governorate": item["governorate"],
                "severity": item["severity"],
                "corroboration": item["corroboration"],
            }
        )
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


def briefing_html(
    report_type: str,
    start: datetime,
    end: datetime,
    events: list[dict],
    narrative: dict,
    chart: bool = False,
) -> str:
    counts = {name: 0 for name in REGIONS}
    governorates = {name: 0 for name in osint.SYRIA_GOVERNORATES}
    for event in events:
        name = region_name(event.get("country") or event.get("region"))
        if name in counts:
            counts[name] += 1
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
            f"<div style=\"width:{width}%;height:10px;border-radius:999px;background:linear-gradient(90deg,#18D4C0 0%,#14A89A 100%);\"></div>"
            "</div></div>"
        )
    region_notes = []
    for name in REGIONS:
        region_notes.append(
            "<h3 style=\"color:#18D4C0;font-size:15px;margin:16px 0 6px;\">"
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
            title, summary = visible_english(item.get("title"), item.get("summary"))
            if not title:
                continue
            summary_line = ""
            if summary:
                summary_line = f"{html.escape(summary)}<br>"
            incident_html.append(
                "<div style=\"border-top:1px solid #1c2229;padding:8px 0;\">"
                f"<strong>{html.escape(title)}</strong><br>"
                f"{summary_line}"
                f"{html.escape(item.get('location_name') or 'Location unavailable')}<br>"
                f"{html.escape(coords)}<br>"
                "<span style=\"display:inline-block;margin-top:4px;color:#18D4C0;border:1px solid #18D4C0;"
                f"border-radius:999px;padding:1px 8px;font-size:12px;\">{html.escape(item.get('source') or 'Live')}</span>"
                "</div>"
            )
        incidents = "".join(incident_html)
    else:
        incidents = "<p>No high-severity or corroborated incidents in this window.</p>"
    title = f"{report_type.upper()} OSINT INTELLIGENCE BRIEFING"
    return (
        "<html><body style=\"margin:0;background:#041222;color:#E2E8F0;font-family:Segoe UI,sans-serif;\">"
        "<div style=\"max-width:720px;margin:0 auto;padding:24px;\">"
        + briefing_banner()
        + "<div style=\"background:#090E16;border:1px solid #0F4C4C;border-radius:12px;padding:16px 18px;\">"
        f"<h1 style=\"color:#18D4C0;font-size:20px;margin:0 0 8px;\">{html.escape(title)}</h1>"
        f"<p style=\"margin:0 0 12px;color:#d5e4e8;\">{html.escape(format_briefing_window(start, end))}</p>"
        + (
            "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"margin:0 0 8px;\">"
            "<tr>"
            "<td width=\"50%\" valign=\"top\" style=\"padding-right:6px;\">"
            "<img src=\"cid:incident_map\" alt=\"Incident map\" style=\"width:100%;border-radius:12px;\" />"
            "</td>"
            "<td width=\"50%\" valign=\"top\" style=\"padding-left:6px;\">"
            "<img src=\"cid:daily_chart\" alt=\"07:00 to 07:00 incident chart\" style=\"width:100%;border-radius:12px;\" />"
            "</td></tr></table>"
            if chart
            else "<img src=\"cid:incident_map\" alt=\"Incident map\" style=\"width:100%;border-radius:12px;\" />"
        )
        + "<h2 style=\"color:#18D4C0;font-size:16px;\">Regions</h2>"
        + "".join(bars)
        + f"<h2 style=\"color:#18D4C0;font-size:16px;\">Overall</h2><p>{html.escape(narrative['overall'])}</p>"
        + "".join(region_notes)
        + "<h2 style=\"color:#18D4C0;font-size:16px;\">Syria</h2>"
        + f"<p>{html.escape(narrative['syria'])}</p><ul>{gov_lines}</ul>"
        + "<h2 style=\"color:#18D4C0;font-size:16px;\">High-severity and corroborated incidents</h2>"
        + incidents
        + "</div></div></body></html>"
    )


def logo_path() -> Path:
    return ROOT / "static" / "logo.png"


def briefing_banner() -> str:
    if logo_path().is_file():
        return (
            "<div style=\"background:#000000;text-align:center;padding:16px 12px;"
            "border-bottom:1px solid #0F4C4C;\">"
            "<img src=\"cid:georisk_logo\" alt=\"GeoRiskGlobal\" height=\"52\" "
            "style=\"height:52px;width:auto;display:block;margin:0 auto;border:0;\" />"
            "</div>"
        )
    return (
        "<div style=\"background:#000000;text-align:center;padding:16px 12px;"
        "border-bottom:1px solid #0F4C4C;color:#18D4C0;font-size:22px;"
        "font-weight:700;letter-spacing:0.04em;\">GeoRiskGlobal</div>"
    )


def briefing_subject(report_type: str, start: datetime, end: datetime) -> str:
    label = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}[report_type]
    return f"[GeoRiskGlobal] {label} Geopolitical & OSINT Briefing — {format_briefing_window(start, end)}"


def send_briefing_message(subject: str, body: str, png: bytes, settings: dict, chart_png: bytes = b"") -> bool:
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
    if chart_png:
        chart_image = MIMEImage(chart_png, _subtype="png")
        chart_image.add_header("Content-ID", "<daily_chart>")
        chart_image.add_header("Content-Disposition", "inline", filename="daily_chart.png")
        message.attach(chart_image)
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


def generate_and_email_periodic_report(report_type: str, recipients: list[str] | None = None) -> bool:
    kind = str(report_type or "").strip().lower()
    if kind not in {"daily", "weekly", "monthly"}:
        print(f"Unknown briefing type. Skipped sending.")
        return False
    chart = daily_chart("yesterday") if kind == "daily" else None
    settings = smtp_settings()
    if settings is None:
        print(f"SMTP is not configured. Skipped sending the {kind} OSINT briefing.")
        return False
    if recipients:
        settings = dict(settings)
        settings["recipient"] = ", ".join(recipients)
    start, end = briefing_window(kind)
    events = period_events(start, end)
    png = render_period_map_png(events)
    chart_png = render_daily_chart_png(chart) if chart is not None else b""
    narrative = briefing_narrative(events, kind, start, end)
    body = briefing_html(kind, start, end, events, narrative, chart=bool(chart_png))
    subject = briefing_subject(kind, start, end)
    sent = send_briefing_message(subject, body, png, settings, chart_png)
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


def send_official_daily_email(moment: datetime) -> None:
    """07:00 only. REPORT_RECIPIENTS and client profiles, otherwise SMTP_TO."""
    if moment.hour != OFFICIAL_HOUR:
        return
    slot = f"briefing-daily-{moment.strftime('%Y-%m-%d')}"
    if slot in _briefing_slots or briefing_already_sent(slot):
        _briefing_slots.add(slot)
        return
    recipients = report_recipients()
    if not recipients:
        print("SMTP is not configured. Skipped sending the daily OSINT briefing.")
        _briefing_slots.add(slot)
        return
    try:
        sent = generate_and_email_periodic_report("daily", recipients)
    except Exception as exc:
        print(f"Briefing daily failed: {exc.__class__.__name__}.")
        sent = False
    if sent:
        _briefing_slots.add(slot)
        mark_briefing_sent(slot)


def maybe_send_briefings() -> None:
    now = datetime.now(LONDON)
    if not (now.hour == 7 and now.minute < 5):
        return
    day = now.strftime("%Y-%m-%d")
    jobs = []
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
