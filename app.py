"""Flask entry for Railway and Render. Routes and schedulers live in serve_map."""

from __future__ import annotations

import mimetypes
import os
import threading
from pathlib import Path

from flask import Flask, Response, abort, request, send_from_directory

mimetypes.add_type("application/manifest+json", ".webmanifest")

def _pin_live_database() -> None:
    """Keep the live feed database on durable storage before serve_map reads DB_PATH."""
    if (os.environ.get("DB_PATH") or "").strip():
        return
    from analytics_store import resolve_data_dir, uses_durable_volume

    data_dir = resolve_data_dir()
    if uses_durable_volume(data_dir):
        os.environ["DB_PATH"] = str(Path(data_dir) / "osint_events.db")


_pin_live_database()

import ppv
import preview_lock
import serve_map

app = Flask(__name__)
from disruption_layer import disruption_bp
app.register_blueprint(disruption_bp)
preview_lock.install(app)
ppv.install(app)


def _enable_basic_auth() -> None:
    if preview_lock.lock_enabled():
        return
    username = (os.environ.get("BASIC_AUTH_USERNAME") or "").strip()
    password = os.environ.get("BASIC_AUTH_PASSWORD") or ""
    if not username or not password:
        return
    from flask_basicauth import BasicAuth

    app.config["BASIC_AUTH_USERNAME"] = username
    app.config["BASIC_AUTH_PASSWORD"] = password
    app.config["BASIC_AUTH_FORCE"] = True
    BasicAuth(app)


_INCIDENT_API_PATHS = {
    "/api/data",
    "/api/news-stream",
    "/api/risk-assessments/country-incidents",
}


def _score_incident_payload(payload: dict) -> dict:
    """Attach deterministic confidence fields without touching the cached snapshot."""
    if not isinstance(payload, dict) or request.path not in _INCIDENT_API_PATHS:
        return payload
    try:
        import confidence_scoring
    except Exception as exc:
        print(f"Confidence scoring was not applied: {exc.__class__.__name__}.")
        return payload
    try:
        return confidence_scoring.apply_to_payload(payload, confidence_scoring.score_incidents)
    except Exception as exc:
        print(f"Confidence scoring was not applied: {exc.__class__.__name__}.")
        return payload


