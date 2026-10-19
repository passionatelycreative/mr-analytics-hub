"""Mr Analytics: bounded CSV analysis and optional, protected AI questions."""
import hmac
from io import BytesIO
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, Request, g, jsonify, redirect, request, send_from_directory, session, Response
from werkzeug.exceptions import RequestEntityTooLarge

from analytics import (
    CSVValidationError, MAX_CSV_COLUMNS, MAX_CSV_ROWS, MAX_UPLOAD_BYTES,
    analyze_csv,
)
from ai_service import AIUnavailableError, answer_question
from report_store import ReportStore
from auth_service import AuthError, SupabaseAuthService, public_user
from workspace_store import SupabaseWorkspaceStore, WorkspaceStoreError

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


def auth_service():
    return SupabaseAuthService()


def workspace_store():
    return SupabaseWorkspaceStore()


def expected_origin():
    return (os.getenv("PUBLIC_ORIGIN", "").strip().rstrip("/")
            or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
            or request.host_url.rstrip("/"))


def check_origin(required=False):
    origin = request.headers.get("Origin", "")
    if not origin and not required:
        return
    try:
        parsed = urlsplit(origin)
        valid = origin == expected_origin() and parsed.scheme in {"http", "https"}
    except ValueError:
        valid = False
    if not valid:
        raise AuthError("Please use the Mr Analytics website for this request.", 403)


def check_csrf():
    check_origin()
    cookie = request.cookies.get("ma_csrf", "")
    token = request.headers.get("X-CSRF-Token", "")
    if not cookie or len(cookie) > 128 or not hmac.compare_digest(cookie.encode(), token.encode()):
        raise AuthError("Please refresh the page and try again.", 403)


def current_user(required=False):
    if not hasattr(g, "account_user"):
        g.account_user = None
        access = request.cookies.get("ma_access", "")
        refresh = request.cookies.get("ma_refresh", "")
        if access or refresh:
            service = auth_service()
            user = service.get_user(access) if access else None
            if user is None and refresh:
                try:
                    tokens = service.refresh(refresh)
                    access = tokens["access_token"]
                    user = service.get_user(access)
                    g.auth_tokens = tokens
                except AuthError as exc:
                    if exc.status != 401:
                        raise
            if user is None:
                g.clear_auth_cookies = True
                raise AuthError("Your sign-in has expired. Please sign in again.", 401)
            g.account_user = {**public_user(user), "access_token": access}
    if required and g.account_user is None:
        raise AuthError("Please sign in to open your saved workspace.", 401)
    return g.account_user


def visible_workspace(workspace):
    # The original CSV is available only through the explicit download action.
    result = dict(workspace)
    result["has_original_csv"] = result.pop("csv_content", None) is not None
    return result


def saved_workspace(report_id):
    user = current_user(required=True)
    workspace = workspace_store().get(user["id"], user["access_token"], report_id.removeprefix("ws_"))
    if workspace is None:
        raise AuthError("Saved workspace not found.", 404)
    return workspace


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
    if request.path.startswith("/api/") or request.path == "/upload" or request.path.endswith(".html"):
        response.headers["Cache-Control"] = "private, no-store"
    secure = app.config["SESSION_COOKIE_SECURE"] or expected_origin().startswith("https://")
    if getattr(g, "clear_auth_cookies", False):
        response.delete_cookie("ma_access", path="/", httponly=True, secure=secure, samesite="Lax")
        response.delete_cookie("ma_refresh", path="/", httponly=True, secure=secure, samesite="Lax")
    elif getattr(g, "auth_tokens", None):
        tokens = g.auth_tokens
        response.set_cookie("ma_access", tokens["access_token"], max_age=3600,
                            httponly=True, secure=secure, samesite="Lax", path="/")
        response.set_cookie("ma_refresh", tokens["refresh_token"], max_age=30 * 24 * 3600,
                            httponly=True, secure=secure, samesite="Lax", path="/")
    if getattr(g, "csrf_token", None):
        response.set_cookie("ma_csrf", g.csrf_token, max_age=30 * 24 * 3600,
                            httponly=True, secure=secure, samesite="Lax", path="/")
    return response


@app.errorhandler(AuthError)
def auth_error(exc):
    return error(str(exc), exc.status)


@app.errorhandler(WorkspaceStoreError)
def workspace_error(exc):
    return jsonify(status="error", message=str(exc), code=exc.code), exc.status


