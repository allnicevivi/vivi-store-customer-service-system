// DEMO_QUESTIONS is injected by the server into window.DEMO_QUESTIONS (see index.html)

// ── Session Management ─────────────────────────────────────────────────────────
// 每次頁面載入產生新的 session_id（不存 localStorage，重新整理即開新 session）
const _sessionId = 'SES-' + crypto.randomUUID();

function getOrCreateSessionId() {
  return _sessionId;
}

const CUSTOMER_ID = 'demo_user';

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('load', () => {
  renderPresets();
  refreshStatus();
  setInterval(refreshStatus, 5000);
  initUsageDateDefaults();
});

function renderPresets() {
  const c = document.getElementById('presets');
  window.DEMO_QUESTIONS.forEach(q => {
    const b = document.createElement('button');
    b.className = 'preset-btn';
    b.textContent = q;
    b.onclick = () => { document.getElementById('query-input').value = q; };
    c.appendChild(b);
  });
}

// ── Status ────────────────────────────────────────────────────────────────────
let _servicesReady = false;

async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const svcs = d.services || [];
    renderServices(svcs);
    const allHealthy = svcs.length > 0 && svcs.every(s => {
      const health = (s.health || '').toLowerCase();
      const state  = (s.state  || '').toLowerCase();
      return health === 'healthy' || (health === '' && state === 'running');
    });
    if (allHealthy && !_servicesReady) {
      _servicesReady = true;
      document.getElementById('query-input').disabled = false;
      document.getElementById('btn-send').disabled = false;
      const waitMsg = document.getElementById('wait-msg');
      if (waitMsg) waitMsg.style.display = 'none';
    } else if (!allHealthy && _servicesReady) {
      _servicesReady = false;
      document.getElementById('query-input').disabled = true;
      document.getElementById('btn-send').disabled = true;
      const waitMsg = document.getElementById('wait-msg');
      if (waitMsg) waitMsg.style.display = '';
    }
  } catch(e) {}
}

function dotClass(s) {
  const st = (s.state || '').toLowerCase();
  const h  = (s.health || '').toLowerCase();
  if (h === 'healthy') return 'dot-green';
  if (h === 'starting' || st === 'starting') return 'dot-yellow';
  if (st === 'running') return 'dot-yellow';
  if (st === 'exited' || st === 'dead') return 'dot-red';
  return 'dot-gray';
}

function renderServices(svcs) {
  const c = document.getElementById('service-list');
  const cnt = document.getElementById('svc-count');
  if (!svcs.length) {
    c.innerHTML = '<div style="color:#4b5563;font-size:.82rem">尚未偵測到容器</div>';
    cnt.textContent = '0 服務';
    return;
  }
  const healthy = svcs.filter(s => (s.health||'').toLowerCase() === 'healthy').length;
  cnt.textContent = `${healthy}/${svcs.length} 健康`;
  c.innerHTML = svcs.map(s => `
    <div class="service-item">
      <div class="dot ${dotClass(s)}"></div>
      <span class="svc-name" title="${s.name}">${s.name}</span>
      <span class="svc-state">${s.health || s.state || ''}</span>
    </div>`).join('');
}

// ── Tab Navigation ─────────────────────────────────────────────────────────────
let _currentTab = 'chat';
let _ticketsInterval = null;

function switchTab(name) {
  // Hide all tabs
  document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

  // Show selected
  document.getElementById('tab-' + name).style.display = 'flex';
  const activeBtn = document.querySelector(`.tab-btn[onclick="switchTab('${name}')"]`);
  if (activeBtn) activeBtn.classList.add('active');

  // Stop tickets polling if leaving tickets tab
  if (_currentTab === 'tickets' && name !== 'tickets') {
    if (_ticketsInterval) { clearInterval(_ticketsInterval); _ticketsInterval = null; }
  }

  _currentTab = name;

  // Trigger data load on tab enter
  if (name === 'analytics') loadAnalytics();
  if (name === 'usage') loadUsageFromUI();
  if (name === 'tickets') {
    loadTickets();
    _ticketsInterval = setInterval(loadTickets, 30000);
  }
}

