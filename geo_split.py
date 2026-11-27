"""Area of Operations catalog, geopolitical summaries, and AO pricing.

Summaries and country templates are composed from this structured baseline
plus the incident counts the caller passes in. This module never calls xAI.
Personal fields are not accepted and are not stored.
"""

from __future__ import annotations

import re

AO_KEY = "georisk.ao.v1"
XAI_CREDITS = 0

REGIONS = (
    {"id": "middle-east", "label": "Middle East", "lat": 28.0, "lng": 45.0, "zoom": 4},
    {"id": "europe", "label": "Europe", "lat": 50.0, "lng": 15.0, "zoom": 4},
    {"id": "africa", "label": "Africa", "lat": 2.0, "lng": 20.0, "zoom": 3},
    {"id": "asia-pacific", "label": "Asia-Pacific", "lat": 12.0, "lng": 115.0, "zoom": 3},
    {"id": "americas", "label": "Americas", "lat": 15.0, "lng": -90.0, "zoom": 3},
    {"id": "global", "label": "Global", "lat": 20.0, "lng": 10.0, "zoom": 2},
    {"id": "maritime", "label": "Maritime", "lat": 15.0, "lng": 55.0, "zoom": 4},
)

REGION_TEXT = {
    "Middle East": {
        "stability": "The Middle East Area of Operations mixes tightly governed Gulf capitals with active conflicts in Syria, Yemen, and parts of Iraq and the Levant. Political control can change by governorate and by week.",
        "actors": "State forces, Iran-aligned armed groups, Israeli and Lebanese armed actors, Ansarallah in Yemen, and residual Islamic State cells are the main security drivers.",
        "borders": "Conflict-zone airspace restrictions cover Syria, Iraq, Iran, and Yemen. Maritime risk sits on the Red Sea, Bab el-Mandeb, and the Strait of Hormuz. Land borders open and close with little notice.",
    },
    "Europe": {
        "stability": "Most of Europe is politically stable and commercially open. The war in Ukraine, plus terrorism, sabotage, and protest risk in parts of the continent, is the exception.",
        "actors": "National governments, NATO and EU institutions, and the parties to the war in Ukraine are the main drivers. City-level protest and organised crime matter on specific assignments.",
        "borders": "Schengen and UK border rules are different. Ukrainian and western Russian airspace is restricted. Elsewhere, commercial aviation is generally open and still needs a current NOTAM check.",
    },
    "Africa": {
        "stability": "Africa's Area of Operations runs from stable commercial capitals to active conflicts in the Sahel, Sudan, Somalia, and eastern Congo. Coup, insurgency, and kidnap risk changes sharply by country.",
        "actors": "National armies, junta authorities in parts of the Sahel, jihadist groups, and communal armed groups are the main drivers. Crime and political protest dominate many capitals.",
        "borders": "Libya, Sudan, Somalia, and Mali have published conflict-zone airspace restrictions. Overland borders close around offensives and elections. Medevac often routes through Nairobi, Cairo, or a European hub.",
    },
    "Asia-Pacific": {
        "stability": "Asia-Pacific is mostly commercially open, with sharp exceptions in Afghanistan, Myanmar, the Korean peninsula, and the South China Sea.",
        "actors": "National governments, Taliban authorities in Afghanistan, and the militaries around Taiwan and Korea are the strategic drivers. Insurgent and criminal groups matter in parts of South and Southeast Asia.",
        "borders": "Afghan airspace needs a current risk acceptance. Taiwan Strait and South China Sea transits need an air and maritime check. Most regional hubs are otherwise open.",
    },
    "Americas": {
        "stability": "The United States and Canada are stable. Parts of Latin America and the Caribbean carry high crime, protest, and, in Haiti, severe insecurity.",
        "actors": "National governments, criminal groups, and protest movements are the main drivers. Hurricane and earthquake seasons add a natural-hazard driver.",
        "borders": "US and Canadian borders are regulated. Some Latin American land borders are slow. Haiti has published civil-aviation restrictions. Hurricane season disrupts Caribbean flights.",
    },
    "Global": {
        "stability": "Global is the worldwide view for travel advice, disasters, and cyber notes that are not tied to one theatre. Pick a country to focus the Area of Operations.",
        "actors": "Government travel-advice services, disaster agencies, and international organisations drive this view.",
        "borders": "No single border regime applies. Use the country template before a deployment.",
    },
    "Maritime": {
        "stability": "Maritime covers sea lanes and chokepoints rather than a country. Risk is highest around Hormuz, Bab el-Mandeb, and the southern Red Sea.",
        "actors": "State navies, UKMTO, and non-state attackers on commercial shipping are the drivers.",
        "borders": "There is no land border. Air routes over these seas often sit under the same conflict-zone bulletins as the adjacent countries.",
    },
}

