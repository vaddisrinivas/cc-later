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
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
.stat-val { font-size: 26px; font-weight: 700; }
.stat-lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }

/* Grid */
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
@media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; }
.card h3 { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 16px; }
.card.full { grid-column: 1 / -1; }

/* Window */
.win-row { display: flex; align-items: center; gap: 20px; margin-bottom: 16px; flex-wrap: wrap; }
.win-time { font-size: 40px; font-weight: 700; letter-spacing: -1px; }
.win-label { font-size: 13px; color: var(--muted); }
.win-track { height: 10px; background: var(--border); border-radius: 5px; margin-bottom: 10px; }
.win-fill { height: 100%; border-radius: 5px; }
.win-meta { display: flex; gap: 24px; flex-wrap: wrap; }
.win-item { font-size: 13px; }
.win-item span { color: var(--muted); }

/* Badges */
.badge { display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px; }
.badge-done { background: var(--green); color: #000; }
.badge-failed { background: var(--red); color: #fff; }
.badge-needs_human { background: var(--yellow); color: #000; }
.badge-skipped { background: var(--dim); color: var(--text); }
.badge-empty { background: var(--surface2); color: var(--muted); }
.badge-unknown { background: var(--surface2); color: var(--muted); }
.badge-p0 { background: var(--red); color: #fff; }
.badge-p1 { background: var(--yellow); color: #000; }
.badge-p2 { background: var(--blue); color: #000; }
.badge-open { background: var(--green-bg); color: var(--green); border: 1px solid var(--green); }
.badge-closed { background: var(--red-bg); color: var(--red); border: 1px solid var(--red); }
.badge-grade-A { background: var(--green-bg); color: var(--green); }
.badge-grade-B { background: #2a3520; color: #7ee787; }
.badge-grade-C { background: var(--yellow-bg); color: var(--yellow); }
.badge-grade-D { background: var(--red-bg); color: var(--red); }

/* Queue health bars */
.qbar-wrap { margin-bottom: 14px; }
.qbar-label { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }
.qbar { height: 8px; background: var(--border); border-radius: 4px; }
.qbar-fill { height: 100%; border-radius: 4px; }

/* Agent results list */
.result-list { max-height: 360px; overflow-y: auto; }
.result-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.result-row:last-child { border-bottom: none; }
.result-section { color: var(--muted); font-size: 11px; min-width: 80px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.result-msg { flex: 1; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.result-ts { color: var(--dim); font-size: 11px; white-space: nowrap; }

/* Task list */
.task-section-header { font-size: 12px; font-weight: 600; color: var(--blue); text-transform: uppercase; letter-spacing: 0.5px; padding: 10px 0 6px; border-top: 1px solid var(--border); margin-top: 6px; }
.task-section-header:first-child { border-top: none; margin-top: 0; }
.task-row { display: flex; align-items: baseline; gap: 8px; padding: 5px 0; font-size: 13px; }
.task-text { flex: 1; }
.task-text.done { text-decoration: line-through; color: var(--muted); }

/* Failure patterns */
.fail-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.fail-row:last-child { border-bottom: none; }
.fail-reason { flex: 1; color: var(--text); font-size: 12px; }
.fail-count { font-weight: 700; color: var(--red); min-width: 30px; text-align: right; }
.fail-bar { width: 80px; height: 6px; background: var(--border); border-radius: 3px; }
.fail-bar-fill { height: 100%; border-radius: 3px; background: var(--red); }

/* Skip reasons */
.skip-tag { display: inline-block; background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; margin: 3px; font-size: 12px; }
.skip-cnt { font-weight: 700; margin-left: 4px; color: var(--yellow); }

.empty { color: var(--dim); font-style: italic; font-size: 13px; padding: 20px; text-align: center; }
.footer { text-align: center; color: var(--dim); font-size: 11px; padding: 24px 0; border-top: 1px solid var(--border); margin-top: 24px; }
</style>
</head>
<body>

<div class="header">
  <h1>cc-later</h1>
  <div class="meta" id="meta"></div>
</div>

<div class="stats" id="stats"></div>

<div class="grid">
  <div class="card"><h3>Queue Health</h3><div id="queue-health"></div></div>
  <div class="card"><h3>Window Status</h3><div id="window-status"></div></div>
  <div class="card"><h3>Agent Results</h3><div style="height:220px;position:relative"><canvas id="results-chart"></canvas></div></div>
  <div class="card"><h3>Skip &amp; Gate Patterns</h3><div id="skip-patterns"></div></div>
  <div class="card full"><h3>Dispatch Timeline</h3><div style="height:240px;position:relative"><canvas id="timeline-chart"></canvas></div></div>
  <div class="card full"><h3>Recent Agent Runs</h3><div class="result-list" id="result-list"></div></div>
  <div class="card"><h3>Failure Patterns</h3><div id="failure-patterns"></div></div>
  <div class="card"><h3>Task List</h3><div id="task-list"></div></div>
</div>

<div class="footer">cc-later &middot; refresh with <code>/cc-later:dashboard</code> &middot; Ctrl+C to stop server</div>

<script>
const D = __DATA_JSON__;

Chart.defaults.color = '#e6edf3';
Chart.defaults.borderColor = '#21262d';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, sans-serif';
Chart.defaults.font.size = 11;

// Meta
document.getElementById('meta').textContent =
  `${D.generated_at} \u00b7 ${D.results.length} runs \u00b7 ${D.later_tasks.length} tasks`;

// ── Stats Row ────────────────────────────────────────────────────────────────
(function(){
  const results = D.results;
  const total = results.length;
  const done = results.filter(r => r.status === 'DONE').length;
  const failed = results.filter(r => r.status === 'FAILED' || r.status === 'UNKNOWN').length;
  const needs = results.filter(r => r.status === 'NEEDS_HUMAN').length;
  const rate = total > 0 ? Math.round(done / total * 100) : 0;

  const pending = D.later_tasks.filter(t => !t.done).length;
  const completed = D.later_tasks.filter(t => t.done).length;

  const dispatches = D.run_log.filter(e => e.event === 'dispatch').length;
  const skips = D.run_log.filter(e => e.event === 'skip').length;

  // Grade: A=>=90%, B=>=75%, C=>=50%, D=<50%
  const grade = rate >= 90 ? 'A' : rate >= 75 ? 'B' : rate >= 50 ? 'C' : total === 0 ? '\u2014' : 'D';

  document.getElementById('stats').innerHTML = [
    {v: pending, l: 'Pending Tasks'},
    {v: completed, l: 'Completed Tasks'},
    {v: dispatches, l: 'Total Dispatched'},
    {v: total > 0 ? rate + '%' : '\u2014', l: 'Success Rate'},
    {v: failed, l: 'Failed Runs'},
    {v: needs, l: 'Needs Human'},
    {v: `<span class="badge badge-grade-${grade}" style="font-size:22px;padding:4px 12px">${grade}</span>`, l: 'Agent Health'},
  ].map(s => `<div class="stat"><div class="stat-val">${s.v}</div><div class="stat-lbl">${s.l}</div></div>`).join('');
})();

// ── Queue Health ──────────────────────────────────────────────────────────────
(function(){
  const tasks = D.later_tasks;
  const total = tasks.length;
  if (!total) {
    document.getElementById('queue-health').innerHTML = '<div class="empty">No tasks in LATER.md</div>';
    return;
  }
  const pending = tasks.filter(t => !t.done);
  const done = tasks.filter(t => t.done);

  const p0 = pending.filter(t => t.priority === '(P0)').length;
  const p1 = pending.filter(t => t.priority === '(P1)').length;
  const p2 = pending.filter(t => t.priority === '(P2)').length;
  const pUnk = pending.length - p0 - p1 - p2;

  const bar = (label, count, total, color) => total === 0 ? '' : `
    <div class="qbar-wrap">
      <div class="qbar-label"><span>${label}</span><span>${count} / ${total}</span></div>
      <div class="qbar"><div class="qbar-fill" style="width:${Math.round(count/total*100)}%;background:${color}"></div></div>
    </div>`;

  document.getElementById('queue-health').innerHTML =
    bar('Pending', pending.length, total, 'var(--blue)') +
    bar('Completed', done.length, total, 'var(--green)') +
    '<div style="border-top:1px solid var(--border);margin:12px 0 10px"></div>' +
    '<div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">Priority Distribution (pending)</div>' +
    (pending.length ? (
      bar('P0 Critical', p0, pending.length, 'var(--red)') +
      bar('P1 Normal', p1, pending.length, 'var(--yellow)') +
      bar('P2 Low', p2, pending.length, 'var(--blue)') +
      (pUnk ? bar('Untagged', pUnk, pending.length, 'var(--dim)') : '')
    ) : '<div class="empty" style="padding:8px">All done!</div>');
})();

// ── Window Status ─────────────────────────────────────────────────────────────
(function(){
  const el = document.getElementById('window-status');
  const w = D.window;
  const state = D.state;

  if (!w) {
    // Show last hook time + window_limit if known
    const lim = state.window_limit_ts;
    const last = state.last_hook_ts;
    el.innerHTML = `
      <div style="color:var(--muted);font-size:13px;margin-bottom:12px">Window data unavailable — no JSONL paths configured</div>
      ${last ? `<div class="win-item"><span>Last hook: </span>${last.replace('T',' ').slice(0,19)} UTC</div>` : ''}
      ${lim ? `<div class="win-item" style="margin-top:6px;color:var(--red)"><span>Window limit hit: </span>${lim.replace('T',' ').slice(0,19)} UTC</div>` : ''}
    `;
    return;
  }

  const remaining = w.remaining_minutes;
  const elapsed = w.elapsed_minutes;
  const duration = w.duration_minutes || 300;
  const pct = Math.min(100, Math.round(elapsed / duration * 100));
  const isLow = remaining <= w.trigger_at;
  const color = remaining <= 15 ? 'var(--red)' : remaining <= w.trigger_at ? 'var(--yellow)' : 'var(--green)';

  // Gate: open if remaining > trigger_at, closed otherwise
  const gateOpen = remaining > w.trigger_at;
  const gateLabel = gateOpen ? 'GATE OPEN' : (remaining <= 0 ? 'WINDOW ENDED' : 'GATE CLOSED \u2014 DISPATCHING');

  const fmtMin = m => m >= 60 ? `${Math.floor(m/60)}h ${m%60}m` : `${m}m`;

  el.innerHTML = `
    <div class="win-row">
      <div>
        <div class="win-time" style="color:${color}">${fmtMin(remaining)}</div>
        <div class="win-label">remaining</div>
      </div>
      <div>
        <span class="badge ${gateOpen ? 'badge-open' : 'badge-closed'}">${gateLabel}</span>
        <div style="font-size:12px;color:var(--muted);margin-top:6px">mode: ${w.dispatch_mode || 'window_aware'}</div>
      </div>
    </div>
    <div class="win-track"><div class="win-fill" style="width:${pct}%;background:${color}"></div></div>
    <div class="win-meta">
      <div class="win-item"><span>Elapsed: </span>${fmtMin(elapsed)}</div>
      <div class="win-item"><span>Duration: </span>${fmtMin(duration)}</div>
      <div class="win-item"><span>Trigger at: </span>${fmtMin(w.trigger_at)} remaining</div>
    </div>
  `;
})();

// ── Results Donut ─────────────────────────────────────────────────────────────
(function(){
  const results = D.results;
  if (!results.length) {
    document.getElementById('results-chart').parentElement.innerHTML = '<div class="empty">No agent runs yet</div>';
    return;
  }
  const counts = {DONE:0, FAILED:0, NEEDS_HUMAN:0, SKIPPED:0, EMPTY:0, UNKNOWN:0};
  results.forEach(r => { counts[r.status] = (counts[r.status]||0) + 1; });
  const nonZero = Object.entries(counts).filter(([,v]) => v > 0);
  const colors = {
    DONE: 'rgba(63,185,80,0.85)',
    FAILED: 'rgba(248,81,73,0.85)',
    NEEDS_HUMAN: 'rgba(210,153,34,0.85)',
    SKIPPED: 'rgba(72,79,88,0.85)',
    EMPTY: 'rgba(139,148,158,0.5)',
    UNKNOWN: 'rgba(88,166,255,0.5)',
  };
  new Chart(document.getElementById('results-chart'), {
    type: 'doughnut',
    data: {
      labels: nonZero.map(([k]) => k),
      datasets: [{
        data: nonZero.map(([,v]) => v),
        backgroundColor: nonZero.map(([k]) => colors[k] || '#888'),
        borderWidth: 0,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '60%',
      plugins: {
        legend: {
          position: 'right',
          labels: { padding: 12, usePointStyle: true, pointStyle: 'rectRounded',
            generateLabels: ch => ch.data.labels.map((l,i) => ({
              text: `${l}  ${ch.data.datasets[0].data[i]}`,
              fillStyle: ch.data.datasets[0].backgroundColor[i],
              strokeStyle: 'transparent', index: i
            }))
          }
        }
      }
    }
  });
})();

// ── Skip Patterns ─────────────────────────────────────────────────────────────
(function(){
  const skips = D.run_log.filter(e => e.event === 'skip');
  if (!skips.length) {
    document.getElementById('skip-patterns').innerHTML = '<div class="empty">No skip events</div>';
    return;
  }
  const reasons = {};
  skips.forEach(e => { const r = e.reason || 'unknown'; reasons[r] = (reasons[r]||0) + 1; });
  const top = Object.entries(reasons).sort((a,b) => b[1]-a[1]);
  document.getElementById('skip-patterns').innerHTML = top.map(([r,c]) =>
    `<span class="skip-tag">${r.replace(/_/g,' ')}<span class="skip-cnt">${c}</span></span>`
  ).join('');
})();

// ── Dispatch Timeline ──────────────────────────────────────────────────────────
(function(){
  const relevant = ['dispatch','skip','capture','agent_abandoned','merge_conflict','resume'];
  const byDate = {};
  D.run_log.forEach(e => {
    if (!relevant.includes(e.event)) return;
    const d = (e.ts||'').slice(0,10);
    if (!d) return;
    if (!byDate[d]) byDate[d] = {};
    byDate[d][e.event] = (byDate[d][e.event]||0) + 1;
  });
  const days = Object.keys(byDate).sort();
  if (!days.length) {
    document.getElementById('timeline-chart').parentElement.innerHTML = '<div class="empty">No events yet</div>';
    return;
  }
  new Chart(document.getElementById('timeline-chart'), {
    type: 'bar',
    data: {
      labels: days.map(d => d.slice(5)),
      datasets: [
        { label:'Dispatch', data: days.map(d=>(byDate[d].dispatch||0)), backgroundColor:'rgba(88,166,255,0.7)', stack:'s' },
        { label:'Capture', data: days.map(d=>(byDate[d].capture||0)), backgroundColor:'rgba(63,185,80,0.7)', stack:'s' },
        { label:'Skip', data: days.map(d=>(byDate[d].skip||0)), backgroundColor:'rgba(72,79,88,0.7)', stack:'s' },
        { label:'Abandoned', data: days.map(d=>(byDate[d].agent_abandoned||0)), backgroundColor:'rgba(248,81,73,0.7)', stack:'s' },
        { label:'Conflict', data: days.map(d=>(byDate[d].merge_conflict||0)), backgroundColor:'rgba(210,153,34,0.7)', stack:'s' },
        { label:'Resume', data: days.map(d=>(byDate[d].resume||0)), backgroundColor:'rgba(188,140,255,0.7)', stack:'s' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { usePointStyle: true, pointStyle: 'rectRounded', padding: 14 } },
      },
      scales: {
        x: { grid: { display: false } },
        y: { stacked: true, ticks: { stepSize: 1 }, grid: { color: '#21262d' } }
      }
    }
  });
})();

// ── Recent Agent Runs ─────────────────────────────────────────────────────────
(function(){
  const results = [...D.results].sort((a,b) => (b.ts||'').localeCompare(a.ts||'')).slice(0,40);
  if (!results.length) {
    document.getElementById('result-list').innerHTML = '<div class="empty">No agent runs yet</div>';
    return;
  }
  document.getElementById('result-list').innerHTML = results.map(r => {
    const ts = r.ts ? r.ts.replace('T',' ').slice(0,16) : '';
    const st = r.status.toLowerCase();
    const msg = r.message || r.task_id || '\u2014';
    return `<div class="result-row">
      <span class="badge badge-${st}">${r.status}</span>
      <span class="result-section">${r.section}</span>
      <span class="result-msg" title="${msg.replace(/"/g,'&quot;')}">${msg}</span>
      <span class="result-ts">${ts}</span>
    </div>`;
  }).join('');
})();

// ── Failure Patterns ──────────────────────────────────────────────────────────
(function(){
  const failures = D.results.filter(r => r.status === 'FAILED' || r.status === 'UNKNOWN');
  if (!failures.length) {
    document.getElementById('failure-patterns').innerHTML = '<div class="empty">No failures — nice!</div>';
    return;
  }
  // Bucket by first meaningful words of message
  const reasons = {};
  failures.forEach(r => {
    const msg = r.message || 'unknown error';
    // Normalize: take first 60 chars, strip task ids
    const key = msg.replace(/tmp[a-z0-9_]+/gi,'').trim().slice(0,60) || 'unknown';
    reasons[key] = (reasons[key]||0) + 1;
  });
  const top = Object.entries(reasons).sort((a,b) => b[1]-a[1]).slice(0,8);
  const max = top[0][1];
  document.getElementById('failure-patterns').innerHTML = top.map(([r,c]) =>
    `<div class="fail-row">
      <div class="fail-bar"><div class="fail-bar-fill" style="width:${Math.round(c/max*100)}%"></div></div>
      <span class="fail-reason">${r}</span>
      <span class="fail-count">${c}</span>
    </div>`
  ).join('');
})();

// ── Task List ─────────────────────────────────────────────────────────────────
(function(){
  const tasks = D.later_tasks;
  if (!tasks.length) {
    document.getElementById('task-list').innerHTML = '<div class="empty">LATER.md is empty</div>';
    return;
  }
  // Group by section
  const sections = {};
  tasks.forEach(t => {
    if (!sections[t.section]) sections[t.section] = [];
    sections[t.section].push(t);
  });
  let html = '';
  for (const [sec, items] of Object.entries(sections)) {
    html += `<div class="task-section-header">${sec}</div>`;
    items.forEach(t => {
      const prio = t.priority.replace(/[()]/g,'').toLowerCase();
      html += `<div class="task-row">
        <span class="badge badge-${prio}">${t.priority.replace(/[()]/g,'')}</span>
        <span class="task-text${t.done ? ' done' : ''}">${t.text}</span>
      </div>`;
    });
  }
  document.getElementById('task-list').innerHTML = html;
})();
</script>
</body>
</html>"""
