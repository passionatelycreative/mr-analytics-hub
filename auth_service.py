"""Stateless Supabase Auth calls; credentials and tokens never enter page JavaScript."""
import os
import uuid

import requests


class AuthError(RuntimeError):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def public_user(user):
    metadata = user.get("user_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {"id": user["id"], "email": user.get("email", ""),
            "display_name": str(metadata.get("display_name") or metadata.get("full_name") or "")[:120]}


class SupabaseAuthService:
    def __init__(self, url=None, key=None, http=None):
        self.url = (url if url is not None else os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
        self.key = (key if key is not None else os.getenv("SUPABASE_KEY", "")).strip()
        self.http = http or requests

    @property
    def configured(self):
        return bool(self.url and self.key)

    def _request(self, method, path, *, token=None, payload=None, params=None, invalid_status=401):
        if not self.configured:
            raise AuthError("Account sign-in is not configured yet.")
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            response = self.http.request(method, self.url + "/auth/v1/" + path,
                                         headers=headers, json=payload, params=params, timeout=(5, 15), allow_redirects=False)
        except requests.RequestException as exc:
            raise AuthError("The account service is temporarily unavailable. Please try again.") from exc
        if response.status_code == 429:
            raise AuthError("Too many account requests. Please wait before trying again.", 429)
        if response.status_code >= 500:
            raise AuthError("The account service is temporarily unavailable. Please try again.")
        if 300 <= response.status_code < 400:
            raise AuthError("The account service returned an unexpected redirect. Please try again.")
        if response.status_code >= 300:
            raise AuthError("Please check your account details and email confirmation, then try again.", invalid_status)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            return data
        except (ValueError, TypeError) as exc:
            raise AuthError("The account service returned an unreadable response. Please try again.") from exc

    @staticmethod
    def _validated_user(user):
        if not isinstance(user, dict):
            raise AuthError("The account service did not verify this user.", 401)
        try:
            uuid.UUID(user.get("id", ""))
        except (ValueError, TypeError, AttributeError) as exc:
            raise AuthError("The account service did not verify this user.", 401) from exc
        return user

    def _session(self, data):
        if not isinstance(data.get("access_token"), str) or not data.get("access_token"):
            raise AuthError("The account service could not start a session.", 401)
        if not isinstance(data.get("refresh_token"), str) or not data.get("refresh_token"):
            raise AuthError("The account service could not start a session.", 401)
        data["user"] = self._validated_user(data.get("user"))
        return data

    def login(self, email, password):
        return self._session(self._request("POST", "token", params={"grant_type": "password"},
                                         payload={"email": email, "password": password}))

    def signup(self, email, password, display_name="", redirect_to=None):
        data = self._request("POST", "signup", payload={"email": email, "password": password,
                             "data": {"display_name": display_name}},
                             params={"redirect_to": redirect_to} if redirect_to else None,
                             invalid_status=400)
        if data.get("access_token"):
            return self._session(data)
        return {"confirmation_required": True}

    def get_user(self, token):
        try:
            return self._validated_user(self._request("GET", "user", token=token))
        except AuthError as exc:
            if exc.status == 401:
                return None
            raise

    def refresh(self, refresh_token):
        return self._session(self._request("POST", "token", params={"grant_type": "refresh_token"},
                                          payload={"refresh_token": refresh_token}))

    def logout(self, access_token):
        self._request("POST", "logout", token=access_token, params={"scope": "local"})