# name, region, lat, lng
COUNTRY_ROWS = (
    ("Syria", "Middle East", 33.5138, 36.2765),
    ("Iraq", "Middle East", 33.3152, 44.3661),
    ("Iran", "Middle East", 35.6892, 51.3890),
    ("Lebanon", "Middle East", 33.8938, 35.5018),
    ("Israel", "Middle East", 31.7683, 35.2137),
    ("Palestine", "Middle East", 31.9522, 35.2332),
    ("Jordan", "Middle East", 31.9454, 35.9284),
    ("Yemen", "Middle East", 15.3694, 44.1910),
    ("Saudi Arabia", "Middle East", 24.7136, 46.6753),
    ("Bahrain", "Middle East", 26.0667, 50.5577),
    ("Kuwait", "Middle East", 29.3759, 47.9774),
    ("Qatar", "Middle East", 25.2854, 51.5310),
    ("UAE", "Middle East", 24.4539, 54.3773),
    ("Oman", "Middle East", 23.5880, 58.3829),
    ("Turkey", "Middle East", 39.9334, 32.8597),
    ("Egypt", "Middle East", 30.0444, 31.2357),
    ("United Kingdom", "Europe", 51.5074, -0.1278),
    ("Ireland", "Europe", 53.3498, -6.2603),
    ("France", "Europe", 48.8566, 2.3522),
    ("Germany", "Europe", 52.5200, 13.4050),
    ("Italy", "Europe", 41.9028, 12.4964),
    ("Spain", "Europe", 40.4168, -3.7038),
    ("Poland", "Europe", 52.2297, 21.0122),
    ("Greece", "Europe", 37.9838, 23.7275),
    ("Netherlands", "Europe", 52.3676, 4.9041),
    ("Belgium", "Europe", 50.8503, 4.3517),
    ("Ukraine", "Europe", 50.4501, 30.5234),
    ("Russia", "Europe", 55.7558, 37.6173),
    ("Sudan", "Africa", 15.5007, 32.5599),
    ("South Sudan", "Africa", 4.8594, 31.5713),
    ("Chad", "Africa", 12.1348, 15.0557),
    ("Libya", "Africa", 32.8872, 13.1913),
    ("Tunisia", "Africa", 36.8065, 10.1815),
    ("Algeria", "Africa", 36.7538, 3.0588),
    ("Morocco", "Africa", 33.9716, -6.8498),
    ("Nigeria", "Africa", 9.0765, 7.3986),
    ("Niger", "Africa", 13.5116, 2.1254),
    ("Mali", "Africa", 12.6392, -8.0029),
    ("Somalia", "Africa", 2.0469, 45.3182),
    ("Ethiopia", "Africa", 9.0320, 38.7469),
    ("Kenya", "Africa", -1.2921, 36.8219),
    ("South Africa", "Africa", -25.7479, 28.2293),
    ("Democratic Republic of the Congo", "Africa", -4.4419, 15.2663),
    ("Afghanistan", "Asia-Pacific", 34.5553, 69.2075),
    ("Pakistan", "Asia-Pacific", 33.6844, 73.0479),
    ("India", "Asia-Pacific", 28.6139, 77.2090),
    ("Bangladesh", "Asia-Pacific", 23.8103, 90.4125),
    ("China", "Asia-Pacific", 39.9042, 116.4074),
    ("Japan", "Asia-Pacific", 35.6762, 139.6503),
    ("South Korea", "Asia-Pacific", 37.5665, 126.9780),
    ("Taiwan", "Asia-Pacific", 25.0330, 121.5654),
    ("Philippines", "Asia-Pacific", 14.5995, 120.9842),
    ("Indonesia", "Asia-Pacific", -6.2088, 106.8456),
    ("Australia", "Asia-Pacific", -33.8688, 151.2093),
    ("Myanmar", "Asia-Pacific", 16.8409, 96.1735),
    ("United States", "Americas", 38.9072, -77.0369),
    ("Canada", "Americas", 45.4215, -75.6972),
    ("Mexico", "Americas", 19.4326, -99.1332),
    ("Brazil", "Americas", -15.7939, -47.8828),
    ("Colombia", "Americas", 4.7110, -74.0721),
    ("Venezuela", "Americas", 10.4806, -66.9036),
    ("Haiti", "Americas", 18.5944, -72.3074),
    ("Maritime", "Maritime", 20.0, 38.5),
)

ALIASES = {
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
    "uae": "UAE",
    "united arab emirates": "UAE",
    "turkiye": "Turkey",
    "czech republic": "Czechia",
    "dr congo": "Democratic Republic of the Congo",
    "drc": "Democratic Republic of the Congo",
    "burma": "Myanmar",
}

GULF = ("Bahrain", "Kuwait", "Qatar", "UAE", "Oman")
LEGACY_MEMBERS = {
    "Middle East": ["Syria", "Iraq", "Saudi Arabia", "Iran", "Lebanon", "Israel", "Palestine", "Jordan", "Yemen", "Sudan", "Chad", "Bahrain", "Kuwait", "Qatar", "UAE", "Oman", "Turkey", "Egypt", "Gulf States", "Maritime"],
    "United States": ["United States"],
    "Canada": ["Canada"],
    "South America": ["Brazil", "Colombia", "Venezuela", "Peru", "Chile", "Argentina", "Ecuador", "Bolivia", "Guyana", "Suriname", "Paraguay", "Uruguay"],
    "APAC": ["Japan", "Taiwan", "China", "Philippines", "South Korea", "North Korea", "Indonesia", "Vietnam", "Thailand", "Malaysia", "Singapore"],
    "Australia": ["Australia"],
    "Afghanistan": ["Afghanistan"],
    "Russia & Ukraine": ["Ukraine", "Russia", "Belarus"],
    "UK & Europe": ["United Kingdom", "Ireland", "France", "Germany", "Italy", "Spain", "Poland", "Greece", "Netherlands", "Belgium", "Ukraine", "Turkey"],
    "Africa": ["Nigeria", "Kenya", "Ethiopia", "Somalia", "South Africa", "Egypt", "Libya", "Sudan", "Chad", "Mali", "Niger"],
    "South Asia": ["India", "Pakistan", "Bangladesh", "Nepal", "Sri Lanka", "Afghanistan"],
}
LEGACY_TO_MACRO = {
    "Middle East": "Middle East",
    "United States": "Americas",
    "Canada": "Americas",
    "South America": "Americas",
    "APAC": "Asia-Pacific",
    "Australia": "Asia-Pacific",
    "Afghanistan": "Asia-Pacific",
    "Russia & Ukraine": "Europe",
    "UK & Europe": "Europe",
    "Europe": "Europe",
    "Africa": "Africa",
    "South Asia": "Asia-Pacific",
    "Global": "Global",
    "Maritime": "Maritime",
    "Gulf States": "Middle East",
    "Asia-Pacific": "Asia-Pacific",
    "Americas": "Americas",
}
GLOBAL_FEEDS = {"Travel Advisory", "Disaster", "Seismic", "Cyber", "Natural Disaster"}
RISK_CATEGORIES = (
    "Conflict/War",
    "Terrorism",
    "Crime",
    "Civil Unrest",
    "Kidnap/Detention",
    "Road/Airspace",
    "Digital/InfoSec",
    "Health/Environmental",
)
HRA_SECTIONS = (
    "Header banner",
    "1. Purpose and outline plan",
    "2. Risk assessment",
    "3. Itinerary and logistics",
    "4. Medical and evacuation",
    "5. Contacts",
    "6. Consultation and sign-off",
    "7. Travelling staff details",
    "8. References, support and data protection",
)

