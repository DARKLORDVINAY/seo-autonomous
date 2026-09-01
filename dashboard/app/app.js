"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const views = [
    ["overview", "Overview"], ["opportunities", "Opportunities"], ["pages", "Pages"], ["queries", "Queries"],
    ["technical", "Technical"], ["serps", "SERPs"], ["ai-search", "AI Search"], ["experiments", "Experiments"],
    ["actions", "Actions"], ["agents", "Agents"], ["failures", "Failures"], ["strategy", "Strategy"], ["approvals", "Approvals"]
  ];
  const columns = {
    opportunities: ["kind", "page_url", "finding", "score", "confidence", "status"],
    pages: ["title", "url", "status_code", "indexed_status", "crawlable"], queries: ["text", "cluster_id", "is_brand"],
    technical: ["kind", "severity", "description", "status"], serps: ["query", "provider", "location", "observed_at", "is_fixture"],
    "ai-search": ["query", "provider", "capability_status", "observed_at", "is_fixture"],
    experiments: ["name", "hypothesis", "primary_outcome", "status", "verdict", "predicted_confidence"],
    actions: ["kind", "risk", "actor", "reason", "created_at"], agents: ["agent_name", "mode", "status", "input_tokens", "output_tokens", "cost_usd"],
    failures: ["category", "predicted", "actual", "root_cause", "preventative_change"],
    approvals: ["revision_id", "decision", "approved_by", "reason", "expires_at"], strategy: ["type", "decision", "claim", "statement", "objective", "created_at"]
  };
  let token = "", siteId = "", active = "overview", requestGeneration = 0;
  const node = (tag, text, className) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (className) n.className = className; return n; };
  const describe = value => value === null || value === undefined ? "Unknown" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const number = value => value === null || value === undefined ? "Unknown" : new Intl.NumberFormat(undefined, {maximumFractionDigits: 2}).format(value);
  function status(message, error = false) { $("status").textContent = message; $("status").classList.toggle("error", error); }
  async function api(path, options = {}) {
    const response = await fetch(path, {...options, credentials: "omit", headers: {"Authorization": `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {})}});
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request rejected (${response.status})`);
    return body;
  }
  function metric(label, value, detail, primary = false) {
    const card = node("article", undefined, `metric${primary ? " primary" : ""}`);
    card.append(node("p", label, "metric-label"), node("p", value, `metric-value${value === "Unknown" ? " unknown" : ""}`), node("p", detail, "metric-detail"));
    return card;
  }
  function fact(container, label, value) { const row = node("div", undefined, "fact-row"); row.append(node("span", label), node("span", describe(value))); container.append(row); }
  function overview(state) {
    const box = $("overview"); box.replaceChildren(); const m = state.metrics;
    const grid = node("div", undefined, "metric-grid");
    grid.append(metric("QUALIFIED ORGANIC CONVERSION VALUE", number(m.qualified_organic_conversion_value), "Observed total · incremental effect needs an experiment", true),
      metric("QUALIFIED ORGANIC CONVERSIONS", number(m.qualified_organic_conversions), "Business qualification must be verified"),
      metric("ORGANIC SESSIONS", number(m.organic_sessions), "Intermediate signal"),
      metric("SEARCH CLICK-THROUGH RATE", m.ctr == null ? "Unknown" : `${number(m.ctr * 100)}%`, "GSC coverage limitations apply"));
    const second = node("div", undefined, "metric-grid");
    second.append(metric("OPEN OPPORTUNITIES", number(m.open_opportunities), "Evidence-backed diagnostics"), metric("RUNNING EXPERIMENTS", number(m.running_experiments), "Typical checkpoints: 7 / 14 / 28 / 56 days"),
      metric("HUMAN APPROVALS REQUIRED", number(m.human_approvals_required), "Approval binds to the exact revision"), metric("FAILURES / REGRESSIONS", number(m.regressions), "Inspect evidence before attributing cause"));
    const lower = node("div", undefined, "overview-lower"), mission = node("section", undefined, "panel"), notes = node("section", undefined, "panel");
    mission.append(node("h2", "Mission state")); fact(mission, "Objective", "Qualified organic conversion value"); fact(mission, "Autonomy", `Level ${state.site.autonomy_level}`);
    fact(mission, "Production authority", state.site.production_enabled ? "Site enabled · global and revision gates still apply" : "Disabled");
    fact(mission, "Non-brand visibility", m.non_brand_visibility); fact(mission, "AI citation visibility", m.ai_citations);
    const blockers = state.mission?.blockers_json || []; fact(mission, "Current blockers", blockers.length ? blockers.join(" · ") : "None recorded");
    notes.append(node("h2", "Measurement discipline")); const list = node("ul", undefined, "facts-list");
    for (const text of state.measurement_notes) list.append(node("li", text)); notes.append(list); lower.append(mission, notes); box.append(grid, second, lower);
  }
  function table(rows, keys) {
    const box = $("table"); box.replaceChildren();
    if (!rows.length) { box.append(node("p", "No canonical records in this view yet. Missing observations are not zero performance.", "empty")); return; }
    const t = node("table"), head = node("thead"), header = node("tr"), body = node("tbody");
    for (const key of keys) header.append(node("th", key.replaceAll("_", " "))); header.append(node("th", "Evidence")); head.append(header);
    for (const row of rows) {
      const tr = node("tr"); for (const key of keys) { const value = describe(row[key]); const cell = node("td", value.length > 230 ? value.slice(0, 230) + "…" : value); tr.append(cell); }
      const td = node("td"), button = node("button", "Inspect record"); button.addEventListener("click", () => { $("record-json").textContent = JSON.stringify(row, null, 2); $("record-dialog").showModal(); }); td.append(button); tr.append(td); body.append(tr);
    }
    t.append(head, body); box.append(t);
  }
  async function render() {
    if (!siteId) return; const generation = ++requestGeneration; status("Reading canonical state…");
    try {
      const state = await api(`/api/sites/${siteId}/state`); if (generation !== requestGeneration) return;
      $("source-badge").textContent = state.source_mode === "fixture" ? "Fixture data" : "Live site";
      $("autonomy-badge").textContent = `Level ${state.site.autonomy_level} · ${state.site.production_enabled ? "bounded authority" : "shadow"}`;
      $("fixture-notice").hidden = state.source_mode !== "fixture";
      $("period").textContent = state.period.from ? `${state.period.from} – ${state.period.to}` : "No observed period";
      $("overview").hidden = active !== "overview"; $("collection").hidden = active === "overview";
      if (active === "overview") overview(state);
      else {
        const result = await api(`/api/sites/${siteId}/${active}`); if (generation !== requestGeneration) return;
        const rows = active === "strategy" ? Object.entries(result).flatMap(([type, items]) => items.map(item => ({type, ...item}))) : result.items;
        $("collection-title").textContent = views.find(v => v[0] === active)[1];
        $("collection-count").textContent = `${rows.length}${result.total != null ? ` of ${result.total}` : ""} records`;
        $("collection-description").textContent = active === "approvals" ? "Immutable approval history. A later rejection or revocation overrides an earlier approval. Review and approval use a separate trusted capability." : "Inspect a record for source IDs, uncertainty and timestamps. Showing the first bounded page of records.";
        table(rows, columns[active]);
      }
      status(`Updated ${new Date().toLocaleTimeString()}`);
    } catch (error) { status(error.message, true); }
  }
  for (const [key, label] of views) {
    const button = node("button", label, key === active ? "active" : ""); button.dataset.view = key;
    button.addEventListener("click", () => { active = key; $("view-title").textContent = label; for (const b of $("navigation").children) b.classList.toggle("active", b.dataset.view === key); render(); }); $("navigation").append(button);
  }
  $("connect-form").addEventListener("submit", async event => {
    event.preventDefault(); token = $("token").value.trim(); $("token").value = ""; status("Connecting…");
    try {
      const sites = await api("/api/sites"); $("site-select").replaceChildren();
      if (!sites.items.length) { token = ""; throw new Error("No sites registered. Use the administrator bootstrap command to register a site."); }
      for (const site of sites.items) { const option = node("option", site.name); option.value = site.id; $("site-select").append(option); }
      siteId = sites.items[0].id; $("connection").hidden = true; $("workspace").hidden = false; await render();
    } catch (error) { token = ""; status(error.message, true); }
  });
  $("site-select").addEventListener("change", () => { siteId = $("site-select").value; render(); });
  $("refresh").addEventListener("click", render);
  $("disconnect").addEventListener("click", () => { token = ""; siteId = ""; requestGeneration++; $("workspace").hidden = true; $("connection").hidden = false; $("source-badge").textContent = "Disconnected"; status("Disconnected."); });
  $("cycle").addEventListener("click", async () => {
    $("cycle").disabled = true; status("Running a bounded observation cycle…");
    try { const result = await api(`/api/sites/${siteId}/cycle`, {method: "POST", body: JSON.stringify({idempotency_key: `dashboard:${crypto.randomUUID()}`})}); await render(); status(`Cycle ${result.status}. Inspect Actions and Agents for the audit trail.`); }
    catch (error) { status(error.message, true); } finally { $("cycle").disabled = false; }
  });
  $("close-dialog").addEventListener("click", () => $("record-dialog").close());
})();
