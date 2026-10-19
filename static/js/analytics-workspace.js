"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const mode = document.body.dataset.workspace;
  const storageKey = "mr-analytics-report";
  let analysis = null;
  let reportId = null;
  let selectedFile = null;
  let busy = false;
  let savedWorkspace = null;
  let account = null;
  let dirty = false;
  let saveTimer = null;
  let saveLoop = null;
  let saveBlocked = false;
  let sectionWorkspaceId = null;
  const accountClient = typeof window !== "undefined" ? window.MrAccount : null;

  function remember(id) {
    try { if (id) sessionStorage.setItem(storageKey, id); else sessionStorage.removeItem(storageKey); } catch { /* Storage is optional. */ }
  }
  function remembered() {
    try { return sessionStorage.getItem(storageKey); } catch { return null; }
  }
  function status(id, message, state = "") {
    const target = $(id);
    target.textContent = message;
    target.className = `status ${state}`;
  }
  function number(value) {
    if (value === null || value === undefined) return "—";
    return typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 3 }) : String(value);
  }
  async function api(url, options) {
    if (accountClient) return accountClient.request(url, options);
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || "The request could not be completed. Please try again.");
    return payload;
  }
  function applyPayload(payload) {
    analysis = payload.analysis || payload.workspace?.analysis;
    reportId = payload.analysis_id || "ws_" + payload.workspace.id;
    savedWorkspace = payload.workspace || null;
    dirty = false; saveBlocked = false;
    remember(reportId);
  }
  function renderSaveState() {
    if (mode !== "analysis") return;
    $("saved-workspace-controls").hidden = !savedWorkspace;
    $("save-signin-link").hidden = Boolean(account?.user);
    $("save-to-account").hidden = !account?.user || !analysis || Boolean(savedWorkspace);
    $("account-save-copy").textContent = savedWorkspace ? "This dataset and your progress are saved to your account." : account?.user ? "New uploads are saved to your account. You can save an open guest report below." : "Sign in before uploading to save your datasets and continue next time.";
    $("remove-report").textContent = savedWorkspace ? "Close dataset" : "Remove dataset";
    if (!savedWorkspace) return;
    $("workspace-title").value = savedWorkspace.title;
    $("workspace-notes").value = savedWorkspace.notes || "";
    $("download-original").hidden = !savedWorkspace.has_original_csv;
    $("download-original").href = `/api/workspaces/${encodeURIComponent(savedWorkspace.id)}/download`;
    const column = savedWorkspace.state?.chart_column;
    if (column && Object.hasOwn(analysis.numeric_histograms, column)) $("chart-column").value = column;
    status("workspace-save-status", "Saved to your account.", "success");
  }
  function updateAnalysisControls() {
    if (mode !== "analysis") return;
    const editing = busy || Boolean(sectionWorkspaceId && savedWorkspace?.id === sectionWorkspaceId);
    ["workspace-title", "workspace-notes", "save-workspace"].forEach((id) => { $(id).disabled = editing; });
    $("chart-column").disabled = editing || !analysis || !Object.keys(analysis.numeric_histograms).length;
    $("analyze-button").disabled = busy || !selectedFile;
    ["sample-button", "csv-file", "remove-report", "save-to-account"].forEach((id) => { $(id).disabled = busy; });
  }
  function scheduleSave() {
    if (!savedWorkspace) return;
    dirty = true;
    status("workspace-save-status", saveBlocked ? "This workspace changed elsewhere. Your edits remain here; reload before saving again." : "Unsaved changes…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, 700);
  }
  function flushSave() {
    if (saveLoop) return saveLoop;
    if (!savedWorkspace || !dirty) return Promise.resolve(true);
    if (saveBlocked) return Promise.resolve(false);
    clearTimeout(saveTimer);
    saveLoop = (async () => {
      while (dirty && savedWorkspace) {
        dirty = false;
        status("workspace-save-status", "Saving your progress…");
        const workspaceId = savedWorkspace.id;
        const changes = {
          title: $("workspace-title").value,
          notes: $("workspace-notes").value,
          state: { ...(savedWorkspace.state || {}), chart_column: $("chart-column").value, active_section: "analysis" },
          expected_revision: savedWorkspace.revision,
        };
        try {
          const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) });
          if (savedWorkspace?.id !== workspaceId) return true;
          savedWorkspace = result.workspace;
          $("report-name").textContent = savedWorkspace.title;
        } catch (error) {
          dirty = true;
          saveBlocked = error.status === 409;
          status("workspace-save-status", `${error.message} Your edits are still on this page.`, "error");
          return false;
        }
      }
      status("workspace-save-status", "All changes saved to your account.", "success");
      return true;
    })().finally(() => { saveLoop = null; });
    return saveLoop;
  }
  async function recordSection(section) {
    if (!savedWorkspace) return;
    const workspaceId = savedWorkspace.id;
    sectionWorkspaceId = workspaceId;
    updateAnalysisControls();
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state: { ...(savedWorkspace.state || {}), active_section: section }, expected_revision: savedWorkspace.revision }) });
      if (savedWorkspace?.id === workspaceId) savedWorkspace = result.workspace;
    } catch (error) {
      if (savedWorkspace?.id === workspaceId) status(mode === "analysis" ? "workspace-save-status" : "question-status", `Your last view could not be saved: ${error.message}`, "error");
    } finally {
      if (sectionWorkspaceId === workspaceId) sectionWorkspaceId = null;
      updateAnalysisControls();
    }
  }
  function makeTable(target, headers, rows) {
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    headers.forEach((label) => { const th = document.createElement("th"); th.scope = "col"; th.textContent = label; head.append(th); });
    const body = table.createTBody();
    rows.forEach((values) => {
      const row = body.insertRow();
      values.forEach((value) => { const cell = row.insertCell(); cell.textContent = number(value); });
    });
    $(target).replaceChildren(table);
  }
  function drawChart() {
    const column = $("chart-column").value;
    const histogram = analysis.numeric_histograms[column];
    const target = $("distribution-chart");
    target.replaceChildren();
    if (!histogram || !histogram.counts.length) { target.textContent = "No numeric values to chart."; return; }
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 560 220");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Distribution of ${column}. ${histogram.labels.map((label, i) => `${label}: ${histogram.counts[i]} values`).join("; ")}`);
    svg.classList.add("histogram");
    const peak = Math.max(...histogram.counts, 1);
    const width = 500 / histogram.counts.length;
    histogram.counts.forEach((count, i) => {
      const bar = document.createElementNS(ns, "rect");
      const height = count / peak * 155;
      bar.setAttribute("x", String(40 + i * width)); bar.setAttribute("y", String(175 - height));
      bar.setAttribute("width", String(Math.max(1, width - 5))); bar.setAttribute("height", String(height));
      const title = document.createElementNS(ns, "title"); title.textContent = `${histogram.labels[i]}: ${count} values`; bar.append(title); svg.append(bar);
    });
    const labels = [["0", 16, 178], [number(peak), 10, 20], [number(histogram.edges[0]), 40, 205]];
    if (histogram.labels.length > 1) labels.push([number(histogram.edges.at(-1)), 540, 205]);
    labels.forEach(([text, x, y], i) => { const label = document.createElementNS(ns, "text"); label.textContent = text; label.setAttribute("x", x); label.setAttribute("y", y); if (i === 3) label.setAttribute("text-anchor", "end"); svg.append(label); });
    target.append(svg);
  }
  function showReport() {
    $("report").hidden = false;
    $("empty-state").hidden = true;
    $("report-name").textContent = savedWorkspace?.title || analysis.filename;
    const metrics = [["Rows", analysis.rows], ["Columns", analysis.columns], ["Missing cells", analysis.total_missing_values], ["Duplicate rows", analysis.duplicate_rows]];
    $("report-stats").replaceChildren(...metrics.map(([label, value]) => {
      const tile = document.createElement("div"); tile.className = "stat";
      const text = document.createElement("span"); text.className = "muted"; text.textContent = label;
      const strong = document.createElement("strong"); strong.textContent = number(value); tile.append(text, strong); return tile;
    }));
    $("insights").replaceChildren(...analysis.insights.map((insight) => { const li = document.createElement("li"); li.textContent = insight; return li; }));
    makeTable("schema-table", ["Column", "Detected type", "Missing", "Potential outliers (IQR)"], analysis.column_names.map((col) => [col, analysis.column_types[col], analysis.missing_values[col], analysis.outliers[col]?.count ?? "—"]));
    const numeric = Object.entries(analysis.numeric_statistics);
    if (numeric.length) {
      makeTable("statistics-table", ["Column", "Count", "Mean", "Median", "Min", "Max", "Std deviation"], numeric.map(([col, s]) => [col, s.count, s.mean, s["50%"], s.min, s.max, s.std]));
    } else $("statistics-table").textContent = "No numeric columns were detected in this dataset.";
    makeTable("preview-table", analysis.column_names, analysis.preview.map((row) => analysis.column_names.map((col) => row[col])));
    $("chart-column").replaceChildren(...Object.keys(analysis.numeric_histograms).map((col) => { const option = document.createElement("option"); option.value = col; option.textContent = col; return option; }));
    $("chart-column").disabled = !numeric.length;
    renderSaveState();
    drawChart();
    updateAnalysisControls();
    $("open-ai").href = `/ai-assistant.html?analysis=${encodeURIComponent(reportId)}`;
  }
  function chooseFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".csv")) { status("upload-status", "Please choose a CSV file.", "error"); return; }
    if (file.size > 10 * 1024 * 1024) { status("upload-status", "The CSV must be 10 MB or smaller.", "error"); return; }
    selectedFile = file;
    $("analyze-button").disabled = busy;
    status("upload-status", `Selected ${file.name}. Ready to analyze.`);
  }
  async function upload() {
    if (!selectedFile || busy) return;
    busy = true;
    updateAnalysisControls();
    $("analyze-button").textContent = "Analyzing…";
    status("upload-status", "Checking your CSV and computing the report…");
    const data = new FormData(); data.append("file", selectedFile);
    try {
      if (!(await flushSave())) throw new Error("Please save or resolve your current workspace changes before uploading another dataset.");
      const result = await api("/upload", { method: "POST", body: data });
      const oldId = reportId;
      applyPayload(result); showReport();
      if (oldId && oldId !== reportId && !oldId.startsWith("ws_")) await api(`/api/analysis/${encodeURIComponent(oldId)}`, { method: "DELETE" }).catch(() => {});
      status("upload-status", `Analyzed ${analysis.filename}: ${number(analysis.rows)} rows and ${analysis.columns} columns.${savedWorkspace ? " Saved to your account." : " This guest report is temporary."}`, "success");
    } catch (error) { status("upload-status", error.message, "error"); }
    finally { busy = false; updateAnalysisControls(); $("analyze-button").textContent = "Analyze CSV"; }
  }
  async function loadReport(id) {
    const payload = await api(`/api/analysis/${encodeURIComponent(id)}`);
    applyPayload({ ...payload, analysis_id: id });
  }
  async function setupAnalysis() {
    busy = true;
    updateAnalysisControls();
    $("csv-file").addEventListener("change", (event) => { selectedFile = null; $("analyze-button").disabled = true; chooseFile(event.target.files[0]); });
    $("upload-form").addEventListener("submit", (event) => { event.preventDefault(); upload(); });
    const zone = $("upload-zone");
    zone.addEventListener("dragover", (event) => { event.preventDefault(); if (!busy) zone.classList.add("is-dragging"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
    zone.addEventListener("drop", (event) => { event.preventDefault(); zone.classList.remove("is-dragging"); if (busy) return; selectedFile = null; $("analyze-button").disabled = true; chooseFile(event.dataTransfer.files[0]); });
    $("sample-button").addEventListener("click", async () => {
      if (busy) return;
      try { const response = await fetch("/static/samples/research-sample.csv"); if (!response.ok) throw new Error("Sample dataset unavailable."); chooseFile(new File([await response.text()], "research-sample.csv", { type: "text/csv" })); await upload(); }
      catch (error) { status("upload-status", error.message, "error"); }
    });
    $("chart-column").addEventListener("change", () => { drawChart(); scheduleSave(); });
    $("workspace-title").addEventListener("input", scheduleSave);
    $("workspace-notes").addEventListener("input", scheduleSave);
    $("save-workspace").addEventListener("click", flushSave);
    $("save-to-account").addEventListener("click", async () => {
      if (!reportId || savedWorkspace || busy) return;
      busy = true;
      updateAnalysisControls();
      try {
        const result = await api("/api/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ analysis_id: reportId }) });
        applyPayload(result); showReport();
        status("upload-status", "This report is saved to your account. Upload the original CSV while signed in if you also want to retain the original file.", "success");
      } catch (error) { status("upload-status", error.message, "error"); }
      finally { busy = false; updateAnalysisControls(); }
    });
    ["back-to-dashboard", "open-ai"].forEach((id) => $(id).addEventListener("click", async (event) => {
      if (!dirty && !saveLoop) return;
      event.preventDefault();
      if (await flushSave()) window.location.assign($(id).href);
    }));
    $("download-report").addEventListener("click", () => {
      if (!analysis) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(analysis, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = `${analysis.filename.replace(/\.csv$/i, "")}-report.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    $("remove-report").addEventListener("click", async () => {
      if (!reportId || busy) return;
      busy = true;
      updateAnalysisControls();
      try {
        if (!(await flushSave())) return;
        const wasSaved = Boolean(savedWorkspace);
        if (!wasSaved) await api(`/api/analysis/${encodeURIComponent(reportId)}`, { method: "DELETE" });
        remember(null); reportId = null; analysis = null; selectedFile = null; savedWorkspace = null;
        $("csv-file").value = ""; $("report").hidden = true; $("empty-state").hidden = false; $("analyze-button").disabled = true;
        renderSaveState();
        status("upload-status", wasSaved ? "Dataset closed. It is still available in My dashboard." : "Dataset removed. Choose another CSV to start again.");
      }
      catch (error) { status("upload-status", error.message, "error"); }
      finally { busy = false; updateAnalysisControls(); }
    });
    if (accountClient) account = await accountClient.ready;
    renderSaveState();
    const params = new URLSearchParams(location.search);
    const id = params.get("new") === "1" ? null : params.get("workspace") ? "ws_" + params.get("workspace") : remembered();
    if (params.get("new") === "1") remember(null);
    if (id) {
      try { await loadReport(id); showReport(); }
      catch (error) { remember(null); status("upload-status", error.message, "error"); }
    }
    busy = false;
    updateAnalysisControls();
    if (savedWorkspace) await recordSection("analysis");
  }
  async function setupAI() {
    let enabled = false;
    let asking = false;
    const refreshButton = () => { $("ask-button").disabled = !enabled || !reportId || asking; };
    document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => { $("question").value = button.dataset.question; $("question").focus(); }));
    if (accountClient) account = await accountClient.ready;
    const params = new URLSearchParams(location.search);
    const id = params.get("workspace") ? "ws_" + params.get("workspace") : params.get("analysis") || remembered();
    if (id) {
      try {
        await loadReport(id); $("active-dataset").textContent = savedWorkspace?.title || analysis.filename;
        $("dataset-description").textContent = `${number(analysis.rows)} rows · ${analysis.columns} columns · ${number(analysis.total_missing_values)} missing cells`;
        for (const message of savedWorkspace?.messages || []) renderAnswer(message.question, message.answer);
        await recordSection("ai");
      }
      catch (error) { remember(null); status("question-status", error.message, "error"); }
    }
    try {
      const availability = await api("/api/ai-status"); enabled = availability.enabled;
      $("ai-status").textContent = enabled ? "AI questions are available. Enter your access code and review the sharing consent below." : "AI questions are not enabled on this installation yet. Automated CSV reports, charts, and quality checks are available in the data workspace.";
    } catch { $("ai-status").textContent = "AI availability could not be checked. Please try again later."; }
    refreshButton();
    $("question-form").addEventListener("submit", async (event) => {
      event.preventDefault(); if (!enabled || !reportId || asking) return;
      const question = $("question").value.trim();
      if (!question || !$("ai-consent").checked) return;
      asking = true; refreshButton(); $("ask-button").textContent = "Thinking…"; status("question-status", "Your analyst is reviewing the computed summary…");
      try {
        const result = await api(`/api/analysis/${encodeURIComponent(reportId)}/question`, { method: "POST", headers: { "Content-Type": "application/json", "X-Analyst-Token": $("ai-access-code").value }, body: JSON.stringify({ question, consent: true }) });
        renderAnswer(question, result.answer);
        status("question-status", result.save_warning || "Answer ready. Check its interpretation against your dataset.", result.save_warning ? "error" : "success");
      } catch (error) { status("question-status", error.message, "error"); }
      finally { asking = false; refreshButton(); $("ask-button").textContent = "Ask analyst"; }
    });
  }
  function renderAnswer(question, text) {
    const item = document.createElement("article"); item.className = "chat-message";
    const heading = document.createElement("h3"); heading.textContent = question;
    const answer = document.createElement("p"); answer.textContent = text; item.append(heading, answer); $("answers").prepend(item);
  }
  if (typeof window !== "undefined") {
    window.MrWorkspace = { flush: flushSave };
    window.addEventListener("beforeunload", (event) => { if (dirty || saveLoop) { event.preventDefault(); event.returnValue = ""; } });
  }
  if (mode === "analysis") setupAnalysis();
  if (mode === "ai") setupAI();
})();
