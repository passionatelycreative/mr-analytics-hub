"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const accountSource = fs.readFileSync(path.join(__dirname, "../static/js/account.js"), "utf8");
const dashboardSource = fs.readFileSync(path.join(__dirname, "../static/js/customer-dashboard.js"), "utf8");

class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.listeners = new Map(); this.attributes = new Map(); this.dataset = {}; this.value = ""; this.disabled = false; this.hidden = false; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Account data must be rendered as text."); }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(type, listener) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(listener); }
  async emit(type) { for (const listener of this.listeners.get(type) || []) await listener({ target: this, preventDefault() {} }); await settle(); }
  focus() { this.focused = true; }
  select() {}
  showModal() { this.open = true; }
  close() { this.open = false; for (const listener of this.listeners.get("close") || []) listener(); }
}
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve)); };
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const descendants = (element) => [element, ...element.children.flatMap(descendants)];
const find = (element, text) => descendants(element).find((item) => item._text === text);

async function account({ user = null, login = false, configured = true, search = "", pathname = "/login.html", authResult, failLogout = false, flush } = {}) {
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const signin = get("signin"), signout = get("signout"), name = get("name");
  const modes = ["login", "signup"].map((mode) => { const element = new Element("button"); element.dataset.accountMode = mode; return element; });
  const requests = [], navigations = [];
  const window = { location: { origin: "https://analytics.example", pathname, search, assign: (url) => navigations.push(url), replace: (url) => navigations.push(url) } };
  if (flush) window.MrWorkspace = { flush };
  vm.runInNewContext(accountSource, {
    window, URL, URLSearchParams, Headers,
    document: {
      getElementById: (id) => id === "account-form" && !login ? null : get(id),
      createElement: (tag) => new Element(tag), querySelector: () => get("main"),
      querySelectorAll: (selector) => ({ "[data-account-signin]": [signin], "[data-account-signout]": [signout], "[data-account-name]": [name], "[data-account-mode]": modes })[selector] || [],
    },
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === "/api/auth/me") return response({ user, configured, csrf_token: "session-csrf" });
      if (url === "/api/auth/logout") return failLogout ? response({ message: "Unable to sign out." }, 503) : response({ status: "success" });
      if (url === "/api/auth/login") { if (authResult) return authResult; user = { id: "member", email: "member@example.com", display_name: "Member" }; return response({ user }); }
      if (url === "/api/auth/signup") return authResult || response({ user: null, confirmation_required: true, message: "Please confirm your email." });
      if (url === "/api/workspaces/failure") return response({ message: "Workspace changed." }, 409);
      return response({ status: "ok" });
    },
  });
  await window.MrAccount.ready; await settle();
  return { window, get, requests, navigations, modes, signin, signout, name };
}

test("account session uses cookie credentials and CSRF for writes without storing authentication tokens", async () => {
  const app = await account({ user: { id: "member", display_name: "A <script>name</script>" } });
  assert.equal(app.signin.hidden, true);
  assert.equal(app.signout.hidden, false);
  assert.equal(app.name.textContent, "A <script>name</script>");
  await app.window.MrAccount.request("/api/workspaces", { method: "POST", body: JSON.stringify({ title: "Study" }) });
  const request = app.requests.at(-1);
  assert.equal(request.options.credentials, "same-origin");
  assert.equal(request.options.headers.get("X-CSRF-Token"), "session-csrf");
  assert.equal(request.options.headers.get("Content-Type"), "application/json");
  assert.equal(request.options.cache, "no-store");
  assert.equal(request.options.headers.has("Authorization"), false);
  await assert.rejects(app.window.MrAccount.request("https://outside.example/api", { method: "POST" }), /this website/);
});

test("API errors preserve their status and server explanation for conflict recovery", async () => {
  const app = await account();
  await assert.rejects(app.window.MrAccount.request("/api/workspaces/failure", { method: "PATCH", body: "{}" }), (error) => error.status === 409 && error.message === "Workspace changed.");
});

