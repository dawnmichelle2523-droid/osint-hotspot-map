"""Global and regional OSINT feeds for the live map.

Each feed has its own short timeout. A failure keeps that feed's last good
items and never blocks the Middle East poller or the map response. Results
are cached in memory and in global_feed_cache.json.

Optional sources stay idle until an environment key is present. Keys are
never written into the cache, the log, or the API payload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "global_feed_cache.json"
REFRESH_SECONDS = 10 * 60
CONNECT_TIMEOUT = 4
READ_TIMEOUT = 8
GDACS_TIMEOUT = 18
MAX_BYTES = 1_500_000
USER_AGENT = (
    "GeoRiskIntel/1.0 (osint-hotspot-map; public OSINT ingest; +https://www.gov.uk/foreign-travel-advice)"
)
NWS_USER_AGENT = "GeoRiskIntel/1.0 (server-side weather ingest)"

_lock = threading.Lock()
_refreshing = False
_started = False
_memory: dict = {"at": 0.0, "feeds": {}}

CENTROIDS = {
    "afghanistan": (33.94, 67.71), "albania": (41.15, 20.17), "algeria": (28.03, 1.66),
    "angola": (-11.20, 17.87), "argentina": (-38.42, -63.62), "armenia": (40.07, 45.04),
    "australia": (-25.27, 133.78), "austria": (47.52, 14.55), "azerbaijan": (40.14, 47.58),
    "bahrain": (26.07, 50.55), "bangladesh": (23.68, 90.36), "belarus": (53.71, 27.95),
    "belgium": (50.50, 4.47), "belize": (17.19, -88.50), "benin": (9.31, 2.32),
    "bolivia": (-16.29, -63.59), "bosnia and herzegovina": (43.92, 17.68),
    "brazil": (-14.24, -51.93), "bulgaria": (42.73, 25.49), "burkina faso": (12.24, -1.56),
    "burma": (21.91, 95.96), "myanmar": (21.91, 95.96), "burundi": (-3.37, 29.92),
    "cambodia": (12.57, 104.99), "cameroon": (7.37, 12.35), "canada": (56.13, -106.35),
    "central african republic": (6.61, 20.94), "chad": (15.45, 18.73), "chile": (-35.68, -71.54),
    "china": (35.86, 104.20), "colombia": (4.57, -74.30), "congo": (-0.23, 15.83),
    "democratic republic of the congo": (-4.04, 21.76), "costa rica": (9.75, -83.75),
    "cuba": (21.52, -77.78), "cyprus": (35.13, 33.43), "czechia": (49.82, 15.47),
    "denmark": (56.26, 9.50), "djibouti": (11.83, 42.59), "dominican republic": (18.74, -70.16),
    "ecuador": (-1.83, -78.18), "egypt": (26.82, 30.80), "el salvador": (13.79, -88.90),
    "eritrea": (15.18, 39.78), "estonia": (58.60, 25.01), "ethiopia": (9.15, 40.49),
    "finland": (61.92, 25.75), "france": (46.23, 2.21), "georgia": (42.32, 43.36),
    "germany": (51.17, 10.45), "ghana": (7.95, -1.02), "greece": (39.07, 21.82),
    "guatemala": (15.78, -90.23), "guinea": (9.95, -9.70), "guyana": (4.86, -58.93),
    "haiti": (18.97, -72.29), "honduras": (15.20, -86.24), "hungary": (47.16, 19.50),
    "india": (20.59, 78.96), "indonesia": (-0.79, 113.92), "iran": (32.43, 53.69),
    "iraq": (33.22, 43.68), "ireland": (53.41, -8.24), "israel": (31.05, 34.85),
    "italy": (41.87, 12.57), "jamaica": (18.11, -77.30), "japan": (36.20, 138.25),
    "jordan": (30.59, 36.24), "kazakhstan": (48.02, 66.92), "kenya": (0.02, 37.91),
    "kosovo": (42.60, 20.90), "kuwait": (29.31, 47.48), "kyrgyzstan": (41.20, 74.77),
    "laos": (19.86, 102.50), "latvia": (56.88, 24.60), "lebanon": (33.85, 35.86),
    "libya": (26.34, 17.23), "lithuania": (55.17, 23.88), "madagascar": (-18.77, 46.87),
    "mali": (17.57, -4.00), "mauritania": (21.01, -10.94), "mexico": (23.63, -102.55),
    "moldova": (47.41, 28.37), "mongolia": (46.86, 103.85), "montenegro": (42.71, 19.37),
    "morocco": (31.79, -7.09), "mozambique": (-18.67, 35.53), "nepal": (28.39, 84.12),
    "netherlands": (52.13, 5.29), "new zealand": (-40.90, 174.89), "nicaragua": (12.87, -85.21),
    "niger": (17.61, 8.08), "nigeria": (9.08, 8.68), "north korea": (40.34, 127.51),
    "north macedonia": (41.51, 21.75), "norway": (60.47, 8.47), "oman": (21.47, 55.98),
    "pakistan": (30.38, 69.35), "palestine": (31.95, 35.23), "panama": (8.54, -80.78),
    "papua new guinea": (-6.31, 143.96), "paraguay": (-23.44, -58.44), "peru": (-9.19, -75.02),
    "philippines": (12.88, 121.77), "poland": (51.92, 19.15), "portugal": (39.40, -8.22),
    "qatar": (25.35, 51.18), "romania": (45.94, 24.97), "russia": (61.52, 105.32),
    "rwanda": (-1.94, 29.87), "saudi arabia": (23.89, 45.08), "senegal": (14.50, -14.45),
    "serbia": (44.02, 21.01), "sierra leone": (8.46, -11.78), "singapore": (1.35, 103.82),
    "somalia": (5.15, 46.20), "south africa": (-30.56, 22.94), "south korea": (35.91, 127.77),
    "south sudan": (6.88, 31.31), "spain": (40.46, -3.75), "sri lanka": (7.87, 80.77),
    "sudan": (12.86, 30.22), "suriname": (3.92, -56.03), "sweden": (60.13, 18.64),
    "switzerland": (46.82, 8.23), "syria": (34.80, 38.99), "taiwan": (23.70, 120.96),
    "tajikistan": (38.86, 71.28), "tanzania": (-6.37, 34.89), "thailand": (15.87, 100.99),
    "togo": (8.62, 0.82), "trinidad and tobago": (10.69, -61.22), "tunisia": (33.89, 9.54),
    "turkey": (38.96, 35.24), "turkiye": (38.96, 35.24), "turkmenistan": (38.97, 59.56),
    "uganda": (1.37, 32.29), "ukraine": (48.38, 31.17), "united arab emirates": (23.42, 53.85),
    "uae": (23.42, 53.85), "united kingdom": (55.38, -3.44), "united states": (39.83, -98.58),
    "usa": (39.83, -98.58), "uruguay": (-32.52, -55.77), "uzbekistan": (41.38, 64.59),
    "venezuela": (6.42, -66.59), "vietnam": (14.06, 108.28), "yemen": (15.55, 48.52),
    "zambia": (-13.13, 27.85), "zimbabwe": (-19.02, 29.15),
}
THEATRE_OF = {
    "united states": "United States", "usa": "United States",
    "canada": "Canada",
    "brazil": "South America", "colombia": "South America", "venezuela": "South America",
    "peru": "South America", "chile": "South America", "argentina": "South America",
    "ecuador": "South America", "bolivia": "South America", "guyana": "South America",
    "suriname": "South America", "paraguay": "South America", "uruguay": "South America",
    "japan": "APAC", "taiwan": "APAC", "china": "APAC", "philippines": "APAC",
    "south korea": "APAC", "north korea": "APAC", "indonesia": "APAC", "vietnam": "APAC",
    "thailand": "APAC", "malaysia": "APAC", "singapore": "APAC", "cambodia": "APAC",
    "laos": "APAC", "myanmar": "APAC", "burma": "APAC",
    "australia": "Australia", "new zealand": "APAC",
    "afghanistan": "Afghanistan",
    "russia": "Russia & Ukraine", "ukraine": "Russia & Ukraine", "belarus": "Russia & Ukraine",
    "syria": "Middle East", "iraq": "Middle East", "iran": "Middle East", "lebanon": "Middle East",
    "israel": "Middle East", "palestine": "Middle East", "jordan": "Middle East",
    "yemen": "Middle East", "saudi arabia": "Middle East", "bahrain": "Middle East",
    "kuwait": "Middle East", "qatar": "Middle East", "united arab emirates": "Middle East",
    "uae": "Middle East", "oman": "Middle East", "sudan": "Middle East", "chad": "Middle East",
    "united kingdom": "UK & Europe", "uk": "UK & Europe", "ireland": "UK & Europe",
    "france": "UK & Europe", "germany": "UK & Europe", "italy": "UK & Europe",
    "spain": "UK & Europe", "poland": "UK & Europe", "greece": "UK & Europe",
    "norway": "UK & Europe", "sweden": "UK & Europe", "finland": "UK & Europe",
    "denmark": "UK & Europe", "netherlands": "UK & Europe", "belgium": "UK & Europe",
    "portugal": "UK & Europe", "austria": "UK & Europe", "switzerland": "UK & Europe",
    "czechia": "UK & Europe", "hungary": "UK & Europe", "romania": "UK & Europe",
    "turkey": "UK & Europe", "turkiye": "UK & Europe",
    "egypt": "Africa", "libya": "Africa", "tunisia": "Africa", "algeria": "Africa",
    "nigeria": "Africa", "kenya": "Africa", "ethiopia": "Africa", "somalia": "Africa",
    "south africa": "Africa", "mali": "Africa", "ghana": "Africa", "senegal": "Africa",
    "tanzania": "Africa", "uganda": "Africa", "mozambique": "Africa", "angola": "Africa",
    "cameroon": "Africa", "niger": "Africa", "morocco": "Africa",
    "democratic republic of the congo": "Africa", "congo": "Africa",
    "south sudan": "Africa", "ivory coast": "Africa", "rwanda": "Africa",
    "india": "South Asia", "pakistan": "South Asia", "bangladesh": "South Asia",
    "nepal": "South Asia", "sri lanka": "South Asia", "maldives": "South Asia",
    "maritime": "Maritime",
}
VOLCANO_POINTS = {
    "great sitkin": (52.076, -176.111), "kilauea": (19.421, -155.287), "mauna loa": (19.475, -155.608),
    "shishaldin": (54.756, -163.970), "pavlof": (55.417, -161.894), "semisopochnoi": (51.930, -179.580),
    "cleveland": (52.825, -169.944), "veniaminof": (56.170, -159.390), "spurr": (61.299, -152.251),
    "rainier": (46.853, -121.760), "st. helens": (46.191, -122.194), "hood": (45.374, -121.696),
    "shasta": (41.409, -122.195), "lassen": (40.488, -121.505), "yellowstone": (44.428, -110.589),
}
OBSERVATORY_POINTS = {
    "avo": (58.0, -160.0), "hvo": (19.4, -155.3), "cvo": (45.4, -122.2),
    "yvo": (44.43, -110.67), "calvo": (40.5, -121.5),
}
US_AIRPORTS = {
    "ATL": (33.64, -84.43), "DFW": (32.90, -97.04), "DEN": (39.86, -104.67),
    "ORD": (41.97, -87.91), "LAX": (33.94, -118.41), "JFK": (40.64, -73.78),
    "EWR": (40.69, -74.17), "SFO": (37.62, -122.38), "SEA": (47.45, -122.31),
    "MIA": (25.80, -80.29), "MCO": (28.43, -81.31), "LAS": (36.08, -115.15),
    "PHX": (33.43, -112.01), "IAH": (29.98, -95.34), "BOS": (42.37, -71.01),
    "MSP": (44.88, -93.22), "DTW": (42.22, -83.36), "CLT": (35.21, -80.94),
    "PHL": (39.87, -75.24), "LGA": (40.78, -73.87), "DCA": (38.85, -77.04),
    "IAD": (38.95, -77.46), "SAN": (32.73, -117.19), "TPA": (27.98, -82.53),
    "BWI": (39.18, -76.67), "SLC": (40.79, -111.98), "HNL": (21.32, -157.92),
    "ANC": (61.17, -150.00), "MDW": (41.79, -87.75), "DAL": (32.85, -96.85),
    "AUS": (30.19, -97.67), "BNA": (36.13, -86.68), "STL": (38.75, -90.37),
    "PDX": (45.59, -122.60), "CLE": (41.41, -81.85), "PIT": (40.49, -80.23),
}
US_STATES = {
    "alabama": (32.81, -86.79), "alaska": (64.20, -149.49), "arizona": (34.05, -111.09),
    "arkansas": (35.20, -91.83), "california": (36.78, -119.42), "colorado": (39.55, -105.78),
    "connecticut": (41.60, -72.76), "delaware": (38.91, -75.53), "florida": (27.66, -81.52),
    "georgia": (32.16, -82.90), "hawaii": (19.90, -155.58), "idaho": (44.07, -114.74),
    "illinois": (40.63, -89.40), "indiana": (40.27, -86.13), "iowa": (41.88, -93.10),
    "kansas": (39.01, -98.48), "kentucky": (37.84, -84.27), "louisiana": (31.24, -92.15),
    "maine": (45.25, -69.45), "maryland": (39.05, -76.64), "massachusetts": (42.41, -71.38),
    "michigan": (44.31, -85.60), "minnesota": (46.73, -94.69), "mississippi": (32.74, -89.68),
    "missouri": (38.46, -92.29), "montana": (46.88, -110.36), "nebraska": (41.49, -99.90),
    "nevada": (38.80, -116.42), "new hampshire": (43.19, -71.57), "new jersey": (40.06, -74.41),
    "new mexico": (34.52, -105.87), "new york": (43.00, -75.50), "north carolina": (35.76, -79.02),
    "north dakota": (47.55, -101.00), "ohio": (40.42, -82.91), "oklahoma": (35.01, -97.09),
    "oregon": (43.80, -120.55), "pennsylvania": (41.20, -77.19), "rhode island": (41.58, -71.48),
    "south carolina": (33.84, -81.16), "south dakota": (43.97, -99.90), "tennessee": (35.52, -86.58),
    "texas": (31.97, -99.90), "utah": (39.32, -111.09), "vermont": (44.56, -72.58),
    "virginia": (37.43, -78.66), "washington": (47.75, -120.74), "west virginia": (38.60, -80.45),
    "wisconsin": (43.78, -88.79), "wyoming": (43.08, -107.29), "district of columbia": (38.91, -77.04),
}
SA_COUNTRIES = (
    "brazil", "colombia", "venezuela", "peru", "chile", "argentina", "ecuador", "bolivia",
    "guyana", "suriname", "paraguay", "uruguay",
)


def _feeds() -> list[dict]:
    feeds = [
        {"id": "eonet", "name": "NASA EONET Natural Events", "region": "Global", "category": "Natural Disaster",
         "url": "https://eonet.gsfc.nasa.gov/api/v3/events/geojson?status=open&limit=100", "parser": "geojson"},
        {"id": "gdacs-rss", "name": "GDACS 24h Disasters", "region": "Global", "category": "Disaster",
         "url": "https://www.gdacs.org/xml/rss_24h.xml", "parser": "gdacs-rss", "timeout": 12},
        {"id": "ifrc", "name": "IFRC GO Events", "region": "Global", "category": "Disaster",
         "url": "https://goadmin.ifrc.org/api/v2/event/?limit=50&ordering=-disaster_start_date", "parser": "ifrc"},
        {"id": "usgs-quakes", "name": "USGS Earthquakes M4.5+", "region": "Global", "category": "Seismic",
         "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson", "parser": "usgs"},
        {"id": "uk-fcdo-atom", "name": "UK FCDO Travel Advice", "region": "Global", "category": "Travel Advisory",
         "url": "https://www.gov.uk/foreign-travel-advice.atom", "parser": "uk-atom"},
        {"id": "us-advisories", "name": "US State Department Advisories", "region": "Global", "category": "Travel Advisory",
         "url": "https://cadataapi.state.gov/api/TravelAdvisories", "parser": "us-advisory"},
        {"id": "us-advisories-rss", "name": "US Travel Advisory RSS", "region": "Global", "category": "Travel Advisory",
         "url": "https://travel.state.gov/_res/rss/TAsTWs.xml", "parser": "us-rss"},
        {"id": "canada-advisories", "name": "Global Affairs Canada Advice", "region": "Global", "category": "Travel Advisory",
         "url": "https://data.international.gc.ca/travel-voyage/index-alpha-eng.json", "parser": "canada-advisory"},
        {"id": "nws-alerts", "name": "NWS Severe Weather Alerts", "region": "United States", "category": "Weather",
         "url": "https://api.weather.gov/alerts/active?severity=Extreme,Severe", "parser": "nws", "user_agent": NWS_USER_AGENT},
        {"id": "usgs-volcanoes", "name": "USGS Elevated Volcanoes", "region": "United States", "category": "Volcano",
         "url": "https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes", "parser": "volcano"},
        {"id": "tsunami-ntwc", "name": "NTWC Pacific Tsunami Atom", "region": "United States", "category": "Tsunami",
         "url": "https://www.tsunami.gov/events/xml/PAAQAtom.xml", "parser": "atom-point"},
        {"id": "tsunami-ptwc", "name": "PTWC Pacific Tsunami Atom", "region": "United States", "category": "Tsunami",
         "url": "https://www.tsunami.gov/events/xml/PHEBAtom.xml", "parser": "atom-point"},
        {"id": "tsunami-ntwc-cap", "name": "NTWC Tsunami CAP", "region": "United States", "category": "Tsunami",
         "url": "https://www.tsunami.gov/events/xml/PAAQCAP.xml", "parser": "cap"},
        {"id": "tsunami-ptwc-cap", "name": "PTWC Tsunami CAP", "region": "United States", "category": "Tsunami",
         "url": "https://www.tsunami.gov/events/xml/PHEBCAP.xml", "parser": "cap"},
        {"id": "nws-tsunami", "name": "NWS Tsunami Warnings", "region": "United States", "category": "Tsunami",
         "url": "https://api.weather.gov/alerts/active?event=Tsunami%20Warning,Tsunami%20Advisory,Tsunami%20Watch",
         "parser": "nws", "user_agent": NWS_USER_AGENT},
        {"id": "nhc", "name": "NHC Atlantic Tropical Cyclones", "region": "United States", "category": "Cyclone",
         "url": "https://www.nhc.noaa.gov/index-at.xml", "parser": "nhc", "anchor": (25.0, -70.0)},
        {"id": "nhc-ep", "name": "NHC Eastern Pacific Cyclones", "region": "United States", "category": "Cyclone",
         "url": "https://www.nhc.noaa.gov/index-ep.xml", "parser": "nhc", "anchor": (15.0, -120.0)},
        {"id": "nhc-cp", "name": "NHC Central Pacific Cyclones", "region": "United States", "category": "Cyclone",
         "url": "https://www.nhc.noaa.gov/index-cp.xml", "parser": "nhc", "anchor": (20.0, -157.0)},
        {"id": "jtwc", "name": "JTWC Tropical Cyclones", "region": "Global", "category": "Cyclone",
         "url": "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss", "parser": "nhc", "anchor": (15.0, 140.0)},
        {"id": "swpc", "name": "NOAA SWPC Space Weather", "region": "United States", "category": "Weather",
         "url": "https://services.swpc.noaa.gov/products/alerts.json", "parser": "swpc"},
        {"id": "faa", "name": "FAA Airport Disruptions", "region": "United States", "category": "Aviation",
         "url": "https://nasstatus.faa.gov/api/airport-events", "parser": "faa"},
        {"id": "cisa-kev", "name": "CISA Known Exploited Vulnerabilities", "region": "Global", "category": "Cyber",
         "url": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json", "parser": "kev"},
        {"id": "cisa-news", "name": "CISA News", "region": "Global", "category": "Cyber",
         "url": "https://www.cisa.gov/news.xml", "parser": "placed-rss", "default_country": "United States", "limit": 8},
        {"id": "ncsc-news", "name": "UK NCSC News", "region": "UK & Europe", "category": "Cyber",
         "url": "https://www.ncsc.gov.uk/api/1/services/v1/news-rss-feed.xml", "parser": "placed-rss",
         "default_country": "United Kingdom", "limit": 8},
        {"id": "cert-eu", "name": "CERT-EU Advisories", "region": "UK & Europe", "category": "Cyber",
         "url": "https://cert.europa.eu/publications/security-advisories-rss", "parser": "placed-rss",
         "default_country": "Belgium", "limit": 8},
        {"id": "eccc", "name": "ECCC Weather Alerts", "region": "Canada", "category": "Weather",
         "url": "https://api.weather.gc.ca/collections/weather-alerts/items?f=json&limit=25", "parser": "geojson"},
        {"id": "alert-ready", "name": "Alert Ready Canada", "region": "Canada", "category": "Weather",
         "url": "https://rss.alertready.ca/", "parser": "atom-point"},
        {"id": "cemaden", "name": "CEMADEN Brazil Alerts", "region": "South America", "category": "Disaster",
         "url": "https://painelalertas.cemaden.gov.br/wsAlertas2", "parser": "cemaden"},
        {"id": "insight", "name": "InSight Crime", "region": "South America", "category": "Security",
         "url": "https://insightcrime.org/feed/", "parser": "insight"},
        {"id": "jma-eq", "name": "JMA Earthquake and Volcano", "region": "APAC", "category": "Seismic",
         "url": "https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml", "parser": "jma"},
        {"id": "jma-typhoon", "name": "JMA Active Typhoons", "region": "APAC", "category": "Cyclone",
         "url": "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json", "parser": "jma-typhoon"},
        {"id": "nsw-rfs", "name": "NSW Rural Fire Service", "region": "Australia", "category": "Disaster",
         "url": "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json", "parser": "geojson"},
        {"id": "vic-emergency", "name": "VicEmergency Incidents", "region": "Australia", "category": "Disaster",
         "url": "https://emergency.vic.gov.au/public/osom-geojson.json", "parser": "geojson", "default_country": "Australia"},
        {"id": "qld-bushfire", "name": "Queensland Bushfire Alerts", "region": "Australia", "category": "Disaster",
         "url": "https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com/content/Feeds/BushfireCurrentIncidents/bushfireAlert.json",
         "parser": "geojson", "default_country": "Australia"},
        {"id": "amu", "name": "Amu TV", "region": "Afghanistan", "category": "Security",
         "url": "https://amu.tv/feed/", "parser": "rss-centroid"},
        {"id": "afintl", "name": "Afghanistan International", "region": "Afghanistan", "category": "Security",
         "url": "https://afintl.com/feed", "parser": "rss-centroid"},
        {"id": "deepstate", "name": "DeepStateMap Snapshot", "region": "Russia & Ukraine", "category": "Conflict",
         "url": "https://deepstatemap.live/api/history/last", "parser": "deepstate"},
        {"id": "uk-floods", "name": "UK Flood Warnings", "region": "UK & Europe", "category": "Weather",
         "url": "https://environment.data.gov.uk/flood-monitoring/id/floods?min-severity=3", "parser": "floods"},
        {"id": "bbc-uk", "name": "BBC News UK", "region": "UK & Europe", "category": "Security",
         "url": "https://feeds.bbci.co.uk/news/uk/rss.xml", "parser": "placed-rss", "default_country": "United Kingdom", "limit": 8},
        {"id": "ncsc-advisories", "name": "UK NCSC Advisories", "region": "UK & Europe", "category": "Cyber",
         "url": "https://www.ncsc.gov.uk/api/1/services/v1/report-rss-feed.xml", "parser": "placed-rss",
         "default_country": "United Kingdom", "limit": 8},
        {"id": "emsc", "name": "EMSC Earthquakes", "region": "UK & Europe", "category": "Seismic",
         "url": "https://www.seismicportal.eu/fdsnws/event/1/query?limit=50&format=json&orderby=time", "parser": "emsc"},
        {"id": "met-norway", "name": "MET Norway Alerts", "region": "UK & Europe", "category": "Weather",
         "url": "https://api.met.no/weatherapi/metalerts/2.0/current.json", "parser": "geojson",
         "default_country": "Norway", "user_agent": "GeoRiskIntel/1.0 (osint-hotspot-map; public weather ingest)"},
        {"id": "bbc-europe", "name": "BBC News Europe", "region": "UK & Europe", "category": "Security",
         "url": "https://feeds.bbci.co.uk/news/world/europe/rss.xml", "parser": "placed-rss", "limit": 8},
        {"id": "balkan", "name": "Balkan Insight", "region": "UK & Europe", "category": "Security",
         "url": "https://balkaninsight.com/feed/", "parser": "placed-rss", "limit": 8},
        {"id": "ecdc", "name": "ECDC Threat Reports", "region": "UK & Europe", "category": "Health",
         "url": "https://www.ecdc.europa.eu/en/taxonomy/term/1505/feed", "parser": "placed-rss",
         "default_country": "Sweden", "limit": 8},
        {"id": "allafrica", "name": "AllAfrica Conflict", "region": "Africa", "category": "Conflict",
         "url": "https://allafrica.com/tools/headlines/rdf/conflict/headlines.rdf", "parser": "placed-rss", "limit": 8},
        {"id": "dabanga", "name": "Radio Dabanga", "region": "Africa", "category": "Conflict",
         "url": "https://www.dabangasudan.org/feed", "parser": "placed-rss", "default_country": "Sudan", "limit": 8},
        {"id": "tamazuj", "name": "Radio Tamazuj", "region": "Africa", "category": "Conflict",
         "url": "https://www.radiotamazuj.org/en/feed", "parser": "placed-rss", "default_country": "South Sudan", "limit": 8},
        {"id": "bbc-africa", "name": "BBC News Africa", "region": "Africa", "category": "Security",
         "url": "https://feeds.bbci.co.uk/news/world/africa/rss.xml", "parser": "placed-rss", "limit": 8},
        {"id": "africa-cdc", "name": "Africa CDC", "region": "Africa", "category": "Health",
         "url": "https://africacdc.org/news-item/feed/", "parser": "placed-rss", "default_country": "Ethiopia", "limit": 8},
        {"id": "sachet", "name": "India NDMA SACHET", "region": "South Asia", "category": "Disaster",
         "url": "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml", "parser": "placed-rss",
         "default_country": "India", "limit": 10},
        {"id": "dawn", "name": "Dawn Pakistan", "region": "South Asia", "category": "Security",
         "url": "https://www.dawn.com/feeds/home", "parser": "placed-rss", "default_country": "Pakistan", "limit": 8},
        {"id": "hindu", "name": "The Hindu National", "region": "South Asia", "category": "Security",
         "url": "https://www.thehindu.com/news/national/feeder/default.rss", "parser": "placed-rss", "default_country": "India", "limit": 8},
        {"id": "dhaka", "name": "Dhaka Tribune", "region": "South Asia", "category": "Security",
         "url": "https://www.dhakatribune.com/feed/", "parser": "placed-rss", "default_country": "Bangladesh", "limit": 8},
        {"id": "kathmandu", "name": "Kathmandu Post", "region": "South Asia", "category": "Security",
         "url": "https://kathmandupost.com/rss", "parser": "placed-rss", "default_country": "Nepal", "limit": 8},
        {"id": "cmf", "name": "Combined Maritime Forces", "region": "Maritime", "category": "Security",
         "url": "https://combinedmaritimeforces.com/feed/", "parser": "placed-rss", "default_country": "Maritime", "limit": 8},
        {"id": "gvp", "name": "Smithsonian Weekly Volcanoes", "region": "Global", "category": "Volcano",
         "url": "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml", "parser": "placed-rss", "limit": 8},
        {"id": "who-don", "name": "WHO Disease Outbreak News", "region": "Global", "category": "Health",
         "url": "https://www.who.int/api/news/diseaseoutbreaknews?$orderby=PublicationDateAndTime%20desc&$top=20", "parser": "who"},
        {"id": "gdelt", "name": "GDELT Security Incidents", "region": "Global", "category": "Security",
         "url": "https://api.gdeltproject.org/api/v2/geo/geo?query=(attack%20OR%20explosion%20OR%20protest%20OR%20unrest%20OR%20kidnapping%20OR%20shooting%20OR%20airstrike%20OR%20security%20OR%20evacuation)&mode=PointData&format=GeoJSON&timespan=24h&maxpoints=150",
         "parser": "gdelt", "timeout": 12},
        {"id": "wire-bbc", "name": "BBC World Security", "region": "Global", "category": "Security",
         "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "parser": "security-rss"},
        {"id": "wire-aj", "name": "Al Jazeera Security", "region": "Global", "category": "Security",
         "url": "https://www.aljazeera.com/xml/rss/all.xml", "parser": "security-rss"},
        {"id": "wire-f24", "name": "France 24 Security", "region": "Global", "category": "Security",
         "url": "https://www.france24.com/en/rss", "parser": "security-rss"},
        {"id": "wire-dw", "name": "DW World Security", "region": "Global", "category": "Security",
         "url": "https://rss.dw.com/xml/rss-en-world", "parser": "security-rss"},
        {"id": "wire-guardian", "name": "Guardian World Security", "region": "Global", "category": "Security",
         "url": "https://www.theguardian.com/world/rss", "parser": "security-rss"},
        {"id": "wire-icg", "name": "International Crisis Group", "region": "Global", "category": "Conflict",
         "url": "https://www.crisisgroup.org/rss.xml", "parser": "security-rss"},
        {"id": "cloudflare", "name": "Cloudflare Radar", "region": "Global", "category": "Cyber",
         "parser": "keyed", "env": ("CLOUDFLARE_RADAR_TOKEN", "CF_API_TOKEN")},
        {"id": "ioda", "name": "IODA Internet Outages", "region": "Global", "category": "Cyber",
         "parser": "keyed", "env": ("IODA_API_KEY",)},
        {"id": "firms", "name": "NASA FIRMS", "region": "Global", "category": "Disaster",
         "parser": "keyed", "env": ("FIRMS_MAP_KEY",), "note": "Used by the satellite heat layer."},
        {"id": "acled", "name": "ACLED", "region": "Global", "category": "Conflict",
         "parser": "keyed", "env": ("ACLED_API_KEY", "ACLED_EMAIL"), "needs_all": True},
    ]
    for slug, country in (
        ("united-kingdom", "United Kingdom"),
        ("germany", "Germany"),
        ("france", "France"),
        ("spain", "Spain"),
        ("italy", "Italy"),
        ("poland", "Poland"),
        ("greece", "Greece"),
    ):
        feeds.append({
            "id": "meteoalarm-" + slug,
            "name": "MeteoAlarm " + country,
            "region": "UK & Europe",
            "category": "Weather",
            "url": "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-" + slug,
            "parser": "placed-rss",
            "default_country": country,
            "limit": 8,
        })
    for slug, label in (
        ("wildfires", "Wildfires"),
        ("severeStorms", "Severe Storms"),
        ("floods", "Floods"),
        ("landslides", "Landslides"),
        ("volcanoes", "Volcanoes"),
        ("seaLakeIce", "Sea and Lake Ice"),
        ("dustHaze", "Dust and Haze"),
    ):
        feeds.append({
            "id": "eonet-" + slug.lower(),
            "name": "NASA EONET " + label,
            "region": "Global",
            "category": "Natural Disaster",
            "url": "https://eonet.gsfc.nasa.gov/api/v3/events/geojson?category=" + slug + "&status=open&limit=40",
            "parser": "geojson",
        })
    feeds.append({
        "id": "wfigs",
        "name": "NIFC Current Wildfires",
        "region": "United States",
        "category": "Natural Disaster",
        "url": "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query?where=1%3D1&outFields=IncidentName,POOState,IncidentTypeCategory&returnGeometry=true&f=geojson&resultRecordCount=25",
        "parser": "geojson",
        "default_country": "United States",
    })
    feeds.append({
        "id": "inciweb",
        "name": "InciWeb Wildfires",
        "region": "United States",
        "category": "Natural Disaster",
        "url": "https://inciweb.wildfire.gov/incidents/rss.xml",
        "parser": "placed-rss",
        "default_country": "United States",
        "limit": 8,
        "timeout": 12,
    })
    return feeds


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(value: object) -> str:
    text = str(value or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _clip(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _pair(lat, lng) -> tuple[float, float] | None:
    lat_n = _float(lat)
    lng_n = _float(lng)
    if lat_n is None or lng_n is None:
        return None
    if not (-90 <= lat_n <= 90 and -180 <= lng_n <= 180):
        return None
    if abs(lat_n) < 0.001 and abs(lng_n) < 0.001:
        return None
    return round(lat_n, 5), round(lng_n, 5)


def _centroid_name(name: object) -> tuple[float, float] | None:
    key = _norm(name)
    if key in CENTROIDS:
        return CENTROIDS[key]
    for known, point in CENTROIDS.items():
        if known in key or key in known:
            return point
    return None


def _theatre(country: object, default: str) -> str:
    return THEATRE_OF.get(_norm(country), default)


def _stamp(value: object) -> str:
    if isinstance(value, (int, float)) and value > 10_000_000_000:
        value = value / 1000
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return _now()
    text = _clip(value, 40)
    return text or _now()


def _jitter(lat: float, lng: float, key: str) -> tuple[float, float]:
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    dlat = ((int(digest[:4], 16) / 65535) - 0.5) * 0.7
    dlng = ((int(digest[4:8], 16) / 65535) - 0.5) * 0.7
    return round(lat + dlat, 5), round(lng + dlng, 5)


def _event(feed: dict, title: str, summary: str, lat: float, lng: float, **extra) -> dict:
    country = _clip(extra.get("country") or feed.get("default_country") or "", 80)
    theatre = extra.get("theatre") or _theatre(country, feed["region"])
    severity = extra.get("severity") or "medium"
    if severity not in {"low", "medium", "high"}:
        severity = "medium"
    source = _clip(extra.get("source") or feed.get("url") or feed["id"], 300)
    try:
        import incident_badges

        origin = _clip(extra.get("coord_origin") or incident_badges.default_coord_origin(feed), 40)
        precision = _clip(extra.get("geo_precision") or incident_badges.precision_for_origin(origin), 80)
    except Exception:
        origin = _clip(extra.get("coord_origin") or "", 40)
        precision = _clip(extra.get("geo_precision") or "", 80)
    return {
        "id": _clip(extra.get("id") or source or title, 180),
        "title": _clip(title, 180) or feed["name"],
        "summary": _clip(summary or title, 500),
        "lat": lat,
        "lng": lng,
        "country": country,
        "region": theatre,
        "theatre": theatre,
        "feed_category": feed["category"],
        "severity": severity,
        "timestamp": _stamp(extra.get("timestamp")),
        "source": source,
        "source_name": feed["name"],
        "geo_precision": precision,
        "coord_origin": origin,
        "location_name": _clip(extra.get("location_name") or extra.get("area") or "", 120),
    }


def _loads(content: bytes):
    return json.loads(content.decode("utf-8", "replace"))


def _ring_point(geometry: dict) -> tuple[float, float] | None:
    if not isinstance(geometry, dict):
        return None
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "Point" and isinstance(coords, list) and len(coords) >= 2:
        return _pair(coords[1], coords[0])
    if kind == "Polygon" and isinstance(coords, list) and coords and isinstance(coords[0], list):
        return _average(coords[0])
    if kind == "MultiPolygon" and isinstance(coords, list) and coords:
        first = coords[0]
        if isinstance(first, list) and first:
            return _average(first[0] if isinstance(first[0], list) else first)
    return None


def _average(ring) -> tuple[float, float] | None:
    lats, lngs = [], []
    for pair in ring[:40]:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            point = _pair(pair[1], pair[0])
            if point:
                lats.append(point[0])
                lngs.append(point[1])
    if not lats:
        return None
    return round(sum(lats) / len(lats), 5), round(sum(lngs) / len(lngs), 5)


def _geojson_events(feed: dict, payload, category_hint: str = "") -> list[dict]:
    features = payload.get("features") if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    if not isinstance(features, list):
        return []
    found = []
    for feature in features[:40]:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or feature
        if not isinstance(props, dict):
            continue
        point = _ring_point(feature.get("geometry") or {})
        if point is None:
            point = _pair(props.get("latitude") or props.get("lat"), props.get("longitude") or props.get("lon") or props.get("lng"))
        if point is None:
            continue
        title = (
            props.get("title") or props.get("headline") or props.get("event") or props.get("name")
            or props.get("IncidentName") or props.get("alert") or feed["name"]
        )
        summary = props.get("description") or props.get("summary") or props.get("areaDesc") or props.get("instruction") or title
        severity = "high" if str(props.get("severity") or "").lower() in {"extreme", "severe", "red"} else "medium"
        country = props.get("country") or feed.get("default_country") or _nearest_country(point[0], point[1])
        found.append(_event(
            feed, str(title), _strip(summary), point[0], point[1],
            country=country,
            severity=severity,
            timestamp=props.get("sent") or props.get("updated") or props.get("effective") or props.get("time"),
            source=props.get("url") or props.get("link") or props.get("@id") or feed.get("url"),
            id=str(props.get("id") or props.get("@id") or title),
        ))
    return found


def _strip(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(text.replace("&nbsp;", " ").replace("&amp;", "&").split())


def parse_gdacs(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload.get("features") if isinstance(payload, dict) else None
    if rows is None and isinstance(payload, dict):
        rows = payload.get("events") or payload.get("items") or []
    if not isinstance(rows, list):
        return []
    rank = {"red": 0, "orange": 1, "green": 2}
    rows.sort(key=lambda item: rank.get(str((item.get("properties") or item).get("alertlevel") or (item.get("properties") or item).get("alert_level") or "").lower(), 3))
    found = []
    for item in rows:
        if not isinstance(item, dict) or len(found) >= 25:
            break
        props = item.get("properties") or item
        point = _ring_point(item.get("geometry") or {})
        if point is None:
            point = _pair(props.get("latitude") or props.get("lat"), props.get("longitude") or props.get("lon"))
        if point is None:
            continue
        level = str(props.get("alertlevel") or props.get("alert_level") or "").lower()
        if level == "green":
            continue
        severity = "high" if level == "red" else "medium"
        title = props.get("name") or props.get("htmldescription") or props.get("eventtype") or "GDACS disaster"
        country = props.get("country") or ""
        found.append(_event(
            feed, _strip(title), _strip(props.get("description") or props.get("severitytext") or title),
            point[0], point[1], country=country, severity=severity,
            timestamp=props.get("fromdate") or props.get("datemodified") or props.get("todate"),
            source=props.get("url") or props.get("link") or feed["url"],
            id=str(props.get("eventid") or props.get("episodeid") or title),
        ))
    return found


def parse_usgs(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    found = []
    for feature in (payload.get("features") or [])[:30]:
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        point = _pair(coords[1], coords[0])
        if point is None:
            continue
        mag = _float(props.get("mag")) or 0
        place = str(props.get("place") or "Earthquake")
        country = place.split(",")[-1].strip() if "," in place else ""
        severity = "high" if mag >= 6 else "medium" if mag >= 5 else "low"
        found.append(_event(
            feed, props.get("title") or f"M{mag:.1f} {place}", place, point[0], point[1],
            country=country, severity=severity, timestamp=props.get("time"),
            source=props.get("url") or feed["url"], id=str(feature.get("id") or props.get("code") or place),
        ))
    return found


def _advisory_level(text: str) -> int:
    lowered = text.lower()
    match = re.search(r"level\s*([1-4])", lowered)
    if match:
        return int(match.group(1))
    if "avoid all travel" in lowered or "do not travel" in lowered:
        return 4
    if "avoid non-essential" in lowered or "avoid all but essential" in lowered or "reconsider travel" in lowered:
        return 3
    if "high degree of caution" in lowered or "increased caution" in lowered:
        return 2
    return 0


def _advisory_events(feed: dict, rows: list[tuple[str, str, str, str]]) -> list[dict]:
    found = []
    for country, text, url, updated in rows:
        level = _advisory_level(text if "level" in text.lower() else text + " " + country)
        if level < 3:
            continue
        point = _centroid_name(country)
        if point is None:
            continue
        lat, lng = _jitter(point[0], point[1], country + feed["id"])
        severity = "high" if level >= 4 else "medium"
        label = "Level 4 — Do not travel" if level >= 4 else "Level 3 — Reconsider travel"
        found.append(_event(
            feed, f"{country}: {label}", text or label, lat, lng,
            country=country, severity=severity, timestamp=updated, source=url or feed.get("url"),
            id=country + ":" + feed["id"],
        ))
        if len(found) >= 40:
            break
    found.sort(key=lambda item: 0 if item["severity"] == "high" else 1)
    return found[:40]


def parse_us_advisory(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and not isinstance(rows, list):
        rows = payload.get("TravelAdvisories") or []
    parsed = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("Title") or item.get("title") or "")
        country = re.sub(r"\s+travel advisory.*$", "", title, flags=re.I).strip()
        category = item.get("Category") or item.get("category") or ""
        if isinstance(category, list):
            category = " ".join(str(part.get("CategoryName") if isinstance(part, dict) else part) for part in category)
        summary = str(item.get("Summary") or item.get("summary") or category or title)
        parsed.append((country, f"{category} {summary}", str(item.get("Link") or item.get("link") or ""), str(item.get("Updated") or item.get("Published") or "")))
    return _advisory_events(feed, parsed)


def _canada_advice(item: dict) -> str:
    state = item.get("advisory-state")
    if isinstance(state, str) and state.strip().isdigit():
        state = int(state.strip())
    if state == 3:
        return "Level 4: Avoid all travel"
    if state == 2:
        return "Level 3: Avoid non-essential travel"
    text = item.get("advisory-text") or item.get("advisory") or ""
    if isinstance(text, str):
        return text
    return ""


def parse_canada_advisory(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = list(rows.values())
    parsed = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        country = str(item.get("country-eng") or item.get("country") or item.get("name") or "")
        advice = _canada_advice(item)
        url = str(item.get("url") or item.get("url-eng") or "")
        if not url and country:
            url = "https://travel.gc.ca/destinations/" + _norm(country).replace(" ", "-")
        elif url.startswith("/"):
            url = "https://travel.gc.ca" + url
        updated = item.get("date-published") or item.get("updated") or ""
        if isinstance(updated, dict):
            updated = updated.get("date") or ""
        parsed.append((country, advice, url, str(updated)))
    return _advisory_events(feed, parsed)


def _entries(content: bytes):
    parsed = feedparser.parse(content)
    return list(getattr(parsed, "entries", []) or [])


UK_WATCH = ("syria", "afghanistan", "yemen", "iran", "iraq", "ukraine", "somalia", "sudan")
UK_PAGE_ALERT = {
    "avoid_all_travel_to_whole_country": 4,
    "avoid_all_but_essential_travel_to_whole_country": 3,
    "avoid_all_travel_to_parts": 3,
    "avoid_all_but_essential_travel_to_parts": 2,
}


def uk_page_level(alerts) -> int:
    if not isinstance(alerts, list) or not alerts:
        return 1
    return max((UK_PAGE_ALERT.get(str(item), 1) for item in alerts), default=1)


def _uk_watched(feed: dict) -> list[dict]:
    """Level 3/4 pages for the countries most often advised against. Failures are skipped."""
    def one(slug: str):
        try:
            payload = _loads(_get(
                "https://www.gov.uk/api/content/foreign-travel-advice/" + slug,
                USER_AGENT,
                6,
            ))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        details = payload.get("details") or {}
        level = uk_page_level(details.get("alert_status"))
        if level < 3:
            return None
        country = str((details.get("country") or {}).get("name") or slug.replace("-", " ").title())
        point = _centroid_name(country)
        if point is None:
            return None
        lat, lng = _jitter(point[0], point[1], "uk-" + slug)
        label = "Level 4 — Advise against all travel" if level >= 4 else "Level 3 — Advise against all but essential travel"
        return _event(
            feed, f"{country}: {label}", _strip(payload.get("description") or label), lat, lng,
            country=country, severity="high" if level >= 4 else "medium",
            timestamp=payload.get("public_updated_at") or "",
            source=str(payload.get("web_url") or "https://www.gov.uk/foreign-travel-advice/" + slug),
            id="uk-page-" + slug,
        )

    found = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for event in pool.map(one, UK_WATCH):
            if event:
                found.append(event)
    return found


def parse_uk_atom(feed: dict, content: bytes) -> list[dict]:
    rows = []
    for entry in _entries(content)[:30]:
        title = _strip(entry.get("title"))
        country = re.sub(r"\s+travel advice.*$", "", title, flags=re.I).strip() or title
        summary = _strip(entry.get("summary") or entry.get("description") or title)
        link = entry.get("link") or ""
        updated = entry.get("updated") or entry.get("published") or ""
        rows.append((country, summary + " " + title, link, updated))
    return _advisory_events(feed, rows)


def parse_us_rss(feed: dict, content: bytes) -> list[dict]:
    rows = []
    for entry in _entries(content)[:40]:
        title = _strip(entry.get("title"))
        country = re.sub(r"\s+travel advisory.*$", "", title, flags=re.I).strip() or title
        summary = _strip(entry.get("summary") or entry.get("description") or title)
        rows.append((country, title + " " + summary, entry.get("link") or "", entry.get("published") or entry.get("updated") or ""))
    return _advisory_events(feed, rows)


def parse_nws(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    found = []
    for feature in (payload.get("features") or [])[:25]:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        point = _ring_point(geometry)
        origin = "feed-point" if geometry.get("type") == "Point" else "place"
        area = str(props.get("areaDesc") or "")
        if point is None:
            state_point = None
            lowered = area.lower()
            for name, coords in US_STATES.items():
                if name in lowered:
                    state_point = coords
                    break
            if state_point is None:
                state_point = CENTROIDS["united states"]
            point = _jitter(state_point[0], state_point[1], str(props.get("id") or area or props.get("headline") or ""))
            origin = "centroid"
        severity = "high" if str(props.get("severity") or "").lower() == "extreme" else "medium"
        found.append(_event(
            feed, props.get("event") or props.get("headline") or "US weather alert",
            props.get("headline") or area, point[0], point[1],
            country="United States", theatre="United States", severity=severity,
            timestamp=props.get("sent") or props.get("effective"),
            source=props.get("id") or feed["url"], id=str(props.get("id") or props.get("headline") or area),
            coord_origin=origin, location_name=area,
        ))
    return found


def _volcano_point(item: dict) -> tuple[float, float] | None:
    point = _pair(item.get("vLat") or item.get("latitude") or item.get("lat"), item.get("vLon") or item.get("vLng") or item.get("longitude") or item.get("lon"))
    if point is None and isinstance(item.get("geometry"), dict):
        point = _ring_point(item["geometry"])
    name = _norm(item.get("volcano_name") or item.get("vName") or item.get("name") or "")
    if point is None:
        for known, coords in VOLCANO_POINTS.items():
            if known in name:
                point = coords
                break
    if point is None:
        point = OBSERVATORY_POINTS.get(str(item.get("obs_abbr") or "").lower())
    if point is None:
        return None
    return _jitter(point[0], point[1], name or str(item.get("vnum") or ""))


def parse_volcano(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload if isinstance(payload, list) else payload.get("volcanoes") or payload.get("data") or payload.get("features") or []
    found = []
    for item in rows:
        if not isinstance(item, dict) or len(found) >= 20:
            break
        point = _volcano_point(item)
        if point is None:
            continue
        name = item.get("volcano_name") or item.get("vName") or item.get("name") or "Elevated volcano"
        notice = item.get("alert_level") or item.get("notice") or item.get("color_code") or "Elevated alert"
        found.append(_event(
            feed, f"{name}: {notice}", f"{name} is at {notice}.", point[0], point[1],
            country="United States", theatre="United States", severity="high",
            timestamp=item.get("sent_utc") or item.get("noticeDate") or item.get("updated"),
            source=item.get("notice_url") or feed["url"], id=str(item.get("vnum") or name),
        ))
    return found


def _coord_in_text(text: str) -> tuple[float, float] | None:
    match = re.search(r"(-?\d{1,2}(?:\.\d+)?)\s*([NS])\s*[, ]+\s*(-?\d{1,3}(?:\.\d+)?)\s*([EW])", text, re.I)
    if match:
        lat = float(match.group(1)) * (-1 if match.group(2).upper() == "S" else 1)
        lng = float(match.group(3)) * (-1 if match.group(4).upper() == "W" else 1)
        return _pair(lat, lng)
    match = re.search(r"(-?\d{1,2}\.\d+)\s*[, ]\s*(-?\d{1,3}\.\d+)", text)
    if match:
        return _pair(match.group(1), match.group(2))
    return None


def parse_atom_point(feed: dict, content: bytes) -> list[dict]:
    found = []
    for entry in _entries(content)[:15]:
        title = _strip(entry.get("title"))
        summary = _strip(entry.get("summary") or entry.get("description") or "")
        blob = title + " " + summary
        lowered = blob.lower()
        if any(phrase in lowered for phrase in ("no tsunami", "no warning", "no current", "there is no")):
            continue
        point = None
        origin = "feed-point"
        for key in ("georss_point", "where"):
            raw = entry.get(key)
            if isinstance(raw, str):
                parts = raw.split()
                if len(parts) >= 2:
                    point = _pair(parts[0], parts[1])
        if point is None:
            point = _coord_in_text(blob)
        if point is None:
            origin = "centroid"
            point = _centroid_name(feed["region"]) or _centroid_name("united states")
            if point:
                point = _jitter(point[0], point[1], title)
        if point is None:
            continue
        if feed.get("category") == "Tsunami" and "tsunami" not in (title or "").lower():
            title = "Tsunami alert: " + (title or feed["name"])
        severity = "high" if feed.get("category") != "Tsunami" or "warning" in lowered else "medium"
        found.append(_event(
            feed, title or feed["name"], summary or title, point[0], point[1],
            country=feed["region"] if feed["region"] != "Global" else "",
            severity=severity, timestamp=entry.get("updated") or entry.get("published"),
            source=entry.get("link") or feed["url"], id=entry.get("id") or title,
            coord_origin=origin,
        ))
    return found


def parse_nhc(feed: dict, content: bytes) -> list[dict]:
    found = []
    for entry in _entries(content)[:12]:
        title = _strip(entry.get("title"))
        summary = _strip(entry.get("summary") or entry.get("description") or "")
        blob = title + " " + summary
        lowered = blob.lower()
        if "no tropical" in lowered or "no active" in lowered:
            continue
        if not any(word in lowered for word in ("hurricane", "tropical", "storm", "cyclone", "depression")):
            continue
        anchor = feed.get("anchor") or (25.0, -70.0)
        text_point = _coord_in_text(blob)
        point = text_point or _jitter(anchor[0], anchor[1], title)
        found.append(_event(
            feed, title or "Tropical cyclone", summary or title, point[0], point[1],
            country="United States" if feed.get("region") == "United States" else _nearest_country(point[0], point[1]),
            theatre=feed.get("region") or "United States", severity="high",
            timestamp=entry.get("published") or entry.get("updated"),
            source=entry.get("link") or feed["url"], id=entry.get("id") or title,
            coord_origin="feed-point" if text_point else "centroid",
        ))
    return found


def parse_faa(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        rows = payload.get("airportEvents") or payload.get("events") or payload.get("data") or []
    found = []
    for item in rows:
        if not isinstance(item, dict) or len(found) >= 20:
            break
        code = str(item.get("airport") or item.get("airportId") or item.get("code") or "").upper()
        point = US_AIRPORTS.get(code)
        if point is None:
            point = _pair(item.get("latitude") or item.get("lat"), item.get("longitude") or item.get("lon"))
        if point is None:
            continue
        reason = item.get("reason") or item.get("event") or item.get("status") or "Airport disruption"
        found.append(_event(
            feed, f"{code}: {reason}" if code else str(reason), str(reason), point[0], point[1],
            country="United States", theatre="United States", severity="medium",
            timestamp=item.get("startTime") or item.get("updated"),
            source=feed["url"], id=code + str(reason),
        ))
    return found


def parse_cisa(feed: dict, content: bytes) -> list[dict]:
    found = []
    base = CENTROIDS["united states"]
    for index, entry in enumerate(_entries(content)[:8]):
        title = _strip(entry.get("title")) or "CISA advisory"
        summary = _strip(entry.get("summary") or entry.get("description") or "")
        lat, lng = _jitter(base[0], base[1], title + str(index))
        found.append(_event(
            feed, title, summary or title, lat, lng,
            country="United States", theatre="Global", severity="medium",
            timestamp=entry.get("published") or entry.get("updated"),
            source=entry.get("link") or feed["url"], id=entry.get("id") or title,
        ))
    return found


def parse_geojson(feed: dict, content: bytes) -> list[dict]:
    return _geojson_events(feed, _loads(content))


def parse_cemaden(feed: dict, content: bytes) -> list[dict]:
    text = content.decode("utf-8", "replace").strip()
    if text.startswith("<"):
        return _xml_points(feed, text)
    payload = json.loads(text)
    rows = payload if isinstance(payload, list) else payload.get("alertas") or payload.get("data") or payload.get("features") or []
    found = []
    for item in rows:
        if not isinstance(item, dict) or len(found) >= 20:
            break
        point = _pair(
            item.get("latitude") or item.get("lat") or item.get("Latitude"),
            item.get("longitude") or item.get("lon") or item.get("lng") or item.get("Longitude"),
        )
        if point is None:
            continue
        title = item.get("municipio") or item.get("nome") or item.get("titulo") or item.get("alerta") or "CEMADEN alert"
        level = str(item.get("nivel") or item.get("risco") or "")
        found.append(_event(
            feed, str(title), level or str(title), point[0], point[1],
            country="Brazil", theatre="South America",
            severity="high" if level.lower() in {"alto", "muito alto", "high", "red"} else "medium",
            timestamp=item.get("data") or item.get("dataInicio"),
            source=feed["url"], id=str(item.get("id") or title),
        ))
    return found


def _xml_points(feed: dict, text: str) -> list[dict]:
    found = []
    for match in re.finditer(r"(-?\d{1,2}\.\d+)\s*[, ]\s*(-?\d{1,3}\.\d+)", text):
        point = _pair(match.group(1), match.group(2))
        if point is None:
            continue
        found.append(_event(feed, "CEMADEN municipal alert", "Municipal alert", point[0], point[1], country="Brazil", theatre="South America", severity="medium", source=feed["url"], id=f"{point[0]},{point[1]}"))
        if len(found) >= 15:
            break
    return found


def parse_insight(feed: dict, content: bytes) -> list[dict]:
    found = []
    for entry in _entries(content)[:12]:
        title = _strip(entry.get("title"))
        summary = _strip(entry.get("summary") or "")
        blob = _norm(title + " " + summary)
        country = ""
        point = None
        for name in SA_COUNTRIES:
            if name in blob:
                country = name.title()
                point = CENTROIDS[name]
                break
        if point is None:
            continue
        lat, lng = _jitter(point[0], point[1], title)
        found.append(_event(
            feed, title, summary or title, lat, lng, country=country, theatre="South America",
            severity="medium", timestamp=entry.get("published") or entry.get("updated"),
            source=entry.get("link") or feed["url"], id=entry.get("id") or title,
        ))
    return found


def parse_jma(feed: dict, content: bytes) -> list[dict]:
    found = []
    base = CENTROIDS["japan"]
    for entry in _entries(content)[:12]:
        title = _strip(entry.get("title")) or "JMA earthquake or volcano notice"
        summary = _strip(entry.get("summary") or "")
        text_point = _coord_in_text(title + " " + summary)
        point = text_point or _jitter(base[0], base[1], title)
        found.append(_event(
            feed, title, summary or title, point[0], point[1],
            country="Japan", theatre="APAC", severity="high" if "津波" in title or "緊急" in title else "medium",
            timestamp=entry.get("updated") or entry.get("published"),
            source=entry.get("link") or feed["url"], id=entry.get("id") or title,
            coord_origin="feed-point" if text_point else "centroid",
        ))
    return found


def parse_jma_typhoon(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    ids = payload if isinstance(payload, list) else payload.get("target") or payload.get("typhoons") or []
    found = []
    if isinstance(payload, dict) and not isinstance(ids, list):
        ids = [payload]
    for item in ids[:4]:
        if isinstance(item, dict):
            point = _walk_point(item)
            name = str(item.get("name") or item.get("id") or "Typhoon")
            if point:
                found.append(_event(feed, f"JMA typhoon {name}", name, point[0], point[1], country="Japan", theatre="APAC", severity="high", source=feed["url"], id=name, coord_origin="feed-point"))
            continue
        typhoon_id = str(item).strip()
        if not typhoon_id:
            continue
        detail_url = f"https://www.jma.go.jp/bosai/typhoon/data/{typhoon_id}/forecast.json"
        try:
            detail = _get(detail_url, USER_AGENT)
            point = _walk_point(_loads(detail))
        except Exception:
            point = None
        origin = "feed-point"
        if point is None:
            point = _jitter(22.0, 135.0, typhoon_id)
            origin = "centroid"
        found.append(_event(
            feed, f"JMA active typhoon {typhoon_id}", f"Japan Meteorological Agency lists {typhoon_id} as an active tropical cyclone.",
            point[0], point[1], country="Japan", theatre="APAC", severity="high",
            source=detail_url if origin == "feed-point" else feed["url"], id=typhoon_id,
            coord_origin=origin,
        ))
    return found


def _walk_point(node, depth: int = 0):
    if depth > 6:
        return None
    if isinstance(node, dict):
        point = _pair(node.get("lat") or node.get("latitude"), node.get("lon") or node.get("lng") or node.get("longitude"))
        if point:
            return point
        for value in node.values():
            found = _walk_point(value, depth + 1)
            if found:
                return found
    if isinstance(node, list):
        for value in node[:30]:
            found = _walk_point(value, depth + 1)
            if found:
                return found
    return None


def parse_wa(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload if isinstance(payload, list) else payload.get("incidents") or payload.get("features") or payload.get("data") or []
    found = []
    for item in rows:
        if not isinstance(item, dict) or len(found) >= 20:
            break
        point = _pair(item.get("latitude") or item.get("lat") or item.get("Latitude"), item.get("longitude") or item.get("lon") or item.get("Longitude"))
        if point is None and isinstance(item.get("geometry"), dict):
            point = _ring_point(item["geometry"])
        if point is None:
            point = _pair(item.get("incidentPoint", {}).get("lat") if isinstance(item.get("incidentPoint"), dict) else None, item.get("incidentPoint", {}).get("lon") if isinstance(item.get("incidentPoint"), dict) else None)
        if point is None:
            continue
        title = item.get("title") or item.get("name") or item.get("type") or item.get("incidentType") or "Western Australia incident"
        found.append(_event(
            feed, str(title), str(item.get("description") or item.get("status") or title), point[0], point[1],
            country="Australia", theatre="Australia", severity="high" if str(item.get("priority") or item.get("severity") or "").lower() in {"high", "emergency"} else "medium",
            timestamp=item.get("startDate") or item.get("updated") or item.get("created"),
            source=item.get("url") or feed["url"], id=str(item.get("id") or title),
        ))
    return found


def parse_rss_centroid(feed: dict, content: bytes) -> list[dict]:
    point = _centroid_name(feed["region"]) or CENTROIDS["afghanistan"]
    found = []
    for entry in _entries(content)[:8]:
        title = _strip(entry.get("title")) or feed["name"]
        summary = _strip(entry.get("summary") or "")
        lat, lng = _jitter(point[0], point[1], title)
        found.append(_event(
            feed, title, summary or title, lat, lng,
            country=feed["region"], theatre=feed["region"], severity="medium",
            timestamp=entry.get("published") or entry.get("updated"),
            source=entry.get("link") or feed["url"], id=entry.get("id") or title,
        ))
    return found


def parse_deepstate(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    points = []
    if isinstance(payload, dict) and isinstance(payload.get("features"), list):
        for feature in payload["features"][:80]:
            point = _ring_point((feature or {}).get("geometry") or {})
            if point:
                points.append(point)
            if len(points) >= 8:
                break
    if not points:
        walked = _walk_point(payload)
        if walked:
            points.append(walked)
    if not points:
        points.append((48.5, 37.8))
    label = "DeepState front-line snapshot"
    if isinstance(payload, dict):
        when = payload.get("datetime") or payload.get("date") or payload.get("updated") or ""
        if when:
            label = f"DeepState snapshot {when}"
    lat, lng = points[0]
    return [_event(
        feed, label, "Latest public DeepStateMap history snapshot. Open the source for the full front line.",
        lat, lng, country="Ukraine", theatre="Russia & Ukraine", severity="high",
        timestamp=payload.get("datetime") if isinstance(payload, dict) else "",
        source="https://deepstatemap.live/", id="deepstate-last",
    )]


SECURITY_RE = re.compile(
    r"\b(attack|attacks|explosion|explode|protest|unrest|kidnapping|kidnap|shooting|"
    r"airstrike|air strike|security|evacuation|blast|bomb|missile|clash|militant|"
    r"terror|hostage|coup|riot|shelling|drone|armed|flood|earthquake|cyclone|wildfire|"
    r"warning|alert|fighting|violence|killed|war|conflict|crisis|ceasefire|troops|bombing)\b",
    re.IGNORECASE,
)
JSON_PARSERS = {"gdacs", "wa", "geojson", "usgs", "nws", "ifrc", "kev", "floods", "emsc", "swpc", "who", "gdelt"}
_SKIP_NEAREST = {"usa", "uae", "turkiye", "burma"}


def _nearest_country(lat: float, lng: float) -> str:
    best_key = ""
    best_distance = 10**9
    for key, point in CENTROIDS.items():
        if key in _SKIP_NEAREST:
            continue
        distance = (lat - point[0]) ** 2 + (lng - point[1]) ** 2
        if distance < best_distance:
            best_distance = distance
            best_key = key
    if not best_key or best_distance > 500:
        return ""
    try:
        import feed_quality

        row = feed_quality.COUNTRIES.get(best_key)
        if row:
            return row[2]
    except Exception:
        pass
    return " ".join(part.capitalize() for part in best_key.split())


def _place_event(feed: dict, title: str, summary: str, country: str = "", **extra) -> dict | None:
    try:
        import feed_quality

        geo = feed_quality.geolocate(title, summary, country or feed.get("default_country") or "", salt=extra.get("id") or title)
    except Exception:
        geo = {"lat": None, "lng": None, "country": country}
    if geo.get("lat") is None:
        return None
    precision = str(geo.get("geo_precision") or "")
    origin = {"Exact": "feed-point", "City / Area": "place", "Country": "centroid"}.get(precision, "")
    return _event(
        feed, title, summary or title, geo["lat"], geo["lng"],
        country=geo.get("country") or country or feed.get("default_country") or "",
        severity=extra.get("severity") or "medium",
        timestamp=extra.get("timestamp"),
        source=extra.get("source") or feed.get("url"),
        id=extra.get("id") or title,
        geo_precision=precision,
        coord_origin=origin,
        location_name=geo.get("area") or "",
    )


def parse_cap(feed: dict, content: bytes) -> list[dict]:
    """One CAP 1.2 alert, including tsunami warning-center messages."""
    text = content.decode("utf-8", "replace")
    lowered = text.lower()
    if any(phrase in lowered for phrase in ("no tsunami", "there is no tsunami", "no warning")):
        return []

    def _tag(name: str) -> str:
        match = re.search(rf"<{name}[^>]*>(.*?)</{name}>", text, re.I | re.S)
        return _strip(match.group(1) if match else "")

    event = _tag("event") or _tag("headline") or feed["name"]
    summary = _tag("description") or _tag("headline") or _tag("areaDesc") or event
    if feed.get("category") == "Tsunami" and "tsunami" not in event.lower():
        event = "Tsunami alert: " + event
    point = None
    origin = "place"
    circle = re.search(r"<circle>\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", text, re.I)
    if circle:
        point = _pair(circle.group(1), circle.group(2))
    if point is None:
        polygon = re.search(r"<polygon>\s*([^<]+)</polygon>", text, re.I)
        if polygon:
            pair = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", polygon.group(1))
            if pair:
                point = _pair(pair.group(1), pair.group(2))
    if point is None:
        point = _coord_in_text(summary)
        origin = "feed-point" if point else "centroid"
    if point is None:
        point = _jitter(*(_centroid_name("united states") or (39.8, -98.6)), event)
        origin = "centroid"
    severity = "high" if "warning" in lowered else "medium"
    return [_event(
        feed, event, summary, point[0], point[1],
        country=_nearest_country(point[0], point[1]) or "United States",
        severity=severity, timestamp=_tag("sent"),
        source=feed.get("url"), id=_tag("identifier") or event,
        coord_origin=origin,
    )]


def parse_gdacs_rss(feed: dict, content: bytes) -> list[dict]:
    found = []
    for entry in _entries(content)[:20]:
        title = _strip(entry.get("title")) or "GDACS alert"
        summary = _strip(entry.get("summary") or entry.get("description") or "")
        blob = title + " " + summary
        lowered = blob.lower()
        if "green alert" in lowered or "green earthquake" in lowered:
            continue
        point = None
        raw = entry.get("georss_point") or entry.get("where")
        if isinstance(raw, str):
            parts = raw.split()
            if len(parts) >= 2:
                point = _pair(parts[0], parts[1])
        if point is None:
            point = _coord_in_text(blob)
        if point is None:
            continue
        severity = "high" if "red" in lowered else "medium"
        found.append(_event(
            feed, title, summary or title, point[0], point[1],
            country=_nearest_country(point[0], point[1]), severity=severity,
            timestamp=entry.get("updated") or entry.get("published"),
            source=entry.get("link") or feed["url"], id=entry.get("id") or title,
        ))
    return found


def parse_ifrc(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload.get("results") if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    found = []
    for item in rows or []:
        if not isinstance(item, dict) or len(found) >= 20:
            break
        countries = item.get("countries") or []
        country = ""
        if isinstance(countries, list) and countries and isinstance(countries[0], dict):
            country = str(countries[0].get("name") or countries[0].get("society_name") or "")
        country = country or str(item.get("country") or "")
        dtype = item.get("dtype") or {}
        kind = dtype.get("name") if isinstance(dtype, dict) else str(dtype or "")
        title = _strip(item.get("name") or kind or "IFRC event")
        summary = _strip(item.get("summary") or item.get("description") or kind or title)
        point = _pair(item.get("lat") or item.get("latitude"), item.get("lon") or item.get("lng") or item.get("longitude"))
        if point is None:
            placed = _place_event(feed, title, summary, country, timestamp=item.get("disaster_start_date"), source=item.get("url") or feed["url"], id=str(item.get("id") or title))
            if placed:
                found.append(placed)
            continue
        found.append(_event(
            feed, title, summary, point[0], point[1], country=country,
            severity="high" if str(kind).lower() in {"earthquake", "cyclone", "flood"} else "medium",
            timestamp=item.get("disaster_start_date") or item.get("created_at"),
            source=item.get("url") or feed["url"], id=str(item.get("id") or title),
        ))
    return found


def parse_kev(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload.get("vulnerabilities") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    rows = sorted(rows, key=lambda item: str(item.get("dateAdded") or ""), reverse=True)[:15]
    base = CENTROIDS["united states"]
    found = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            continue
        cve = str(item.get("cveID") or "CVE")
        name = _strip(item.get("vulnerabilityName") or item.get("product") or "Exploited vulnerability")
        title = f"{cve}: {name}"
        summary = _strip(item.get("shortDescription") or item.get("notes") or title)
        lat, lng = _jitter(base[0], base[1], cve + str(index))
        ransomware = str(item.get("knownRansomwareCampaignUse") or "").lower() == "known"
        found.append(_event(
            feed, title, summary, lat, lng, country="United States", theatre="Global",
            severity="high" if ransomware else "medium",
            timestamp=item.get("dateAdded"), source="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
            id=cve,
        ))
    return found


def parse_uk_floods(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    items = payload.get("items") if isinstance(payload, dict) else []
    found = []
    for item in items or []:
        if not isinstance(item, dict) or len(found) >= 20:
            break
        area = item.get("floodArea") if isinstance(item.get("floodArea"), dict) else {}
        point = _pair(area.get("lat"), area.get("long") or area.get("lon"))
        county = str(area.get("county") or item.get("eaAreaName") or "")
        summary = _strip(item.get("message") or item.get("description") or "")
        title = _strip(item.get("severity") or "Flood warning")
        if county:
            title = f"{title}: {county}"
        if point is None:
            placed = _place_event(
                feed, title, f"{county} {summary}", "United Kingdom",
                severity="high" if str(item.get("severityLevel")) in {"1", "2"} else "medium",
                timestamp=item.get("timeMessageChanged") or item.get("timeRaised"),
                source=item.get("@id") or feed["url"], id=str(item.get("floodAreaID") or title),
            )
            if placed:
                found.append(placed)
            continue
        found.append(_event(
            feed, title, summary or title, point[0], point[1], country="United Kingdom",
            severity="high" if str(item.get("severityLevel")) in {"1", "2"} else "medium",
            timestamp=item.get("timeMessageChanged") or item.get("timeRaised"),
            source=item.get("@id") or feed["url"], id=str(item.get("floodAreaID") or title),
        ))
    return found


def parse_emsc(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    found = []
    for feature in (payload.get("features") or [])[:25]:
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        point = _pair(coords[1], coords[0])
        if point is None:
            continue
        mag = _float(props.get("mag")) or 0
        place = str(props.get("flynn_region") or props.get("place") or "Earthquake")
        country = place.split(",")[-1].strip() if "," in place else _nearest_country(point[0], point[1])
        title = props.get("title") or f"M{mag:.1f} {place}"
        severity = "high" if mag >= 6 else "medium" if mag >= 5 else "low"
        found.append(_event(
            feed, str(title), place, point[0], point[1], country=country, severity=severity,
            timestamp=props.get("time"), source=props.get("url") or feed["url"],
            id=str(feature.get("id") or props.get("unid") or title),
        ))
    return found


def parse_swpc(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = payload if isinstance(payload, list) else payload.get("alerts") or []
    found = []
    for index, item in enumerate(rows[:8]):
        if not isinstance(item, dict):
            continue
        message = _strip(item.get("message") or item.get("product_id") or "Space weather alert")
        title = _strip(item.get("product_id") or "Space weather alert")
        lat, lng = _jitter(40.015, -105.2705, title + str(index))
        severity = "high" if "warning" in message.lower() else "medium"
        found.append(_event(
            feed, title, message, lat, lng, country="United States", theatre="United States",
            severity=severity, timestamp=item.get("issue_datetime"),
            source="https://www.swpc.noaa.gov/", id=str(item.get("issue_datetime") or title) + str(index),
        ))
    return found


def parse_who(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("value") or (payload.get("d") or {}).get("results") or payload.get("results") or []
    elif isinstance(payload, list):
        rows = payload
    found = []
    for item in rows:
        if not isinstance(item, dict) or len(found) >= 12:
            break
        title = _strip(item.get("Title") or item.get("title") or item.get("Name") or "Disease outbreak")
        summary = _strip(item.get("Overview") or item.get("Summary") or item.get("summary") or title)
        placed = _place_event(
            feed, title, summary, "",
            timestamp=item.get("PublicationDateAndTime") or item.get("DatePublished") or "",
            source=item.get("Url") or item.get("ItemDefaultUrl") or feed["url"],
            id=str(item.get("Id") or item.get("id") or title),
        )
        if placed:
            found.append(placed)
    return found


def parse_gdelt(feed: dict, content: bytes) -> list[dict]:
    payload = _loads(content)
    features = payload.get("features") if isinstance(payload, dict) else []
    if not isinstance(features, list):
        raise FeedError("GDELT returned no points")
    found = []
    per_country: dict[str, int] = {}
    for feature in features:
        if len(found) >= 80 or not isinstance(feature, dict):
            break
        point = _ring_point(feature.get("geometry") or {})
        if point is None:
            continue
        props = feature.get("properties") or {}
        html = str(props.get("html") or "")
        snippets = re.findall(r">([^<]{12,180})<", html)
        headline = _strip(snippets[-1] if snippets else "")
        place = _strip(props.get("name") or "")
        title = headline or place or "Security incident"
        summary = _strip(place + " " + headline) or title
        country = _nearest_country(point[0], point[1])
        if per_country.get(country, 0) >= 3:
            continue
        per_country[country] = per_country.get(country, 0) + 1
        lowered = (title + " " + summary).lower()
        severity = "high" if any(word in lowered for word in ("attack", "explosion", "airstrike", "shooting", "kidnap")) else "medium"
        found.append(_event(
            feed, title, summary, point[0], point[1], country=country, severity=severity,
            timestamp=_now(), source=feed["url"], id=f"{point[0]},{point[1]}:{title[:80]}",
        ))
    return found


def parse_placed_rss(feed: dict, content: bytes) -> list[dict]:
    limit = int(feed.get("limit") or 8)
    found = []
    for entry in _entries(content)[: limit + 4]:
        if len(found) >= limit:
            break
        title = _strip(entry.get("title")) or feed["name"]
        summary = _strip(entry.get("summary") or entry.get("description") or "")
        placed = _place_event(
            feed, title, summary, feed.get("default_country") or "",
            timestamp=entry.get("published") or entry.get("updated"),
            source=entry.get("link") or feed.get("url"), id=entry.get("id") or entry.get("link") or title,
        )
        if placed:
            found.append(placed)
    return found


def parse_security_rss(feed: dict, content: bytes) -> list[dict]:
    limit = int(feed.get("limit") or 8)
    found = []
    for entry in _entries(content)[:24]:
        if len(found) >= limit:
            break
        title = _strip(entry.get("title")) or ""
        summary = _strip(entry.get("summary") or entry.get("description") or "")
        if not SECURITY_RE.search(title + " " + summary):
            continue
        placed = _place_event(
            feed, title, summary, feed.get("default_country") or "",
            severity="high" if SECURITY_RE.search(title) else "medium",
            timestamp=entry.get("published") or entry.get("updated"),
            source=entry.get("link") or feed.get("url"), id=entry.get("id") or entry.get("link") or title,
        )
        if placed:
            found.append(placed)
    return found


def prepare_events(events: list[dict]) -> list[dict]:
    """Spam-filter, translate into English, and keep a coordinate on every alert."""
    try:
        import feed_quality

        cleaned = []
        for event in events:
            item = feed_quality.enrich_event(event, live_translation=True, ingest=True)
            if item is not None and item.get("lat") is not None:
                cleaned.append(item)
        return feed_quality.dedupe(cleaned)
    except Exception:
        return [event for event in events if isinstance(event, dict) and event.get("lat") is not None]


PARSERS = {
    "gdacs": parse_gdacs,
    "gdacs-rss": parse_gdacs_rss,
    "cap": parse_cap,
    "ifrc": parse_ifrc,
    "kev": parse_kev,
    "floods": parse_uk_floods,
    "emsc": parse_emsc,
    "swpc": parse_swpc,
    "who": parse_who,
    "gdelt": parse_gdelt,
    "placed-rss": parse_placed_rss,
    "security-rss": parse_security_rss,
    "usgs": parse_usgs,
    "uk-atom": parse_uk_atom,
    "us-advisory": parse_us_advisory,
    "us-rss": parse_us_rss,
    "canada-advisory": parse_canada_advisory,
    "nws": parse_nws,
    "volcano": parse_volcano,
    "atom-point": parse_atom_point,
    "nhc": parse_nhc,
    "faa": parse_faa,
    "cisa": parse_cisa,
    "geojson": parse_geojson,
    "cemaden": parse_cemaden,
    "insight": parse_insight,
    "jma": parse_jma,
    "jma-typhoon": parse_jma_typhoon,
    "wa": parse_wa,
    "rss-centroid": parse_rss_centroid,
    "deepstate": parse_deepstate,
}


def _env_value(names: tuple[str, ...], need_all: bool) -> str:
    values = [(os.environ.get(name) or "").strip() for name in names]
    if need_all:
        return " ".join(values) if all(values) else ""
    return next((value for value in values if value and "your_" not in value and "placeholder" not in value), "")


def _keyed_status(feed: dict) -> dict:
    present = bool(_env_value(feed.get("env") or (), bool(feed.get("needs_all"))))
    if not present:
        return {"ok": False, "status": "standby", "error": "Optional API key not set", "events": []}
    return {"ok": True, "status": "active", "error": feed.get("note") or "Key is set. This source does not add extra pins.", "events": []}


class FeedError(Exception):
    pass


def _get(url: str, user_agent: str, read_timeout: int = READ_TIMEOUT) -> bytes:
    response = requests.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json, application/geo+json, application/xml, application/atom+xml, */*"},
        timeout=(CONNECT_TIMEOUT, read_timeout),
        stream=True,
    )
    try:
        if response.status_code == 429:
            raise requests.HTTPError("rate limited")
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                break
        return b"".join(chunks)
    finally:
        response.close()


def _fetch_feed(feed: dict, previous: list[dict]) -> dict:
    if feed.get("parser") == "keyed":
        return _keyed_status(feed)
    try:
        content = _get(feed["url"], feed.get("user_agent") or USER_AGENT, int(feed.get("timeout") or READ_TIMEOUT))
        if feed["parser"] in JSON_PARSERS and content.lstrip().startswith(b"<"):
            raise FeedError("Source returned a web page instead of feed data")
        events = prepare_events(PARSERS[feed["parser"]](feed, content))
        if feed.get("id") == "uk-fcdo-atom":
            seen = {str(item.get("id")) for item in events}
            for extra in prepare_events(_uk_watched(feed)):
                if str(extra.get("id")) not in seen:
                    events.append(extra)
        return {"ok": True, "status": "active", "error": "", "events": events}
    except FeedError as exc:
        return {"ok": False, "status": "offline", "error": str(exc)[:160], "events": previous}
    except requests.Timeout:
        return {"ok": False, "status": "offline", "error": "timeout", "events": previous}
    except requests.HTTPError as exc:
        text = str(exc).lower()
        if "429" in text or "rate" in text:
            message = "rate limited"
        elif "403" in text or "401" in text:
            message = "access denied"
        else:
            message = "HTTP error"
        return {"ok": False, "status": "offline", "error": message, "events": previous}
    except Exception as exc:
        return {"ok": False, "status": "offline", "error": exc.__class__.__name__, "events": previous}


def _read_disk() -> dict:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_disk(feeds: dict) -> None:
    payload = {"fetched_at": _now(), "feeds": feeds}
    try:
        temporary = CACHE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(CACHE_PATH)
    except OSError as exc:
        print(f"Global feed cache was not written ({exc.__class__.__name__}).")


def _load_memory() -> None:
    if _memory.get("feeds"):
        return
    disk = _read_disk()
    feeds = disk.get("feeds") if isinstance(disk.get("feeds"), dict) else {}
    _memory["feeds"] = feeds
    _memory["at"] = time.time() if feeds else 0.0


def refresh(force: bool = False) -> None:
    """Fetch every global feed. One slow or failed feed cannot cancel the others."""
    global _refreshing
    with _lock:
        _load_memory()
        if not force and _memory.get("feeds") and time.time() - float(_memory.get("at") or 0) < REFRESH_SECONDS:
            return
        if _refreshing:
            return
        _refreshing = True
        previous = dict(_memory.get("feeds") or {})
    feeds = _feeds()
    updated = dict(previous)
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {}
            for feed in feeds:
                prior = ((previous.get(feed["id"]) or {}).get("events") or []) if isinstance(previous.get(feed["id"]), dict) else []
                futures[pool.submit(_fetch_feed, feed, prior)] = feed
            for future in as_completed(futures):
                feed = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"ok": False, "status": "offline", "error": exc.__class__.__name__, "events": []}
                updated[feed["id"]] = {
                    "name": feed["name"],
                    "region": feed["region"],
                    "category": feed["category"],
                    "status": result.get("status") or "offline",
                    "error": _clip(result.get("error") or "", 160),
                    "checked": _now(),
                    "events": result.get("events") or [],
                }
                state = "ok" if result.get("ok") else result.get("status")
                print(f"Global feed {feed['name']}: {state}, {len(updated[feed['id']]['events'])} events.")
        known = {feed["id"] for feed in feeds}
        updated = {key: value for key, value in updated.items() if key in known}
    finally:
        with _lock:
            _memory["feeds"] = updated
            _memory["at"] = time.time()
            _refreshing = False
        _write_disk(updated)


def _loop() -> None:
    while True:
        try:
            refresh(force=True)
        except Exception as exc:
            print(f"Global feed refresh failed: {exc.__class__.__name__}.")
        time.sleep(REFRESH_SECONDS)


def start() -> None:
    """Load the cache and refresh in the background. Safe to call more than once."""
    global _started
    with _lock:
        _load_memory()
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="global-feeds", daemon=True).start()


def kick() -> None:
    """Start a refresh if the cache is stale, without waiting for it."""
    with _lock:
        _load_memory()
        stale = not _memory.get("feeds") or time.time() - float(_memory.get("at") or 0) >= REFRESH_SECONDS
        busy = _refreshing
    if stale and not busy:
        threading.Thread(target=refresh, name="global-feeds-kick", daemon=True).start()


def features() -> list[dict]:
    with _lock:
        _load_memory()
        feeds = dict(_memory.get("feeds") or {})
    catalog = {feed["id"]: feed for feed in _feeds()}
    seen = set()
    rows = []
    for feed_id, row in feeds.items():
        if feed_id not in catalog or not isinstance(row, dict):
            continue
        for event in row.get("events") or []:
            if not isinstance(event, dict):
                continue
            point = _pair(event.get("lat"), event.get("lng"))
            if point is None:
                continue
            key = str(event.get("id") or event.get("source") or "")
            title_key = _norm(event.get("title") or "")[:90]
            if (key and key in seen) or (title_key and title_key in seen):
                continue
            if key:
                seen.add(key)
            if title_key:
                seen.add(title_key)
            origin = event.get("coord_origin") or ""
            if not origin:
                try:
                    import incident_badges

                    origin = incident_badges.default_coord_origin(catalog[feed_id])
                except Exception:
                    origin = ""
            rows.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [point[1], point[0]]},
                "properties": {
                    "title": _english(event.get("title") or row.get("name") or "OSINT event"),
                    "summary": _english(event.get("summary") or ""),
                    "severity": event.get("severity") or "medium",
                    "timestamp": event.get("timestamp") or "",
                    "source": event.get("source") or "",
                    "source_name": event.get("source_name") or row.get("name") or "",
                    "source_label": event.get("source_name") or row.get("name") or "",
                    "source_tier": "Official / Primary",
                    "verification_tier": "Official / Primary",
                    "region": event.get("region") or row.get("region") or "Global",
                    "theatre": event.get("theatre") or event.get("region") or row.get("region") or "Global",
                    "country": event.get("country") or "",
                    "area": event.get("area") or "",
                    "location_name": event.get("location_name") or event.get("area") or event.get("country") or "",
                    "lat": event.get("lat"),
                    "lng": event.get("lng"),
                    "original_text": event.get("original_text") or "",
                    "geo_precision": event.get("geo_precision") or "",
                    "coord_origin": origin,
                    "category": event.get("feed_category") or row.get("category") or "OSINT",
                    "feed_category": event.get("feed_category") or row.get("category") or "",
                    "feed_id": feed_id,
                },
            })
    grouped: dict[str, list] = {}
    for feature in rows:
        grouped.setdefault(str(feature["properties"].get("feed_id") or ""), []).append(feature)
    balanced = []
    rank = {"high": 0, "medium": 1, "low": 2}
    for items in grouped.values():
        items.sort(key=lambda feature: rank.get(feature["properties"]["severity"], 1))
        feed_id = str(items[0]["properties"].get("feed_id") or "")
        limit = 36 if feed_id == "gdelt" else 12 if feed_id in {"eonet", "gdacs-rss"} else 8
        balanced.extend(items[:limit])
    balanced.sort(key=lambda feature: rank.get(feature["properties"]["severity"], 1))
    return balanced[:420]


def _english(value: object) -> str:
    text = " ".join(str(value or "").split())
    try:
        import feed_quality

        return feed_quality.translate_text(text, live=True, ingest=True)
    except Exception:
        return text


def status_rows() -> list[dict]:
    catalog = {feed["id"]: feed for feed in _feeds()}
    with _lock:
        _load_memory()
        recorded = dict(_memory.get("feeds") or {})
    rows = []
    for feed_id, feed in catalog.items():
        row = recorded.get(feed_id) if isinstance(recorded.get(feed_id), dict) else {}
        status = str(row.get("status") or "")
        if status not in {"active", "offline", "standby"}:
            status = "standby" if feed.get("parser") == "keyed" and not _env_value(feed.get("env") or (), bool(feed.get("needs_all"))) else "unchecked"
        rows.append({
            "source_name": feed["name"],
            "type": "global",
            "region": feed["region"],
            "category": feed["category"],
            "status": status,
            "last_checked": row.get("checked") or "",
            "last_error": row.get("error") or "",
        })
    return rows
