/* 字幕节奏与镜头边界校正台 —— 前端（原生 JS） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let state = null;       // /api/state
let analysis = null;    // /api/analysis
let solution = null;    // 最近一次 /api/solve 结果

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function fmtMs(ms) {
  ms = Math.max(0, Math.round(ms));
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const milli = ms % 1000;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:` +
         `${String(s).padStart(2, "0")},${String(milli).padStart(3, "0")}`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function msg(text, kind = "") {
  const el = $("#msg");
  el.textContent = text;
  el.className = "msg " + kind;
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// 状态渲染
// ---------------------------------------------------------------------------

function renderSourceInfo() {
  if (!state) return;
  const parts = [];
  const sub = state.subtitle;
  if (sub.filename) {
    parts.push(`字幕：<b>${esc(sub.filename)}</b>（${(sub.format || "").toUpperCase()}，${sub.cue_count} 条，SHA256 <code>${esc((sub.sha256 || "").slice(0, 16))}…</code>）`);
  } else {
    parts.push("未导入字幕");
  }
  if (state.shots.filename) {
    parts.push(`镜头：<b>${esc(state.shots.filename)}</b>（${state.shots.shot_count} 个镜头）`);
  } else {
    parts.push("未导入镜头（将不做跨镜头检查与镜头约束）");
  }
  $("#source-info").innerHTML = parts.join("　｜　");
}

function fillSettings() {
  if (!state) return;
  $("#in-cps").value = state.settings.max_cps;
  $("#in-gap").value = state.settings.min_gap_ms;
  $("#in-tol").value = state.settings.boundary_tolerance_ms;
}

// ---------------------------------------------------------------------------
// 导入 / 重置 / 设置
// ---------------------------------------------------------------------------

$("#btn-import").addEventListener("click", async () => {
  const sf = $("#subtitle-file").files[0];
  const jf = $("#shots-file").files[0];
  if (!sf && !jf) { msg("请先选择字幕或镜头文件", "err"); return; }
  const fd = new FormData();
  if (sf) fd.append("subtitle", sf);
  if (jf) fd.append("shots", jf);
  msg("正在导入…");
  try {
    state = await api("/api/import", { method: "POST", body: fd });
    analysis = null;
    solution = state.solution || null;
    renderSourceInfo();
    clearTables();
    msg(`导入成功：${state.subtitle.cue_count} 条字幕，${state.shots.shot_count} 个镜头`, "ok");
  } catch (e) {
    msg(e.message, "err");
  }
});

$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("确定清空已导入的字幕、镜头与校正方案？")) return;
  state = await api("/api/reset", { method: "POST" });
  analysis = null; solution = null;
  renderSourceInfo();
  clearTables();
  msg("已清空全部数据", "ok");
});

$("#btn-save-settings").addEventListener("click", async () => {
  const body = {
    max_cps: parseFloat($("#in-cps").value),
    min_gap_ms: parseInt($("#in-gap").value, 10),
    boundary_tolerance_ms: parseInt($("#in-tol").value, 10),
  };
  if (!(body.max_cps > 0) || body.min_gap_ms < 0 || body.boundary_tolerance_ms < 0) {
    msg("参数不合法", "err"); return;
  }
  state.settings = await api("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  solution = null;
  clearTables();
  msg("参数已保存（旧方案已失效，请重新检查/校正）", "ok");
});

// ---------------------------------------------------------------------------
// 分析
// ---------------------------------------------------------------------------

$("#btn-analyze").addEventListener("click", async () => {
  try {
    analysis = await api("/api/analysis");
    renderIssues();
    switchTab("issues");
    const nErr = analysis.cues.reduce(
      (acc, c) => acc + c.issues.filter((i) => i.severity === "error").length, 0);
    msg(nErr ? `检查完成：发现 ${nErr} 处问题` : "检查完成：未发现违规", nErr ? "err" : "ok");
  } catch (e) { msg(e.message, "err"); }
});

function issueTags(issues) {
  if (!issues || !issues.length) return '<span class="tag info">无</span>';
  return issues.map((i) =>
    `<span class="tag ${i.severity}" title="${esc(i.message)}">${esc(i.message)}</span>`
  ).join("");
}

function renderIssues() {
  const tbody = $("#issues-table tbody");
  if (!analysis) { tbody.innerHTML = ""; $("#issues-summary").innerHTML = ""; return; }
  let errN = 0, badCues = 0;
  const rows = analysis.cues.map((c, i) => {
    const errs = c.issues.filter((x) => x.severity === "error");
    errN += errs.length;
    if (errs.length) badCues++;
    const cps = c.duration_ms > 0 ? (c.grapheme_count / (c.duration_ms / 1000)).toFixed(2) : "∞";
    return `<tr>
      <td>${i + 1}</td>
      <td class="mono">${esc(c.cue_id)}</td>
      <td class="mono">${fmtMs(c.start_ms)} →<br>${fmtMs(c.end_ms)}</td>
      <td class="mono">${c.duration_ms}</td>
      <td>${c.grapheme_count}</td>
      <td class="mono">${cps}</td>
      <td>${issueTags(c.issues)}</td>
      <td class="text-cell">${esc(c.text)}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.join("");
  const s = state.settings;
  $("#issues-summary").innerHTML = `
    <span class="pill">共 ${analysis.cues.length} 条</span>
    <span class="pill ${badCues ? "bad" : "good"}">${badCues} 条存在违规（${errN} 处）</span>
    <span class="pill">CPS 上限 ${s.max_cps}</span>
    <span class="pill">最小间隔 ${s.min_gap_ms} ms</span>
    <span class="pill">边界容差 ±${s.boundary_tolerance_ms} ms</span>`;
}

// ---------------------------------------------------------------------------
// 求解
// ---------------------------------------------------------------------------

$("#btn-solve").addEventListener("click", async () => {
  msg("正在搜索确定性最优平移方案…");
  try {
    solution = await api("/api/solve", { method: "POST" });
    state.solution = solution;
    state.has_solution = true;
    renderSolution();
    renderConflicts();
    renderTimeline();
    switchTab(solution.feasible ? "solution" : "conflicts");
    const st = solution.stats;
    if (solution.feasible) {
      msg(`已找到最优方案：总移动 ${st.total_abs_shift_ms} ms，改动 ${st.changed_count} 条`, "ok");
    } else {
      msg(`约束无法全部满足：${solution.conflict_chains.length} 条冲突链；其余条目仍给出最优建议`, "err");
    }
  } catch (e) { msg(e.message, "err"); }
});

function statusLabel(st) {
  return {
    corrected: "已校正",
    unmodified: "无需改动",
    conflict: "冲突链中",
    cps_conflict: "CPS 超限",
  }[st] || st;
}

function renderSolution() {
  const tbody = $("#solution-table tbody");
  if (!solution) { tbody.innerHTML = ""; $("#solution-summary").innerHTML = ""; return; }
  const rows = solution.items.map((it) => {
    const lockable = it.status === "conflict" || it.status === "cps_conflict";
    const remain = (it.issues_after || []).filter((x) => x.severity === "error");
    return `<tr>
      <td><input type="checkbox" data-cue="${esc(it.cue_id)}" ${it.adopted ? "checked" : ""}
        ${lockable ? "disabled" : ""}></td>
      <td>${it.order + 1}</td>
      <td class="mono">${esc(it.cue_id)}${it.adopted ? ' <span class="tag st-adopted">已采纳</span>' : ""}</td>
      <td class="mono">${fmtMs(it.original_start_ms)} →<br><b>${fmtMs(it.corrected_start_ms)}</b></td>
      <td class="mono" style="color:${it.delta_ms ? '#1d4ed8' : '#64748b'}">${it.delta_ms > 0 ? "+" : ""}${it.delta_ms}</td>
      <td class="mono">${it.duration_ms}</td>
      <td><span class="tag st-${it.status}">${statusLabel(it.status)}</span></td>
      <td>${remain.length ? issueTags(remain) : '<span class="tag info">—</span>'}</td>
      <td class="text-cell">${esc(it.text)}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.join("");
  $$("#solution-table input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      try {
        await api(`/api/cues/${encodeURIComponent(cb.dataset.cue)}/adopt`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ adopted: cb.checked }),
        });
        const it = solution.items.find((x) => x.cue_id === cb.dataset.cue);
        if (it) it.adopted = cb.checked;
        renderSolution();
      } catch (e) { msg(e.message, "err"); cb.checked = !cb.checked; }
    });
  });
  const st = solution.stats;
  const adopted = solution.items.filter((i) => i.adopted).length;
  $("#solution-summary").innerHTML = `
    <span class="pill ${solution.feasible ? "good" : "bad"}">${solution.feasible ? "全部约束可满足" : "存在不可满足冲突"}</span>
    <span class="pill">总移动 ${st.total_abs_shift_ms} ms（L1）</span>
    <span class="pill">改动 ${st.changed_count} 条（L2）</span>
    <span class="pill bad">时序冲突 ${st.conflict_count} 条</span>
    <span class="pill bad">CPS 冲突 ${st.cps_conflict_count} 条</span>
    <span class="pill">已采纳 ${adopted} 条</span>`;
}

$("#btn-adopt-all").addEventListener("click", async () => {
  if (!solution) { msg("请先生成校正方案", "err"); return; }
  const r = await api("/api/adopt", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  solution.items.forEach((it) => {
    if (it.status !== "conflict" && it.status !== "cps_conflict") it.adopted = true;
  });
  renderSolution();
  msg(`已采纳 ${r.adopted_count} 条`, "ok");
});

$("#btn-adopt-reset").addEventListener("click", async () => {
  if (!solution) { msg("请先生成校正方案", "err"); return; }
  await api("/api/adopt/reset", { method: "POST" });
  solution.items.forEach((it) => { it.adopted = false; });
  renderSolution();
  msg("已清除全部采纳标记", "ok");
});

// ---------------------------------------------------------------------------
// 冲突链
// ---------------------------------------------------------------------------

function renderConflicts() {
  const box = $("#conflicts-box");
  if (!solution) { box.innerHTML = ""; return; }
  if (!solution.conflict_chains.length) {
    box.innerHTML = '<p class="hint" style="font-size:14px;color:#166534">✅ 所有约束均可满足，无冲突链。</p>';
    return;
  }
  box.innerHTML = solution.conflict_chains.map((ch, idx) => {
    const title = ch.type === "cps_intrinsic" ? `CPS 内在冲突 #${idx + 1}` : `时序冲突链 #${idx + 1}`;
    const ids = ch.cue_ids.map((id) => {
      const role = (ch.roles && ch.roles[id]) || "member";
      return `<span class="cid ${role}" title="${esc(role)}">${esc(id)}</span>`;
    }).join('<span class="arrow"> → </span>');
    return `<div class="chain">
      <h3>${title}${ch.pivot ? `（关键节点：${esc(ch.pivot)}）` : ""}</h3>
      <div class="reason">${esc(ch.reason)}</div>
      <div class="ids">${ids}</div>
    </div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// 时间轴
// ---------------------------------------------------------------------------

function renderTimeline() {
  const box = $("#timeline-box");
  if (!solution || !state) { box.innerHTML = ""; return; }
  const items = solution.items;
  const shots = state._shots || [];
  const maxMs = Math.max(
    ...items.map((i) => Math.max(i.original_end_ms, i.corrected_end_ms)),
    shots.reduce((a, s) => Math.max(a, s.end_ms), 0),
    1
  );
  const W = 1100;
  const pct = (ms) => (ms / maxMs) * 100;

  let shotHtml = "";
  if (shots.length) {
    shotHtml = `<div class="tl-track shots">` + shots.map((s) =>
      `<div class="tl-seg shot" style="left:${pct(s.start_ms)}%;width:${Math.max(0.1, pct(s.end_ms - s.start_ms))}%"
        title="${esc(s.shot_id)} ${fmtMs(s.start_ms)}→${fmtMs(s.end_ms)}"></div>`
    ).join("") + `</div>`;
  }

  const cueHtml = items.map((it) => {
    const conflict = it.status === "conflict" || it.status === "cps_conflict";
    const orig = `<div class="tl-seg ${conflict ? "conflict" : ""}"
      style="left:${pct(it.original_start_ms)}%;width:${Math.max(0.15, pct(it.duration_ms))}%"
      title="${esc(it.cue_id)} 原 ${fmtMs(it.original_start_ms)}→${fmtMs(it.original_end_ms)}"></div>`;
    const corr = it.delta_ms !== 0 ? `<div class="tl-seg corr"
      style="left:${pct(it.corrected_start_ms)}%;width:${Math.max(0.15, pct(it.duration_ms))}%"
      title="${esc(it.cue_id)} 校正 ${fmtMs(it.corrected_start_ms)} (Δ${it.delta_ms})"></div>` : "";
    return `<div class="track">${orig}${corr}</div>`;
  }).join("");

  const ticks = Array.from({ length: 11 }, (_, i) => {
    const ms = Math.round((maxMs * i) / 10);
    return `<span class="tl-tick" style="left:${i * 10}%">${fmtMs(ms)}</span>`;
  }).join("");

  box.innerHTML = `
    <div class="tl-legend">
      <span><span class="sw" style="background:#bfdbfe;border:1px solid #93c5fd"></span>原始位置</span>
      <span><span class="sw" style="background:#86efac;border:1px solid #4ade80"></span>校正后位置</span>
      <span><span class="sw" style="background:#e5e7eb;border:1px solid #d1d5db"></span>镜头</span>
      <span><span class="sw" style="background:#fecaca;border:1px solid #ef4444"></span>冲突条</span>
    </div>
    <div class="tl">${shotHtml}${cueHtml}<div class="tl-axis">${ticks}</div></div>`;
}

// ---------------------------------------------------------------------------
// 导出
// ---------------------------------------------------------------------------

$("#btn-export").addEventListener("click", async () => {
  if (!solution) { msg("请先生成校正方案", "err"); return; }
  try {
    const res = await fetch("/api/export", { method: "POST" });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || res.statusText);
    }
    const blob = await res.blob();
    const disp = res.headers.get("Content-Disposition") || "";
    const m = disp.match(/filename="?([^"]+)"?/);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = m ? m[1] : "correction.json";
    a.click();
    URL.revokeObjectURL(url);
    state.export_count = (state.export_count || 0) + 1;
    msg("已导出校正 JSON", "ok");
  } catch (e) { msg(e.message, "err"); }
});

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

function clearTables() {
  $("#issues-table tbody").innerHTML = "";
  $("#issues-summary").innerHTML = "";
  $("#solution-table tbody").innerHTML = "";
  $("#solution-summary").innerHTML = "";
  $("#conflicts-box").innerHTML = "";
  $("#timeline-box").innerHTML = "";
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

async function init() {
  state = await api("/api/state");
  renderSourceInfo();
  fillSettings();
  if (state.solution) solution = state.solution;
  try {
    analysis = await api("/api/analysis");
    state._shots = analysis.shots || [];
    renderIssues();
  } catch (e) {
    state._shots = [];
  }
  if (solution) {
    renderSolution();
    renderConflicts();
    renderTimeline();
  }
}

init();
