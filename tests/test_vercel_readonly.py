"""Read-only serverless bundles must not crash while choosing a data directory."""

import errno
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analytics_store import resolve_data_dir

ROOT = Path(__file__).resolve().parents[1]
EPHEMERAL = Path(tempfile.gettempdir()) / "georiskintel-data"


class ReadOnlyDataDirTests(unittest.TestCase):
    def test_vercel_falls_back_to_a_writable_temp_directory(self):
        root = Path(self._tmp())
        local = root / "bundled-data"
        mount = root / "missing-mount"
        resolved = resolve_data_dir({"VERCEL": "1"}, mount=mount, local=local)
        self.assertEqual(resolved, EPHEMERAL)
        self.assertTrue(resolved.is_dir())
        self.assertFalse(local.exists())
        probe = resolved / f".vercel-probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()

        chosen = root / "explicit-data"
        override = resolve_data_dir(
            {"VERCEL": "1", "GEORISKINTEL_DATA_DIR": str(chosen)},
            mount=mount,
            local=local,
        )
        self.assertEqual(override, chosen)
        self.assertTrue(chosen.is_dir())

        data_dir = root / "data-dir-override"
        from_data_dir = resolve_data_dir(
            {"VERCEL": "1", "DATA_DIR": str(data_dir)},
            mount=mount,
            local=local,
        )
        self.assertEqual(from_data_dir, data_dir)

        preferred = root / "preferred"
        other = root / "other"
        both = resolve_data_dir(
            {
                "VERCEL": "1",
                "GEORISKINTEL_DATA_DIR": str(preferred),
                "DATA_DIR": str(other),
            },
            mount=mount,
            local=local,
        )
        self.assertEqual(both, preferred)
        self.assertFalse(other.exists())

    def test_erofs_mkdir_falls_back_and_copies_seed_files(self):
        root = Path(self._tmp())
        local = root / "bundled-data"
        local.mkdir()
        (local / "incidents.db").write_bytes(b"seed-database")
        (local / "note.txt").write_text("bundled seed", encoding="utf-8")
        (local / "nested").mkdir()
        (local / "nested" / "skip.txt").write_text("not a top-level seed", encoding="utf-8")
        for name in ("incidents.db", "note.txt"):
            target = EPHEMERAL / name
            if target.exists():
                target.unlink()
        mount = root / "missing-mount"
        real_mkdir = Path.mkdir

        def guarded(target, *args, **kwargs):
            if Path(target) == local:
                raise OSError(errno.EROFS, "Read-only file system")
            return real_mkdir(target, *args, **kwargs)

        try:
            with patch.object(Path, "mkdir", guarded):
                resolved = resolve_data_dir({}, mount=mount, local=local)
            self.assertEqual(resolved, EPHEMERAL)
            self.assertTrue(resolved.is_dir())
            self.assertEqual((resolved / "incidents.db").read_bytes(), b"seed-database")
            self.assertEqual((resolved / "note.txt").read_text(encoding="utf-8"), "bundled seed")
            self.assertFalse((resolved / "nested").exists())
            self.assertEqual((local / "note.txt").read_text(encoding="utf-8"), "bundled seed")
        finally:
            for name in ("incidents.db", "note.txt"):
                target = EPHEMERAL / name
                if target.is_file() and target.read_bytes() in {b"seed-database", b"bundled seed"}:
                    target.unlink()

    def test_unwritable_override_on_vercel_uses_the_temp_directory(self):
        root = Path(self._tmp())
        blocked = root / "blocked"
        local = root / "local-data"
        real_mkdir = Path.mkdir

        def guarded(target, *args, **kwargs):
            if Path(target) == blocked:
                raise OSError(errno.EACCES, "Permission denied")
            return real_mkdir(target, *args, **kwargs)

        with patch.object(Path, "mkdir", guarded):
            resolved = resolve_data_dir(
                {"VERCEL": "1", "DATA_DIR": str(blocked), "GEORISKINTEL_DATA_DIR": str(blocked)},
                mount=root / "missing-mount",
                local=local,
            )
        self.assertEqual(resolved, EPHEMERAL)
        self.assertFalse(local.exists())

    def test_app_imports_on_vercel_and_serves_status_and_risk_pages(self):
        env = os.environ.copy()
        env["VERCEL"] = "1"
        env["DISABLE_BACKGROUND"] = "1"
        env["DISABLE_HISTORICAL_SYNC"] = "1"
        for name in (
            "DB_PATH",
            "DATA_DIR",
            "GEORISKINTEL_DATA_DIR",
            "PREVIEW_PASSWORD",
            "PREVIEW_USER",
            "BASIC_AUTH_PASSWORD",
            "BASIC_AUTH_USERNAME",
            "SITE_PRIVATE_MODE",
            "OKTA_ISSUER",
            "OKTA_CLIENT_ID",
            "OKTA_CLIENT_SECRET",
            "ANTHROPIC_API_KEY",
            "XAI_API_KEY",
            "ENABLE_XAI_INGEST",
        ):
            env.pop(name, None)
        script = r"""
import os
import sqlite3
from pathlib import Path

from app import app
import serve_map

app.testing = True
client = app.test_client()
status = client.get("/api/ai/claude-status")
alias = client.get("/api/claude/status")
page = client.get("/risk-assessments")
assert status.status_code == 200, status.status_code
assert alias.status_code != 500, alias.status_code
assert page.status_code == 200, page.status_code
body = status.get_json()
assert body["configured"] is False
assert body["on_demand"] is True
assert "sk-ant" not in status.get_data(as_text=True)
assert b"Risk Assessments" in page.data
db_path = os.environ.get("DB_PATH") or ""
assert db_path.endswith("osint_events.db"), db_path
assert "georiskintel-data" in db_path, db_path
conn = sqlite3.connect(db_path)
conn.execute("CREATE TABLE IF NOT EXISTS vercel_probe (id INTEGER)")
conn.commit()
conn.close()
opened = serve_map.db_connect()
try:
    opened.execute("SELECT 1").fetchone()
finally:
    opened.close()
assert "georiskintel-data" in str(serve_map.FEEDS_DB)
print("ok")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + "\n" + completed.stderr,
        )
        self.assertIn("ok", completed.stdout)

    def _tmp(self) -> str:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return folder.name


class LiveDatabaseRelocationTests(unittest.TestCase):
    def test_db_connect_leaves_a_read_only_bundle(self):
        import analytics_store
        import serve_map

        original = serve_map.FEEDS_DB
        blocked_dir = Path(tempfile.mkdtemp())
        blocked = blocked_dir / "osint_events.db"
        real_connect = sqlite3.connect
        real_mkdir = Path.mkdir
        real_can_write = analytics_store._can_write

        def flaky(path, *args, **kwargs):
            if Path(path) == blocked:
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(path, *args, **kwargs)

        def guarded_mkdir(target, *args, **kwargs):
            if Path(target) == blocked_dir:
                raise OSError(errno.EROFS, "Read-only file system")
            return real_mkdir(target, *args, **kwargs)

        def guarded_write(path):
            if Path(path) == blocked_dir:
                return False
            return real_can_write(path)

        try:
            serve_map.FEEDS_DB = blocked
            with patch("serve_map.sqlite3.connect", flaky), patch.object(Path, "mkdir", guarded_mkdir), patch(
                "analytics_store._can_write", guarded_write
            ):
                conn = serve_map.db_connect()
            try:
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
            finally:
                conn.close()
            self.assertIn("georiskintel-data", str(serve_map.FEEDS_DB))
            self.assertTrue(Path(serve_map.FEEDS_DB).is_file())
        finally:
            serve_map.FEEDS_DB = original


if __name__ == "__main__":
    unittest.main()
