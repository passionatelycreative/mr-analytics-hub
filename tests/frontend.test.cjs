"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/js/analytics-workspace.js"), "utf8");

// A small DOM contract keeps the interaction tests independent of browser packages.
// Rejecting innerHTML also makes unsafe rendering of CSV values fail these tests.
class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.checked = false;
    this.className = "";
    this._text = "";
    this.classList = { add() {}, remove() {} };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Untrusted content must be rendered using textContent."); }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) {
    this._text = "";
    this.children = children;
    if (this.tagName === "select") this.value = children[0]?.value || "";
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  async emit(type, extra = {}) {
    const event = { target: this, preventDefault() {}, ...extra };
    for (const listener of this.listeners.get(type) || []) await listener(event);
    await settle();
  }
  createTHead() { const section = new Element("thead"); this.append(section); return section; }
  createTBody() { const section = new Element("tbody"); this.append(section); return section; }
  insertRow() { const row = new Element("tr"); this.append(row); return row; }
  insertCell() { const cell = new Element("td"); this.append(cell); return cell; }
  focus() { this.focused = true; }
  click() {}
}

async function settle() {
  // Event callbacks intentionally start some async work without returning it.
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve));
}

function report() {
  const hostile = '<img src=x onerror="alert(1)">';
  return {
    filename: hostile + ".csv", rows: 3, columns: 2, total_missing_values: 1,
    duplicate_rows: 0, column_names: [hostile, "score"],
    column_types: { [hostile]: "categorical", score: "numeric" },
    missing_values: { [hostile]: 1, score: 0 }, outliers: { score: { count: 0 } },
    numeric_statistics: { score: { count: 3, mean: 2, "50%": 2, min: 1, max: 3, std: 1 } },
    numeric_histograms: { score: { labels: ["1–2", "2–3"], counts: [1, 2], edges: [1, 2, 3] } },
    preview: [{ [hostile]: "<script>danger()</script>", score: 1 }],
    insights: ["1 missing cell requires review."],
  };
}

async function workspace({ mode = "analysis", stored = null, enabled = true, failUpload = false, failQuestion = false, persistent = false, failSave = false, saveGate = null, uploadGate = null, viewGate = null, promotionGate = null, guestReport = false, search = "" } = {}) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element(id === "chart-column" ? "select" : "div"));
    return elements.get(id);
  };
  const storage = new Map(stored ? [["mr-analytics-report", stored]] : []);
  const requests = [];
  const prompts = [new Element("button")];
  prompts[0].dataset.question = "What needs cleaning?";
  get("report").hidden = true;
  const dataset = report();
  let saved = { id: "11111111-1111-4111-8111-111111111111", title: "Saved research", filename: "research.csv", analysis: dataset, notes: "Prior research notes", state: { chart_column: "score", active_section: mode }, revision: 3, has_original_csv: true, messages: [{ question: "A prior question", answer: "A saved answer" }] };
  const savedById = new Map([[saved.id, saved]]);
  let uploads = 0;
  const respond = (payload, ok = true) => ({ ok, json: async () => payload });
  const fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/static/samples/research-sample.csv") return { ok: true, text: async () => "category,score\nA,1\nB,2\n" };
    if (url === "/upload") {
      uploads++;
      if (uploadGate) await uploadGate;
      if (failUpload) return respond({ message: "CSV contains inconsistent row lengths." }, false);
      if (persistent) {
        saved = { ...saved, id: "22222222-2222-4222-8222-" + String(uploads).padStart(12, "0"), title: "New saved dataset " + uploads, notes: "", state: {}, revision: 1 };
        savedById.set(saved.id, saved);
        return respond({ analysis_id: "ws_" + saved.id, analysis: dataset, workspace: saved, persistent: true });
      }
      return respond({ analysis_id: "report-test-123", analysis: dataset });
    }
    if (url === "/api/workspaces" && options.method === "POST") {
      if (promotionGate) await promotionGate;
      saved = { ...saved, id: "33333333-3333-4333-8333-333333333333", revision: 1 };
      savedById.set(saved.id, saved);
      return respond({ analysis_id: "ws_" + saved.id, analysis: dataset, workspace: saved, persistent: true });
    }
    if (url.startsWith("/api/workspaces/") && options.method === "PATCH") {
      const { expected_revision, ...changes } = JSON.parse(options.body);
      const workspaceId = url.split("/").at(-1);
      if (saveGate && Object.hasOwn(changes, "notes")) await saveGate;
      if (viewGate && !Object.hasOwn(changes, "notes")) await viewGate;
      if (failSave && Object.hasOwn(changes, "notes")) return { ok: false, status: 409, json: async () => ({ message: "This workspace changed elsewhere." }) };
      const original = savedById.get(workspaceId);
      assert.equal(expected_revision, original.revision);
      const updated = { ...original, ...changes, revision: original.revision + 1 };
      savedById.set(workspaceId, updated);
      if (saved.id === workspaceId) saved = updated;
      return respond({ workspace: updated });
    }
    if (url === "/api/ai-status") return respond({ enabled });
    if (url.endsWith("/question")) return failQuestion ? respond({ message: "AI access code is invalid." }, false) : respond({ answer: "<script>untrusted answer</script> Check the missing values first." });
    if (options.method === "DELETE") return respond({ deleted: true });
    if (url.startsWith("/api/analysis/")) return respond({ analysis: dataset, ...(persistent && !guestReport ? { workspace: saved, persistent: true } : {}) });
    throw new Error("Unexpected request: " + url);
  };
  const context = {
    document: {
      body: { dataset: { workspace: mode } },
      getElementById: get,
      querySelectorAll: () => prompts,
      createElement: (tag) => new Element(tag),
      createElementNS: (_, tag) => new Element(tag),
    },
    fetch, FormData, File, Blob, URL, URLSearchParams, setTimeout, clearTimeout,
    location: { search },
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
  };
  if (persistent) {
    context.window = {
      addEventListener() {},
      location: { assign: (url) => navigations.push(url) },
      MrAccount: {
        ready: Promise.resolve({ user: { id: "customer-1" }, configured: true }),
        request: async (url, options) => {
          const response = await fetch(url, options);
          const payload = await response.json();
          if (!response.ok) { const error = new Error(payload.message); error.status = response.status; throw error; }
          return payload;
        },
      },
    };
  }
  const navigations = [];
  vm.runInNewContext(source, context, { filename: "analytics-workspace.js" });
  await settle();
  return { get, requests, storage, prompts, dataset, window: context.window, navigations };
}