@app.get("/api/auth/me")
def auth_me():
    token = request.cookies.get("ma_csrf", "")
    if not token or len(token) > 128:
        token = secrets.token_urlsafe(32)
    g.csrf_token = token
    try:
        user = current_user()
    except AuthError as exc:
        if exc.status != 401:
            raise
        user = None
    return jsonify(user={key: value for key, value in user.items() if key != "access_token"} if user else None,
                   configured=auth_service().configured, csrf_token=token)


def account_input(signup=False):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise AuthError("Enter your email and password.", 400)
    email, password = data.get("email"), data.get("password")
    if not isinstance(email, str) or not 3 <= len(email.strip()) <= 254 or "@" not in email:
        raise AuthError("Enter a valid email address.", 400)
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise AuthError("Enter your password.", 400)
    if signup and len(password) < 12:
        raise AuthError("Use a password with at least 12 characters.", 400)
    return data, email.strip(), password


@app.post("/api/auth/login")
def auth_login():
    check_csrf()
    _data, email, password = account_input()
    tokens = auth_service().login(email, password)
    g.auth_tokens = tokens
    return jsonify(status="success", user=public_user(tokens["user"]))


@app.post("/api/auth/signup")
def auth_signup():
    check_csrf()
    data, email, password = account_input(signup=True)
    name = data.get("display_name", "")
    if not isinstance(name, str) or len(name) > 120:
        raise AuthError("Use a name of 120 characters or fewer.", 400)
    result = auth_service().signup(email, password, name.strip())
    if result.get("access_token"):
        g.auth_tokens = result
        return jsonify(status="success", user=public_user(result["user"]), confirmation_required=False)
    return jsonify(status="success", user=None, confirmation_required=True,
                   message="Check your email to confirm your account, then sign in.")


@app.post("/api/auth/logout")
def auth_logout():
    check_csrf()
    access = request.cookies.get("ma_access")
    g.clear_auth_cookies = True
    if access:
        try:
            auth_service().logout(access)
        except AuthError:
            app.logger.warning("Remote sign-out unavailable; local account cookies cleared")
    session.pop("analysis_owner", None)
    return jsonify(status="success")


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
    return send_from_directory(BASE_DIR, "login.html")


@app.get("/<page>.html")
def page(page):
    if page == "signup":
        return send_from_directory(BASE_DIR, "login.html")
    if page in {"platform", "account"}:
        try:
            current_user(required=True)
        except AuthError as exc:
            if exc.status == 401:
                return redirect("/login.html?next=/platform.html")
            raise
        return send_from_directory(BASE_DIR, "platform.html")
    if page == "research-planning":
        return send_from_directory(BASE_DIR, "investment-research.html")
    allowed = {"index", "platform", "dashboard", "research", "ai-assistant",
               "market-research", "investment-research"}
    if page not in allowed:
        return error("This page is no longer available.", 404)
    return send_from_directory(BASE_DIR, page + ".html")


@app.post("/upload")
def upload_file():
    check_origin()
    user = current_user()
    if user:
        check_csrf()
    if "file" not in request.files:
        return error("No file uploaded", 400)
    file = request.files["file"]
    if not file.filename:
        return error("No file selected", 400)
    try:
        analysis = analyze_csv(file)
    except CSVValidationError as exc:
        return error(str(exc), 400)
    if user:
        file.stream.seek(0)
        csv_content = file.stream.read(MAX_UPLOAD_BYTES + 1).decode("utf-8-sig")
        workspace = workspace_store().create(user["id"], user["access_token"], analysis, csv_content=csv_content)
        return jsonify(status="success", message="CSV analyzed and saved to your account.", analysis=analysis,
                       analysis_id="ws_" + workspace["id"], persistent=True, workspace=visible_workspace(workspace))
    owner = session.get("analysis_owner")
    if not owner:
        owner = secrets.token_urlsafe(32)
        session["analysis_owner"] = owner
    report_id = reports().save(owner, analysis)
    return jsonify(status="success", message="CSV analyzed successfully",
                   analysis=analysis, analysis_id=report_id, expires_in=3600, persistent=False)


@app.get("/api/workspaces")
def list_workspaces():
    user = current_user(required=True)
    items = workspace_store().list(user["id"], user["access_token"])
    return jsonify(status="success", workspaces=items)