# Short public baselines. No personal names, no private numbers.
PROFILES = {
    "Syria": {
        "stability": "Syria remains in active conflict. Control, checkpoints, and unexploded-ordnance risk vary by governorate and can change with little warning.",
        "actors": "State forces, local armed groups, and foreign-backed units all operate. Detention risk is elevated for journalists, researchers, and local staff.",
        "borders": "The Damascus flight information region is under a conflict-zone restriction. Commercial arrival is not a normal planning assumption. Land borders open and close without notice.",
        "medical": "Trauma care is uneven. Plan medical evacuation toward Amman and confirm a viable hospital before deployment. Specialist care is limited outside the largest cities.",
        "comms": "Internet shutdowns and monitoring recur. Use an encrypted channel and a satellite backup. Do not store source names on a device you carry.",
        "legal": "Accreditation and filming rules are enforced unevenly. Confirm the visa and press rules before travel. Friday prayers and Ramadan change movement patterns.",
        "hazards": {"Conflict/War": (5, 5, "Hostile-environment training and PPE. Do not cross ground that changed hands recently.", 3, 4), "Kidnap/Detention": (4, 4, "Approved documents only. Tell the ops room before the last checkpoint.", 2, 3), "Road/Airspace": (4, 5, "No commercial arrival until the current NOTAM allows it.", 3, 4)},
    },
    "Iraq": {
        "stability": "Iraq's cities are open for managed movement, but militia activity, residual Islamic State cells, and protest spikes still drive the risk.",
        "actors": "State security forces, Iran-aligned militias, and residual insurgent cells are the main actors. Political blocs contest Baghdad and the south.",
        "borders": "The Baghdad flight information region has published civil-aviation restrictions. Check the current bulletin before an overflight or tech stop. Western and Syrian borders are sensitive.",
        "medical": "Private hospitals in Erbil and Baghdad cover routine trauma better than provincial clinics. Confirm medevac to Amman, Dubai, or Europe.",
        "comms": "Devices are inspected at some checkpoints. Use an official handset for work and keep source material off it.",
        "legal": "Visas, federal and Kurdish permissions, and media accreditation are separate processes. Carry the permission that matches the province.",
        "hazards": {"Terrorism": (3, 5, "Vary routes and avoid predictable convoys. Abort if the local picture changes.", 2, 4), "Road/Airspace": (3, 4, "Read the Baghdad FIR bulletin on the day of travel.", 2, 3)},
    },
    "Sudan": {
        "stability": "Sudan is in an active internal war. Khartoum is not a normal operating base. Front lines, checkpoints, and looting risk move between the capital, Darfur, and the east.",
        "actors": "The Sudanese Armed Forces and the Rapid Support Forces are the principal parties. Allied militias, communal armed groups, and criminal gangs shape local movement, especially in Darfur.",
        "borders": "Sudan airspace remains under a conflict-zone restriction. Commercial access to Khartoum is not a planning assumption. Crossings with Chad, South Sudan, Egypt, and Eritrea close around offensives. Port Sudan is the usual eastern gateway and still needs a fresh check.",
        "medical": "Hospital care in Khartoum cannot be assumed. Plan evacuation toward Port Sudan only if the road is open, and otherwise toward an external hub such as Nairobi or Cairo. Blood, trauma, and ambulance cover are unreliable.",
        "comms": "Telecoms and internet fail by city and by week. Carry a satellite communicator. Do not keep staff rosters or medical notes on a device that could be taken at a checkpoint.",
        "legal": "Permissions issued before the war may be meaningless on the ground. Confirm who controls the route on the day of movement. Looting and arbitrary detention of local staff are standing risks.",
        "hazards": {
            "Conflict/War": (5, 5, "No movement through contested neighbourhoods. A written abort line is agreed before departure.", 4, 4),
            "Crime": (4, 4, "Low profile, no visible valuables, and no night movement.", 3, 3),
            "Kidnap/Detention": (4, 5, "Local staff use the same check-in standard. Checkpoint documents are the minimum set.", 3, 4),
            "Road/Airspace": (5, 5, "Do not plan a Khartoum arrival. Reconfirm Port Sudan and any overland corridor the same day.", 3, 4),
            "Health/Environmental": (4, 4, "Carry a trauma kit and a heat plan. Do not rely on local blood supply.", 3, 3),
        },
    },
    "Yemen": {
        "stability": "Yemen remains fragmented by conflict. Front lines, air strikes, and maritime attacks in the Red Sea affect both land movement and regional flights.",
        "actors": "Ansarallah, the recognised government and allied forces, and southern groups all hold ground. Shipping attacks draw in outside navies.",
        "borders": "Yemeni airspace and the southern Red Sea need a current bulletin. Land entry is politically controlled and often closed to commercial travellers.",
        "medical": "Medical evacuation is the plan, not local definitive care. Confirm a receiving hospital outside Yemen before deployment.",
        "comms": "Connectivity is poor outside main cities. Use a satellite device and a fixed check-in cadence.",
        "legal": "Permissions are faction-specific. A document that works in one governorate can create risk in another.",
        "hazards": {"Conflict/War": (5, 5, "Stay on the agreed ground. Leave if strikes or front-line movement start.", 3, 4), "Road/Airspace": (5, 5, "No overflight or arrival without the current NOTAM and a maritime picture.", 3, 4)},
    },
    "Lebanon": {
        "stability": "Lebanon is politically fragile and exposed to the Israel border conflict. Services, the currency, and airport operations can deteriorate quickly.",
        "actors": "State institutions, Hezbollah, other armed factions, and the Israeli military across the southern border are the drivers.",
        "borders": "Beirut operations have been interrupted in this conflict cycle. The southern border district is not a routine movement area. Read the current airspace bulletin.",
        "medical": "Beirut private hospitals can stabilise trauma when they are open. Plan a regional medevac option in parallel.",
        "comms": "Mobile networks congest during strikes and protests. Keep a second channel.",
        "legal": "Filming military sites and the southern districts is sensitive. Accreditation does not guarantee access.",
        "hazards": {"Conflict/War": (4, 5, "No movement in the south without a fresh picture. Leave Beirut if the airport is at risk.", 3, 4)},
    },
    "Israel": {
        "stability": "Israel is in an active conflict cycle with rocket, drone, and regional escalation risk. Cities function, but alerts and reserve call-ups change movement at short notice.",
        "actors": "The Israeli military and government, Palestinian armed groups, and regional state and non-state actors are the drivers.",
        "borders": "Airports can close during barrages. Land crossings with Gaza, the West Bank, Lebanon, and Jordan have their own rules and can shut immediately.",
        "medical": "Trauma care in major cities is strong. Follow local alert instructions and confirm insurance covers conflict-related injury.",
        "comms": "Sirens and shelter drills are part of the operating picture. Keep the team on a local alert channel.",
        "legal": "Security screening is thorough. Filming military sites and some borders is restricted.",
        "hazards": {"Conflict/War": (4, 5, "Know the shelter plan at every stop. Abort exposed movement during an alert.", 2, 4), "Terrorism": (3, 5, "Avoid crowds after an attack. Vary routine.", 2, 3)},
    },
    "Palestine": {
        "stability": "Gaza is an active war zone and is not a routine deployment. The West Bank carries raid, protest, and movement-restriction risk that changes by town and by day.",
        "actors": "Israeli security forces, Palestinian Authority security forces in parts of the West Bank, and armed Palestinian groups are the drivers.",
        "borders": "Crossings and internal checkpoints close without notice. There is no dependable commercial air link inside the territory.",
        "medical": "Medical evacuation from Gaza cannot be assumed. West Bank care is limited for major trauma. Identify an external receiving hospital first.",
        "comms": "Communications are disrupted during operations. A satellite or radio backup is part of the plan, not an extra.",
        "legal": "Access, press, and humanitarian permissions are tightly controlled and can be withdrawn on the day.",
        "hazards": {"Conflict/War": (5, 5, "Gaza movement requires a separate go/no-go. West Bank routes are rechecked the same morning.", 4, 4)},
    },
    "Iran": {
        "stability": "Iran is a tightly controlled state with protest, detention, and regional-conflict risk. Tehran flight information region restrictions affect civil aviation.",
        "actors": "State security institutions, the Revolutionary Guard, and regional partners are the drivers. Protest waves raise detention risk for visitors and local contacts.",
        "borders": "Read the Tehran FIR bulletin before any overflight. Land borders with Iraq, Turkey, Afghanistan, and Pakistan each have their own closure pattern.",
        "medical": "Major-city private care can stabilise. Medevac out of the country can be delayed by airspace rules. Confirm that before travel.",
        "comms": "Assume monitoring. Use clean devices and do not carry contact lists that identify local sources.",
        "legal": "Dress, protest, and media rules are enforced. Dual nationals face additional detention risk.",
        "hazards": {"Kidnap/Detention": (3, 5, "Clean devices. No local source names on the handset. An abort plan if a colleague is held.", 2, 4), "Road/Airspace": (4, 4, "No Tehran FIR transit until the bulletin and NOTAM allow it.", 2, 3)},
    },
    "Libya": {
        "stability": "Libya is divided and armed groups control much of the real movement picture. Front lines and airport access change.",
        "actors": "Rival governments, city armed groups, and foreign backers are the drivers.",
        "borders": "Libyan airspace is under a long-running conflict-zone restriction. Confirm any airport is actually open on the day.",
        "medical": "Medical evacuation is the primary medical plan. Local trauma capacity is limited and politically exposed.",
        "comms": "Use a satellite device. Local SIMs are fine for logistics and poor for sensitive traffic.",
        "legal": "Visas and sponsorship are faction-sensitive. A sponsor in one city may not protect movement in another.",
        "hazards": {"Conflict/War": (4, 5, "Armed-group checkpoints are treated as no-negotiation points. Turn back if the escort is not the agreed one.", 3, 4), "Road/Airspace": (4, 5, "No flight until the current restriction and the airport status both allow it.", 3, 4)},
    },
    "Ukraine": {
        "stability": "Ukraine is a country at war. Western and central cities are more functional, but missiles, drones, and air-raid alerts are a national pattern. Front-line oblasts are a separate, higher decision.",
        "actors": "Ukrainian state forces and Russian forces are the principal parties. Air defence and civil alerts shape daily movement.",
        "borders": "Ukrainian airspace is closed to normal civil overflight. Entry is by land from the west. Front-line oblasts are not a standard deployment.",
        "medical": "City hospitals treat war trauma. Plan evacuation westward by road, and confirm insurance covers war injury.",
        "comms": "Air-raid apps and a shelter plan are mandatory brief items. Power cuts are common.",
        "legal": "Accreditation, curfews, and military-area bans are enforced. Filming defence sites is prohibited.",
        "hazards": {"Conflict/War": (5, 5, "Shelter on every alert. No movement toward the front without a separate authorisation.", 3, 4), "Road/Airspace": (5, 4, "Land entry only. Do not plan an overflight.", 3, 3)},
    },
    "Afghanistan": {
        "stability": "Taliban authorities control the state. Terrorism, detention, and restrictive social rules are standing features. Kabul is not a low-risk capital.",
        "actors": "Taliban security forces and Islamic State Khorasan are the main violent actors. Local enforcement of social codes affects every movement.",
        "borders": "Airspace and carrier acceptance need a current check. Land borders with Pakistan and Iran close around security incidents.",
        "medical": "Evacuation out of the country is the medical plan. Confirm a receiving hospital and an insurer that will actually fly.",
        "comms": "Assume monitoring. Women team members need a specific movement protocol agreed in advance.",
        "legal": "Local law and enforcement of dress, gender, and media rules is strict. Consular help is limited.",
        "hazards": {"Terrorism": (4, 5, "Vary routes. No predictable hotel-to-office pattern.", 3, 4), "Kidnap/Detention": (3, 5, "Documents and a local sponsor are confirmed before landing.", 2, 4)},
    },
    "Somalia": {
        "stability": "Somalia faces a long-running insurgency. Mogadishu movement is possible only with a current security plan. Rural areas are often not permissive.",
        "actors": "Federal and local forces and al-Shabaab are the principal security actors.",
        "borders": "Somali airspace carries a conflict-zone restriction. Airport acceptance and the city route are separate decisions.",
        "medical": "Medevac to Nairobi is the usual medical plan. Do not rely on definitive care in Mogadishu.",
        "comms": "Satellite communications are part of the standard kit.",
        "legal": "Access depends on the local authority in control of that district.",
        "hazards": {"Terrorism": (5, 5, "Armoured or hardened movement only where the plan requires it. Abort if the route picture is stale.", 3, 4), "Road/Airspace": (4, 5, "Confirm the airspace bulletin and the airport on the day.", 3, 4)},
    },
    "Nigeria": {
        "stability": "Nigeria's commercial cities operate, with kidnap-for-ransom, crime, and regional insurgency that is worse in the north-east and some highways.",
        "actors": "Federal security forces, criminal kidnap gangs, and jihadist factions in the north-east are the drivers.",
        "borders": "Airports in Abuja and Lagos are the normal gateways. Night road movement between cities is avoided.",
        "medical": "Private hospitals in Lagos and Abuja stabilise. Confirm medevac for anything beyond that.",
        "comms": "Use a low profile and do not discuss itineraries in public or with unvetted drivers.",
        "legal": "Police checkpoints are common. Keep vehicle papers and IDs to hand.",
        "hazards": {"Kidnap/Detention": (4, 5, "No night inter-city road moves. Vetted driver. A check-in at every leg.", 2, 4), "Crime": (4, 3, "Hotel-to-meeting movement only. No visible laptops in traffic.", 2, 2)},
    },
    "United States": {
        "stability": "The United States is a stable operating environment with localised crime, demonstration, and severe-weather risk.",
        "actors": "Federal, state, and city authorities are the security actors. Severe weather and crowd events are the usual disruptors.",
        "borders": "Entry is regulated and predictable. Airspace is open aside from temporary security restrictions and storms.",
        "medical": "Emergency care is widely available and expensive. Confirm insurance before travel.",
        "comms": "Normal commercial communications. A severe-weather backup plan is enough for most trips.",
        "legal": "Local law applies. Protest sites and federal buildings have their own enforcement picture.",
        "hazards": {"Crime": (2, 3, "Use the same city-crime brief you would for any large capital.", 1, 2), "Health/Environmental": (2, 4, "Track severe-weather alerts for the state you are in.", 1, 3)},
    },
    "United Kingdom": {
        "stability": "The United Kingdom is a stable, low-conflict environment with residual terrorism and protest risk in major cities.",
        "actors": "Police and intelligence services lead the security picture. Protest organisers and residual terrorism plots are the exceptions.",
        "borders": "Border control is outside Schengen. Airspace is open. Check transport strikes before a fixed arrival.",
        "medical": "Emergency care is available. Visitors should confirm how their insurance interacts with NHS emergency treatment.",
        "comms": "Normal commercial coverage. Encrypted apps are a privacy choice, not a survival requirement.",
        "legal": "Journalism and protest have published rules. Follow police directions at a march.",
        "hazards": {"Terrorism": (2, 5, "Know the local run-hide-tell brief. Avoid dwelling at a symbolic site after an alert.", 1, 3)},
    },
}

