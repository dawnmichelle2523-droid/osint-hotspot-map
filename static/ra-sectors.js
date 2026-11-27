/* GeoRiskIntel risk-assessment sector taxonomy.

   /risk-assessments reads window.RA_SECTORS and rebuilds every dropdown
   when the sector changes. Lists are suggestions: the page still accepts
   a typed custom entry. localLabels name the browser-only fields. Those
   values are never sent to the server or to Grok.

   To add a sector, copy an object and keep id unique. Templates need a
   stable id. Set pdr: true on the pre-deployment template.
*/
(function (root, factory) {
  var sectors = factory();
  if (typeof module === "object" && module.exports) module.exports = sectors;
  root.RA_SECTORS = sectors;
})(typeof window !== "undefined" ? window : globalThis, function () {
  function template(id, title, standards, pdr) {
    return { id: id, title: title, standards: standards, pdr: !!pdr };
  }
  function control(id, label, text, residualL, residualI, appliesTo) {
    return {
      id: id,
      label: label,
      text: text,
      residualL: residualL,
      residualI: residualI,
      appliesTo: appliesTo
    };
  }

  return [
    {
      id: "media",
      label: "Media & Journalism",
      summary: "News bureaus, hostile environment, broadcasters, freelancers, fixers and drivers.",
      templates: [
        template("hefat", "Hostile Environment / Media Safety", "ISO 31000 · ISO 31030 · Hostile Environment / Media Safety"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("bureau", "Corporate Duty of Care (Bureau)", "ISO 31030 · ISO 45001 · Corporate Duty of Care"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Frontline / Conflict Reporting",
        "Protest / Civil Unrest Coverage",
        "Documentary Field Shoot",
        "Fixer/Local Crew Deployment",
        "Bureau / stringer rotation",
        "Live broadcast from a hostile environment",
        "Interview / access trip"
      ],
      threats: [
        "Detention/Arrest",
        "Crossfire/UXO",
        "Digital/Source Surveillance",
        "Crowd Crush",
        "Kidnap of crew or fixer",
        "Communications blackout",
        "Targeting of marked press",
        "Accreditation or visa denial",
        "Road traffic collision",
        "Equipment seizure"
      ],
      controls: [
        control("detention", "Detention card and bureau legal contact briefed", "Carry a detention card. The bureau legal contact is briefed before deployment. Do not argue at a checkpoint, and the desk is told if the team is held.", 2, 3, ["Detention/Arrest", "Accreditation or visa denial"]),
        control("hefat", "HEFAT completed and PPE checked", "HEFAT is complete for every deployer. Helmet, plate and eye protection are issued and inspected before movement. Abort if PPE is missing.", 2, 4, ["Crossfire/UXO", "Targeting of marked press"]),
        control("sources", "Encrypted devices and no source names in shared notes", "Devices are encrypted. Source names stay on this device and are not typed into the assessment, the desk log, or Auto-Draft.", 2, 3, ["Digital/Source Surveillance"]),
        control("crowd", "Elevated position, mapped exits, abort if density rises", "Film from an elevated position with two mapped exits. Leave if the crowd densifies or the mood turns.", 2, 3, ["Crowd Crush"]),
        control("lowpro", "Low-profile movement with a vetted fixer-driver", "Use an unmarked vehicle and a vetted fixer-driver. No press markings, and no night movement.", 2, 3, ["Kidnap of crew or fixer", "Road traffic collision"]),
        control("signal", "Encrypted Signal, satphone and a 2-hour desk check-in", "Encrypted Signal plus a satellite phone. Check in with the bureau every two hours, and at last light.", 2, 2, ["Communications blackout"]),
        control("gear", "Equipment logged; do not resist a seizure", "Log the kit before departure. Do not resist a seizure. The bureau replaces equipment; the team does not escalate.", 2, 2, ["Equipment seizure"])
      ],
      transport: ["Vetted local driver", "Soft-skin low-profile", "Unmarked bureau vehicle", "Commercial air", "No night movement"],
      accommodation: ["Vetted hotel away from protest sites", "Bureau compound or staff house", "Low-profile guesthouse in a neutral name", "No ground-floor room facing the street"],
      comms: ["Encrypted Signal", "Satellite phone", "Garmin inReach", "VHF with the fixer", "Check-in cadence: every 2 hours", "Check-in cadence: arrival, last light, and departure"],
      owners: ["Bureau editor", "Team leader", "Fixer", "Security advisor", "Medic"],
      contextTitle: "Hostile environment",
      contextNote: "Crew, fixer and driver names stay in the local-only section and are not uploaded.",
      context: [
        { id: "hefat", label: "Hostile-environment training", options: ["Completed", "Booked", "Not completed"] },
        { id: "ppe", label: "PPE", options: ["Issued and checked", "Partial", "Not issued"] },
        { id: "accreditation", label: "Accreditation", options: ["Held", "Applied", "Not required"] }
      ],
      localLabels: {
        heading: "Crew, Fixer and Driver Names (Local-Only)",
        names: "Crew, fixer and driver names (Local-Only)",
        contacts: "Bureau desk, legal and next-of-kin (Local-Only)",
        medical: "Blood type, allergies and medication (Local-Only)",
        passport: "Press pass, visa and passport reference (Local-Only)"
      }
    },
    {
      id: "government",
      label: "Governments & Public Sector",
      summary: "Diplomatic missions, ministerial and official delegations, and field inspectors.",
      templates: [
        template("mission", "Diplomatic / Official Travel Standard", "ISO 31000 · ISO 31030 · Diplomatic / Official Travel"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("delegation", "Ministerial Delegation Risk Assessment", "ISO 31000 · ISO 22361"),
        template("inspector", "Field Inspector Duty of Care", "ISO 31030 · ISO 45001"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Diplomatic mission visit",
        "Ministerial / official delegation",
        "Field inspection",
        "Consular assistance movement",
        "Bilateral meeting off-site",
        "Election observation",
        "Post security survey"
      ],
      threats: [
        "Targeted attack on officials",
        "Protest against the mission",
        "Surveillance of the delegation",
        "Detention of locally engaged staff",
        "Route attack",
        "Device compromise",
        "Insider threat at the venue",
        "Medical emergency away from post",
        "Civil unrest on the route",
        "Kidnap"
      ],
      controls: [
        control("escort", "Host-nation or police escort on the principal", "The principal moves with the agreed police or host-nation escort. Unscheduled stops are refused.", 1, 3, ["Targeted attack on officials", "Kidnap"]),
        control("protest", "Relocate if a demonstration is announced", "If a protest is announced at the mission or the venue, move the meeting. Do not transit a crowd.", 2, 3, ["Protest against the mission", "Civil unrest on the route"]),
        control("devices", "Official handsets only in the meeting", "Personal devices stay out of the meeting. Official handsets are encrypted and collected if the room requires it.", 2, 2, ["Surveillance of the delegation", "Device compromise"]),
        control("les", "Locally engaged staff use the same check-in standard", "Locally engaged staff get the same check-in and medevac standard as the delegation. Names stay in the local-only section.", 2, 3, ["Detention of locally engaged staff"]),
        control("advance", "Route, venue and hospital advanced", "Primary and alternate routes, the venue and the viable hospital are advanced before movement.", 2, 3, ["Route attack", "Medical emergency away from post"]),
        control("insider", "Venue access list held at post", "The access list is held at post, not in this file. Staff not on the list do not enter the room.", 2, 2, ["Insider threat at the venue"])
      ],
      transport: ["Host-nation police escort", "Armoured official vehicle", "Soft-skin pool car with a driver", "Commercial air", "No unscheduled stops"],
      accommodation: ["Official residence", "Mission compound", "Vetted hotel with controlled access", "Government guesthouse"],
      comms: ["Encrypted official handset", "Post radio net", "VHF", "Satellite phone for field inspection", "Check-in cadence: every movement leg"],
      owners: ["Post security officer", "Delegation lead", "Duty officer", "Medic"],
      contextTitle: "Official travel",
      contextNote: "Delegation and locally engaged staff names stay in the local-only section.",
      context: [
        { id: "movement", label: "Movement type", options: ["Official motorcade", "Low profile", "Host-nation escort"] },
        { id: "size", label: "Delegation size band", options: ["1–5", "6–20", "20+"] },
        { id: "devices", label: "Device policy", options: ["Official handsets only", "Mixed", "Uncontrolled"] }
      ],
      localLabels: {
        heading: "Delegation and Locally Engaged Staff (Local-Only)",
        names: "Delegation and locally engaged staff names (Local-Only)",
        contacts: "Post duty officer and home ministry contact (Local-Only)",
        medical: "Medical restrictions and blood type (Local-Only)",
        passport: "Diplomatic passport or laissez-passer reference (Local-Only)"
      }
    },
    {
      id: "tmc",
      label: "Travel Risk Agencies & TMCs",
      summary: "Corporate travel programmes, multi-leg client itineraries, and duty-of-care desks.",
      templates: [
        template("doc", "Corporate Duty of Care", "ISO 31030 · Corporate Duty of Care"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("itinerary", "Multi-leg Itinerary Risk Review", "ISO 31030 · Multi-leg Itinerary"),
        template("desk", "Duty-of-Care Desk Assessment", "ISO 31030 · ISO 22361"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Corporate travel programme review",
        "Multi-leg client itinerary",
        "Duty-of-care desk assessment",
        "High-risk destination approval",
        "Executive roadshow support",
        "Group incentive travel",
        "Expat assignment look-ahead"
      ],
      threats: [
        "High-risk destination on the itinerary",
        "Missed connection in a higher-risk hub",
        "Crime at the arrival transfer",
        "Civil unrest at a stopover",
        "Medical emergency mid-itinerary",
        "Lost communications with the traveller",
        "Ground handler or hotel failure",
        "Natural hazard disrupting a leg",
        "Traveller-manifest data leak",
        "Duty-of-care approval gap"
      ],
      controls: [
        control("approval", "Pre-trip approval before ticketing", "The destination rating is checked and the trip is approved before ticketing. A higher advisory pauses the ticket.", 2, 2, ["High-risk destination on the itinerary", "Duty-of-care approval gap"]),
        control("reroute", "Alternate hub if a stopover advisory rises", "Each leg has an alternate hub. The desk reroutes if a stopover advisory rises or a connection is missed.", 2, 3, ["Missed connection in a higher-risk hub", "Civil unrest at a stopover", "Natural hazard disrupting a leg"]),
        control("transfer", "Vetted meet-and-greet, no street hail", "Arrival is a vetted meet-and-greet booked by the TMC. No street hail.", 2, 2, ["Crime at the arrival transfer", "Ground handler or hotel failure"]),
        control("assist", "Assistance provider confirmed for every country", "Medical and security assistance is confirmed for every country on the ticket before departure.", 2, 2, ["Medical emergency mid-itinerary"]),
        control("checkin", "Duty desk check-in on every leg", "The traveller checks in to the duty desk on landing and at departure for every leg.", 2, 2, ["Lost communications with the traveller"]),
        control("manifest", "Manifest held by the TMC, not in this file", "The traveller manifest stays on the TMC system. This assessment records the programme, not the names.", 1, 2, ["Traveller-manifest data leak"])
      ],
      transport: ["Commercial air", "Vetted meet-and-greet transfer", "Licensed taxi booked by the TMC", "Rail on a pre-approved corridor", "No self-drive in an elevated destination"],
      accommodation: ["Company-preferred hotel", "Hotel inside a secure compound", "Serviced apartment on the approved list", "Airport hotel for a tight connection"],
      comms: ["Duty-desk check-in on landing", "Assistance-provider app", "SMS fallback if data roaming fails", "Check-in cadence: each leg, arrival and departure", "Encrypted channel for the duty desk"],
      owners: ["Duty-of-care desk", "Travel manager", "Programme lead", "Client security"],
      contextTitle: "Programme context",
      contextNote: "Traveller names and emergency contacts stay in the local-only section.",
      context: [
        { id: "pattern", label: "Itinerary pattern", options: ["Single destination", "Multi-leg", "Open jaw"] },
        { id: "approval", label: "Approval state", options: ["Pre-approved", "Pending", "Exception"] },
        { id: "assistance", label: "Assistance provider", options: ["Confirmed", "Not confirmed"] }
      ],
      localLabels: {
        heading: "Traveller Manifest and Emergency Contacts (Local-Only)",
        names: "Traveller manifest (Local-Only)",
        contacts: "Duty-of-care desk and traveller emergency contacts (Local-Only)",
        medical: "Assistance policy and medical notes (Local-Only)",
        passport: "Passport and visa status (Local-Only)"
      }
    },
    {
      id: "insurance",
      label: "Insurance & Compliance",
      summary: "Underwriting evidence packs, ISO 31030 audit trail, and claims or policy duty of care.",
      templates: [
        template("evidence", "Underwriting Evidence Pack", "ISO 31000 · ISO 31030 · Underwriting Evidence Pack"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("claims", "Claims / Policy Duty of Care", "ISO 31030 · Claims / Policy Duty of Care"),
        template("audit", "ISO 31030 Audit Trail", "ISO 31030 · ISO 45001 · Audit Trail"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Underwriting evidence pack",
        "Pre-bind travel risk opinion",
        "Claims duty-of-care review",
        "Policy wording audit",
        "Kidnap and ransom exposure note",
        "Group scheme risk rating",
        "Post-incident claims narrative"
      ],
      threats: [
        "Undisclosed high-risk activity",
        "Kidnap and ransom exposure",
        "Medical evacuation cost escalation",
        "War or terror exclusion trigger",
        "Duty-of-care audit-trail gap",
        "Aggregation of insureds in one city",
        "Sanctions or prohibited destination",
        "Late incident notification",
        "Documentation gap for a claim",
        "Advisory change mid-policy"
      ],
      controls: [
        control("pack", "Evidence pack filed before bind", "Advisory level, the 7-day incident log and a named control owner are on file before bind. No personal data is stored in the pack.", 2, 2, ["Undisclosed high-risk activity", "Duty-of-care audit-trail gap", "Documentation gap for a claim"]),
        control("wording", "Activity matched to the wording and exclusions", "The declared activity is matched to the policy wording. War, terror and high-risk activity exclusions are explicit.", 2, 3, ["War or terror exclusion trigger", "Kidnap and ransom exposure"]),
        control("agg", "Aggregation kept inside the treaty cap", "Insureds in one city stay inside the treaty aggregation cap. A breach stops new bindings to that city.", 2, 3, ["Aggregation of insureds in one city"]),
        control("medevac", "Assistance and medevac named on the schedule", "The assistance provider and the medevac trigger are named on the schedule. Cost escalation follows that trigger, not an ad-hoc evacuation.", 2, 2, ["Medical evacuation cost escalation"]),
        control("notify", "Notification timeline written into the procedure", "The incident notification timeline is written into the duty-of-care procedure and briefed before travel.", 2, 2, ["Late incident notification", "Advisory change mid-policy"]),
        control("sanctions", "Sanctions and destination screen completed", "The sanctions and destination screen is completed. The result is stored without names or document numbers.", 1, 3, ["Sanctions or prohibited destination"])
      ],
      transport: ["Declared commercial air", "Declared ground transfer", "Self-drive excluded unless endorsed", "Charter only on a separate schedule", "B6 armoured where the wording endorses it"],
      accommodation: ["Declared hotel", "Compound or camp declared to underwriters", "Staff housing on the schedule", "Undeclared lodging treated as outside the warranty"],
      comms: ["Assistance provider 24/7 number", "Incident notification within the policy hours", "Check-in cadence stated in the travel procedure", "Encrypted channel for claims notes with no medical detail"],
      owners: ["Underwriter", "Claims lead", "Compliance officer", "Risk manager"],
      contextTitle: "Underwriting context",
      contextNote: "Insured names, policy numbers and medical notes stay in the local-only section.",
      context: [
        { id: "opinion", label: "Opinion type", options: ["Pre-bind", "Mid-term", "Claims"] },
        { id: "declared", label: "Activity declared", options: ["Yes", "Partial", "No"] },
        { id: "aggregation", label: "Aggregation", options: ["Within treaty", "Near cap", "Above cap"] }
      ],
      localLabels: {
        heading: "Insured Names and Policy References (Local-Only)",
        names: "Insured names and roles (Local-Only)",
        contacts: "Broker, assistance provider and claimant contacts (Local-Only)",
        medical: "Policy medical notes (Local-Only)",
        passport: "Policy number and identity references (Local-Only)"
      }
    },
    {
      id: "ngo",
      label: "NGOs & Humanitarian",
      summary: "Aid convoys, field clinics, remote camp operations, and access or negotiation.",
      templates: [
        template("access", "Humanitarian Access & Duty of Care", "ISO 31030 · Humanitarian Access & Duty of Care"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("clinic", "Field Clinic Safety", "ISO 45001 · ISO 31030"),
        template("camp", "Remote Camp Operations", "ISO 31030 · ISO 22361"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Aid convoy",
        "Field clinic deployment",
        "Remote camp operations",
        "Access / negotiation mission",
        "Needs assessment",
        "Distribution in a contested area",
        "Staff hibernation or relocation"
      ],
      threats: [
        "Access denial or blockade",
        "Crossfire/UXO on the convoy route",
        "Kidnap of national staff",
        "Checkpoint harassment",
        "Crowd at a distribution",
        "Communications failure",
        "Aid diversion",
        "Clinic overrun or looting",
        "Outbreak inside the camp",
        "Sexual exploitation and abuse"
      ],
      controls: [
        control("deconflict", "Humanitarian notification filed before movement", "Humanitarian notification or deconfliction is filed before the convoy moves. If it is refused, the movement does not start.", 2, 3, ["Access denial or blockade", "Checkpoint harassment"]),
        control("pace", "Unmarked convoy, no night movement, PACE route", "Unmarked soft-skin vehicles, no night movement, and a primary plus alternate route. Stop if the road is contested.", 2, 4, ["Crossfire/UXO on the convoy route"]),
        control("parity", "National staff on the same medevac and check-in standard", "National staff have the same medevac cover and check-in standard as international staff.", 2, 3, ["Kidnap of national staff"]),
        control("crowd", "Distribution crowd plan and an abort trigger", "A community liaison is briefed. The distribution has a crowd plan and a trigger to stop if the site is overwhelmed.", 2, 3, ["Crowd at a distribution", "Aid diversion", "Clinic overrun or looting"]),
        control("radio", "Radio check before departure and on arrival", "VHF check before departure and on arrival. A satellite phone or Garmin inReach covers the last mile.", 2, 2, ["Communications failure"]),
        control("psea", "Safeguarding and PSEA briefing completed", "Safeguarding and PSEA briefing is complete. The reporting line is confirmed. Case details are not typed into this assessment.", 2, 2, ["Sexual exploitation and abuse", "Outbreak inside the camp"])
      ],
      transport: ["Soft-skin convoy", "Vetted local driver", "Single vehicle, low profile", "Commercial air into the hub, road for the last mile", "No night movement"],
      accommodation: ["Organisation guesthouse", "Camp staff compound", "Vetted hotel in the hub city", "Hibernation location identified", "No lodging with the cargo"],
      comms: ["VHF", "Satellite phone", "Garmin inReach", "Check-in cadence: departure, each checkpoint, and arrival", "Encrypted Signal to the country director"],
      owners: ["Security focal point", "Convoy leader", "Country director", "Medic"],
      contextTitle: "Humanitarian access",
      contextNote: "Staff, driver and partner names stay in the local-only section.",
      context: [
        { id: "notification", label: "Humanitarian notification", options: ["Filed", "Not required", "Outstanding"] },
        { id: "profile", label: "Movement profile", options: ["Single vehicle", "Convoy", "Last-mile only"] },
        { id: "parity", label: "National staff cover", options: ["Same as international", "Partial", "Gap"] }
      ],
      localLabels: {
        heading: "Staff, Drivers and Local Partner Names (Local-Only)",
        names: "Staff, drivers and local partner names (Local-Only)",
        contacts: "Country director, security focal point and next-of-kin (Local-Only)",
        medical: "Staff medical notes, blood type and allergies (Local-Only)",
        passport: "Organisation ID and passport reference (Local-Only)"
      }
    },
    {
      id: "psc",
      label: "Private Security Companies (PSCs) & Ops Rooms",
      summary: "Close protection, residential and site security, secure transit, and GSOC.",
      templates: [
        template("cp", "Close Protection SOP", "ISO 18788 · Close Protection SOP"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("site", "Residential / Site Security", "ISO 18788 · Residential / Site Security"),
        template("gsoc", "GSOC Operating Standard", "ISO 18788 · ISO 22361 · GSOC"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Close protection advance",
        "Residential / site security",
        "Secure transit",
        "GSOC overwatch",
        "Venue security",
        "Airport meet and move",
        "Static guard and access control"
      ],
      threats: [
        "Attack on the route",
        "Hostile surveillance",
        "Principal separated from the detail",
        "Evacuation route blocked",
        "Communications failure with GSOC",
        "Unsafe hospital on the medical plan",
        "Insider threat",
        "Armed robbery at the site",
        "Venue not viable",
        "Protest at the venue"
      ],
      controls: [
        control("advance", "Primary and alternate routes advanced with timings", "Primary and alternate routes are advanced with timings. The detail aborts if the pattern of surveillance repeats.", 2, 3, ["Attack on the route", "Hostile surveillance"]),
        control("stack", "Detail stays stacked; lost-principal drill briefed", "The detail stays in contact with the principal. The lost-principal drill is briefed before wheels roll.", 2, 3, ["Principal separated from the detail"]),
        control("evac", "Ground evacuation and a viable hospital rehearsed", "The ground evacuation and a viable hospital are rehearsed. A blocked route switches to the alternate without debate.", 2, 3, ["Evacuation route blocked", "Unsafe hospital on the medical plan"]),
        control("gsoc", "GSOC live before wheels roll; lost-comms drill briefed", "GSOC tracking is live before wheels roll. The lost-comms drill and the duress word sit with GSOC, not in this file.", 2, 2, ["Communications failure with GSOC"]),
        control("access", "Access control, visitor log and a tested panic alarm", "The site has one controlled entrance, a visitor log and a panic alarm tested on the shift. People not on the list do not enter.", 2, 2, ["Insider threat", "Armed robbery at the site"]),
        control("venue", "Venue dropped if the advance fails", "If the advance fails or a protest forms, the venue is dropped and the principal returns to the surveyed residence.", 2, 3, ["Venue not viable", "Protest at the venue"])
      ],
      transport: ["B6 armoured", "Soft-skin low-profile", "Two-car detail", "Secure transit with a lead and a follow", "Commercial air", "Sterile airport arrival"],
      accommodation: ["Principal residence, already surveyed", "Hotel suite with controlled lift access", "Safe house", "Site accommodation inside the wire"],
      comms: ["Encrypted GSOC net", "VHF", "Satellite phone", "Garmin inReach", "Check-in cadence: every stop and every 30 minutes on the move"],
      owners: ["Team leader", "Advance", "GSOC", "Medic", "Site supervisor"],
      contextTitle: "ISO 18788 advance",
      contextNote: "Principal names and callsigns stay in the local-only section.",
      context: [
        { id: "route", label: "Route type", options: ["Road", "Air", "Mixed"] },
        { id: "venue", label: "Venue", options: ["Hotel", "Residence", "Office", "Event site", "Site / compound"] },
        { id: "evac", label: "Evacuation method", options: ["Road", "Rotary", "Fixed wing", "Armoured"] }
      ],
      localLabels: {
        heading: "Principal & Close-Protection Callsigns (Local-Only)",
        names: "Principal & Close-Protection Callsigns (Local-Only)",
        contacts: "GSOC duty manager and principal emergency contact (Local-Only)",
        medical: "Principal medical flags for the medic (Local-Only)",
        passport: "Principal travel document reference (Local-Only)"
      }
    },
    {
      id: "schools",
      label: "Schools & Universities (BS 8848)",
      summary: "Overseas educational trips, Duke of Edinburgh and field studies, safeguarding, ratios, and UK DfE / OEAP.",
      templates: [
        template("bs8848", "BS 8848 Educational Visits", "BS 8848 · UK DfE / OEAP · Educational Visits"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030 · BS 8848"),
        template("dofe", "Duke of Edinburgh / Field Studies", "BS 8848 · Duke of Edinburgh / Field Studies"),
        template("exchange", "Exchange and Study-Abroad Safeguarding", "BS 8848 · ISO 31030 · Safeguarding"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Cultural City Visit",
        "Remote Fieldwork / Expedition",
        "Exchange Programme",
        "Duke of Edinburgh expedition",
        "Sports fixture overseas",
        "Curriculum fieldwork",
        "University study-abroad placement"
      ],
      threats: [
        "Safeguarding/Supervision Gap",
        "Medical/Allergy Emergency",
        "Lost Student",
        "Coach/Road Traffic",
        "Crime against the group",
        "Lost communications with the school",
        "Political unrest at the destination",
        "Water or weather on fieldwork",
        "Overnight supervision failure",
        "Inappropriate contractor or host"
      ],
      controls: [
        control("ratios", "Ratios and headcount at every move", "Staff to student ratios meet BS 8848 and OEAP for the age and the activity. Headcount is taken at every move.", 2, 2, ["Safeguarding/Supervision Gap", "Overnight supervision failure"]),
        control("medical", "Allergy and consent forms held by the visit leader on paper", "Allergy, medication and consent forms are held by the visit leader on paper. They are not typed into this assessment or sent to Grok.", 2, 2, ["Medical/Allergy Emergency"]),
        control("buddy", "Buddy system and a rendezvous briefed before free time", "Students move in buddies. A rendezvous and a cut-off time are briefed before any free time.", 2, 2, ["Lost Student", "Crime against the group"]),
        control("coach", "Licensed coach, seatbelts, no night road leg", "The coach is a licensed operator with seatbelts. There is no night road leg. Staff sit at the front and the rear.", 2, 3, ["Coach/Road Traffic"]),
        control("school", "Visit leader phone and a 24-hour school contact", "The visit leader phone and a 24-hour school contact are live. Students do not post the hotel location.", 2, 2, ["Lost communications with the school"]),
        control("weather", "Weather and water turn-back trigger", "Fieldwork has a weather and water limit. The group turns back when the limit is met. Contractors are on the school's approved list.", 2, 3, ["Water or weather on fieldwork", "Political unrest at the destination", "Inappropriate contractor or host"])
      ],
      transport: ["School coach", "Commercial air", "Public transport with staff at front and rear", "Walking route recce'd in daylight", "Minibus with a second driver", "No student self-drive"],
      accommodation: ["School-approved hotel, staff rooms adjoining students", "Host family, already checked", "Bunkhouse or field centre", "University residence", "Rooms arranged to the school's policy"],
      comms: ["School emergency phone", "Visit leader phone", "Check-in cadence: morning, after each move, and lights out", "Staff-only group message", "Garmin inReach on remote fieldwork"],
      owners: ["Visit leader", "Head teacher", "Safeguarding lead", "Coach operator"],
      contextTitle: "BS 8848 / study-abroad",
      contextNote: "Pupil names, allergies and parent contacts stay in the local-only section and are not uploaded.",
      context: [
        { id: "age", label: "Age range", options: ["Under 16", "16–18", "18+"] },
        { id: "ratio", label: "Staff to student ratio", options: ["1:6", "1:8", "1:10", "1:15"] },
        { id: "lead", label: "Safeguarding lead role", options: ["Visit leader", "Head of year", "University staff", "External provider"] }
      ],
      localLabels: {
        heading: "Pupil Roster, Allergies & Parent Emergency Contacts (Local-Only)",
        names: "Pupil roster (Local-Only)",
        contacts: "Parent emergency contacts (Local-Only)",
        medical: "Allergies, medication and medical consent (Local-Only)",
        passport: "Pupil passport and consent references (Local-Only)"
      }
    },
    {
      id: "private",
      label: "Private Individuals & Expats",
      summary: "Solo travel, relocation, and independent consultants.",
      templates: [
        template("personal", "ISO 31030 Personal Travel", "ISO 31030 · Personal Travel"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("relocation", "Relocation Risk Assessment", "ISO 31030 · Relocation"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Solo travel",
        "Relocation",
        "Independent consultant assignment",
        "Family holiday",
        "Long-stay expat",
        "Property search trip",
        "Commute between home and a project site"
      ],
      threats: [
        "Street crime and robbery",
        "Road traffic collision",
        "Health emergency far from care",
        "Scam or fraud",
        "Political unrest",
        "No check-in contact",
        "Natural hazard",
        "Housing security on relocation",
        "Lost documents",
        "Harassment"
      ],
      controls: [
        control("hail", "Licensed transport, no street hail after dark", "Use licensed transport. No street hail after dark. Walk routes that were looked at in daylight.", 2, 2, ["Street crime and robbery", "Harassment"]),
        control("drive", "Daylight driving in a hire car from a major firm", "If self-drive is unavoidable, use a major hire firm and drive in daylight only.", 2, 3, ["Road traffic collision"]),
        control("cover", "Insurance includes medical treatment and evacuation", "Insurance includes medical treatment and evacuation. The policy number stays in the local-only section.", 2, 2, ["Health emergency far from care"]),
        control("scam", "No up-front payment to an unverified party", "Do not pay a deposit or share documents with a party that was not verified independently.", 2, 2, ["Scam or fraud"]),
        control("checkin", "One home contact gets the day's plan", "One contact at home has the day's plan and a check-in on arrival and each evening.", 2, 2, ["No check-in contact", "Political unrest", "Natural hazard"]),
        control("home", "Relocation housing viewed in daylight, documents copied", "View the apartment and the route to work in daylight. Keep document copies separate from the originals.", 2, 2, ["Housing security on relocation", "Lost documents"])
      ],
      transport: ["Commercial air", "Licensed taxi or ride-hail from the airport", "Hire car from a major firm, daylight only", "Public transport on main routes", "Vetted local driver", "Walking in daylight on recce'd streets"],
      accommodation: ["Hotel in a busy, well-lit area", "Serviced apartment with a doorman", "Relocation housing after a daylight viewing", "Stay with a known host", "No ground-floor street-facing room"],
      comms: ["Phone with roaming or a local SIM from the airport", "Check-in message on arrival and each evening", "Encrypted Signal with one home contact", "Embassy registration for stays over 30 days", "Offline maps and a power bank"],
      owners: ["Traveller", "Family contact", "Local host"],
      contextTitle: "Personal travel",
      contextNote: "Names, medical notes and document numbers stay in the local-only section.",
      context: [
        { id: "pattern", label: "Trip pattern", options: ["Solo", "Family", "Relocation"] },
        { id: "insurance", label: "Insurance", options: ["Medical and evacuation", "Medical only", "None confirmed"] },
        { id: "embassy", label: "Embassy registration", options: ["Done", "Not required", "Outstanding"] }
      ],
      localLabels: {
        heading: "Traveller and Family Details (Local-Only)",
        names: "Traveller and family names (Local-Only)",
        contacts: "Home emergency contact (Local-Only)",
        medical: "Medical notes, allergies and medication (Local-Only)",
        passport: "Passport and insurance reference (Local-Only)"
      }
    },
    {
      id: "csuite",
      label: "C-Suite Executives & Family Offices",
      summary: "Executive travel, board offsites, residential and HNWI footprint, and low-profile protection.",
      templates: [
        template("doc", "Corporate Duty of Care", "ISO 31030 · ISO 22361 · Corporate Duty of Care"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("executive", "Executive Travel Risk Assessment", "ISO 31000 · ISO 31030 · ISO 22361 · Executive Travel"),
        template("lowpro", "Low-Profile Protection", "ISO 18788 · ISO 31030 · Low-Profile Protection"),
        template("pdr", "Executive Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Executive travel",
        "Board offsite",
        "Residential / HNWI footprint",
        "Family office movement",
        "Investor roadshow",
        "Site visit with the principal",
        "Public speaking appearance"
      ],
      threats: [
        "Kidnap & Ransom / Express Kidnap",
        "Executive Doxxing",
        "Targeted Protest",
        "Hostile surveillance of the residence",
        "Attack on the route",
        "Itinerary leak",
        "Intrusion at the family residence",
        "Medical emergency of the principal",
        "Insider threat among household staff",
        "Crowd at a public appearance"
      ],
      controls: [
        control("kr", "K&R cover confirmed; name kept off the shared file", "Kidnap and ransom cover is confirmed with the family office. The principal's name is not written into the shared assessment.", 2, 3, ["Kidnap & Ransom / Express Kidnap"]),
        control("doxx", "Personal accounts locked down before travel", "Personal accounts are locked down before travel. A doxxing watch runs for the travel window. Findings are not copied into this file.", 2, 2, ["Executive Doxxing", "Itinerary leak"]),
        control("protest", "Offsite relocates if a protest is announced", "If a targeted protest is announced, the offsite moves. The principal does not transit the crowd.", 2, 3, ["Targeted Protest", "Crowd at a public appearance"]),
        control("residence", "Residence advanced, routes varied, no branding", "The residence is advanced. Routes vary. Vehicles carry no branding. Repeat surveillance aborts the move.", 2, 3, ["Hostile surveillance of the residence", "Attack on the route", "Intrusion at the family residence"]),
        control("medic", "Medic plan and evacuation cover confirmed", "The medic plan and medical evacuation cover are confirmed. Clinical detail stays in the local-only section.", 2, 2, ["Medical emergency of the principal"]),
        control("staff", "Household staff vetted; access list held by the family office", "Household staff are vetted. The access list is held by the family office, not in this assessment.", 2, 2, ["Insider threat among household staff"])
      ],
      transport: ["B6 armoured", "Soft-skin low-profile", "Commercial air, private terminal", "Private aviation", "Vetted local driver", "No principal self-drive"],
      accommodation: ["Principal residence, already surveyed", "Hotel suite booked in a company name", "Private villa with access control", "Board offsite with a controlled guest list", "Family office property"],
      comms: ["Encrypted Signal on a travel handset", "Family-office duty phone", "Satellite phone in the vehicle", "Check-in cadence: wheels up, landing, and arrival at the residence", "No itinerary on personal email"],
      owners: ["Family office", "Close-protection lead", "GSOC", "Medic"],
      contextTitle: "Executive footprint",
      contextNote: "Principal names and callsigns stay in the local-only section.",
      context: [
        { id: "profile", label: "Profile", options: ["Low profile", "Visible programme", "Public appearance"] },
        { id: "advance", label: "Residential advance", options: ["Completed", "Partial", "Not done"] },
        { id: "kr", label: "Kidnap and ransom cover", options: ["Confirmed", "Not confirmed"] }
      ],
      localLabels: {
        heading: "Principal & Close-Protection Callsigns (Local-Only)",
        names: "Principal & Close-Protection Callsigns (Local-Only)",
        contacts: "Family office duty contact and principal emergency contact (Local-Only)",
        medical: "Principal and family medical notes (Local-Only)",
        passport: "Travel document references (Local-Only)"
      }
    },
    {
      id: "medical",
      label: "Medical Risk & Assistance",
      summary: "Remote clinics, medevac and repatriation teams, outbreak and infectious-disease deployments, and trauma readiness.",
      templates: [
        template("clinical", "Clinical/Outbreak & Medevac Standard", "ISO 45001 · ISO 31030 · Clinical/Outbreak & Medevac Standard"),
        template("iso", "ISO 31000 + ISO 31030", "ISO 31000 · ISO 31030"),
        template("clinic", "Remote Clinic Safety", "ISO 45001 · Remote Clinic Safety"),
        template("aero", "Aeromedical Evacuation Plan", "ISO 31030 · ISO 45001 · Aeromedical Evacuation"),
        template("pdr", "Pre-Deployment Report", "ISO 31000 · ISO 31030 · ISO 22361 · ISO 45001", true)
      ],
      operations: [
        "Outbreak Response",
        "Remote Site Medical Support",
        "Aeromedical Evacuation",
        "Trauma team deployment",
        "Repatriation escort",
        "Field hospital setup",
        "Infectious-disease assessment"
      ],
      threats: [
        "Vector/Infectious Outbreak",
        "Cold-Chain Failure",
        "Blood/Trauma Shortage",
        "Facility Insecurity",
        "Medevac delay",
        "Communications failure during transfer",
        "Needlestick or exposure",
        "Unreliable ambulance",
        "Counterfeit medicines",
        "Team member becomes a patient"
      ],
      controls: [
        control("ppe", "Clinical PPE and a donning drill for the named pathogen", "Clinical PPE matches the named pathogen and the donning drill is done before the first patient. Patient identifiers are not typed into this file.", 2, 3, ["Vector/Infectious Outbreak", "Needlestick or exposure"]),
        control("cold", "Cold chain logged twice a day with a spare cooler", "The cold chain is logged twice a day, with a spare cooler and a temperature alarm. A breach stops use of that batch.", 2, 2, ["Cold-Chain Failure", "Counterfeit medicines"]),
        control("blood", "Local blood and trauma stock confirmed before deployment", "Local blood and trauma stock, and the resupply route, are confirmed before deployment.", 2, 3, ["Blood/Trauma Shortage"]),
        control("facility", "One controlled entrance agreed with the host", "Facility security is agreed with the host. One controlled entrance. Clinical staff do not manage the gate alone.", 2, 3, ["Facility Insecurity"]),
        control("medevac", "Medevac trigger, receiving hospital and aircraft window agreed", "The medevac trigger, the receiving hospital and the aircraft window are agreed before the team deploys.", 2, 2, ["Medevac delay", "Unreliable ambulance", "Team member becomes a patient"]),
        control("line", "Open line to the assistance desk during every transfer", "The clinical lead keeps an open line to the assistance desk during every transfer. No patient detail goes on an open channel.", 2, 2, ["Communications failure during transfer"])
      ],
      transport: ["Commercial air", "Air ambulance", "Ground ambulance, dedicated", "Soft-skin for outreach, daylight only", "Road convoy with a clinical lead and a security lead"],
      accommodation: ["Clinic compound", "Team house separate from the ward", "Vetted hotel in the hub", "Aircraft staging hotel near the airport", "No sleeping in the clinical area"],
      comms: ["Open line to the assistance desk during transfer", "Satellite phone", "VHF", "Garmin inReach", "Encrypted Signal", "Check-in cadence: shift start, shift end, and wheels-up for every transfer"],
      owners: ["Clinical lead", "Medevac coordinator", "Security lead", "Facility manager"],
      contextTitle: "Destination healthcare context",
      contextNote: "Clinical capabilities, blood type and the medevac policy stay in the local-only section. Do not record patient identifiers.",
      context: [
        { id: "standard", label: "Healthcare standard", options: ["Excellent", "Good", "Moderate", "Poor", "Very Poor"] },
        { id: "ambulance", label: "Ambulance reliability", options: ["Reliable", "Uneven", "Unreliable"] },
        { id: "trauma", label: "Trauma access", options: ["Major trauma centre", "Limited", "Absent"] },
        { id: "cold", label: "Cold chain", options: ["Monitored", "Partial", "Not in use"] }
      ],
      localLabels: {
        heading: "Clinical/Trauma Capabilities, Blood Type & Medevac Policy (Local-Only)",
        names: "Clinical roster (Local-Only)",
        contacts: "Assistance desk and next-of-kin (Local-Only)",
        medical: "Clinical/Trauma Capabilities, Blood Type & Medevac Policy (Local-Only)",
        passport: "Licence to practise and passport reference (Local-Only)"
      }
    }
  ];
});