@app.post("/api/workspaces")
def save_guest_workspace():
    user = current_user(required=True)
    check_csrf()
    data = request.get_json(silent=True)
    report_id = data.get("analysis_id") if isinstance(data, dict) else None
    owner = session.get("analysis_owner")
    if not isinstance(report_id, str) or report_id.startswith("ws_") or not owner:
        return error("Open a temporary report before saving it to your account.", 400)
    analysis = reports().get(report_id, owner)
    if analysis is None:
        return error("This temporary report expired. Upload the CSV again to save it.", 404)
    workspace = workspace_store().create(user["id"], user["access_token"], analysis)
    reports().delete(report_id, owner)
    return jsonify(status="success", workspace=visible_workspace(workspace), analysis=analysis,
                   analysis_id="ws_" + workspace["id"], persistent=True)


@app.route("/api/workspaces/<workspace_id>", methods=["GET", "PATCH", "DELETE"])
def workspace_item(workspace_id):
    user = current_user(required=True)
    store = workspace_store()
    if request.method == "GET":
        workspace = store.get(user["id"], user["access_token"], workspace_id)
        if workspace is None:
            return error("Saved workspace not found.", 404)
        return jsonify(status="success", workspace=visible_workspace(workspace))
    check_csrf()
    if request.method == "DELETE":
        if not store.delete(user["id"], user["access_token"], workspace_id):
            return error("Saved workspace not found.", 404)
        return jsonify(status="success")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error("Enter a valid workspace update.", 400)
    changes = {key: value for key, value in data.items() if key in {"title", "notes", "state"}}
    workspace = store.update(user["id"], user["access_token"], workspace_id, changes, data.get("expected_revision"))
    return jsonify(status="success", workspace=visible_workspace(workspace))


@app.get("/api/workspaces/<workspace_id>/download")
def download_workspace_csv(workspace_id):
    workspace = saved_workspace(workspace_id)
    if workspace.get("csv_content") is None:
        return error("The original CSV was not retained for this report. Upload it again while signed in.", 404)
    response = Response(workspace["csv_content"], content_type="text/csv; charset=utf-8")
    from werkzeug.utils import secure_filename
    filename = secure_filename(workspace.get("filename", "dataset.csv")) or "dataset.csv"
    response.headers["Content-Disposition"] = 'attachment; filename="' + filename + '"'
    return response


def owned_report(report_id):
    if report_id.startswith("ws_"):
        return saved_workspace(report_id)["analysis"]
    owner = session.get("analysis_owner")
    if not owner:
        return None
    return reports().get(report_id, owner)


@app.get("/api/analysis/<report_id>")
def get_analysis(report_id):
    if report_id.startswith("ws_"):
        workspace = saved_workspace(report_id)
        return jsonify(status="success", analysis=workspace["analysis"], analysis_id=report_id,
                       persistent=True, workspace=visible_workspace(workspace))
    analysis = owned_report(report_id)
    if analysis is None:
        return error("This analysis has expired or is unavailable. Upload your CSV again.", 404)
    return jsonify(status="success", analysis=analysis, analysis_id=report_id, persistent=False)


@app.delete("/api/analysis/<report_id>")
def delete_analysis(report_id):
    if report_id.startswith("ws_"):
        user = current_user(required=True)
        check_csrf()
        if not workspace_store().delete(user["id"], user["access_token"], report_id[3:]):
            return error("Saved workspace not found.", 404)
        return jsonify(status="success")
    check_origin()
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
    check_origin(required=True)
    user = current_user() if report_id.startswith("ws_") else None
    if user:
        check_csrf()
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
    question_owner = "account:" + user["id"] if user else session["analysis_owner"]
    if not reports().reserve_question(question_owner):
        response, code = error("The hourly AI question limit has been reached. Try again later.", 429)
        response.headers["Retry-After"] = "3600"
        return response, code
    try:
        answer = answer_question(analysis, question.strip())
    except AIUnavailableError:
        app.logger.warning("AI provider unavailable")
        return error("The AI service could not answer right now. Please try again later.", 503)
    saved = False
    if user:
        try:
            workspace_store().append_message(user["id"], user["access_token"], report_id[3:], question.strip(), answer)
            saved = True
        except WorkspaceStoreError:
            app.logger.warning("AI answer delivered but conversation save failed")
    return jsonify(status="success", answer=answer, saved=saved,
                   save_warning="The answer could not be saved to your account." if user and not saved else None)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