FACILITIES = {
    "Sudan": [
        {"name": "Do not plan on Khartoum hospitals", "city": "Khartoum", "note": "War damage and access make definitive care an unsafe assumption."},
        {"name": "Port Sudan receiving options", "city": "Port Sudan", "note": "Use only if the road and the port city are open that day. Reconfirm before movement."},
        {"name": "External medevac hub", "city": "Nairobi or Cairo", "note": "Name the receiving hospital and the assistance provider before deployment."},
    ],
    "Syria": [
        {"name": "External medevac hub", "city": "Amman", "note": "Amman is the usual receiving hub. Confirm acceptance before the team moves."},
    ],
    "Ukraine": [
        {"name": "Westward road evacuation", "city": "Lviv or a Polish border city", "note": "Air evacuation inside Ukraine is not the plan."},
    ],
}

RATES = {
    "country_base": {"gbp": 149, "usd": 189},
    "country_extra": {"gbp": 49, "usd": 62},
    "region_base": {"gbp": 399, "usd": 499},
    "region_extra": {"gbp": 199, "usd": 249},
    "global": {"gbp": 899, "usd": 1149},
    "pass": {"gbp": 59, "usd": 75},
    "briefing": {
        "monthly": {"country": {"gbp": 29, "usd": 37}, "region": {"gbp": 69, "usd": 89}, "global": {"gbp": 129, "usd": 165}},
        "weekly": {"country": {"gbp": 59, "usd": 75}, "region": {"gbp": 129, "usd": 165}, "global": {"gbp": 249, "usd": 315}},
        "daily": {"country": {"gbp": 119, "usd": 149}, "region": {"gbp": 249, "usd": 315}, "global": {"gbp": 449, "usd": 569}},
        "bundle": {"country": {"gbp": 169, "usd": 215}, "region": {"gbp": 349, "usd": 445}, "global": {"gbp": 649, "usd": 825}},
    },
}
BRIEFINGS = ("none", "monthly", "weekly", "daily", "bundle")
MODES = ("country", "region", "global", "pass")


