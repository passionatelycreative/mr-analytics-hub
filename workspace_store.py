"""Private, durable analytics workspaces backed by Supabase Postgres.

The publishable project key identifies the project. Every request also uses the
signed-in user's access token so database row-level security remains in effect.
There is deliberately no local or temporary fallback for account workspaces.
"""

import json
import os
from urllib.parse import urlsplit
from uuid import UUID

import requests


MAX_WORKSPACES = 25
MAX_ACCOUNT_BYTES = 50 * 1024 * 1024
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_ANALYSIS_BYTES = 2 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_NOTES_CHARACTERS = 20_000
MAX_MESSAGES = 200
WORKSPACE_METADATA = (
    "id,title,filename,row_count,column_count,state,revision,created_at,"
    "updated_at,storage_bytes"
)
MESSAGE_FIELDS = "id,question,answer,created_at"


class WorkspaceStoreError(Exception):
    def __init__(self, message="Saved workspaces are temporarily unavailable.", status=503,
                 code="workspace_unavailable"):
        super().__init__(message)
        self.status = status
        self.code = code


class WorkspaceConfigurationError(WorkspaceStoreError):
    def __init__(self):
        super().__init__("Account workspaces are not configured yet.",
                         code="workspace_not_configured")


class WorkspaceConflict(WorkspaceStoreError):
    def __init__(self):
        super().__init__("This workspace changed or was removed. Reload it before saving again.",
                         status=409, code="workspace_conflict")


class WorkspaceLimitError(WorkspaceStoreError):
    def __init__(self, message, status=413):
        super().__init__(message, status=status, code="workspace_limit")


def _identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise WorkspaceStoreError("Invalid workspace or account identifier.", 400,
                                  "invalid_workspace") from error


def _json_size(value, description, limit):
    if not isinstance(value, dict):
        raise WorkspaceStoreError(f"{description} must be an object.", 400, "invalid_workspace")
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        size = len(serialized.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError) as error:
        raise WorkspaceStoreError(f"{description} contains unsupported values.", 400,
                                  "invalid_workspace") from error
    if size > limit:
        raise WorkspaceLimitError(f"{description} is too large to save.")


def _text(value, description, limit, required=False):
    if not isinstance(value, str) or "\x00" in value:
        raise WorkspaceStoreError(f"{description} must be valid text.", 400, "invalid_workspace")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise WorkspaceStoreError(f"{description} must be valid text.", 400,
                                  "invalid_workspace") from error
    if len(value) > limit:
        raise WorkspaceLimitError(f"{description} is too long.")
    if required and not value.strip():
        raise WorkspaceStoreError(f"{description} cannot be empty.", 400, "invalid_workspace")
    return value