test("sample upload renders real report values and treats CSV content as text", async () => {
  const app = await workspace();
  await app.get("sample-button").emit("click");
  const upload = app.requests.find(({ url }) => url === "/upload");
  assert.equal(upload.options.method, "POST");
  assert.equal(upload.options.body.get("file").name, "research-sample.csv");
  assert.equal(app.get("report").hidden, false);
  assert.equal(app.get("empty-state").hidden, true);
  assert.equal(app.get("report-name").textContent, app.dataset.filename);
  assert.equal(app.get("report-stats").children[0].textContent, "Rows3");
  assert.match(app.get("preview-table").textContent, /<script>danger\(\)<\/script>/);
  assert.match(app.get("schema-table").textContent, /<img src=x/);
  assert.equal(app.get("distribution-chart").children[0].tagName, "svg");
  assert.equal(app.get("open-ai").href, "/ai-assistant.html?analysis=report-test-123");
  assert.equal(app.storage.get("mr-analytics-report"), "report-test-123");
  assert.match(app.get("upload-status").className, /success/);
});

test("removing a restored report deletes it and clears the visible workspace", async () => {
  const app = await workspace({ stored: "stored-report" });
  assert.equal(app.get("report").hidden, false);
  await app.get("remove-report").emit("click");
  assert.ok(app.requests.some(({ url, options }) => url === "/api/analysis/stored-report" && options.method === "DELETE"));
  assert.equal(app.get("report").hidden, true);
  assert.equal(app.get("empty-state").hidden, false);
  assert.equal(app.storage.has("mr-analytics-report"), false);
  assert.equal(app.get("analyze-button").disabled, true);
});

test("upload validation and API errors remain visible and allow retry", async () => {
  const app = await workspace({ failUpload: true });
  app.get("csv-file").files = [new File(["x"], "wrong.txt")];
  await app.get("csv-file").emit("change");
  assert.match(app.get("upload-status").textContent, /choose a CSV/);
  assert.equal(app.get("analyze-button").disabled, true);
  assert.equal(app.requests.some(({ url }) => url === "/upload"), false);
  app.get("csv-file").files = [new File(["a\n1"], "valid.csv")];
  await app.get("csv-file").emit("change");
  await app.get("upload-form").emit("submit");
  assert.match(app.get("upload-status").textContent, /inconsistent row lengths/);
  assert.match(app.get("upload-status").className, /error/);
  assert.equal(app.get("analyze-button").disabled, false);
  assert.equal(app.get("csv-file").disabled, false);
  assert.equal(app.get("report").hidden, true);
});

test("AI unavailable state prevents questions and explains where analysis is available", async () => {
  const app = await workspace({ mode: "ai", stored: "stored-report", enabled: false });
  assert.equal(app.get("ask-button").disabled, true);
  assert.match(app.get("ai-status").textContent, /not enabled/);
  app.get("question").value = "Summarize the data";
  app.get("ai-consent").checked = true;
  await app.get("question-form").emit("submit");
  assert.equal(app.requests.some(({ url }) => url.endsWith("/question")), false);
});

