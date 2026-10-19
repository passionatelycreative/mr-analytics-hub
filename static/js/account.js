/* Account sessions stay in server cookies. Never persist credentials here. */
(() => {
  "use strict";
  const session = { user: null, configured: false, csrf_token: "", error: null };
  async function readJson(response) {
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      const error = new Error(data.message || data.error || "We could not complete that request. Please try again.");
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }
  function showSession() {
    document.querySelectorAll("[data-account-signin]").forEach((element) => { element.hidden = Boolean(session.user); });
    document.querySelectorAll("[data-account-signout]").forEach((element) => { element.hidden = !session.user; });
    document.querySelectorAll("[data-account-name]").forEach((element) => { element.textContent = session.user?.display_name || session.user?.email || "Your account"; });
  }
  async function refresh() {
    try {
      const data = await readJson(await fetch("/api/auth/me", { credentials: "same-origin", cache: "no-store" }));
      session.user = data.user || null;
      session.configured = Boolean(data.configured);
      session.csrf_token = data.csrf_token || "";
      session.error = null;
    } catch (error) {
      session.user = null;
      session.csrf_token = "";
      session.error = error;
    }
    showSession();
    return session;
  }
  const ready = refresh();
  async function request(url, options = {}) {
    await ready;
    const target = new URL(url, window.location.origin);
    if (target.origin !== window.location.origin) throw new Error("Account requests must use this website.");
    const headers = new Headers(options.headers || {});
    const method = (options.method || "GET").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
      if (!session.csrf_token) {
        await refresh();
        if (!session.csrf_token) throw new Error("Your account session could not be verified. Reload the page and try again.");
      }
      headers.set("X-CSRF-Token", session.csrf_token);
      if (typeof options.body === "string" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    }
    return readJson(await fetch(url, { ...options, method, headers, credentials: "same-origin", cache: "no-store" }));
  }
  window.MrAccount = { ready, request, refresh, get user() { return session.user; } };
  document.querySelectorAll("[data-account-signout]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      const original = button.textContent;
      button.textContent = "Signing out…";
      try {
        if (window.MrWorkspace?.flush && !(await window.MrWorkspace.flush())) throw new Error("Save your changes or reload before signing out.");
        await request("/api/auth/logout", { method: "POST" });
        window.location.assign("/login.html");
      } catch (error) {
        button.disabled = false;
        button.textContent = original;
        let notice = document.getElementById("account-session-status");
        if (!notice) {
          notice = document.createElement("p");
          notice.id = "account-session-status";
          notice.className = "notice error account-session-status";
          notice.setAttribute("role", "alert");
          document.querySelector("main")?.prepend(notice);
        }
        notice.textContent = error.message;
      }
    });
  });
  const form = document.getElementById("account-form");
  if (!form) return;
  const get = (id) => document.getElementById(id);
  let mode = "login";
  let submitting = false;
  const parameters = new URLSearchParams(window.location.search);
  function safeDestination(destination) {
    if (!destination || !destination.startsWith("/") || /[\\\u0000-\u001f\u007f]/.test(destination)) return "/platform.html";
    try {
      const target = new URL(destination, window.location.origin);
      if (target.origin !== window.location.origin || /^\/(?:login|signup|signin|register)(?:\.html)?\/?$/i.test(decodeURIComponent(target.pathname))) return "/platform.html";
      return target.pathname + target.search + target.hash;
    } catch (_) { return "/platform.html"; }
  }
  const next = safeDestination(parameters.get("next"));
  const status = (message, kind = "") => {
    get("account-status").textContent = message;
    get("account-status").className = "account-form-status" + (kind ? " " + kind : "");
  };
  function setMode(value) {
    if (submitting) return;
    mode = value;
    const signup = mode === "signup";
    get("account-title").textContent = signup ? "Make room for your next discovery." : "Pick up where you left off.";
    get("account-intro").textContent = signup ? "Create an account to keep your datasets, findings, and research together." : "Sign in to open your saved analytics workspaces.";
    get("display-name-field").hidden = !signup;
    get("display-name").required = signup;
    get("account-password").autocomplete = signup ? "new-password" : "current-password";
    get("account-password").minLength = signup ? 12 : 1;
    get("password-help").hidden = !signup;
    get("account-submit").textContent = signup ? "Create account" : "Sign in";
    document.querySelectorAll("[data-account-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.accountMode === mode)));
    status("");
  }
  document.querySelectorAll("[data-account-mode]").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.accountMode)));
  if (window.location.pathname === "/signup.html" || parameters.get("mode") === "signup") {
    setMode("signup");
    status("Connecting to your account…");
  }
  ready.then((state) => {
    if (state.user) { window.location.replace(next); return; }
    if (state.error) status("We could not reach the account service. Reload this page to try again.", "error");
    else if (!state.configured) status("Account sign-in is being configured. Please check back shortly; your existing account is not changed.", "warning");
    else { get("account-fields").disabled = false; get("account-submit").disabled = false; status(""); }
    if (state.error || !state.configured) document.querySelectorAll("[data-account-mode]").forEach((button) => { button.disabled = true; });
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    get("account-submit").disabled = true;
    get("account-fields").disabled = true;
    status(mode === "signup" ? "Creating your account…" : "Signing you in…");
    const body = { email: get("account-email").value.trim(), password: get("account-password").value };
    if (mode === "signup") body.display_name = get("display-name").value.trim();
    try {
      const result = await request("/api/auth/" + mode, { method: "POST", body: JSON.stringify(body) });
      if (mode === "signup" && result.confirmation_required) {
        get("account-password").value = "";
        submitting = false;
        setMode("login");
        status(result.message || "Check your email to confirm your account, then come back here to sign in.", "success");
        get("account-email").focus();
      } else {
        const state = await refresh();
        if (!state.user) throw new Error("Your session could not be opened. Please try signing in again.");
        window.location.assign(next);
      }
    } catch (error) { status(error.message, "error"); }
    finally { submitting = false; get("account-fields").disabled = false; get("account-submit").disabled = false; }
  });
})();