class SupabaseWorkspaceStore:
    def __init__(self, url=None, key=None, session=None):
        self.url = (os.environ.get("SUPABASE_URL", "") if url is None else url).strip().rstrip("/")
        self.key = (os.environ.get("SUPABASE_KEY", "") if key is None else key).strip()
        self.http = session if session is not None else requests

    @property
    def configured(self):
        try:
            parsed = urlsplit(self.url)
            return bool(self.key and parsed.scheme in ("https", "http") and parsed.netloc
                        and not parsed.username and not parsed.password
                        and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def _request(self, method, table, user_id, token, params=None, payload=None):
        if not self.configured:
            raise WorkspaceConfigurationError()
        owner_id = _identifier(user_id)
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise WorkspaceStoreError("Sign in again to open your workspace.", 401,
                                      "authentication_required")
        filters = dict(params or {})
        filters["owner_id"] = f"eq.{owner_id}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        try:
            response = self.http.request(method, f"{self.url}/rest/v1/{table}",
                                         params=filters, json=payload, headers=headers,
                                         timeout=(5, 30), allow_redirects=False)
        except requests.RequestException as error:
            raise WorkspaceStoreError() from error
        if response.status_code >= 300:
            self._raise_response_error(response)
        if response.status_code == 204:
            return []
        try:
            data = response.json()
        except (ValueError, TypeError) as error:
            raise WorkspaceStoreError() from error
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise WorkspaceStoreError()
        # Defense in depth: a misconfigured service must not return another
        # customer's record even if its database policy or filters are broken.
        for record in data:
            if "owner_id" in record and record["owner_id"] != owner_id:
                raise WorkspaceStoreError()
        return data

    @staticmethod
    def _raise_response_error(response):
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        code = payload.get("code", "")
        message = str(payload.get("message", ""))
        if response.status_code in (401, 403) or code in ("42501", "PGRST301", "PGRST302"):
            raise WorkspaceStoreError("Sign in again to open your workspace.", 401,
                                      "authentication_required")
        if code in ("42P01", "PGRST205"):
            raise WorkspaceConfigurationError()
        if "workspace_count_limit" in message:
            raise WorkspaceLimitError("Your account has reached its 25-dataset limit. Delete a dataset before uploading another.", 409)
        if "workspace_storage_limit" in message:
            raise WorkspaceLimitError("Your account has reached its 50 MB storage limit. Delete a dataset to make room.", 409)
        if "workspace_message_limit" in message:
            raise WorkspaceLimitError("This workspace has reached its 200-question history limit.", 409)
        if code in ("23514", "22001", "22023"):
            raise WorkspaceStoreError("The workspace contains invalid or oversized values.", 400,
                                      "invalid_workspace")
        raise WorkspaceStoreError()

    def create(self, user_id, token, analysis, csv_content=None, title=None):
        _json_size(analysis, "Analysis report", MAX_ANALYSIS_BYTES)
        filename = _text(analysis.get("filename", "dataset.csv"), "Filename", 255, required=True)
        title = _text(filename[:120] if title is None else title, "Workspace title", 120, required=True)
        if csv_content is not None:
            _text(csv_content, "CSV content", MAX_CSV_BYTES)
            if len(csv_content.encode("utf-8")) > MAX_CSV_BYTES:
                raise WorkspaceLimitError("The CSV file is too large. Maximum size is 10 MB.")
        rows = analysis.get("rows", 0)
        columns = analysis.get("columns", 0)
        if type(rows) is not int or not 0 <= rows <= 100_000:
            raise WorkspaceStoreError("Invalid dataset row count.", 400, "invalid_workspace")
        if type(columns) is not int or not 0 <= columns <= 100:
            raise WorkspaceStoreError("Invalid dataset column count.", 400, "invalid_workspace")
        payload = {"owner_id": _identifier(user_id), "title": title.strip(),
                   "filename": filename, "row_count": rows, "column_count": columns,
                   "analysis": analysis, "csv_content": csv_content}
        records = self._request("POST", "workspaces", user_id, token, payload=payload)
        if len(records) != 1:
            raise WorkspaceStoreError()
        return records[0]

    def list(self, user_id, token):
        return self._request("GET", "workspaces", user_id, token, params={
            "select": WORKSPACE_METADATA, "order": "updated_at.desc,id.asc", "limit": MAX_WORKSPACES,
        })

    def get(self, user_id, token, workspace_id):
        records = self._request("GET", "workspaces", user_id, token, params={
            "id": f"eq.{_identifier(workspace_id)}", "select": "*", "limit": 1,
        })
        if not records:
            return None
        workspace = records[0]
        workspace["messages"] = self._request("GET", "workspace_messages", user_id, token, params={
            "workspace_id": f"eq.{_identifier(workspace_id)}", "select": MESSAGE_FIELDS,
            "order": "created_at.asc,id.asc", "limit": MAX_MESSAGES,
        })
        return workspace

    def update(self, user_id, token, workspace_id, changes, expected_revision):
        if not isinstance(changes, dict) or not changes or set(changes) - {"title", "notes", "state"}:
            raise WorkspaceStoreError("Choose a title, notes, or workspace view to update.", 400,
                                      "invalid_workspace")
        if type(expected_revision) is not int or expected_revision < 1:
            raise WorkspaceStoreError("A valid workspace revision is required.", 400,
                                      "invalid_workspace")
        payload = dict(changes)
        if "title" in payload:
            payload["title"] = _text(payload["title"], "Workspace title", 120, required=True).strip()
        if "notes" in payload:
            _text(payload["notes"], "Workspace notes", MAX_NOTES_CHARACTERS)
        if "state" in payload:
            _json_size(payload["state"], "Workspace view", MAX_STATE_BYTES)
        records = self._request("PATCH", "workspaces", user_id, token, params={
            "id": f"eq.{_identifier(workspace_id)}", "revision": f"eq.{expected_revision}",
        }, payload=payload)
        if not records:
            raise WorkspaceConflict()
        if len(records) != 1:
            raise WorkspaceStoreError()
        return records[0]

    def delete(self, user_id, token, workspace_id):
        records = self._request("DELETE", "workspaces", user_id, token, params={
            "id": f"eq.{_identifier(workspace_id)}", "select": "id",
        })
        return bool(records)

    def append_message(self, user_id, token, workspace_id, question, answer):
        _text(question, "Question", 4_000, required=True)
        _text(answer, "AI answer", 20_000, required=True)
        payload = {"owner_id": _identifier(user_id), "workspace_id": _identifier(workspace_id),
                   "question": question, "answer": answer}
        records = self._request("POST", "workspace_messages", user_id, token,
                                params={"select": MESSAGE_FIELDS}, payload=payload)
        if len(records) != 1:
            raise WorkspaceStoreError()
        return records[0]