test("existing users can sign in with their old password and next destination cannot leave this site", async () => {
  const app = await account({ login: true, search: "?next=%2F%2Foutside.example" });
  app.get("account-email").value = "member@example.com";
  app.get("account-password").value = "oldpass";
  await app.get("account-form").emit("submit");
  const request = app.requests.find(({ url }) => url === "/api/auth/login");
  assert.equal(JSON.parse(request.options.body).password, "oldpass");
  assert.deepEqual(app.navigations, ["/platform.html"]);
});

test("sign-up confirmation keeps the email and returns to sign-in without pretending an account session exists", async () => {
  const app = await account({ login: true });
  await app.modes[1].emit("click");
  assert.equal(app.get("account-password").minLength, 12);
  app.get("account-email").value = "new@example.com";
  app.get("account-password").value = "long-new-password";
  app.get("display-name").value = "New Researcher";
  await app.get("account-form").emit("submit");
  assert.match(app.get("account-status").textContent, /confirm your email/);
  assert.equal(app.get("account-password").value, "");
  assert.equal(app.get("account-password").minLength, 1);
  assert.equal(app.get("account-email").value, "new@example.com");
  assert.equal(app.navigations.length, 0);
  assert.equal(app.get("account-submit").disabled, false);
});

test("sign-up links select registration and control characters or account loops cannot redirect sign-in", async () => {
  for (const options of [{ pathname: "/signup.html" }, { search: "?mode=signup" }]) {
    const app = await account({ login: true, ...options });
    assert.equal(app.get("account-password").minLength, 12);
    assert.equal(app.get("account-submit").textContent, "Create account");
    assert.equal(app.get("display-name-field").hidden, false);
  }
  for (const next of ["/\t/outside.example", "/\u0000/outside.example", "/login.html", "/signup.html?next=%2Fsignup.html", "/%6cogin.html"]) {
    const app = await account({ login: true, user: { id: "member" }, search: "?next=" + encodeURIComponent(next) });
    assert.deepEqual(app.navigations, ["/platform.html"]);
  }
  const valid = await account({ login: true, user: { id: "member" }, search: "?next=" + encodeURIComponent("/dashboard.html?workspace=saved-id") });
  assert.deepEqual(valid.navigations, ["/dashboard.html?workspace=saved-id"]);
});

test("sign-in configuration problems stay visible and sign-out waits for unsaved work", async () => {
  const disabled = await account({ login: true, configured: false });
  assert.match(disabled.get("account-status").textContent, /being configured/);
  assert.equal(disabled.modes[1].disabled, true);
  const app = await account({ user: { id: "member" }, flush: async () => false });
  await app.signout.emit("click");
  assert.equal(app.requests.some(({ url }) => url === "/api/auth/logout"), false);
  assert.equal(app.navigations.length, 0);
  assert.match(app.get("account-session-status").textContent, /Save your changes/);
  assert.equal(app.signout.disabled, false);
});

