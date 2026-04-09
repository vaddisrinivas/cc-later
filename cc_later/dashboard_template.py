"""Dashboard HTML template — served from localhost, Chart.js from CDN."""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>cc-later Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #0d1117; --surface: #161b22; --surface2: #1c2129; --border: #30363d;
  --text: #e6edf3; --muted: #8b949e; --dim: #484f58;
  --green: #3fb950; --green-bg: #1a3a2a; --yellow: #d29922; --yellow-bg: #3a2f1a;
  --red: #f85149; --red-bg: #3a1a1a; --blue: #58a6ff; --blue-bg: #1a2a3a;
  --purple: #bc8cff; --orange: #d18616;
}
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 24px 32px; max-width: 1280px; margin: 0 auto; line-height: 1.5; }

.header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
.header h1 { font-size: 22px; font-weight: 600; }
.header .meta { color: var(--muted); font-size: 13px; }

/* Stats */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }
.stat-val { font-size: 24px; font-weight: 700; }
.stat-lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }

/* Grid */
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
@media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; }
.card h3 { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 16px; }
.card.full { grid-column: 1 / -1; }

/* Badges */
.badge { display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 4px; white-space: nowrap; }
.badge-done { background: var(--green); color: #000; }
.badge-failed { background: var(--red); color: #fff; }
.badge-needs_human { background: var(--yellow); color: #000; }
.badge-skipped { background: var(--dim); color: var(--text); }
.badge-empty { background: var(--surface2); color: var(--muted); }
.badge-unknown { background: var(--surface2); color: var(--muted); }
.badge-pending { background: var(--blue-bg); color: var(--blue); border: 1px solid var(--blue); }
.badge-p0 { background: var(--red); color: #fff; }
.badge-p1 { background: var(--yellow); color: #000; }
.badge-p2 { background: var(--blue); color: #000; }
.badge-open { background: var(--green-bg); color: var(--green); border: 1px solid rgba(63,185,80,.4); }
.badge-closed { background: var(--red-bg); color: var(--red); border: 1px solid rgba(248,81,73,.4); }
.badge-grade-A { background: var(--green-bg); color: var(--green); font-size: 13px; padding: 3px 10px; }
.badge-grade-B { background: #2a3520; color: #7ee787; font-size: 13px; padding: 3px 10px; }
.badge-grade-C { background: var(--yellow-bg); color: var(--yellow); font-size: 13px; padding: 3px 10px; }
.badge-grade-D { background: var(--red-bg); color: var(--red); font-size: 13px; padding: 3px 10px; }

/* Project cards */
.proj-card { background: var(--bg); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
.proj-header { display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer; user-select: none; }
.proj-header:hover { background: var(--surface2); }
.proj-name { font-size: 14px; font-weight: 600; flex: 1; }
.proj-meta { font-size: 12px; color: var(--muted); }
.proj-chevron { color: var(--dim); font-size: 12px; transition: transform .2s; }
.proj-chevron.open { transform: rotate(90deg); }
.proj-body { display: none; padding: 0 16px 14px; border-top: 1px solid var(--border); }
.proj-body.open { display: block; }

/* Dispatch rows */
.dispatch-row { padding: 8px 0; border-bottom: 1px solid var(--border); cursor: pointer; }
.dispatch-row:last-child { border-bottom: none; }
.dispatch-row:hover .dispatch-summary { color: var(--text); }
.dispatch-summary { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.dispatch-ts { color: var(--dim); font-size: 11px; white-space: nowrap; min-width: 110px; }
.dispatch-section { color: var(--blue); font-size: 12px; min-width: 80px; }
.dispatch-count { color: var(--muted); font-size: 12px; }
.dispatch-detail { display: none; margin-top: 8px; padding: 10px 12px; background: var(--surface2); border-radius: 6px; font-size: 12px; }
.dispatch-detail.open { display: block; }
.task-pill { display: inline-block; background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; margin: 2px; font-size: 11px; color: var(--text); }
.result-box { margin-top: 8px; padding: 8px 10px; border-radius: 5px; font-size: 12px; }
.result-box.done { background: var(--green-bg); border-left: 3px solid var(--green); }
.result-box.failed { background: var(--red-bg); border-left: 3px solid var(--red); }
.result-box.needs_human { background: var(--yellow-bg); border-left: 3px solid var(--yellow); }
.result-box.empty { background: var(--surface2); border-left: 3px solid var(--dim); }
.result-box.pending { background: var(--blue-bg); border-left: 3px solid var(--blue); }
.result-box.unknown { background: var(--surface2); border-left: 3px solid var(--dim); }
.result-box.skipped { background: var(--surface2); border-left: 3px solid var(--dim); }

/* Window */
.win-row { display: flex; align-items: center; gap: 20px; margin-bottom: 14px; flex-wrap: wrap; }
.win-time { font-size: 38px; font-weight: 700; letter-spacing: -1px; }
.win-label { font-size: 12px; color: var(--muted); }
.win-track { height: 8px; background: var(--border); border-radius: 4px; margin-bottom: 10px; }
.win-fill { height: 100%; border-radius: 4px; }
.win-meta { display: flex; gap: 20px; flex-wrap: wrap; font-size: 12px; }
.win-item span { color: var(--muted); }

/* Task list */
.task-section-hdr { font-size: 11px; font-weight: 600; color: var(--blue); text-transform: uppercase; letter-spacing: .5px; padding: 8px 0 4px; border-top: 1px solid var(--border); margin-top: 4px; }
.task-section-hdr:first-child { border-top: none; margin-top: 0; }
.task-row { display: flex; align-items: baseline; gap: 8px; padding: 4px 0; font-size: 13px; }
.task-text.done { text-decoration: line-through; color: var(--muted); }

/* Queue bars */
.qbar-wrap { margin-bottom: 12px; }
.qbar-label { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }
.qbar { height: 7px; background: var(--border); border-radius: 4px; }
.qbar-fill { height: 100%; border-radius: 4px; }

/* Patterns */
.skip-tag { display: inline-block; background: var(--surface2); border: 1px solid var(--border); border-radius: 5px; padding: 3px 9px; margin: 3px; font-size: 12px; }
.skip-cnt { font-weight: 700; margin-left: 4px; color: var(--yellow); }
.fail-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.fail-row:last-child { border-bottom: none; }
.fail-reason { flex: 1; color: var(--muted); }
.fail-cnt { font-weight: 700; color: var(--red); min-width: 24px; text-align: right; }

.empty { color: var(--dim); font-style: italic; font-size: 13px; padding: 16px; text-align: center; }
.footer { text-align: center; color: var(--dim); font-size: 11px; padding: 24px 0; border-top: 1px solid var(--border); margin-top: 24px; }
.ago { color: var(--muted); font-size: 11px; }
</style>
</head>
<body>

<div class="header">
  <h1>cc-later</h1>
  <div class="meta" id="meta"></div>
</div>

<div class="stats" id="stats"></div>

<div class="grid">
  <div class="card full"><h3>Projects &amp; Last Fire</h3><div id="projects"></div></div>
  <div class="card"><h3>Window Status</h3><div id="window-status"></div></div>
  <div class="card"><h3>Queue Health</h3><div id="queue-health"></div></div>
  <div class="card"><h3>Agent Results</h3><div style="height:200px;position:relative"><canvas id="results-chart"></canvas></div></div>
  <div class="card"><h3>Skip &amp; Gate Reasons</h3><div id="skip-patterns"></div></div>
  <div class="card full"><h3>Dispatch Timeline</h3><div style="height:220px;position:relative"><canvas id="timeline-chart"></canvas></div></div>
  <div class="card"><h3>Failure Patterns</h3><div id="failure-patterns"></div></div>
  <div class="card"><h3>Task List</h3><div id="task-list" style="max-height:420px;overflow-y:auto"></div></div>
</div>

<div class="footer">cc-later &middot; refresh with <code>/cc-later:dashboard</code> &middot; Ctrl+C to stop server</div>

<script>
const D = __DATA_JSON__;

Chart.defaults.color = '#e6edf3';
Chart.defaults.borderColor = '#21262d';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, sans-serif';
Chart.defaults.font.size = 11;

// Helpers
function ago(ts) {
  if (!ts) return '';
  const diff = Math.floor((Date.now() - new Date(ts)) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return `${Math.floor(diff/86400)}d ago`;
}
function fmt_ts(ts) {
  return ts ? ts.replace('T',' ').slice(0,16) : '';
}
function badge(status) {
  const s = (status||'unknown').toLowerCase();
  return `<span class="badge badge-${s}">${status||'?'}</span>`;
}
function status_color(status) {
  const m = {DONE:'var(--green)',FAILED:'var(--red)',NEEDS_HUMAN:'var(--yellow)',SKIPPED:'var(--dim)',PENDING:'var(--blue)',EMPTY:'var(--dim)',UNKNOWN:'var(--dim)'};
  return m[status] || 'var(--muted)';
}

// ── Meta ──────────────────────────────────────────────────────────────────────
document.getElementById('meta').textContent =
  `${D.generated_at} · ${D.dispatches.length} dispatches · ${D.later_tasks.length} tasks`;

// ── Stats Row ─────────────────────────────────────────────────────────────────
(function(){
  const total = D.dispatches.length;
  const done = D.dispatches.filter(d => d.result_status === 'DONE').length;
  const failed = D.dispatches.filter(d => ['FAILED','UNKNOWN'].includes(d.result_status)).length;
  const rate = total > 0 ? Math.round(done/total*100) : 0;
  const pending = D.later_tasks.filter(t => !t.done).length;
  const completed = D.later_tasks.filter(t => t.done).length;
  const grade = rate >= 90 ? 'A' : rate >= 75 ? 'B' : rate >= 50 ? 'C' : total === 0 ? '—' : 'D';
  const projects = D.projects.length;
  const skips = Object.values(D.skip_reasons).reduce((a,b)=>a+b, 0);

  document.getElementById('stats').innerHTML = [
    {v: pending, l: 'Pending Tasks'},
    {v: completed, l: 'Completed'},
    {v: total, l: 'Total Dispatches'},
    {v: total > 0 ? rate+'%' : '—', l: 'Success Rate'},
    {v: failed, l: 'Failed'},
    {v: projects, l: 'Projects'},
    {v: skips, l: 'Gate Skips'},
    {v: `<span class="badge badge-grade-${grade}">${grade}</span>`, l: 'Agent Health'},
  ].map(s=>`<div class="stat"><div class="stat-val">${s.v}</div><div class="stat-lbl">${s.l}</div></div>`).join('');
})();

// ── Projects & Last Fire ──────────────────────────────────────────────────────
(function(){
  const el = document.getElementById('projects');
  if (!D.projects.length) {
    el.innerHTML = '<div class="empty">No dispatches yet — agents will run near window end</div>';
    return;
  }

  el.innerHTML = D.projects.map((p, pi) => {
    const grade = p.success_rate >= 90 ? 'A' : p.success_rate >= 75 ? 'B' : p.success_rate >= 50 ? 'C' : 'D';
    const lastAgo = ago(p.last_dispatch_ts);

    const dispatchRows = p.recent.map((d, di) => {
      const resultClass = d.result_status.toLowerCase();
      const taskPills = (d.entries||[]).map(e => `<span class="task-pill">${e.length > 60 ? e.slice(0,57)+'…' : e}</span>`).join('');
      const resultMsg = d.result_message ? `<div class="result-box ${resultClass}"><b>${d.result_status}</b>${d.result_message ? ': '+d.result_message : ''}</div>` : `<div class="result-box ${resultClass}"><b>${d.result_status}</b></div>`;
      const model = d.model ? `<span style="color:var(--purple);font-size:11px">${d.model}</span>` : '';
      const rem = d.remaining_minutes != null ? `<span style="color:var(--muted);font-size:11px">${d.remaining_minutes}m left in window</span>` : '';
      const resume = d.auto_resume ? `<span class="badge badge-pending" style="font-size:9px">RESUME</span>` : '';

      return `<div class="dispatch-row" onclick="toggleDetail('d-${pi}-${di}')">
        <div class="dispatch-summary">
          ${badge(d.result_status)}
          <span class="dispatch-section">${d.section || 'default'}</span>
          <span class="dispatch-count">${d.entries_dispatched || (d.entries||[]).length} task${(d.entries_dispatched||1)===1?'':'s'}</span>
          ${resume}
          <span style="flex:1"></span>
          ${model} ${rem}
          <span class="dispatch-ts">${fmt_ts(d.ts)} <span class="ago">${ago(d.ts)}</span></span>
        </div>
        <div class="dispatch-detail" id="d-${pi}-${di}">
          <div style="margin-bottom:6px;color:var(--muted)">${taskPills || '<em>no task list recorded</em>'}</div>
          ${resultMsg}
        </div>
      </div>`;
    }).join('');

    return `<div class="proj-card">
      <div class="proj-header" onclick="toggleProj('proj-${pi}')">
        <span class="proj-chevron" id="chev-${pi}">▶</span>
        <span class="proj-name">${p.repo_short}</span>
        <span class="badge badge-grade-${grade}">${grade}</span>
        <span class="proj-meta">${p.done}/${p.total_dispatches} done &nbsp;·&nbsp; last fired <b>${lastAgo}</b></span>
        <span class="proj-meta" style="color:var(--dim);font-size:11px">${p.repo}</span>
      </div>
      <div class="proj-body" id="proj-${pi}">
        <div style="padding-top:10px">${dispatchRows}</div>
      </div>
    </div>`;
  }).join('');

  // Auto-expand first project
  if (D.projects.length) {
    document.getElementById('proj-0').classList.add('open');
    document.getElementById('chev-0').classList.add('open');
  }
})();

function toggleProj(id) {
  const body = document.getElementById(id);
  const idx = id.split('-')[1];
  const chev = document.getElementById('chev-' + idx);
  body.classList.toggle('open');
  chev.classList.toggle('open');
}

function toggleDetail(id) {
  document.getElementById(id).classList.toggle('open');
}

// ── Window Status ─────────────────────────────────────────────────────────────
(function(){
  const el = document.getElementById('window-status');
  const w = D.window;
  const state = D.state;

  if (!w) {
    const last = state.last_hook_ts;
    const lim = state.window_limit_ts;
    el.innerHTML = `
      <div style="color:var(--muted);font-size:13px;margin-bottom:10px">No window data — JSONL paths not configured</div>
      ${last ? `<div style="font-size:12px"><span style="color:var(--muted)">Last hook: </span>${fmt_ts(last)} UTC <span class="ago">${ago(last)}</span></div>` : ''}
      ${lim ? `<div style="font-size:12px;color:var(--red);margin-top:6px"><span style="color:var(--muted)">Limit hit: </span>${fmt_ts(lim)} UTC</div>` : ''}
    `;
    return;
  }

  const rem = w.remaining_minutes, el2 = w.elapsed_minutes, dur = w.duration_minutes || 300;
  const pct = Math.min(100, Math.round(el2/dur*100));
  const color = rem <= 15 ? 'var(--red)' : rem <= w.trigger_at ? 'var(--yellow)' : 'var(--green)';
  const gateOpen = rem > w.trigger_at;
  const fmtMin = m => m >= 60 ? `${Math.floor(m/60)}h ${m%60}m` : `${m}m`;

  el.innerHTML = `
    <div class="win-row">
      <div><div class="win-time" style="color:${color}">${fmtMin(rem)}</div><div class="win-label">remaining</div></div>
      <div><span class="badge ${gateOpen?'badge-open':'badge-closed'}">${gateOpen?'GATE OPEN':'DISPATCHING'}</span>
      <div style="font-size:11px;color:var(--muted);margin-top:5px">mode: ${w.dispatch_mode||'window_aware'}</div></div>
    </div>
    <div class="win-track"><div class="win-fill" style="width:${pct}%;background:${color}"></div></div>
    <div class="win-meta">
      <div class="win-item"><span>Elapsed: </span>${fmtMin(el2)}</div>
      <div class="win-item"><span>Duration: </span>${fmtMin(dur)}</div>
      <div class="win-item"><span>Fires at: </span>${fmtMin(w.trigger_at)} left</div>
    </div>
  `;
})();

// ── Queue Health ──────────────────────────────────────────────────────────────
(function(){
  const tasks = D.later_tasks;
  if (!tasks.length) {
    document.getElementById('queue-health').innerHTML = '<div class="empty">No tasks in LATER.md</div>';
    return;
  }
  const total = tasks.length;
  const pending = tasks.filter(t => !t.done);
  const done = tasks.filter(t => t.done).length;
  const p0 = pending.filter(t => t.priority==='(P0)').length;
  const p1 = pending.filter(t => t.priority==='(P1)').length;
  const p2 = pending.filter(t => t.priority==='(P2)').length;

  const bar = (label, count, max, color) => `
    <div class="qbar-wrap">
      <div class="qbar-label"><span>${label}</span><span>${count}</span></div>
      <div class="qbar"><div class="qbar-fill" style="width:${max>0?Math.round(count/max*100):0}%;background:${color}"></div></div>
    </div>`;

  document.getElementById('queue-health').innerHTML =
    bar('Pending', pending.length, total, 'var(--blue)') +
    bar('Completed', done, total, 'var(--green)') +
    (pending.length ? '<div style="margin:10px 0 8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Pending by priority</div>' +
      bar('P0 Critical', p0, pending.length, 'var(--red)') +
      bar('P1 Normal', p1, pending.length, 'var(--yellow)') +
      bar('P2 Low', p2, pending.length, 'var(--blue)') : '');
})();

// ── Results Donut ─────────────────────────────────────────────────────────────
(function(){
  const dispatches = D.dispatches;
  if (!dispatches.length) {
    document.getElementById('results-chart').parentElement.innerHTML = '<div class="empty">No dispatches yet</div>';
    return;
  }
  const counts = {};
  dispatches.forEach(d => { counts[d.result_status] = (counts[d.result_status]||0)+1; });
  const entries = Object.entries(counts).filter(([,v])=>v>0);
  const colors = {DONE:'rgba(63,185,80,.85)',FAILED:'rgba(248,81,73,.85)',NEEDS_HUMAN:'rgba(210,153,34,.85)',SKIPPED:'rgba(72,79,88,.85)',EMPTY:'rgba(139,148,158,.5)',PENDING:'rgba(88,166,255,.7)',UNKNOWN:'rgba(88,166,255,.4)'};
  new Chart(document.getElementById('results-chart'), {
    type: 'doughnut',
    data: { labels: entries.map(([k])=>k), datasets: [{ data: entries.map(([,v])=>v), backgroundColor: entries.map(([k])=>colors[k]||'#888'), borderWidth: 0 }] },
    options: { responsive:true, maintainAspectRatio:false, cutout:'60%',
      plugins: { legend: { position:'right', labels: { padding:12, usePointStyle:true, pointStyle:'rectRounded',
        generateLabels: ch => ch.data.labels.map((l,i)=>({text:`${l}  ${ch.data.datasets[0].data[i]}`, fillStyle:ch.data.datasets[0].backgroundColor[i], strokeStyle:'transparent', index:i}))
      }}}
    }
  });
})();

// ── Skip Patterns ─────────────────────────────────────────────────────────────
(function(){
  const reasons = D.skip_reasons;
  const entries = Object.entries(reasons).sort((a,b)=>b[1]-a[1]);
  if (!entries.length) {
    document.getElementById('skip-patterns').innerHTML = '<div class="empty">No gate skips</div>';
    return;
  }
  document.getElementById('skip-patterns').innerHTML = entries.map(([r,c]) =>
    `<span class="skip-tag">${r.replace(/_/g,' ')}<span class="skip-cnt">${c}</span></span>`
  ).join('');
})();

// ── Dispatch Timeline ──────────────────────────────────────────────────────────
(function(){
  const relevant = ['dispatch','skip','capture','agent_abandoned','merge_conflict','resume'];
  const byDate = {};
  D.run_log.forEach(e => {
    if (!relevant.includes(e.event)) return;
    const d = (e.ts||'').slice(0,10); if (!d) return;
    if (!byDate[d]) byDate[d] = {};
    byDate[d][e.event] = (byDate[d][e.event]||0)+1;
  });
  const days = Object.keys(byDate).sort();
  if (!days.length) {
    document.getElementById('timeline-chart').parentElement.innerHTML = '<div class="empty">No events yet</div>';
    return;
  }
  new Chart(document.getElementById('timeline-chart'), {
    type: 'bar',
    data: {
      labels: days.map(d=>d.slice(5)),
      datasets: [
        {label:'Dispatch', data:days.map(d=>byDate[d].dispatch||0), backgroundColor:'rgba(88,166,255,.7)', stack:'s'},
        {label:'Capture', data:days.map(d=>byDate[d].capture||0), backgroundColor:'rgba(63,185,80,.7)', stack:'s'},
        {label:'Skip', data:days.map(d=>byDate[d].skip||0), backgroundColor:'rgba(72,79,88,.7)', stack:'s'},
        {label:'Abandoned', data:days.map(d=>byDate[d].agent_abandoned||0), backgroundColor:'rgba(248,81,73,.7)', stack:'s'},
        {label:'Conflict', data:days.map(d=>byDate[d].merge_conflict||0), backgroundColor:'rgba(210,153,34,.7)', stack:'s'},
        {label:'Resume', data:days.map(d=>byDate[d].resume||0), backgroundColor:'rgba(188,140,255,.7)', stack:'s'},
      ]
    },
    options: { responsive:true, maintainAspectRatio:false,
      plugins: { legend: { labels: { usePointStyle:true, pointStyle:'rectRounded', padding:14 } } },
      scales: { x:{grid:{display:false}}, y:{stacked:true, ticks:{stepSize:1}, grid:{color:'#21262d'}} }
    }
  });
})();

// ── Failure Patterns ──────────────────────────────────────────────────────────
(function(){
  const failures = D.dispatches.filter(d => ['FAILED','UNKNOWN'].includes(d.result_status));
  if (!failures.length) {
    document.getElementById('failure-patterns').innerHTML = '<div class="empty">No failures — nice!</div>';
    return;
  }
  const reasons = {};
  failures.forEach(d => {
    const msg = d.result_message || 'unknown error';
    const key = msg.slice(0,70).replace(/tmp[a-z0-9_]+/gi,'').trim() || 'unknown';
    reasons[key] = (reasons[key]||0)+1;
  });
  const top = Object.entries(reasons).sort((a,b)=>b[1]-a[1]).slice(0,6);
  document.getElementById('failure-patterns').innerHTML = top.map(([r,c]) =>
    `<div class="fail-row"><span class="fail-reason">${r}</span><span class="fail-cnt">${c}</span></div>`
  ).join('');
})();

// ── Task List ─────────────────────────────────────────────────────────────────
(function(){
  // Group by repo then section
  const byRepo = D.later_by_repo;
  if (!byRepo.length || !byRepo.some(r=>r.tasks.length)) {
    document.getElementById('task-list').innerHTML = '<div class="empty">LATER.md is empty</div>';
    return;
  }
  let html = '';
  byRepo.forEach(repo => {
    if (!repo.tasks.length) return;
    if (byRepo.filter(r=>r.tasks.length).length > 1) {
      html += `<div style="font-size:12px;font-weight:700;color:var(--purple);padding:8px 0 4px;border-top:1px solid var(--border);margin-top:4px">${repo.repo_short}</div>`;
    }
    const sections = {};
    repo.tasks.forEach(t => {
      if (!sections[t.section]) sections[t.section] = [];
      sections[t.section].push(t);
    });
    for (const [sec, items] of Object.entries(sections)) {
      html += `<div class="task-section-hdr">${sec}</div>`;
      items.forEach(t => {
        const prio = t.priority.replace(/[()]/g,'').toLowerCase();
        html += `<div class="task-row">
          <span class="badge badge-${prio}">${t.priority.replace(/[()]/g,'')}</span>
          <span class="task-text${t.done?' done':''}">${t.text}</span>
        </div>`;
      });
    }
  });
  document.getElementById('task-list').innerHTML = html;
})();
</script>
</body>
</html>"""
