"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const mode = document.body.dataset.workspace;
  const storageKey = "mr-analytics-report";
  let analysis = null;
  let reportId = null;
  let selectedFile = null;
  let busy = false;

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
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || "The request could not be completed. Please try again.");
    return payload;
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
    $("report-name").textContent = analysis.filename;
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
    drawChart();
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
    $("analyze-button").disabled = true; $("sample-button").disabled = true; $("csv-file").disabled = true;
    $("analyze-button").textContent = "Analyzing…";
    status("upload-status", "Checking your CSV and computing the report…");
    const data = new FormData(); data.append("file", selectedFile);
    try {
      const result = await api("/upload", { method: "POST", body: data });
      const oldId = reportId;
      analysis = result.analysis; reportId = result.analysis_id; remember(reportId); showReport();
      if (oldId && oldId !== reportId) await api(`/api/analysis/${encodeURIComponent(oldId)}`, { method: "DELETE" }).catch(() => {});
      status("upload-status", `Analyzed ${analysis.filename}: ${number(analysis.rows)} rows and ${analysis.columns} columns.`, "success");
    } catch (error) { status("upload-status", error.message, "error"); }
    finally { busy = false; $("analyze-button").disabled = !selectedFile; $("sample-button").disabled = false; $("csv-file").disabled = false; $("analyze-button").textContent = "Analyze CSV"; }
  }
  async function loadReport(id) {
    const payload = await api(`/api/analysis/${encodeURIComponent(id)}`);
    analysis = payload.analysis; reportId = id; remember(id);
  }
  async function setupAnalysis() {
    $("csv-file").addEventListener("change", (event) => { selectedFile = null; $("analyze-button").disabled = true; chooseFile(event.target.files[0]); });
    $("upload-form").addEventListener("submit", (event) => { event.preventDefault(); upload(); });
    const zone = $("upload-zone");
    zone.addEventListener("dragover", (event) => { event.preventDefault(); if (!busy) zone.classList.add("is-dragging"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
    zone.addEventListener("drop", (event) => { event.preventDefault(); zone.classList.remove("is-dragging"); if (busy) return; selectedFile = null; $("analyze-button").disabled = true; chooseFile(event.dataTransfer.files[0]); });
    $("sample-button").addEventListener("click", async () => {
      try { const response = await fetch("/static/samples/research-sample.csv"); if (!response.ok) throw new Error("Sample dataset unavailable."); chooseFile(new File([await response.text()], "research-sample.csv", { type: "text/csv" })); await upload(); }
      catch (error) { status("upload-status", error.message, "error"); }
    });
    $("chart-column").addEventListener("change", drawChart);
    $("download-report").addEventListener("click", () => {
      if (!analysis) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(analysis, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = `${analysis.filename.replace(/\.csv$/i, "")}-report.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    $("remove-report").addEventListener("click", async () => {
      if (!reportId || busy) return;
      try { await api(`/api/analysis/${encodeURIComponent(reportId)}`, { method: "DELETE" }); remember(null); reportId = null; analysis = null; selectedFile = null; $("csv-file").value = ""; $("report").hidden = true; $("empty-state").hidden = false; $("analyze-button").disabled = true; status("upload-status", "Dataset removed. Choose another CSV to start again."); }
      catch (error) { status("upload-status", error.message, "error"); }
    });
    const id = remembered();
    if (id) { try { await loadReport(id); showReport(); } catch { remember(null); } }
  }
  async function setupAI() {
    let enabled = false;
    let asking = false;
    const refreshButton = () => { $("ask-button").disabled = !enabled || !reportId || asking; };
    document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => { $("question").value = button.dataset.question; $("question").focus(); }));
    const id = new URLSearchParams(location.search).get("analysis") || remembered();
    if (id) {
      try { await loadReport(id); $("active-dataset").textContent = analysis.filename; $("dataset-description").textContent = `${number(analysis.rows)} rows · ${analysis.columns} columns · ${number(analysis.total_missing_values)} missing cells`; }
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
        const item = document.createElement("article"); item.className = "chat-message";
        const heading = document.createElement("h3"); heading.textContent = question;
        const answer = document.createElement("p"); answer.textContent = result.answer; item.append(heading, answer); $("answers").prepend(item);
        status("question-status", "Answer ready. Check its interpretation against your dataset.", "success");
      } catch (error) { status("question-status", error.message, "error"); }
      finally { asking = false; refreshButton(); $("ask-button").textContent = "Ask analyst"; }
    });
  }
  if (mode === "analysis") setupAnalysis();
  if (mode === "ai") setupAI();
})();
