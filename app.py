"""Mr Analytics: bounded CSV analysis and optional, protected AI questions."""
import hmac
from io import BytesIO
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, Request, jsonify, redirect, request, send_from_directory, session
from werkzeug.exceptions import RequestEntityTooLarge

from analytics import (
    CSVValidationError, MAX_CSV_COLUMNS, MAX_CSV_ROWS, MAX_UPLOAD_BYTES,
    analyze_csv,
)
from ai_service import AIUnavailableError, answer_question
from report_store import ReportStore

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class InMemoryUploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        # Bounded requests stay in memory, including Werkzeug's multipart parser.
        return BytesIO()


app = Flask(__name__, static_folder=str(BASE_DIR / "static"))
app.request_class = InMemoryUploadRequest
app.config.update(
    SECRET_KEY=os.getenv("SESSION_SECRET") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=11 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    ANALYSIS_DB_PATH=os.getenv("ANALYSIS_DB_PATH", str(BASE_DIR / "instance" / "analysis.sqlite3")),
)


def reports():
    return ReportStore(app.config["ANALYSIS_DB_PATH"])


def error(message, code):
    return jsonify(status="error", message=message), code


def ai_enabled():
    return all(os.getenv(name, "").strip() for name in (
        "OPENAI_API_KEY", "OPENAI_MODEL", "ANALYST_ACCESS_TOKEN"
    ))


@app.after_request
def response_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.path.startswith("/api/") or request.path == "/upload":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    return error("The upload is too large. Choose a CSV file up to 10 MB.", 413)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/login.html")
def login():
    return redirect("/platform.html")


@app.get("/<page>.html")
def page(page):
    if page == "research-planning":
        return send_from_directory(BASE_DIR, "investment-research.html")
    allowed = {"index", "platform", "dashboard", "research", "ai-assistant",
               "market-research", "investment-research"}
    if page not in allowed:
        return error("This page is no longer available.", 404)
    return send_from_directory(BASE_DIR, page + ".html")


@app.post("/upload")
def upload_file():
    if "file" not in request.files:
        return error("No file uploaded", 400)
    file = request.files["file"]
    if not file.filename:
        return error("No file selected", 400)
    try:
        analysis = analyze_csv(file)
    except CSVValidationError as exc:
        return error(str(exc), 400)
    owner = session.get("analysis_owner")
    if not owner:
        owner = secrets.token_urlsafe(32)
        session["analysis_owner"] = owner
    report_id = reports().save(owner, analysis)
    return jsonify(status="success", message="CSV analyzed successfully",
                   analysis=analysis, analysis_id=report_id, expires_in=3600)


def owned_report(report_id):
    owner = session.get("analysis_owner")
    if not owner:
        return None
    return reports().get(report_id, owner)


@app.get("/api/analysis/<report_id>")
def get_analysis(report_id):
    analysis = owned_report(report_id)
    if analysis is None:
        return error("This analysis has expired or is unavailable. Upload your CSV again.", 404)
    return jsonify(status="success", analysis=analysis, analysis_id=report_id)


@app.delete("/api/analysis/<report_id>")
def delete_analysis(report_id):
    if owned_report(report_id) is None:
        return error("Analysis not found.", 404)
    reports().delete(report_id, session["analysis_owner"])
    return jsonify(status="success")


@app.get("/api/ai-status")
def ai_status():
    return jsonify(enabled=ai_enabled(), requires_access_code=ai_enabled())


@app.post("/api/analysis/<report_id>/question")
def ask_question(report_id):
    analysis = owned_report(report_id)
    if analysis is None:
        return error("This analysis has expired or is unavailable. Upload your CSV again.", 404)
    if not ai_enabled():
        return error("AI questions are not configured yet. Your CSV analysis is still available.", 503)
    origin = request.headers.get("Origin", "")
    expected_origin = os.getenv("PUBLIC_ORIGIN", "").strip().rstrip("/") or request.host_url.rstrip("/")
    try:
        parsed_origin = urlsplit(origin)
        valid_origin = origin == expected_origin and parsed_origin.scheme in {"http", "https"}
    except ValueError:
        valid_origin = False
    if not valid_origin:
        return error("Please ask questions from the Mr Analytics workspace.", 403)
    token = request.headers.get("X-Analyst-Token", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not hmac.compare_digest(token.encode(), os.environ["ANALYST_ACCESS_TOKEN"].strip().encode()):
        return error("Enter a valid AI access code.", 401)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("consent") is not True:
        return error("Please consent to sharing the dataset summary with the AI service.", 400)
    question = data.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        return error("Enter a question between 1 and 2,000 characters.", 400)
    if not reports().reserve_question(session["analysis_owner"]):
        response, code = error("The hourly AI question limit has been reached. Try again later.", 429)
        response.headers["Retry-After"] = "3600"
        return response, code
    try:
        answer = answer_question(analysis, question.strip())
    except AIUnavailableError:
        app.logger.warning("AI provider unavailable")
        return error("The AI service could not answer right now. Please try again later.", 503)
    return jsonify(status="success", answer=answer)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
