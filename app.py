"""Flask entry for Railway and Render. Routes and schedulers live in serve_map."""

from __future__ import annotations

import os

from flask import Flask, Response, abort, request, send_from_directory

import serve_map

app = Flask(__name__)


def _enable_basic_auth() -> None:
    username = (os.environ.get("BASIC_AUTH_USERNAME") or "").strip()
    password = os.environ.get("BASIC_AUTH_PASSWORD") or ""
    if not username or not password:
        return
    from flask_basicauth import BasicAuth

    app.config["BASIC_AUTH_USERNAME"] = username
    app.config["BASIC_AUTH_PASSWORD"] = password
    app.config["BASIC_AUTH_FORCE"] = True
    BasicAuth(app)


def _json(payload: dict) -> Response:
    body = serve_map.json.dumps(payload, ensure_ascii=False)
    response = Response(body, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
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
    return _json(serve_map.country_assets_payload(country))


@app.route("/api/<path:endpoint>", methods=["GET", "POST"])
def api_forward(endpoint: str) -> Response:
    return _forward_api()


@app.get("/")
def index() -> Response:
    return send_from_directory(serve_map.ROOT, "index.html")


@app.get("/briefing")
def briefing_page() -> Response:
    return send_from_directory(serve_map.ROOT, "briefing.html")


@app.get("/world-risk")
def world_risk_page() -> Response:
    return send_from_directory(serve_map.ROOT, "world-risk.html")


@app.get("/countries")
def countries_page() -> Response:
    return send_from_directory(serve_map.ROOT, "countries.html")


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
    return send_from_directory(serve_map.ROOT, filename)


_enable_basic_auth()
serve_map.start_background()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