def _json(payload: dict) -> Response:
    if ppv.is_sensitive(request.path):
        payload = ppv.restrict(payload)
    payload = _score_incident_payload(payload)
    body = serve_map.json.dumps(payload, ensure_ascii=False)
    response = Response(body, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


def _page(filename: str, directory=None) -> Response:
    """Read HTML into the response body.

    send_from_directory leaves the file in passthrough mode, and the preview
    lock has to read HTML responses to add the robots tag.
    """
    folder = serve_map.ROOT if directory is None else Path(directory)
    path = (folder / filename).resolve()
    root = folder.resolve()
    if path != root and root not in path.parents:
        abort(404)
    if not path.is_file():
        abort(404)
    return Response(path.read_bytes(), mimetype="text/html; charset=utf-8")


@app.after_request
def _no_store(response: Response) -> Response:
    path = request.path or ""
    if path == "/sw.js" and response.status_code == 200:
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Content-Type"] = "application/javascript"
        return response
    response.headers["Cache-Control"] = "no-store"
    if path in {"/manifest.webmanifest", "/static/manifest.webmanifest"} and response.status_code == 200:
        response.headers["Content-Type"] = "application/manifest+json"
    return response


@app.route("/api/data", methods=["GET", "POST"])
def api_data() -> Response:
    if request.method == "POST":
        return _json(serve_map.data_post_response(request.get_data(cache=False, as_text=False) or b""))
    return _json(serve_map.data_response())


@app.get("/api/thermal")
def api_thermal() -> Response:
    return _json(serve_map.thermal_response())


@app.post("/api/summary")
def api_summary() -> Response:
    return _json(serve_map.summary_response())


@app.post("/api/poll-now")
def api_poll() -> Response:
    return _json(serve_map.poll_response())


@app.post("/api/report")
def api_report() -> Response:
    return _json(serve_map.report_response(request.get_data(cache=False, as_text=False) or b""))


def _forward_api() -> Response:
    target = request.path
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", "replace")
    handled = serve_map.handle_api(request.method, target, request.get_data(cache=False, as_text=False) or b"")
    if handled is None:
        abort(404)
    status, content_type, payload = handled
    if request.path in _INCIDENT_API_PATHS and "json" in content_type.lower():
        try:
            decoded = serve_map.json.loads(payload)
        except Exception:
            decoded = None
        if isinstance(decoded, dict):
            decoded = _score_incident_payload(decoded)
            payload = serve_map.json.dumps(decoded, ensure_ascii=False).encode("utf-8")
    response = Response(payload, status=status)
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/feed-status")
def api_feed_status() -> Response:
    return _forward_api()


@app.get("/api/daily-chart")
def api_daily_chart() -> Response:
    return _forward_api()


@app.get("/api/daily-chart.png")
def api_daily_chart_png() -> Response:
    return _forward_api()


@app.get("/api/export.csv")
def api_export_csv() -> Response:
    return _forward_api()


@app.get("/api/export.geojson")
def api_export_geojson() -> Response:
    return _forward_api()


@app.get("/api/country-assets/<path:country>")
def api_country_assets(country: str) -> Response:
    grant = ppv.current()
    if grant and not ppv.matches({"country": country}, grant):
        return _json({"ok": False, "message": "This pass does not include that country.", "assets": []})
    return _json(serve_map.country_assets_payload(country))


@app.get("/api/country-calendar/<iso2>")
def api_country_calendar(iso2: str) -> Response:
    return _json(serve_map.country_calendar_payload(iso2))


@app.route("/api/analytics", methods=["GET"])
@app.route("/api/reports", methods=["GET", "POST"])
@app.route("/api/reports/<path:rest>", methods=["GET", "POST"])
def historical_api(rest: str = "") -> Response:
    import report_builder

    result = report_builder.dispatch(
        request.method,
        request.path,
        request.args,
        request.get_json(silent=True) or {},
    )
    body = result["body"]
    if not isinstance(body, str):
        body = serve_map.json.dumps(body, ensure_ascii=False)
    response = Response(body, status=result.get("status", 200), mimetype=result.get("mimetype", "application/json"))
    for key, value in (result.get("headers") or {}).items():
        response.headers[key] = value
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/<path:endpoint>", methods=["GET", "POST"])
def api_forward(endpoint: str) -> Response:
    return _forward_api()


@app.get("/")
def index() -> Response:
    return _page("index.html")


@app.get("/briefing")
def briefing_page() -> Response:
    return _page("briefing.html")


@app.get("/world-risk")
def world_risk_page() -> Response:
    return _page("world-risk.html")


@app.get("/countries")
def countries_page() -> Response:
    return _page("countries.html")


@app.get("/grok-ops")
def grok_ops_page() -> Response:
    return _page("grok-ops.html")


@app.get("/risk-assessments")
def risk_assessments_page() -> Response:
    return _page("risk-assessments.html")


@app.get("/pricing")
def pricing_page() -> Response:
    return _page("pricing.html")


@app.get("/enterprise")
def enterprise_page() -> Response:
    return _page("enterprise.html")


@app.get("/reports")
def reports_page() -> Response:
    return _page("reports.html", serve_map.ROOT / "static")


@app.get("/manifest.webmanifest")
def web_manifest() -> Response:
    response = send_from_directory(serve_map.ROOT / "static", "manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    return response


@app.get("/sw.js")
def service_worker() -> Response:
    response = send_from_directory(serve_map.ROOT / "static", "sw.js")
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


def _sse(chunks):
    response = Response(chunks, mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.post("/api/grok/summarize-governorate")
def api_grok_governorate() -> Response:
    import platform_phase1

    return _sse(platform_phase1.stream_governorate(request.get_json(silent=True) or {}))


@app.post("/api/grok/briefing")
def api_grok_briefing() -> Response:
    import platform_phase1

    return _sse(platform_phase1.stream_briefing(request.get_json(silent=True) or {}))


@app.post("/api/grok/risk-draft")
def api_grok_risk_draft() -> Response:
    import platform_phase1

    return _sse(platform_phase1.stream_risk_draft(request.get_json(silent=True) or {}))


@app.post("/api/grok/copilot")
def api_grok_copilot() -> Response:
    import platform_phase1

    return _sse(platform_phase1.stream_copilot(request.get_json(silent=True) or {}))


@app.get("/api/travel-advisories")
def api_travel_advisories() -> Response:
    return _json(serve_map.travel_advisories_payload())


@app.get("/<path:filename>")
def static_file(filename: str) -> Response:
    name = filename.rsplit("/", 1)[-1]
    if serve_map.blocked_static(name) or ".." in filename.split("/"):
        abort(404)
    target = (serve_map.ROOT / filename).resolve()
    if not str(target).startswith(str(serve_map.ROOT)) or not target.is_file():
        abort(404)
    if filename.endswith(".html"):
        return _page(filename)
    return send_from_directory(serve_map.ROOT, filename)


def _install_slack_alerts() -> None:
    """Register Slack ops-room alerts and wake the dispatcher after each ingest batch."""
    try:
        import slack_alerts
    except Exception as exc:
        print(f"Slack alerts were not loaded: {exc.__class__.__name__}.")
        return
    app.register_blueprint(slack_alerts.slack_bp)
    original = serve_map.ingest_feeds

    def _ingest_with_slack_dispatch():
        result = original()
        try:
            slack_alerts.notify_after_ingest()
        except Exception as exc:
            print(f"Slack alert hook failed: {exc.__class__.__name__}.")
        return result

    serve_map.ingest_feeds = _ingest_with_slack_dispatch
    if (os.environ.get("DISABLE_BACKGROUND") or "").strip().lower() in {"1", "true", "yes"}:
        return
    try:
        slack_alerts.start_dispatcher()
    except Exception as exc:
        print(f"Slack alert dispatcher did not start: {exc.__class__.__name__}.")


def _schedule_sentinel_evaluation() -> None:
    """Match the latest incidents to Sentinel Zones without stalling ingest."""

    def _run() -> None:
        try:
            import sentinel_zones

            incidents = sentinel_zones.load_live_incidents()
            sentinel_zones.evaluate_incidents_for_zones(incidents)
        except Exception as exc:
            print(f"Sentinel zone evaluation failed: {exc.__class__.__name__}.")

    try:
        if (os.environ.get("SENTINEL_EVAL_SYNC") or "").strip().lower() in {"1", "true", "yes"}:
            _run()
            return
        threading.Thread(target=_run, name="sentinel-zones", daemon=True).start()
    except Exception as exc:
        print(f"Sentinel zone evaluation was not scheduled: {exc.__class__.__name__}.")


def _install_sentinel_zones() -> None:
    """Register geofenced trip watches and evaluate them after each ingest batch."""
    try:
        import sentinel_zones
    except Exception as exc:
        print(f"Sentinel zones were not loaded: {exc.__class__.__name__}.")
        return
    app.register_blueprint(sentinel_zones.sentinel_bp)
    previous = serve_map.ingest_feeds

    def _ingest_with_sentinel():
        result = previous()
        try:
            _schedule_sentinel_evaluation()
        except Exception as exc:
            print(f"Sentinel zone evaluation was not scheduled: {exc.__class__.__name__}.")
        return result

    serve_map.ingest_feeds = _ingest_with_sentinel


_enable_basic_auth()
_install_slack_alerts()
_install_sentinel_zones()
if (os.environ.get("DISABLE_BACKGROUND") or "").strip().lower() not in {"1", "true", "yes"}:
    serve_map.start_background()
import report_builder

report_builder.start_background()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