const savedWorkspace = (values = {}) => ({ id: "safe-id", title: "Research study", filename: "study.csv", row_count: 12, column_count: 5, revision: 3, created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-19T10:00:00Z", ...values });
async function dashboard({ workspaces = [], failure = false, renameConflict = false } = {}) {
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const requests = [], navigations = [];
  const window = {
    location: { assign: (url) => navigations.push(url), replace: (url) => navigations.push(url) },
    MrAccount: { ready: Promise.resolve({ user: { id: "member" } }), request: async (url, options = {}) => {
      requests.push({ url, options });
      if (failure) throw new Error("The account service is unavailable.");
      if (options.method === "PATCH") {
        if (renameConflict) { const error = new Error("Conflict"); error.status = 409; throw error; }
        workspaces = workspaces.map((item) => url.endsWith(item.id) ? { ...item, title: JSON.parse(options.body).title } : item);
        return { status: "success" };
      }
      return { workspaces };
    } },
  };
  vm.runInNewContext(dashboardSource, { window, document: { getElementById: get, createElement: (tag) => new Element(tag) }, Intl, Date, encodeURIComponent });
  await settle();
  return { get, requests, navigations };
}

test("customer dashboard resumes the newest real workspace and renders untrusted names safely", async () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const app = await dashboard({ workspaces: [savedWorkspace({ id: "older", updated_at: "2026-10-18T10:00:00Z" }), savedWorkspace({ title: hostile })] });
  assert.equal(app.get("workspace-count").textContent, "2 workspaces");
  assert.equal(app.get("continue-title").textContent, hostile);
  assert.equal(app.get("continue-workspace").href, "/dashboard.html?workspace=safe-id");
  assert.match(app.get("workspace-list").textContent, /12 rows · 5 columns/);
  assert.equal(app.get("workspace-empty").hidden, true);
  app.get("workspace-search").value = "study.csv";
  await app.get("workspace-search").emit("input");
  assert.equal(app.get("workspace-list").children.length, 2);
  app.get("workspace-search").value = "no matching dataset";
  await app.get("workspace-search").emit("input");
  assert.equal(app.get("workspace-list").children.length, 0);
  assert.equal(app.get("workspace-no-results").hidden, false);
});

test("empty account and account service failure produce different recoverable states", async () => {
  const empty = await dashboard();
  assert.equal(empty.get("workspace-empty").hidden, false);
  assert.equal(empty.get("workspace-continue").hidden, true);
  assert.equal(empty.get("workspace-count").textContent, "0 workspaces");
  const broken = await dashboard({ failure: true });
  assert.match(broken.get("workspace-list-status").textContent, /unavailable/);
  assert.equal(broken.get("workspace-retry").hidden, false);
});

test("continue resumes the AI page when that was the saved workspace position", async () => {
  const app = await dashboard({ workspaces: [savedWorkspace({ state: { active_section: "ai" } })] });
  assert.equal(app.get("continue-workspace").href, "/ai-assistant.html?workspace=safe-id");
  assert.equal(find(app.get("workspace-list").children[0], "Resume →").href, "/dashboard.html?workspace=safe-id");
});

test("renaming sends the saved revision and retains input after a simultaneous edit conflict", async () => {
  const app = await dashboard({ workspaces: [savedWorkspace()], renameConflict: true });
  const card = app.get("workspace-list").children[0];
  await find(card, "Rename").emit("click");
  const form = descendants(card).find((item) => item.tagName === "form");
  const input = descendants(form).find((item) => item.tagName === "input");
  input.value = "Updated study";
  await form.emit("submit");
  const request = app.requests.find(({ options }) => options.method === "PATCH");
  assert.deepEqual(JSON.parse(request.options.body), { title: "Updated study", expected_revision: 3 });
  assert.match(form.textContent, /changed in another tab/);
  assert.equal(input.value, "Updated study");
  assert.equal(input.disabled, false);
});

test("workspace removal waits for explicit confirmation and updates the real list", async () => {
  const app = await dashboard({ workspaces: [savedWorkspace()] });
  await find(app.get("workspace-list").children[0], "Delete").emit("click");
  assert.equal(app.get("workspace-delete-dialog").open, true);
  assert.equal(app.requests.some(({ options }) => options.method === "DELETE"), false);
  await app.get("cancel-workspace-delete").emit("click");
  assert.equal(app.requests.some(({ options }) => options.method === "DELETE"), false);
  await find(app.get("workspace-list").children[0], "Delete").emit("click");
  await app.get("confirm-workspace-delete").emit("click");
  assert.equal(app.requests.find(({ options }) => options.method === "DELETE").url, "/api/workspaces/safe-id");
  assert.equal(app.get("workspace-list").children.length, 0);
  assert.equal(app.get("workspace-count").textContent, "0 workspaces");
  assert.equal(app.get("workspace-empty").hidden, false);
});