test("AI requests require consent and send the question and access code without raw records", async () => {
  const app = await workspace({ mode: "ai", stored: "stored-report" });
  assert.equal(app.get("ask-button").disabled, false);
  await app.prompts[0].emit("click");
  assert.equal(app.get("question").value, "What needs cleaning?");
  app.get("ai-access-code").value = "owner-access-code";
  await app.get("question-form").emit("submit");
  assert.equal(app.requests.some(({ url }) => url.endsWith("/question")), false);
  app.get("ai-consent").checked = true;
  await app.get("question-form").emit("submit");
  const request = app.requests.find(({ url }) => url.endsWith("/question"));
  assert.equal(request.options.headers["X-Analyst-Token"], "owner-access-code");
  assert.deepEqual(JSON.parse(request.options.body), { question: "What needs cleaning?", consent: true });
  assert.match(app.get("answers").textContent, /<script>untrusted answer<\/script>/);
  assert.match(app.get("question-status").className, /success/);
  assert.equal(app.get("ask-button").disabled, false);
});

test("AI access errors are rendered and release the submit button", async () => {
  const app = await workspace({ mode: "ai", stored: "stored-report", failQuestion: true });
  app.get("question").value = "What should I investigate?";
  app.get("ai-consent").checked = true;
  app.get("ai-access-code").value = "wrong-code";
  await app.get("question-form").emit("submit");
  assert.match(app.get("question-status").textContent, /access code is invalid/);
  assert.match(app.get("question-status").className, /error/);
  assert.equal(app.get("answers").children.length, 0);
  assert.equal(app.get("ask-button").disabled, false);
});

test("a returning customer reopens notes, chart position, and the original download", async () => {
  const app = await workspace({ persistent: true, stored: "ws_11111111-1111-4111-8111-111111111111" });
  assert.equal(app.get("workspace-notes").value, "Prior research notes");
  assert.equal(app.get("workspace-title").value, "Saved research");
  assert.equal(app.get("chart-column").value, "score");
  assert.equal(app.get("saved-workspace-controls").hidden, false);
  assert.match(app.get("download-original").href, /\/download$/);
  assert.equal(app.get("remove-report").textContent, "Close dataset");
});

test("notes save with the current revision and closing retains the saved dataset", async () => {
  const app = await workspace({ persistent: true, stored: "ws_11111111-1111-4111-8111-111111111111" });
  app.get("workspace-notes").value = "Continue comparing the response groups.";
  await app.get("workspace-notes").emit("input");
  assert.equal(await app.window.MrWorkspace.flush(), true);
  const saved = app.requests.find(({ options }) => options.method === "PATCH" && Object.hasOwn(JSON.parse(options.body), "notes"));
  assert.equal(JSON.parse(saved.options.body).notes, "Continue comparing the response groups.");
  assert.equal(JSON.parse(saved.options.body).expected_revision, 4);
  assert.match(app.get("workspace-save-status").textContent, /All changes saved/);
  await app.get("remove-report").emit("click");
  assert.equal(app.get("report").hidden, true);
  assert.equal(app.requests.some(({ options }) => options.method === "DELETE"), false);
});

test("uploading another saved dataset never deletes the customer's previous workspace", async () => {
  const app = await workspace({ persistent: true, stored: "ws_11111111-1111-4111-8111-111111111111" });
  await app.get("sample-button").emit("click");
  assert.equal(app.requests.some(({ options }) => options.method === "DELETE"), false);
  assert.match(app.storage.get("mr-analytics-report"), /^ws_22222222/);
});

test("an overlapping save keeps the customer's edits and prevents silent loss on close", async () => {
  const app = await workspace({ persistent: true, failSave: true, stored: "ws_11111111-1111-4111-8111-111111111111" });
  app.get("workspace-notes").value = "These edits must remain visible.";
  await app.get("workspace-notes").emit("input");
  assert.equal(await app.window.MrWorkspace.flush(), false);
  assert.equal(app.get("workspace-notes").value, "These edits must remain visible.");
  await app.get("remove-report").emit("click");
  assert.equal(app.get("report").hidden, false);
  assert.match(app.get("workspace-save-status").textContent, /edits are still on this page/);
});

test("navigation waits for an in-flight autosave instead of treating it as already saved", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const app = await workspace({ persistent: true, saveGate: gate, stored: "ws_11111111-1111-4111-8111-111111111111" });
  app.get("workspace-notes").value = "A save that is still in flight.";
  await app.get("workspace-notes").emit("input");
  const first = app.window.MrWorkspace.flush();
  const second = app.window.MrWorkspace.flush();
  assert.equal(first, second);
  let prevented = false;
  const navigation = app.get("open-ai").emit("click", { preventDefault() { prevented = true; } });
  await settle();
  assert.equal(prevented, true);
  assert.deepEqual(app.navigations, []);
  release();
  assert.equal(await first, true);
  await navigation;
  assert.deepEqual(app.navigations, [app.get("open-ai").href]);
});

