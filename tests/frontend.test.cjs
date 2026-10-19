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

async function workspace({ mode = "analysis", stored = null, enabled = true, failUpload = false, failQuestion = false } = {}) {
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
  const respond = (payload, ok = true) => ({ ok, json: async () => payload });
  const fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/static/samples/research-sample.csv") return { ok: true, text: async () => "category,score\nA,1\nB,2\n" };
    if (url === "/upload") return failUpload ? respond({ message: "CSV contains inconsistent row lengths." }, false) : respond({ analysis_id: "report-test-123", analysis: dataset });
    if (url === "/api/ai-status") return respond({ enabled });
    if (url.endsWith("/question")) return failQuestion ? respond({ message: "AI access code is invalid." }, false) : respond({ answer: "<script>untrusted answer</script> Check the missing values first." });
    if (options.method === "DELETE") return respond({ deleted: true });
    if (url.startsWith("/api/analysis/")) return respond({ analysis: dataset });
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
    fetch, FormData, File, Blob, URL, URLSearchParams, setTimeout,
    location: { search: "" },
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
  };
  vm.runInNewContext(source, context, { filename: "analytics-workspace.js" });
  await settle();
  return { get, requests, storage, prompts, dataset };
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