def _norm(value: object) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = text.replace("'", "'").replace("'", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def countries() -> list[dict]:
    return [
        {"name": name, "region": region, "lat": lat, "lng": lng}
        for name, region, lat, lng in COUNTRY_ROWS
    ]


def _by_name() -> dict[str, dict]:
    return {row["name"]: row for row in countries()}


def region_labels() -> list[str]:
    return [row["label"] for row in REGIONS]


def canonical_country(value: object) -> str:
    text = _norm(value)
    if not text:
        return ""
    if text in ALIASES:
        return ALIASES[text]
    for row in countries():
        if _norm(row["name"]) == text:
            return row["name"]
    if text == "gulf states":
        return "Gulf States"
    if text == "maritime":
        return "Maritime"
    return ""


def region_of(value: object) -> str:
    name = canonical_country(value) or str(value or "").strip()
    if name == "Gulf States":
        return "Middle East"
    if name == "Maritime":
        return "Maritime"
    row = _by_name().get(name)
    return row["region"] if row else ""


def macro_region(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text or text == "all":
        return ""
    if text in LEGACY_TO_MACRO:
        return LEGACY_TO_MACRO[text]
    if text in region_labels():
        return text
    return region_of(text)


def is_macro(value: object) -> bool:
    return str(value or "") in region_labels()


def _props(feature: dict) -> dict:
    if isinstance(feature.get("properties"), dict):
        return feature["properties"]
    return feature


def event_country_name(props: dict) -> str:
    sea = {"Red Sea / Maritime", "Strait of Hormuz / Gulf", "Maritime"}
    for key in ("country", "region", "theatre", "location_name", "area"):
        raw = " ".join(str(props.get(key) or "").split())
        if raw in sea:
            return "Maritime"
        canon = canonical_country(raw)
        if canon and canon != "Gulf States":
            return canon
    return ""


def _event_macro(props: dict) -> str:
    country = event_country_name(props)
    if country:
        region = region_of(country)
        if region:
            return region
    return macro_region(props.get("theatre") or props.get("region") or "")


def _matches_global(props: dict) -> bool:
    feed = str(props.get("feed_category") or props.get("category") or "")
    return str(props.get("theatre") or "") == "Global" or str(props.get("region") or "") == "Global" or feed in GLOBAL_FEEDS


def _matches_gulf(props: dict) -> bool:
    country = event_country_name(props)
    if country in GULF:
        return True
    return str(props.get("theatre") or "") == "Gulf States" or str(props.get("region") or "") == "Gulf States"


def _matches_maritime(props: dict) -> bool:
    if event_country_name(props) == "Maritime":
        return True
    blob = " ".join(str(props.get(key) or "") for key in ("region", "theatre", "country", "category")).lower()
    return "maritime" in blob


def _matches_legacy(props: dict, name: str) -> bool:
    members = LEGACY_MEMBERS.get(name) or []
    country = event_country_name(props)
    region_name = str(props.get("region") or "")
    theatre = str(props.get("theatre") or "")
    return country in members or region_name in members or theatre == name or region_name == name


def event_matches_scope(props: dict, scope: object) -> bool:
    name = " ".join(str(scope or "").split())
    if not name or name == "all":
        return True
    if name == "Global":
        return _matches_global(props)
    if name == "Gulf States":
        return _matches_gulf(props)
    if name == "Maritime":
        return _matches_maritime(props)
    if name in LEGACY_MEMBERS and name not in region_labels():
        return _matches_legacy(props, name)
    if is_macro(name):
        return _event_macro(props) == name
    if name in LEGACY_MEMBERS:
        return _matches_legacy(props, name)
    country = canonical_country(name)
    if country:
        return event_country_name(props) == country
    wanted = _norm(name)
    return any(_norm(props.get(key)) == wanted for key in ("country", "region", "theatre"))


def event_matches(feature: dict, region: object = "", country: object = "") -> bool:
    props = _props(feature if isinstance(feature, dict) else {})
    if country and str(country) != "all" and not event_matches_scope(props, country):
        return False
    if region and str(region) != "all" and not event_matches_scope(props, region):
        return False
    return True


def countries_for(region: object) -> list[str]:
    macro = "" if not region or str(region) in {"all", "Global"} else (macro_region(region) or "")
    names = [row["name"] for row in countries() if row["name"] != "Maritime" and (not macro or row["region"] == macro)]
    if not macro or macro == "Middle East":
        if "Gulf States" not in names:
            names.append("Gulf States")
    if macro == "Maritime":
        return ["Maritime"]
    if macro == "Africa" and "Sudan" not in names:
        names.append("Sudan")
    return names


def _severity(value: object) -> str:
    text = str(value or "medium").strip().lower()
    return text if text in {"high", "medium", "low"} else "medium"


def _category(props: dict) -> str:
    return str(props.get("feed_category") or props.get("category") or "OSINT").strip() or "OSINT"


def trend_sentence(total: int, high: int, top: str) -> str:
    if total <= 0:
        return "No live OSINT events are stored for this Area of Operations. Baseline monitoring still applies."
    lead = f"{total} live OSINT event" + ("s" if total != 1 else "")
    if high <= 0:
        sentence = lead + ", none at high severity."
    elif high == 1:
        sentence = lead + ", 1 at high severity."
    else:
        sentence = lead + f", {high} at high severity."
    if top:
        sentence += f" The busiest category is {top}."
    if high >= 3 or (total and high / total >= 0.4):
        sentence += " The activity trend is elevated."
    elif high == 0:
        sentence += " The activity trend is steady."
    else:
        sentence += " The activity trend is mixed."
    return sentence


def _profile(country: str) -> dict:
    return PROFILES.get(country) or {}


def _region_text(region: str) -> dict:
    return REGION_TEXT.get(region) or REGION_TEXT["Global"]


def compose_summary(events: list, region: object = "", country: object = "") -> dict:
    """Deterministic geopolitical summary. events are public incident records only."""
    region_name = macro_region(region) or (str(region or "").strip() if str(region or "").strip() in LEGACY_MEMBERS else "")
    if str(region or "").strip() in region_labels():
        region_name = str(region).strip()
    country_name = canonical_country(country) if country and str(country) != "all" else ""
    if country_name in {"Gulf States", "Maritime"}:
        country_name = country_name
    elif country and str(country) != "all" and not country_name:
        country_name = ""
    if country_name and country_name not in {"Gulf States", "Maritime"} and not region_name:
        region_name = region_of(country_name)
    matched = []
    for feature in events or []:
        if not isinstance(feature, dict):
            continue
        props = _props(feature)
        if not event_matches(props, region_name, country_name):
            continue
        title = " ".join(str(props.get("title") or "").split())
        if not title:
            continue
        matched.append({
            "title": title[:180],
            "severity": _severity(props.get("severity")),
            "category": _category(props)[:80],
            "timestamp": str(props.get("timestamp") or ""),
        })
    matched.sort(key=lambda item: item["timestamp"], reverse=True)
    counts = {"high": 0, "medium": 0, "low": 0}
    categories: dict[str, int] = {}
    for item in matched:
        counts[item["severity"]] += 1
        categories[item["category"]] = categories.get(item["category"], 0) + 1
    top = ""
    if categories:
        top = sorted(categories.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]
    if country_name:
        profile = _profile(country_name if country_name not in {"Gulf States", "Maritime"} else "")
        base_region = region_of(country_name) or region_name or "Global"
        region_copy = _region_text(base_region)
        scope = "country"
        title = country_name
        if profile:
            stability, actors, borders = profile["stability"], profile["actors"], profile["borders"]
        else:
            stability = f"{country_name} uses the {base_region} baseline. Confirm the current government travel advice before movement. {region_copy['stability']}"
            actors = region_copy["actors"]
            borders = region_copy["borders"]
        shown_region = base_region
    elif region_name:
        scope = "region"
        label = macro_region(region_name) or region_name
        copy = _region_text(label if label in REGION_TEXT else "Global")
        if region_name in LEGACY_MEMBERS and region_name not in REGION_TEXT:
            copy = _region_text(macro_region(region_name) or "Global")
            stability = f"{region_name} sits inside the {macro_region(region_name) or region_name} Area of Operations. {copy['stability']}"
        else:
            stability = copy["stability"]
        actors = copy["actors"]
        borders = copy["borders"]
        title = region_name if region_name in REGION_TEXT or region_name in LEGACY_MEMBERS else label
        shown_region = macro_region(region_name) or region_name
    else:
        scope = "none"
        title = "All regions"
        shown_region = ""
        stability = "Select a region or a country to read the geopolitical summary for that Area of Operations."
        actors = ""
        borders = ""
    return {
        "ok": True,
        "xai_credits": XAI_CREDITS,
        "scope": scope,
        "title": title,
        "region": shown_region,
        "country": country_name,
        "stability": stability,
        "actors": actors,
        "borders": borders,
        "trend": {
            "total": len(matched),
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
            "top_category": top,
            "headlines": [item["title"] for item in matched[:3]],
            "sentence": trend_sentence(len(matched), counts["high"], top),
        },
    }


def _default_hazard(category: str, country: str) -> dict:
    text = {
        "Conflict/War": f"Confirm whether armed conflict affects the route in {country} before movement.",
        "Terrorism": f"Check the current terrorism posture for {country} and avoid symbolic sites after an alert.",
        "Crime": f"Use a city-crime brief for {country}. No visible valuables in traffic.",
        "Civil Unrest": f"Map protest sites in {country} on the day and do not transit a crowd.",
        "Kidnap/Detention": f"Agree an arrest and kidnap protocol before deployment to {country}.",
        "Road/Airspace": f"Read NOTAMs, the flight information region, and the road picture for {country} on the day.",
        "Digital/InfoSec": "Encrypted devices. No staff names, medical notes, or source names in shared files.",
        "Health/Environmental": f"Name a hospital and a medevac hub for {country} before departure.",
    }[category]
    return {
        "category": category,
        "threat": category,
        "likelihood": 2,
        "impact": 3,
        "mitigation": text,
        "control": text,
        "residual_likelihood": 2,
        "residual_impact": 2,
    }


def hazards_for(country: str) -> list[dict]:
    profile = _profile(country)
    overrides = profile.get("hazards") or {}
    rows = []
    for category in RISK_CATEGORIES:
        if category in overrides:
            likelihood, impact, mitigation, residual_l, residual_i = overrides[category]
            rows.append({
                "category": category,
                "threat": category,
                "likelihood": int(likelihood),
                "impact": int(impact),
                "mitigation": mitigation,
                "control": mitigation,
                "residual_likelihood": int(residual_l),
                "residual_impact": int(residual_i),
            })
        else:
            rows.append(_default_hazard(category, country or "the country"))
    return rows


def risk_tags(hazards: list[dict]) -> list[str]:
    tags = []
    for row in hazards:
        score = int(row["likelihood"]) * int(row["impact"])
        if score >= 10 and row["category"] not in tags:
            label = row["category"].split("/")[0]
            if label not in tags:
                tags.append(label)
    return tags or ["Baseline"]


def overall_rating(hazards: list[dict]) -> dict:
    score = 0
    for row in hazards:
        score = max(score, int(row["likelihood"]) * int(row["impact"]))
    if score <= 4:
        band = "LOW"
    elif score <= 9:
        band = "MEDIUM"
    elif score <= 16:
        band = "HIGH"
    else:
        band = "SEVERE"
    return {"band": band, "score": score, "scale": "1-25"}


def country_template(country: object) -> dict:
    """Country risk-assessment baseline. User-entered names stay out of this payload."""
    name = canonical_country(country) or " ".join(str(country or "").split())[:80]
    if not name:
        name = "Selected country"
    known = name if name in _by_name() or name in PROFILES or name in {"Gulf States", "Maritime"} else ""
    label = known or name
    region = region_of(label) or "Global"
    profile = _profile(label)
    region_copy = _region_text(region if region in REGION_TEXT else "Global")
    specific = bool(profile)
    hazards = hazards_for(label if specific or known else label)
    rating = overall_rating(hazards)
    if profile:
        stability, actors, borders = profile["stability"], profile["actors"], profile["borders"]
        medical = profile["medical"]
        comms = profile["comms"]
        legal = profile["legal"]
    else:
        stability = f"{label} uses the GeoRiskIntel {region} baseline. Confirm the current travel advice before movement. {region_copy['stability']}"
        actors = region_copy["actors"]
        borders = region_copy["borders"]
        medical = f"Name a hospital and a medevac hub for {label} before departure. Do not rely on this baseline alone."
        comms = "Agree an encrypted primary channel and a check-in cadence. Keep staff and medical detail on this device only."
        legal = f"Confirm visa, permit, and local registration rules for {label}. Note religious calendars and photography limits."
    situation = " ".join([stability, actors, borders])
    return {
        "ok": True,
        "xai_credits": XAI_CREDITS,
        "country": label,
        "region": region,
        "baseline": not specific,
        "title": f"{label} country template" if specific else f"{label} baseline template",
        "branding": "GeoRiskIntel",
        "rating": rating,
        "risk_tags": risk_tags(hazards),
        "stability": stability,
        "actors": actors,
        "borders": borders,
        "situation": situation,
        "sections": {
            "threat": profile.get("stability") if profile else stability,
            "security": actors,
            "airspace": borders,
            "medical": medical,
            "comms": comms,
            "legal": legal,
        },
        "hazards": hazards,
        "facilities": FACILITIES.get(label, [{"name": "Confirm a receiving hospital", "city": label, "note": "Add the facility your assistance provider accepts. Do not enter a private mobile number here."}] ),
        "references": [
            f"UK FCDO foreign travel advice for {label}",
            f"US State Department travel advisory for {label}",
            "GeoRiskIntel live 7-day OSINT feed for this country",
            "Current NOTAMs and conflict-zone bulletins for the flight information region",
        ],
        "local_only_fields": ["roster", "phone", "email", "imei", "poc_contact", "staff_details", "passport", "medical_notes"],
        "hra_sections": list(HRA_SECTIONS),
    }


def catalog() -> dict:
    return {
        "ok": True,
        "xai_credits": XAI_CREDITS,
        "regions": [dict(item) for item in REGIONS],
        "region_text": REGION_TEXT,
        "countries": countries(),
        "categories": list(RISK_CATEGORIES),
        "hra_sections": list(HRA_SECTIONS),
        "priority": list(PROFILES),
        "profiles": {name: country_template(name) for name in PROFILES},
        "rates": RATES,
        "legacy_members": LEGACY_MEMBERS,
        "legacy_to_macro": LEGACY_TO_MACRO,
        "aliases": ALIASES,
        "gulf": list(GULF),
    }


def _money(amount: int, currency: str) -> str:
    symbol = "£" if currency == "gbp" else "$"
    return symbol + f"{int(amount):,}"


def quote_ao(mode: object = "country", count: object = 1, briefing: object = "none", currency: object = "gbp", billing: object = "monthly") -> dict:
    """Area of Operations price. No personal data is read or returned."""
    chosen = str(mode or "country").strip().lower()
    if chosen not in MODES:
        chosen = "country"
    cycle = "annual" if str(billing or "").strip().lower() == "annual" else "monthly"
    money = "usd" if str(currency or "").strip().lower() == "usd" else "gbp"
    extra = str(briefing or "none").strip().lower()
    if extra not in BRIEFINGS:
        extra = "none"
    try:
        units = int(count)
    except (TypeError, ValueError):
        units = 1
    units = min(30, max(1, units))
    if chosen in {"global", "pass"}:
        units = 1
    suggestion = ""
    includes: list[str] = []
    if chosen == "country":
        base = RATES["country_base"][money] + RATES["country_extra"][money] * (units - 1)
        scope = "country"
        if units >= 4:
            region_price = _money(RATES["region_base"][money], money)
            suggestion = (
                f"Four or more countries in one region: a Regional Area of Operations starts at {region_price} a month "
                "and may cost less than adding further countries. Compare the region price before you add another country."
            )
            if base >= RATES["region_base"][money]:
                suggestion += f" A single region at {region_price} a month costs less than these {units} countries ({_money(base, money)})."
    elif chosen == "region":
        base = RATES["region_base"][money] + RATES["region_extra"][money] * (units - 1)
        scope = "region"
        if base >= RATES["global"][money]:
            suggestion = (
                f"Global All-Access at {_money(RATES['global'][money], money)} a month costs less than this regional total "
                "and includes every region, Okta SSO, and White-Label Studio."
            )
    elif chosen == "global":
        base = RATES["global"][money]
        scope = "global"
        includes = ["Okta SSO", "White-Label Studio", "Priority custom risk assessments"]
    else:
        base = RATES["pass"][money]
        scope = "pass"
    briefing_total = 0
    if chosen == "pass":
        if extra != "none":
            suggestion = (suggestion + " " if suggestion else "") + "Briefings are monthly add-ons for a subscription Area of Operations. They are not added to the 7-day pass."
        extra = "none"
    elif extra != "none":
        rate = RATES["briefing"][extra][scope][money]
        briefing_total = rate if scope == "global" else rate * units
    multiplier = 10 if cycle == "annual" and chosen != "pass" else 1
    total = (base + briefing_total) * multiplier
    period = "one-off" if chosen == "pass" else ("a year" if cycle == "annual" else "a month")
    noun = {"country": "country" if units == 1 else "countries", "region": "region" if units == 1 else "regions", "global": "global", "pass": "pass"}[chosen]
    if chosen == "country":
        lead = f"Country-by-country Area of Operations for {units} {noun} is {_money(base * multiplier, money)} {period}."
    elif chosen == "region":
        lead = f"Region-by-region Area of Operations for {units} {noun} is {_money(base * multiplier, money)} {period}."
    elif chosen == "global":
        lead = f"Global All-Access is {_money(base * multiplier, money)} {period}. It includes Okta SSO, White-Label Studio, and priority custom risk assessments."
    else:
        lead = f"Single-country 7-day pass is {_money(base, money)}, a one-off for a short trip, school trip, or single assignment."
    if briefing_total:
        names = {"monthly": "Monthly briefing", "weekly": "Weekly briefing", "daily": "Daily briefing", "bundle": "All-three briefings bundle"}
        lead += f" {names[extra]} adds {_money(briefing_total * multiplier, money)}. Total {_money(total, money)} {period}."
    else:
        lead += f" Total {_money(total, money)} {period}."
    return {
        "ok": True,
        "xai_credits": XAI_CREDITS,
        "mode": chosen,
        "count": units,
        "briefing": extra,
        "currency": money,
        "billing": "one-off" if chosen == "pass" else cycle,
        "base": base * multiplier,
        "briefing_total": briefing_total * multiplier,
        "total": total,
        "total_label": _money(total, money) + (" one-off" if chosen == "pass" else ("/yr" if cycle == "annual" else "/mo")),
        "suggestion": suggestion.strip(),
        "summary": lead,
        "includes": includes,
    }


def ao_from_query(params: dict | None, stored: dict | None = None) -> dict:
    params = params or {}
    raw_region = " ".join(str(params.get("region") or "").split())
    raw_country = " ".join(str(params.get("country") or "").split())
    governorate = " ".join(str(params.get("governorate") or "").split())
    if raw_region or raw_country or governorate:
        country = "Syria" if governorate else (canonical_country(raw_country) if raw_country else "")
        if raw_region in LEGACY_MEMBERS and raw_region not in region_labels():
            region = raw_region
        elif raw_region:
            region = macro_region(raw_region) or "all"
        else:
            region = "all"
        if country:
            implied = region_of(country)
            if region == "all":
                region = implied or "all"
            elif implied and region in region_labels() and macro_region(region) != implied:
                region = implied
            elif region in LEGACY_MEMBERS and country not in (LEGACY_MEMBERS.get(region) or []):
                region = implied or region
        if governorate:
            country = "Syria"
            if region == "all":
                region = "Middle East"
        return {"region": region or "all", "country": country or "all", "source": "url"}
    if stored and (stored.get("region") or stored.get("country")):
        region = stored.get("region") if stored.get("region") in LEGACY_MEMBERS or stored.get("region") in region_labels() else (macro_region(stored.get("region")) or "all")
        country = canonical_country(stored.get("country")) or ("all" if stored.get("country") in {"", "all", None} else "all")
        if stored.get("country") in {"Gulf States", "Maritime"}:
            country = stored["country"]
        if region == "all" and country == "all":
            return {"region": "all", "country": "all", "source": "default"}
        return {"region": region or "all", "country": country or "all", "source": "pinned"}
    return {"region": "all", "country": "all", "source": "default"}
