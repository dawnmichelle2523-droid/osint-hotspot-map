import json
import os
import secrets
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from html import escape
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import checkin_beacon
import checkins
import preview_lock
from checkin_beacon import SCHOOLS_NOTICE, ZERO_PII_NOTICE, format_utc, utcnow

WEBHOOK = "https://hooks.slack.com/services/T000TEST1/B000TEST1/SECRETTOKENNOTINRESPONSES"
SECRET = "SECRETTOKENNOTINRESPONSES"


def make_app() -> Flask:
    app = Flask(__name__)
    app.testing = True
    preview_lock.install(app)
    app.register_blueprint(checkin_beacon.checkin_bp)

    @app.get("/pricing")
    def pricing():
        return "Pricing"

    return app


class CheckinBeaconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {}
        for name in (
            "CHECKIN_DB_PATH", "CHECKIN_SLACK_SYNC", "CHECKIN_RETENTION_DAYS", "DISABLE_BACKGROUND",
            "PREVIEW_PASSWORD", "PREVIEW_USER", "BASIC_AUTH_PASSWORD", "BASIC_AUTH_USERNAME",
        ):
            self.saved[name] = os.environ.get(name)
        os.environ["CHECKIN_DB_PATH"] = str(Path(self.tmp.name) / "checkins.db")
        os.environ["CHECKIN_SLACK_SYNC"] = "1"
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ.pop("CHECKIN_RETENTION_DAYS", None)
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)
        checkin_beacon.reset_store()
        self.app = make_app()
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        checkin_beacon.reset_store()
        self.tmp.cleanup()
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _create(self, **overrides):
        payload = {
            "callsign": "CREW-BEIRUT-1",
            "sector": "ngo",
            "cadence_minutes": 60,
            "slack_webhook_url": WEBHOOK,
            "adult_staff_only": True,
            "schools_ack": False,
        }
        payload.update(overrides)
        return self.client.post("/api/checkins", json=payload)

    def test_database_path_prefers_writable_data_mount(self) -> None:
        os.environ.pop("CHECKIN_DB_PATH", None)
        mount = Path(self.tmp.name) / "data"
        mount.mkdir()
        self.assertEqual(checkin_beacon.database_path(mount=mount), mount / "checkins.db")
        missing = Path(self.tmp.name) / "missing"
        self.assertEqual(checkin_beacon.database_path(mount=missing), Path("checkins.db"))
        os.environ["CHECKIN_DB_PATH"] = str(Path(self.tmp.name) / "override.db")
        self.assertEqual(checkin_beacon.database_path(mount=mount), Path(self.tmp.name) / "override.db")

    def test_token_creation_is_high_entropy_and_unique(self) -> None:
        first = self._create()
        second = self._create(callsign="TEAM-ALPHA-01", sector="media")
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        path_a = first.get_json()["beacon"]["mobile_path"]
        path_b = second.get_json()["beacon"]["mobile_path"]
        token_a = path_a.rsplit("/", 1)[-1]
        token_b = path_b.rsplit("/", 1)[-1]
        self.assertNotEqual(token_a, token_b)
        self.assertGreaterEqual(len(token_a), 40)
        self.assertRegex(token_a, r"^[A-Za-z0-9_-]{40,}$")
        self.assertTrue(preview_lock.is_public_checkin_path("/c/" + token_a))
        self.assertTrue(checkin_beacon.valid_token(token_a))
        self.assertEqual(first.get_json()["beacon"]["callsign"], "CREW-BEIRUT-1")
        self.assertEqual(second.get_json()["beacon"]["callsign"], "TEAM-ALPHA-01")
        duplicate = self._create()
        self.assertEqual(duplicate.status_code, 409)

    def test_zero_pii_masking_and_rejection(self) -> None:
        created = self._create(callsign="leader-paris-bus2", sector="schools", schools_ack=True)
        self.assertEqual(created.status_code, 200, created.get_json())
        body = created.get_data(as_text=True)
        self.assertNotIn(SECRET, body)
        self.assertNotIn("slack_webhook_url", created.get_json()["beacon"])
        self.assertEqual(created.get_json()["beacon"]["slack_webhook_masked"], checkin_beacon.MASKED_WEBHOOK)
        self.assertTrue(created.get_json()["beacon"]["slack_configured"])
        self.assertEqual(created.get_json()["beacon"]["callsign"], "LEADER-PARIS-BUS2")

        listed = self.client.get("/api/checkins")
        self.assertEqual(listed.status_code, 200)
        listed_body = listed.get_data(as_text=True)
        self.assertNotIn(SECRET, listed_body)
        self.assertNotIn('"slack_webhook_url"', listed_body)
        self.assertIn(checkin_beacon.MASKED_WEBHOOK, listed.get_json()["beacons"][0]["slack_webhook_masked"])
        self.assertEqual(listed.get_json()["notice"], SCHOOLS_NOTICE)
        self.assertIn("TEAM-ALPHA-01", listed.get_json()["zero_pii"])

        token = created.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        page = self.client.get("/c/" + token)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(SECRET.encode(), page.data)
        self.assertNotIn(b"slack_webhook", page.data)
        self.assertIn(b"LEADER-PARIS-BUS2", page.data)

        rejected = {
            "leader@school.org": 400,
            "MARY JONES": 400,
            "JOHN-SMITH": 400,
            "PUPIL-BUS-1": 400,
            "STUDENT-01": 400,
            "447911123456": 400,
            "CREW-1234567": 400,
        }
        for callsign in rejected:
            response = self._create(callsign=callsign, sector="media", slack_webhook_url="")
            self.assertGreaterEqual(response.status_code, 400, callsign)
            self.assertNotIn(SECRET, response.get_data(as_text=True))
        pupil = self._create(callsign="PUPIL-BUS-1", sector="schools", schools_ack=True, slack_webhook_url="")
        self.assertEqual(pupil.status_code, 400)
        self.assertIn(SCHOOLS_NOTICE, pupil.get_json()["message"])

        note = self.client.post(
            "/api/checkin/tap/" + token,
            json={"event": "SAFE", "note": "mail jane@school.org"},
        )
        self.assertEqual(note.status_code, 400)
        self.assertIn("email", note.get_json()["message"].lower())
        phone = self.client.post(
            "/api/checkin/tap/" + token,
            json={"event": "SAFE", "note": "call +44 7911 123456"},
        )
        self.assertEqual(phone.status_code, 400)
        with sqlite3.connect(os.environ["CHECKIN_DB_PATH"]) as conn:
            callsigns = [row[0] for row in conn.execute("SELECT callsign FROM beacons")]
            notes = [row[0] or "" for row in conn.execute("SELECT note FROM checkin_events")]
            hooks = [row[0] or "" for row in conn.execute("SELECT slack_webhook_url FROM beacons")]
        self.assertEqual(callsigns, ["LEADER-PARIS-BUS2"])
        self.assertNotIn("PUPIL-BUS-1", callsigns)
        self.assertTrue(all("@" not in note and "7911" not in note for note in notes))
        self.assertEqual(hooks, [WEBHOOK])

    def test_schools_ack_and_adult_staff_gate(self) -> None:
        missing_adult = self._create(adult_staff_only=False, slack_webhook_url="")
        self.assertEqual(missing_adult.status_code, 400)
        self.assertIn("adult", missing_adult.get_json()["message"].lower())
        missing_schools = self._create(sector="schools", schools_ack=False, slack_webhook_url="", callsign="LEADER-PARIS-BUS2")
        self.assertEqual(missing_schools.status_code, 400)
        self.assertEqual(missing_schools.get_json()["message"], SCHOOLS_NOTICE)
        bad_hook = self._create(slack_webhook_url="https://example.com/hook")
        self.assertEqual(bad_hook.status_code, 400)
        bad_hook_internal = self._create(slack_webhook_url="http://127.0.0.1/services/T000TEST1/B000TEST1/SECRETTOKENNOTINRESPONSES")
        self.assertEqual(bad_hook_internal.status_code, 400)
        bad_cadence = self._create(cadence_minutes=90, slack_webhook_url="")
        self.assertEqual(bad_cadence.status_code, 400)

    def test_safe_assistance_and_panic_taps(self) -> None:
        created = self._create()
        token = created.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        tap = "/api/checkin/tap/" + token
        with patch("checkin_beacon.post_slack") as post:
            safe = self.client.post(tap, json={"event": "SAFE", "note": "AT HOTEL", "lat": 33.893, "lng": 35.501})
            self.assertEqual(safe.status_code, 200, safe.get_json())
            self.assertEqual(safe.get_json()["status"], "OK")
            self.assertEqual(safe.get_json()["badge"], "On Time")
            self.assertEqual(post.call_count, 0)
            self.assertNotIn(SECRET, safe.get_data(as_text=True))
            due = safe.get_json()["next_due_utc"]
            self.assertAlmostEqual(
                checkin_beacon.parse_utc(due).timestamp(),
                (utcnow() + timedelta(minutes=60)).timestamp(),
                delta=5,
            )
            listed = self.client.get("/api/checkins")
            self.assertEqual(listed.get_json()["beacons"][0]["last_approx_region"], "33.9,35.5")
            self.assertEqual(listed.get_json()["events"][0]["region"], "33.9,35.5")
            self.assertEqual(listed.get_json()["events"][0]["note"], "AT HOTEL")
            self.assertNotIn(SECRET, listed.get_data(as_text=True))

            assistance = self.client.post(tap, json={"event": "ASSISTANCE", "note": "NEED WATER"})
            self.assertEqual(assistance.status_code, 200, assistance.get_json())
            self.assertEqual(assistance.get_json()["badge"], "Assistance")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args[0][0], WEBHOOK)
            self.assertIn("ASSISTANCE", post.call_args[0][1]["text"])
            self.assertNotIn(SECRET, json.dumps(post.call_args[0][1]))

            unconfirmed = self.client.post(tap, json={"event": "PANIC"})
            self.assertEqual(unconfirmed.status_code, 400)
            self.assertIn("second", unconfirmed.get_json()["message"].lower())
            self.assertEqual(post.call_count, 1)

            panic = self.client.post(tap, json={"event": "PANIC", "confirm": True})
            self.assertEqual(panic.status_code, 200, panic.get_json())
            self.assertEqual(panic.get_json()["status"], "PANIC")
            self.assertEqual(panic.get_json()["badge"], "PANIC")
            self.assertEqual(post.call_count, 2)
            self.assertIn("PANIC", post.call_args[0][1]["text"])
            self.assertNotIn(SECRET, panic.get_data(as_text=True))

        form = self.client.post(tap, data={"event": "SAFE", "note": "ON FOOT"})
        self.assertEqual(form.status_code, 303)
        self.assertIn("ack=SAFE", form.headers["Location"])
        followed = self.client.post(tap, data={"event": "SAFE"}, follow_redirects=True)
        self.assertEqual(followed.status_code, 200)
        self.assertIn(b"Safe check-in recorded", followed.data)
        naked_panic = self.client.post(tap, data={"event": "PANIC"})
        self.assertEqual(naked_panic.status_code, 400)
        self.assertIn(b"second", naked_panic.data.lower())

    def test_overdue_sweep_is_deduplicated(self) -> None:
        created = self._create(callsign="TEAM-ALPHA-01", cadence_minutes=120)
        beacon_id = created.get_json()["beacon"]["id"]
        future = utcnow() + timedelta(days=3)
        with patch("checkin_beacon.post_slack") as post:
            self.assertEqual(checkin_beacon.sweep_and_alert(now=future), 1)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args[0][0], WEBHOOK)
            self.assertIn("OVERDUE", post.call_args[0][1]["text"])
            self.assertNotIn(SECRET, json.dumps(post.call_args[0][1]))
            self.assertEqual(checkin_beacon.sweep_and_alert(now=future), 0)
            self.assertEqual(post.call_count, 1)
            events = checkin_beacon.get_store().recent_events()
            self.assertEqual([item["event_type"] for item in events if item["event_type"] == "OVERDUE_ALERT"], ["OVERDUE_ALERT"])
            board = self.client.get("/api/checkins")
            self.assertEqual(board.get_json()["beacons"][0]["badge"], "Overdue")
            self.assertEqual(board.get_json()["beacons"][0]["status"], "OVERDUE")

            safe = self.client.post(
                created.get_json()["beacon"]["mobile_path"].replace("/c/", "/api/checkin/tap/"),
                json={"event": "SAFE"},
            )
            self.assertEqual(safe.status_code, 200)
            self.assertEqual(checkin_beacon.sweep_and_alert(now=future), 1)
            self.assertEqual(post.call_count, 2)
            overdue_events = [item for item in checkin_beacon.get_store().recent_events() if item["event_type"] == "OVERDUE_ALERT"]
            self.assertEqual(len(overdue_events), 2)

        resumed = self._create(callsign="OPS-DESK-4", slack_webhook_url=WEBHOOK)
        paused_id = resumed.get_json()["beacon"]["id"]
        self.client.post(f"/api/checkins/{paused_id}/pause")
        with patch("checkin_beacon.post_slack") as post:
            self.assertEqual(checkin_beacon.sweep_and_alert(now=future), 0)
            self.assertEqual(post.call_count, 0)
        paused = self.client.get("/api/checkins").get_json()["beacons"]
        paused_row = next(item for item in paused if item["id"] == paused_id)
        self.assertEqual(paused_row["status"], "PAUSED")
        self.assertEqual(paused_row["badge"], "Paused")
        blocked = self.client.post("/api/checkin/tap/" + paused_row["mobile_path"].rsplit("/", 1)[-1], json={"event": "SAFE"})
        self.assertEqual(blocked.status_code, 409)

        checkin_beacon.get_store().expire_window(beacon_id)
        with patch("checkin_beacon.post_slack") as post:
            first = self.client.post("/api/checkins/sweep-overdue")
            second = self.client.post("/api/checkins/sweep-overdue")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["alerts"], 0)
        self.assertEqual(second.get_json()["alerts"], 0)
        self.assertEqual(post.call_count, 0)
        self.client.post(f"/api/checkins/{beacon_id}/resume")
        checkin_beacon.get_store().expire_window(beacon_id)
        with patch("checkin_beacon.post_slack") as post:
            http_first = self.client.post("/api/checkins/sweep-overdue")
            http_second = self.client.post("/api/checkins/sweep-overdue")
        self.assertEqual(http_first.get_json()["alerts"], 1)
        self.assertEqual(http_second.get_json()["alerts"], 0)
        self.assertEqual(post.call_count, 1)
        self.client.delete(f"/api/checkins/{beacon_id}")
        self.assertEqual(self.client.get(created.get_json()["beacon"]["mobile_path"]).status_code, 404)

    def test_pages_return_200_and_show_the_schools_rule(self) -> None:
        ops = self.client.get("/check-in")
        self.assertEqual(ops.status_code, 200)
        self.assertIn(escape(SCHOOLS_NOTICE).encode(), ops.data)
        self.assertIn(escape(ZERO_PII_NOTICE).encode(), ops.data)
        self.assertIn(b"Create callsign", ops.data)
        self.assertIn(b"Schools", ops.data)
        self.assertIn(b"UK/EU GDPR Pseudonymisation Notice", ops.data)
        self.assertIn(b"Art. 5(1)(e)", ops.data)
        self.assertIn(b"Art. 17", ops.data)
        self.assertIn(b"Data Controller", ops.data)
        self.assertIn(b"after 30 days", ops.data)
        self.assertIn(b"Purge Event Log", ops.data)
        created = self._create(callsign="LEADER-PARIS-BUS2", sector="schools", schools_ack=True, slack_webhook_url="")
        token = created.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        mobile = self.client.get("/c/" + token)
        self.assertEqual(mobile.status_code, 200)
        self.assertIn(b"TAP TO CONFIRM SAFE", mobile.data)
        self.assertIn(b"Need Non-Urgent Assistance", mobile.data)
        self.assertIn(b"HOLD / 2-TAP PANIC BEACON", mobile.data)
        self.assertIn(escape(SCHOOLS_NOTICE).encode(), mobile.data)
        self.assertIn("pupils/minors".encode(), mobile.data)
        self.assertIn("\u2014".encode(), SCHOOLS_NOTICE.encode())
        armed = self.client.get("/c/" + token + "?arm=panic")
        self.assertEqual(armed.status_code, 200)
        self.assertIn(b"TAP AGAIN TO SEND PANIC", armed.data)
        missing = self.client.get("/c/" + secrets.token_urlsafe(32))
        self.assertEqual(missing.status_code, 404)
        self.assertIn(escape(SCHOOLS_NOTICE).encode(), missing.data)
        self.assertNotIn(token.encode(), missing.data)

    def test_field_link_bypasses_preview_lock_and_ops_room_does_not(self) -> None:
        created = self._create(slack_webhook_url="")
        token = created.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        os.environ["PREVIEW_PASSWORD"] = "correct-horse"
        locked = self.client.get("/check-in")
        self.assertEqual(locked.status_code, 302)
        self.assertIn("/login", locked.headers["Location"])
        pricing = self.client.get("/pricing")
        self.assertEqual(pricing.status_code, 302)
        api = self.client.get("/api/checkins")
        self.assertEqual(api.status_code, 401)
        sweep = self.client.post("/api/checkins/sweep-overdue")
        self.assertEqual(sweep.status_code, 401)
        purge = self.client.post("/api/checkins/purge-events")
        self.assertEqual(purge.status_code, 401)
        self.assertFalse(preview_lock.is_public_checkin_path("/api/checkins/purge-events"))
        self.assertFalse(preview_lock.is_public_checkin_path("/check-in"))
        self.assertFalse(preview_lock.is_public_checkin_path("/pricing"))
        self.assertFalse(preview_lock.is_public_checkin_path("/api/checkins"))
        self.assertFalse(preview_lock.is_public_checkin_path("/c/short"))
        mobile = self.client.get("/c/" + token)
        self.assertEqual(mobile.status_code, 200)
        self.assertIn(b"TAP TO CONFIRM SAFE", mobile.data)
        tap = self.client.post("/api/checkin/tap/" + token, json={"event": "SAFE"})
        self.assertEqual(tap.status_code, 200, tap.get_json())
        self.assertEqual(tap.get_json()["badge"], "On Time")
        unknown = self.client.get("/c/" + secrets.token_urlsafe(32))
        self.assertEqual(unknown.status_code, 404)
        self.assertNotIn("/login", unknown.headers.get("Location", ""))

    def _restamp(self, note: str, created_at: str, callsign: str | None = None, last_checkin: str | None = None) -> None:
        store = checkin_beacon.get_store()

        def work():
            store._conn.execute("UPDATE checkin_events SET created_at = ? WHERE note = ?", (created_at, note))
            if callsign is not None and last_checkin is not None:
                store._conn.execute(
                    "UPDATE beacons SET last_checkin_utc = ?, last_note = ? WHERE callsign = ?",
                    (last_checkin, note, callsign),
                )

        store._transaction(work)

    def test_retention_days_default_and_override(self) -> None:
        os.environ.pop("CHECKIN_RETENTION_DAYS", None)
        self.assertEqual(checkins.retention_days(), 30)
        os.environ["CHECKIN_RETENTION_DAYS"] = "14"
        self.assertEqual(checkins.retention_days(), 14)
        os.environ["CHECKIN_RETENTION_DAYS"] = "nope"
        self.assertEqual(checkins.retention_days(), 30)
        self.assertIn("14 days", checkins.gdpr_notice(14))
        self.assertIn("Data Controller", checkins.gdpr_notice())
        self.assertIn("Art. 5(1)(e)", checkins.gdpr_notice())
        self.assertIn("Art. 17", checkins.gdpr_notice())

    def test_thirty_day_retention_purge_keeps_newer_events(self) -> None:
        moment = utcnow()
        created = self._create(slack_webhook_url="")
        token = created.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        tap = "/api/checkin/tap/" + token
        self.assertEqual(self.client.post(tap, json={"event": "SAFE", "note": "OLD WINDOW"}).status_code, 200)
        self.assertEqual(self.client.post(tap, json={"event": "SAFE", "note": "EDGE WINDOW"}).status_code, 200)
        self.assertEqual(self.client.post(tap, json={"event": "SAFE", "note": "STILL HERE"}).status_code, 200)
        self._restamp("OLD WINDOW", format_utc(moment - timedelta(days=30, seconds=1)))
        self._restamp("EDGE WINDOW", format_utc(moment - timedelta(days=30)))
        removed = checkin_beacon.get_store().purge_expired_events(now=moment)
        notes = [item["note"] for item in checkin_beacon.get_store().recent_events()]
        self.assertEqual(removed, 1)
        self.assertNotIn("OLD WINDOW", notes)
        self.assertIn("EDGE WINDOW", notes)
        self.assertIn("STILL HERE", notes)
        os.environ["CHECKIN_RETENTION_DAYS"] = "7"
        self._restamp("EDGE WINDOW", format_utc(moment - timedelta(days=8)))
        self.assertEqual(checkin_beacon.get_store().purge_expired_events(now=moment), 1)
        notes = [item["note"] for item in checkin_beacon.get_store().recent_events()]
        self.assertNotIn("EDGE WINDOW", notes)
        self.assertIn("STILL HERE", notes)

    def test_startup_and_sweep_purge_expired_events(self) -> None:
        moment = utcnow()
        created = self._create(callsign="TEAM-ALPHA-01", slack_webhook_url="")
        beacon_id = created.get_json()["beacon"]["id"]
        token = created.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        self.client.post("/api/checkin/tap/" + token, json={"event": "SAFE", "note": "AGED NOTE"})
        old = format_utc(moment - timedelta(days=31))
        self._restamp("AGED NOTE", old, callsign="TEAM-ALPHA-01", last_checkin=old)
        checkin_beacon.reset_store()
        store = checkin_beacon.get_store()
        self.assertEqual(store.recent_events(), [])
        beacon = store.get_public(beacon_id)
        self.assertEqual(beacon["last_note"], "")
        self.assertEqual(beacon["last_approx_region"], "")
        self.client.post("/api/checkin/tap/" + token, json={"event": "SAFE", "note": "SWEEP AGED"})
        self._restamp("SWEEP AGED", old)
        with patch("checkin_beacon.post_slack"):
            checkin_beacon.sweep_and_alert(now=moment)
        self.assertNotIn("SWEEP AGED", [item["note"] for item in checkin_beacon.get_store().recent_events()])

    def test_delete_and_purge_erase_events_immediately(self) -> None:
        first = self._create(callsign="CREW-BEIRUT-1", slack_webhook_url="")
        second = self._create(callsign="LEADER-PARIS-BUS2", sector="schools", schools_ack=True, slack_webhook_url="")
        first_id = first.get_json()["beacon"]["id"]
        second_id = second.get_json()["beacon"]["id"]
        first_token = first.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        second_token = second.get_json()["beacon"]["mobile_path"].rsplit("/", 1)[-1]
        self.client.post("/api/checkin/tap/" + first_token, json={"event": "SAFE", "note": "FIRST LOG"})
        self.client.post("/api/checkin/tap/" + second_token, json={"event": "SAFE", "note": "SECOND LOG"})
        self.client.post("/api/checkin/tap/" + second_token, json={"event": "ASSISTANCE", "note": "SECOND HELP"})
        deleted = self.client.delete(f"/api/checkins/{first_id}")
        self.assertEqual(deleted.status_code, 200)
        with sqlite3.connect(os.environ["CHECKIN_DB_PATH"]) as conn:
            gone = conn.execute("SELECT COUNT(*) FROM checkin_events WHERE beacon_id = ?", (first_id,)).fetchone()[0]
            kept = conn.execute("SELECT COUNT(*) FROM checkin_events WHERE beacon_id = ?", (second_id,)).fetchone()[0]
            beacon_gone = conn.execute("SELECT COUNT(*) FROM beacons WHERE id = ?", (first_id,)).fetchone()[0]
        self.assertEqual(gone, 0)
        self.assertEqual(beacon_gone, 0)
        self.assertEqual(kept, 2)
        self.assertEqual(self.client.get("/c/" + first_token).status_code, 404)
        wiped = self.client.post("/api/checkins/purge-events")
        self.assertEqual(wiped.status_code, 200, wiped.get_json())
        self.assertEqual(wiped.get_json()["deleted"], 2)
        self.assertEqual(checkin_beacon.get_store().recent_events(), [])
        listed = self.client.get("/api/checkins").get_json()["beacons"]
        self.assertEqual([item["callsign"] for item in listed], ["LEADER-PARIS-BUS2"])
        self.assertEqual(listed[0]["last_note"], "")


class ProductionCheckinRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["DISABLE_BACKGROUND"] = "1"
        os.environ["DISABLE_HISTORICAL_SYNC"] = "1"
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)
        import serve_map

        serve_map.start_background = lambda: None
        import app as production

        cls.production = production

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CHECKIN_DB_PATH"] = str(Path(self.tmp.name) / "checkins.db")
        os.environ["CHECKIN_SLACK_SYNC"] = "1"
        os.environ.pop("PREVIEW_PASSWORD", None)
        os.environ.pop("BASIC_AUTH_PASSWORD", None)
        checkin_beacon.reset_store()
        self.client = self.production.app.test_client()

    def tearDown(self) -> None:
        checkin_beacon.reset_store()
        self.tmp.cleanup()

    def test_production_app_serves_checkin_pages(self) -> None:
        source = Path(checkin_beacon.__file__).resolve().parents[0].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn("import checkin_beacon", source)
        self.assertIn("app.register_blueprint(checkin_beacon.checkin_bp)", source)
        ops = self.client.get("/check-in")
        self.assertEqual(ops.status_code, 200)
        self.assertIn(escape(SCHOOLS_NOTICE).encode(), ops.data)
        self.assertIn(b"Create callsign", ops.data)
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b'href="/check-in"', home.data)
        self.assertIn(b"Global Geopolitical Risk Intelligence", home.data)
        live = self.client.get("/map")
        self.assertEqual(live.status_code, 200)
        self.assertIn(b'href="/check-in"', live.data)
        self.assertIn(b"leaflet", live.data.lower())
        created = self.client.post("/api/checkins", json={
            "callsign": "CREW-BEIRUT-1",
            "sector": "media",
            "cadence_minutes": 240,
            "adult_staff_only": True,
        })
        self.assertEqual(created.status_code, 200, created.get_json())
        path = created.get_json()["beacon"]["mobile_path"]
        mobile = self.client.get(path)
        self.assertEqual(mobile.status_code, 200)
        self.assertIn(b"TAP TO CONFIRM SAFE", mobile.data)
        self.assertIn(b"HOLD / 2-TAP PANIC BEACON", mobile.data)
        tap = self.client.post("/api/checkin/tap/" + path.rsplit("/", 1)[-1], json={"event": "SAFE"})
        self.assertEqual(tap.status_code, 200, tap.get_json())
        pricing = self.client.get("/pricing")
        self.assertEqual(pricing.status_code, 200)
        self.assertIn(b"Pricing", pricing.data)


if __name__ == "__main__":
    unittest.main()