// ── Query / Chat ──────────────────────────────────────────────────────────────
async function sendQuery() {
  const input = document.getElementById('query-input');
  const query = input.value.trim();
  if (!query) return;
  input.value = '';

  document.getElementById('empty-state')?.remove();
  addBubble('user', query, null, null);
  const thinkingId = addThinkingBubble();
  document.getElementById('btn-send').disabled = true;

  let r, d;
  try {
    const sessionId = getOrCreateSessionId();
    r = await fetch('/api/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query, session_id: sessionId, customer_id: CUSTOMER_ID }),
    });
    d = await r.json();
  } catch(e) {
    removeThinking(thinkingId);
    addBubble('bot', '無法連接後端：' + e, null, null, true);
    document.getElementById('btn-send').disabled = false;
    return;
  }

  if (d.error) {
    removeThinking(thinkingId);
    addBubble('bot', d.error, null, null, true);
    document.getElementById('btn-send').disabled = false;
    return;
  }

  const es = new EventSource('/api/stream/' + d.query_id);
  es.onmessage = (e) => {
    es.close();
    removeThinking(thinkingId);
    document.getElementById('btn-send').disabled = false;
    try {
      const payload = JSON.parse(e.data);
      if (payload.error) {
        addBubble('bot', payload.error, null, null, true);
      } else if (payload.warning) {
        addBubble('bot', payload.warning, null, null, true);
      } else {
        addBubble('bot', payload.response, payload.routed_to, payload.escalated, false, payload);
      }
    } catch {
      addBubble('bot', '無法解析回覆', null, null, true);
    }
  };
  es.onerror = () => {
    es.close();
    removeThinking(thinkingId);
    document.getElementById('btn-send').disabled = false;
    addBubble('bot', 'SSE 連線錯誤', null, null, true);
  };
}

function routeTag(route, escalated) {
  if (escalated) return '<span class="route-tag rt-human">人工轉接</span>';
  const map = {
    'queries.agent':  ['rt-complex', 'AI 解析'],
    'queries.human':  ['rt-human',   '人工'],
  };
  if (!route) return '';
  const [cls, label] = map[route] || ['rt-unknown', route];
  return `<span class="route-tag ${cls}">${label}</span>`;
}