test("upload locks old workspace edits and competing actions after saving the previous notes", async () => {
  let release;
  const uploadGate = new Promise((resolve) => { release = resolve; });
  const app = await workspace({ persistent: true, uploadGate, stored: "ws_11111111-1111-4111-8111-111111111111" });
  app.get("workspace-notes").value = "Keep these notes with the previous dataset.";
  await app.get("workspace-notes").emit("input");
  const pending = app.get("sample-button").emit("click");
  await settle();
  for (const id of ["workspace-title", "workspace-notes", "chart-column", "save-workspace", "remove-report", "save-to-account"]) assert.equal(app.get(id).disabled, true, id);
  await app.get("remove-report").emit("click");
  await app.get("save-to-account").emit("click");
  assert.equal(app.get("report").hidden, false);
  const noteRequest = app.requests.find(({ options }) => options.method === "PATCH" && Object.hasOwn(JSON.parse(options.body), "notes"));
  assert.equal(JSON.parse(noteRequest.options.body).notes, "Keep these notes with the previous dataset.");
  assert.equal(app.requests.some(({ url }) => url === "/api/workspaces"), false);
  release(); await pending;
  assert.equal(app.get("workspace-notes").value, "");
  assert.equal(app.get("workspace-notes").disabled, false);
  assert.match(app.storage.get("mr-analytics-report"), /^ws_22222222/);
});

test("late view updates cannot reopen a closed dataset or replace a newly uploaded workspace", async () => {
  let releaseClosed;
  const closedGate = new Promise((resolve) => { releaseClosed = resolve; });
  const closed = await workspace({ persistent: true, viewGate: closedGate, stored: "ws_11111111-1111-4111-8111-111111111111" });
  await closed.get("remove-report").emit("click");
  releaseClosed(); await settle();
  assert.equal(closed.get("report").hidden, true);
  assert.equal(closed.get("saved-workspace-controls").hidden, true);
  assert.equal(closed.storage.has("mr-analytics-report"), false);
  let releaseOld;
  const oldGate = new Promise((resolve) => { releaseOld = resolve; });
  const app = await workspace({ persistent: true, viewGate: oldGate, stored: "ws_11111111-1111-4111-8111-111111111111" });
  await app.get("sample-button").emit("click");
  releaseOld(); await settle();
  app.get("workspace-notes").value = "These notes belong to the new dataset.";
  await app.get("workspace-notes").emit("input");
  assert.equal(await app.window.MrWorkspace.flush(), true);
  const noteRequest = app.requests.find(({ options }) => options.method === "PATCH" && Object.hasOwn(JSON.parse(options.body), "notes"));
  assert.match(noteRequest.url, /22222222-2222-4222-8222-/);
  assert.equal(app.get("report-name").textContent, "New saved dataset 1");
});

test("guest promotion creates one saved workspace and blocks upload and close until complete", async () => {
  let release;
  const promotionGate = new Promise((resolve) => { release = resolve; });
  const app = await workspace({ persistent: true, guestReport: true, promotionGate, stored: "report-test-123" });
  assert.equal(app.get("save-to-account").hidden, false);
  const pending = app.get("save-to-account").emit("click");
  await settle();
  assert.equal(app.get("save-to-account").disabled, true);
  await app.get("save-to-account").emit("click");
  await app.get("sample-button").emit("click");
  await app.get("remove-report").emit("click");
  assert.equal(app.requests.filter(({ url }) => url === "/api/workspaces").length, 1);
  assert.equal(app.requests.some(({ url }) => url === "/upload"), false);
  assert.equal(app.requests.some(({ options }) => options.method === "DELETE"), false);
  release(); await pending;
  assert.match(app.storage.get("mr-analytics-report"), /^ws_33333333/);
  assert.equal(app.get("saved-workspace-controls").hidden, false);
  assert.equal(app.get("save-to-account").hidden, true);
  assert.equal(app.get("csv-file").disabled, false);
});

test("new analysis starts empty while returning to AI restores saved answers", async () => {
  const fresh = await workspace({ persistent: true, stored: "ws_11111111-1111-4111-8111-111111111111", search: "?new=1" });
  assert.equal(fresh.get("report").hidden, true);
  assert.equal(fresh.requests.some(({ url }) => url.startsWith("/api/analysis/")), false);
  const ai = await workspace({ persistent: true, mode: "ai", stored: "ws_11111111-1111-4111-8111-111111111111" });
  assert.match(ai.get("answers").textContent, /A saved answer/);
});
