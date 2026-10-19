(() => {
  "use strict";
  const get = (id) => document.getElementById(id);
  const list = get("workspace-list");
  if (!list || !window.MrAccount) return;
  let workspaces = [];
  let pendingDelete = null;
  let returnFocus = null;
  const date = (value) => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "Saved in your account" : "Saved " + new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parsed);
  };
  const number = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "—";
  const url = (workspace) => "/dashboard.html?workspace=" + encodeURIComponent(workspace.id);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const notify = (message, kind = "") => {
    get("workspace-list-status").textContent = message;
    get("workspace-list-status").className = "account-form-status" + (kind ? " " + kind : "");
  };
  function workspaceCard(workspace) {
    const card = node("article", undefined, "panel workspace-card");
    const heading = node("div", undefined, "workspace-card-heading");
    const icon = node("span", "CSV", "workspace-file-mark");
    icon.setAttribute("aria-hidden", "true");
    const title = node("h3");
    const link = node("a", workspace.title || workspace.filename || "Untitled workspace");
    link.href = url(workspace);
    title.append(link);
    heading.append(icon, title);
    const filename = node("p", workspace.filename || "CSV dataset", "workspace-filename");
    const dimensions = node("p", number(workspace.row_count) + " rows · " + number(workspace.column_count) + " columns", "workspace-dimensions");
    const saved = node("p", date(workspace.updated_at || workspace.created_at), "workspace-date");
    const actions = node("div", undefined, "actions workspace-card-actions");
    const resume = node("a", "Resume →", "button button-small");
    resume.href = url(workspace);
    const rename = node("button", "Rename", "button button-small button-secondary");
    rename.type = "button";
    rename.setAttribute("aria-label", "Rename " + (workspace.title || workspace.filename || "workspace"));
    const remove = node("button", "Delete", "workspace-delete-button");
    remove.type = "button";
    remove.setAttribute("aria-label", "Delete " + (workspace.title || workspace.filename || "workspace"));
    actions.append(resume, rename, remove);
    const renameForm = node("form", undefined, "workspace-rename-form");
    renameForm.hidden = true;
    const label = node("label", "Workspace name");
    const input = node("input");
    input.id = "rename-" + workspace.id;
    label.htmlFor = input.id;
    input.type = "text";
    input.required = true;
    input.maxLength = 120;
    input.value = workspace.title || workspace.filename || "Untitled workspace";
    const renameActions = node("div", undefined, "actions");
    const save = node("button", "Save name", "button button-small");
    save.type = "submit";
    const cancel = node("button", "Cancel", "button button-small button-secondary");
    cancel.type = "button";
    const errorMessage = node("p", "", "account-form-status error");
    errorMessage.setAttribute("role", "status");
    renameActions.append(save, cancel);
    renameForm.append(label, input, renameActions, errorMessage);
    rename.addEventListener("click", () => { renameForm.hidden = false; actions.hidden = true; input.focus(); input.select(); });
    cancel.addEventListener("click", () => { renameForm.hidden = true; actions.hidden = false; rename.focus(); });
    renameForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const newTitle = input.value.trim();
      if (!newTitle) { errorMessage.textContent = "Give this workspace a name."; input.focus(); return; }
      save.disabled = true; cancel.disabled = true; input.disabled = true; errorMessage.textContent = "";
      try {
        await window.MrAccount.request("/api/workspaces/" + encodeURIComponent(workspace.id), { method: "PATCH", body: JSON.stringify({ title: newTitle, expected_revision: workspace.revision }) });
        if (await load()) notify("Workspace renamed.", "success");
      } catch (error) {
        errorMessage.textContent = error.status === 409 ? "This workspace changed in another tab. Refresh your dashboard before renaming it." : error.message;
      } finally { save.disabled = false; cancel.disabled = false; input.disabled = false; }
    });
    remove.addEventListener("click", () => {
      pendingDelete = workspace; returnFocus = remove;
      get("delete-workspace-name").textContent = workspace.title || workspace.filename || "this workspace";
      get("delete-workspace-status").textContent = "";
      get("workspace-delete-dialog").showModal(); get("cancel-workspace-delete").focus();
    });
    card.append(heading, filename, dimensions, saved, actions, renameForm);
    return card;
  }
  function render() {
    const query = get("workspace-search").value.trim().toLocaleLowerCase();
    const matching = workspaces.filter((workspace) => [workspace.title, workspace.filename].filter(Boolean).join(" ").toLocaleLowerCase().includes(query));
    list.replaceChildren(...matching.map(workspaceCard));
    get("workspace-count").textContent = workspaces.length + (workspaces.length === 1 ? " workspace" : " workspaces");
    get("workspace-count").hidden = false;
    get("workspace-search-field").hidden = workspaces.length === 0;
    get("workspace-empty").hidden = workspaces.length > 0;
    get("workspace-no-results").hidden = matching.length > 0 || workspaces.length === 0;
    get("workspace-continue").hidden = workspaces.length === 0;
    if (workspaces.length) {
      const latest = workspaces[0];
      get("continue-title").textContent = latest.title || latest.filename || "Untitled workspace";
      get("continue-saved").textContent = date(latest.updated_at || latest.created_at);
      get("continue-workspace").href = latest.state?.active_section === "ai" ? "/ai-assistant.html?workspace=" + encodeURIComponent(latest.id) : url(latest);
    }
  }
  async function load() {
    get("workspace-retry").hidden = true; get("workspace-loading").hidden = false;
    get("workspace-collection").setAttribute("aria-busy", "true");
    try {
      const data = await window.MrAccount.request("/api/workspaces");
      workspaces = (Array.isArray(data.workspaces) ? data.workspaces : []).sort((a, b) => (Date.parse(b.updated_at || b.created_at) || 0) - (Date.parse(a.updated_at || a.created_at) || 0));
      notify(""); render(); return true;
    } catch (error) {
      if (error.status === 401) { window.location.assign("/login.html?next=%2Fplatform.html"); return false; }
      notify(error.message || "We could not load your saved workspaces. Please try again.", "error");
      get("workspace-retry").hidden = false; return false;
    } finally { get("workspace-loading").hidden = true; get("workspace-collection").setAttribute("aria-busy", "false"); }
  }
  get("workspace-search").addEventListener("input", render);
  get("workspace-retry").addEventListener("click", load);
  get("cancel-workspace-delete").addEventListener("click", () => get("workspace-delete-dialog").close());
  get("workspace-delete-dialog").addEventListener("close", () => { pendingDelete = null; returnFocus?.focus(); });
  get("confirm-workspace-delete").addEventListener("click", async () => {
    if (!pendingDelete) return;
    const workspace = pendingDelete;
    get("confirm-workspace-delete").disabled = true; get("cancel-workspace-delete").disabled = true;
    try {
      await window.MrAccount.request("/api/workspaces/" + encodeURIComponent(workspace.id), { method: "DELETE" });
      get("workspace-delete-dialog").close(); workspaces = workspaces.filter((item) => item.id !== workspace.id); render();
      notify("Workspace deleted.", "success"); get("workspace-list-title").focus();
    } catch (error) { get("delete-workspace-status").textContent = error.message; }
    finally { get("confirm-workspace-delete").disabled = false; get("cancel-workspace-delete").disabled = false; }
  });
  window.MrAccount.ready.then(async (session) => {
    if (session.error) { get("workspace-loading").hidden = true; notify("We could not verify your account. Reload this page to try again.", "error"); return; }
    if (!session.user) { window.location.replace("/login.html?next=%2Fplatform.html"); return; }
    await load();
  });
})();