function addBubble(who, text, route, escalated, isWarn=false, pipeline=null) {
  const chat = document.getElementById('chat-area');
  const div = document.createElement('div');
  div.className = `msg ${who}`;
  const time = new Date().toLocaleTimeString('zh-TW', {hour:'2-digit', minute:'2-digit'});
  div.innerHTML = `
    <div class="bubble${isWarn ? ' alert alert-warn' : ''}">${escHtml(text)}</div>
    <div class="meta">
      <span>${time}</span>
      ${who === 'bot' ? routeTag(route, escalated) : ''}
    </div>
    ${who === 'bot' && pipeline ? renderPipeline(pipeline) : ''}`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function renderPipeline(p) {
  const steps = [];

  // Router step
  const intentBadge = p.intent
    ? `<span class="pl-badge pl-intent">${escHtml(p.intent)}</span>` : '';
  const emotionBadge = p.emotion
    ? `<span class="pl-badge pl-emotion">${escHtml(p.emotion)}</span>` : '';
  steps.push(`<div class="pl-step"><span class="pl-svc">Router</span>${intentBadge}${emotionBadge}</div>`);

  // Resolution step (if tools were called)
  if (p.tool_calls && p.tool_calls.length) {
    const tools = p.tool_calls
      .map(t => `<span class="pl-badge pl-tool">${escHtml(t.tool || String(t))}</span>`)
      .join('<span class="pl-arrow">›</span>');
    steps.push(`<div class="pl-step"><span class="pl-svc">Agent</span>${tools}</div>`);
  }

  const lat = p.latency_ms
    ? `<span class="pl-badge pl-lat">${Math.round(p.latency_ms)} ms</span>` : '';

  return `<details class="pl-trace">
    <summary class="pl-summary">流程追蹤 ${lat}</summary>
    <div class="pl-body">
      ${steps.join('')}
      ${p.routing_reason ? `<div class="pl-reason">${escHtml(p.routing_reason)}</div>` : ''}
    </div>
  </details>`;
}

function addThinkingBubble() {
  const chat = document.getElementById('chat-area');
  const id = 'think-' + Date.now();
  const div = document.createElement('div');
  div.className = 'msg bot'; div.id = id;
  div.innerHTML = '<div class="bubble"><span class="spinner"></span> 處理中...</div>';
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return id;
}

function removeThinking(id) {
  document.getElementById(id)?.remove();
}

// ── Log stream ────────────────────────────────────────────────────────────────
let logEs = null;
function startLogStream() {
  if (logEs) { logEs.close(); }
  logEs = new EventSource('/api/logs');
  logEs.onmessage = e => {
    try { appendLog(JSON.parse(e.data)); } catch {}
  };
}

function appendLog(line) {
  const p = document.getElementById('log-panel');
  const d = document.createElement('div');
  d.textContent = line;
  p.appendChild(d);
  if (p.children.length > 300) p.removeChild(p.firstChild);
  p.scrollTop = p.scrollHeight;
}

let logOpen = false;
function toggleLog() {
  const p = document.getElementById('log-panel');
  const b = document.querySelector('.log-toggle');
  logOpen = !logOpen;
  p.classList.toggle('hidden', !logOpen);
  b.textContent = (logOpen ? '▲' : '▼') + ' 服務 Log';
  if (logOpen && !logEs) startLogStream();
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
           .replace(/\n/g,'<br>');
}

// ── Analytics Tab ─────────────────────────────────────────────────────────────
const ROUTE_COLORS = {
  'queries.agent':    '#a78bfa',
  'queries.human':    '#f87171',
  'queries.faq':      '#60a5fa',
  'queries.emotional':'#fbbf24',
};

async function loadAnalytics() {
  try {
    const [metricsRes, intentRes, escRes, gapsRes] = await Promise.all([
      fetch('/api/llm-metrics'),
      fetch('/api/analytics/intent-distribution'),
      fetch('/api/analytics/escalation-rate'),
      fetch('/api/analytics/faq-gaps'),
    ]);
    const [metrics, intent, esc, gaps] = await Promise.all([
      metricsRes.json(), intentRes.json(), escRes.json(), gapsRes.json(),
    ]);

    // LLM KPI
    document.getElementById('kpi-total-calls').textContent = metrics.total_calls ?? '—';
    const errRate = metrics.error_rate != null ? (metrics.error_rate * 100).toFixed(1) + '%' : '—';
    document.getElementById('kpi-error-rate').textContent = errRate;
    document.getElementById('kpi-avg-latency').textContent =
      metrics.avg_latency_ms != null ? Math.round(metrics.avg_latency_ms) : '—';
    document.getElementById('kpi-total-tokens').textContent = metrics.total_tokens ?? '—';

    // Intent distribution
    renderIntentDistribution(intent.distribution || []);

    // Escalation rate
    renderEscalationRate(esc.daily || []);

    // FAQ gaps
    renderFaqGaps(gaps.gaps || []);
  } catch(e) {
    console.error('loadAnalytics error', e);
  }
}

function renderIntentDistribution(distribution) {
  const el = document.getElementById('intent-distribution');
  if (!distribution.length) { el.innerHTML = '<div class="no-data">尚無資料</div>'; return; }
  const total = distribution.reduce((s, d) => s + (d.count || 0), 0) || 1;
  el.innerHTML = distribution.map(d => {
    const pct = Math.round((d.count / total) * 100);
    const color = ROUTE_COLORS[d.routed_to] || '#94a3b8';
    return `<div class="intent-bar-wrap">
      <div class="intent-bar-label">
        <span>${escHtml(d.routed_to || d.intent || '未知')}</span>
        <span>${d.count} (${pct}%)</span>
      </div>
      <div class="intent-bar-track">
        <div class="intent-bar-fill" style="width:${pct}%;background:${color}"></div>
      </div>
    </div>`;
  }).join('');
}

function renderEscalationRate(daily) {
  const el = document.getElementById('escalation-rate');
  if (!daily.length) { el.innerHTML = '<div class="no-data">尚無資料</div>'; return; }
  el.innerHTML = `<table class="esc-table">
    <thead><tr><th>日期</th><th>總量</th><th>升級數</th><th>升級率</th></tr></thead>
    <tbody>${daily.map(row => `<tr>
      <td>${escHtml(String(row.date || ''))}</td>
      <td>${row.total ?? 0}</td>
      <td>${row.escalated ?? 0}</td>
      <td>${row.rate != null ? (row.rate * 100).toFixed(1) + '%' : '—'}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

function renderFaqGaps(gaps) {
  const el = document.getElementById('faq-gaps');
  if (!gaps.length) { el.innerHTML = '<div class="no-data">尚無資料</div>'; return; }
  el.innerHTML = gaps.map(g => {
    const samples = (g.sample_queries || []).slice(0, 3)
      .map(q => `<div class="gap-sample">${escHtml(q)}</div>`).join('');
    return `<div class="gap-card">
      <div class="gap-intent">${escHtml(g.intent || '未知')}</div>
      <div class="gap-count">升級次數：${g.escalation_count ?? 0}</div>
      ${samples}
    </div>`;
  }).join('');
}

// ── Usage Tab ─────────────────────────────────────────────────────────────────
function initUsageDateDefaults() {
  const now = new Date();
  const past24h = new Date(now - 24 * 3600 * 1000);
  document.getElementById('usage-to').value = toLocalISO(now);
  document.getElementById('usage-from').value = toLocalISO(past24h);
}

function toLocalISO(d) {
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function loadUsageFromUI() {
  const from = document.getElementById('usage-from').value;
  const to   = document.getElementById('usage-to').value;
  loadUsage(from ? new Date(from).toISOString() : null,
            to   ? new Date(to).toISOString()   : null);
}

async function loadUsage(from, to) {
  try {
    let url = '/api/usage';
    const params = [];
    if (from) params.push('from=' + encodeURIComponent(from));
    if (to)   params.push('to='   + encodeURIComponent(to));
    if (params.length) url += '?' + params.join('&');
    const r = await fetch(url);
    const d = await r.json();

    const t = d.total || {};
    document.getElementById('u-queries').textContent          = t.queries          ?? '—';
    document.getElementById('u-sessions').textContent         = t.sessions         ?? '—';
    document.getElementById('u-prompt-tokens').textContent    = t.prompt_tokens    ?? '—';
    document.getElementById('u-completion-tokens').textContent = t.completion_tokens ?? '—';
    document.getElementById('u-embedding-calls').textContent  = t.embedding_calls  ?? '—';
    document.getElementById('u-cost').textContent     = t.cost_usd != null ? '$' + t.cost_usd.toFixed(4) : '—';
    document.getElementById('u-latency').textContent  = t.avg_latency_ms != null ? Math.round(t.avg_latency_ms) : '—';

    // Per session avg
    const psa = d.per_session_avg;
    const psEl = document.getElementById('usage-per-session');
    if (psa && Object.keys(psa).length) {
      psEl.innerHTML = `<div class="per-session-grid">
        <div class="per-session-item">Queries: <strong>${psa.queries ?? '—'}</strong></div>
        <div class="per-session-item">Tokens: <strong>${psa.tokens ?? '—'}</strong></div>
        <div class="per-session-item">Cost: <strong>${psa.cost_usd != null ? '$' + psa.cost_usd.toFixed(4) : '—'}</strong></div>
      </div>`;
    } else {
      psEl.innerHTML = '<div class="no-data">尚無資料</div>';
    }

    // By route table
    const byRoute = d.by_route || [];
    const brEl = document.getElementById('usage-by-route');
    if (byRoute.length) {
      brEl.innerHTML = `<table class="route-table">
        <thead><tr><th>Route</th><th>Queries</th><th>Cost (USD)</th><th>Avg Latency (ms)</th></tr></thead>
        <tbody>${byRoute.map(row => `<tr>
          <td>${escHtml(String(row.route || ''))}</td>
          <td>${row.queries ?? 0}</td>
          <td>${row.cost_usd != null ? '$' + row.cost_usd.toFixed(4) : '—'}</td>
          <td>${row.avg_latency_ms != null ? Math.round(row.avg_latency_ms) : '—'}</td>
        </tr>`).join('')}</tbody>
      </table>`;
    } else {
      brEl.innerHTML = '<div class="no-data">尚無資料</div>';
    }
  } catch(e) {
    console.error('loadUsage error', e);
  }
}

// ── Tickets Tab ───────────────────────────────────────────────────────────────
async function loadTickets() {
  try {
    const r = await fetch('/api/tickets');
    const d = await r.json();
    const tickets = d.tickets || [];
    const badge = document.getElementById('tickets-refresh-badge');
    badge.textContent = '最後更新：' + new Date().toLocaleTimeString('zh-TW');

    const el = document.getElementById('tickets-container');
    if (!tickets.length) {
      el.innerHTML = '<div class="tickets-empty">✅ 目前無開放工單</div>';
      return;
    }
    el.innerHTML = `<table class="tickets-table">
      <thead><tr><th>Priority</th><th>Query ID</th><th>Customer</th><th>Reason</th><th>Created At</th></tr></thead>
      <tbody>${tickets.map(t => {
        const p = t.priority ?? 9;
        const pClass = p === 1 ? 'priority-1' : p === 2 ? 'priority-2' : p === 3 ? 'priority-3' : 'priority-other';
        return `<tr>
          <td class="${pClass}">P${p}</td>
          <td style="font-size:.75rem;color:#64748b">${escHtml(String(t.query_id || ''))}</td>
          <td>${escHtml(String(t.customer_id || ''))}</td>
          <td>${escHtml(String(t.reason || ''))}</td>
          <td style="font-size:.75rem;color:#64748b">${escHtml(String(t.created_at || ''))}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
  } catch(e) {
    console.error('loadTickets error', e);
  }
}
