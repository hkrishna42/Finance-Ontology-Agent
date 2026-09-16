/* Entitlement Console — vanilla JS, no build step */
const $ = (sel, el = document) => el.querySelector(sel);
const view = $('#view');
let statusCache = null;

async function api(path, opts = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new Error(data.error || res.statusText);
    e.data = data;
    throw e;
  }
  return data;
}

function toast(label, text) {
  $('#toastLabel').textContent = label;
  $('#toastText').textContent = text;
  const t = $('#toast');
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 4200);
}
function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function errBox(err) {
  const extra = err.data && err.data.hint ? `<div class="note">${esc(err.data.hint)}</div>` : '';
  const sql = err.data && err.data.executed && err.data.executed.length
    ? `<div class="sqllog">${esc(err.data.executed.join('\n'))}</div>` : '';
  return `<div class="err">${esc(err.message)}${extra}${sql}</div>`;
}

async function refreshStatus() {
  try {
    statusCache = await api('/status');
    if (statusCache.connected) {
      $('#statusText').textContent =
        `Connected to ${(await api('/health')).database} as ${statusCache.me} · policy ${statusCache.objects.policy ? 'ON' : 'not installed'}`;
      $('#railFoot').textContent = `Signed in as ${statusCache.me}`;
    } else {
      $('#statusText').textContent = `Not connected — ${statusCache.error || ''}`;
      $('#railFoot').textContent = 'Not connected';
    }
  } catch (e) {
    $('#statusText').textContent = 'Server unreachable';
  }
}

/* ── Views ─────────────────────────────────────────────── */

async function renderOverview() {
  const h = await api('/health');
  let s = statusCache || (await api('/status'));
  const row = (name, ok) =>
    `<tr><td>${esc(name)}</td><td>${ok ? '<span class="check">✓ in place</span>' : '<span class="cross">✗ missing</span>'}</td></tr>`;
  view.innerHTML = `
    <h1>Overview &amp; setup</h1>
    <div class="pagesub">One entitlement table decides who sees which rows and columns. This console reads it, edits it, and pushes column rules.</div>
    <div class="lbl">Connection</div>
    <div class="panel">
      <table>
        <tr><td>SQL endpoint</td><td>${esc(h.server || '— set FABRIC_SQL_SERVER in .env')}</td></tr>
        <tr><td>Database</td><td>${esc(h.database || '—')}</td></tr>
        <tr><td>Signing in with</td><td>${esc(h.authMode)}</td></tr>
        <tr><td>Connected as</td><td>${s.connected ? esc(s.me) : '<span class="cross">not connected</span>' + (s.error ? ' — ' + esc(s.error) : '')}</td></tr>
      </table>
    </div>
    <div class="lbl">Demo objects</div>
    <div class="panel">
      <table>
        ${row('sales.orders (sample data)', s.connected && s.objects.salesOrders)}
        ${row('gov.entitlement (the entitlement layer)', s.connected && s.objects.entitlement)}
        ${row('Row-level security policy (reads the entitlement table)', s.connected && s.objects.policy)}
        ${row('Masking on account_number', s.connected && s.objects.masking)}
        ${row('gov.change_log (evidence of every change)', s.connected && s.objects.changeLog)}
      </table>
      <div class="formrow" style="margin-top:14px">
        <button id="btnSetup" ${s.connected ? '' : 'disabled'}>Set up demo objects</button>
        <span class="note">Safe to run more than once — each step checks itself first. Also entitles the connected identity to All regions.</span>
      </div>
      <div id="setupOut"></div>
    </div>`;
  $('#btnSetup').onclick = async () => {
    $('#btnSetup').disabled = true;
    try {
      const r = await api('/setup', { method: 'POST' });
      $('#setupOut').innerHTML = `<div class="sqllog">${r.results
        .map((x) => `${x.ok ? '✓' : '✗'} ${esc(x.step)}${x.error ? ' — ' + esc(x.error) : ''}`)
        .join('\n')}</div>`;
      toast(r.ok ? 'READY' : 'CHECK', r.ok ? 'Demo objects are in place.' : 'Some steps failed — see the log.');
      await refreshStatus();
      renderOverview();
    } catch (e) { $('#setupOut').innerHTML = errBox(e); $('#btnSetup').disabled = false; }
  };
}

async function renderTables() {
  view.innerHTML = `
    <h1>Tables &amp; columns</h1>
    <div class="pagesub">Everything the connected identity can see in this warehouse. Governance objects are marked.</div>
    <div class="grid2">
      <div><div class="lbl">Tables</div><div class="panel" id="tblList">Loading…</div></div>
      <div><div class="lbl">Columns</div><div class="panel" id="colList"><span class="note">Select a table.</span></div></div>
    </div>`;
  try {
    const tables = await api('/tables');
    $('#tblList').innerHTML = `<table>${tables
      .map((t, i) =>
        `<tr class="clickable" data-i="${i}"><td>${esc(t.schema)}.${esc(t.table)}</td><td>${t.governance ? '<span class="badge">GOVERNANCE</span>' : ''}</td></tr>`)
      .join('')}</table>`;
    $('#tblList').querySelectorAll('tr').forEach((tr) => {
      tr.onclick = async () => {
        $('#tblList').querySelectorAll('tr').forEach((x) => x.classList.remove('selected'));
        tr.classList.add('selected');
        const t = tables[tr.dataset.i];
        const cols = await api(`/columns?schema=${encodeURIComponent(t.schema)}&table=${encodeURIComponent(t.table)}`);
        let grants = [];
        try { grants = await api(`/column-rules?schema=${encodeURIComponent(t.schema)}&table=${encodeURIComponent(t.table)}`); } catch (_) {}
        $('#colList').innerHTML = `
          <table><tr><th>Column</th><th>Type</th><th></th></tr>${cols
            .map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.type)}</td><td>${c.masked ? '<span class="badge">MASKED</span>' : ''}</td></tr>`)
            .join('')}</table>
          <div class="lbl" style="margin-top:16px">Who has select on this table</div>
          ${grants.length ? `<table>${grants
              .map((g) => `<tr><td>${esc(g.principal)}</td><td>${g.tableWide ? 'all columns' : esc(g.columns.join(', ')) || '—'}</td></tr>`)
              .join('')}</table>` : '<span class="note">No explicit grants. Workspace admins see everything regardless — that is the bypass the proposal warns about.</span>'}`;
      };
    });
  } catch (e) { $('#tblList').innerHTML = errBox(e); }
}

async function renderRows() {
  view.innerHTML = `
    <h1>Row rules</h1>
    <div class="pagesub">Who may see which regions. Changes apply instantly — the row filter reads this table on every query.</div>
    <div class="lbl">Add a rule</div>
    <div class="panel">
      <div class="formrow">
        <input type="email" id="rEmail" placeholder="person@company.com" list="knownUsers" />
        <select id="rRegion"></select>
        <button id="rAdd">Add rule</button>
      </div>
      <datalist id="knownUsers"></datalist>
      <div id="rErr"></div>
    </div>
    <div class="lbl">Current entitlements</div>
    <div class="panel" id="rList">Loading…</div>`;
  const load = async () => {
    try {
      const [ents, regions] = await Promise.all([api('/entitlements'), api('/regions')]);
      $('#rRegion').innerHTML = regions.map((r) => `<option>${esc(r)}</option>`).join('');
      $('#knownUsers').innerHTML = [...new Set(ents.map((e) => e.user_email))].map((u) => `<option value="${esc(u)}">`).join('');
      $('#rList').innerHTML = ents.length
        ? `<table><tr><th>User</th><th>Region</th><th></th></tr>${ents
            .map((e) => `<tr><td>${esc(e.user_email)}</td><td>${esc(e.region)}</td>
              <td><button class="ghost small" data-u="${esc(e.user_email)}" data-r="${esc(e.region)}">Remove</button></td></tr>`)
            .join('')}</table>`
        : '<span class="note">No rules yet. Run setup on the Overview page, then add one above.</span>';
      $('#rList').querySelectorAll('button').forEach((b) => {
        b.onclick = async () => {
          await api('/entitlements', { method: 'DELETE', body: { user_email: b.dataset.u, region: b.dataset.r } });
          toast('REMOVED', `${b.dataset.u} no longer sees ${b.dataset.r}.`);
          load();
        };
      });
    } catch (e) { $('#rList').innerHTML = errBox(e); }
  };
  $('#rAdd').onclick = async () => {
    $('#rErr').innerHTML = '';
    try {
      const r = await api('/entitlements', { method: 'POST', body: { user_email: $('#rEmail').value.trim(), region: $('#rRegion').value } });
      toast('APPLIED', `Row rule active — ${r.applied}.`);
      $('#rEmail').value = '';
      load();
    } catch (e) { $('#rErr').innerHTML = errBox(e); }
  };
  load();
}

async function renderColumns() {
  view.innerHTML = `
    <h1>Column rules</h1>
    <div class="pagesub">Tick exactly the columns a user may see on a table, then push. Pushing runs REVOKE + GRANT on the warehouse.</div>
    <div class="lbl">Rule</div>
    <div class="panel" id="cPanel">
      <div class="formrow">
        <select id="cTable"></select>
        <input type="email" id="cEmail" placeholder="person@company.com" list="knownUsers2" />
        <button class="ghost" id="cLoad">Load current access</button>
      </div>
      <datalist id="knownUsers2"></datalist>
      <div id="cCols"></div>
      <div class="formrow" style="margin-top:14px">
        <button id="cPush" disabled>Push rule to warehouse</button>
        <span class="note" id="cState"></span>
      </div>
      <div id="cOut"></div>
    </div>`;
  const tables = (await api('/tables')).filter((t) => !t.governance);
  $('#cTable').innerHTML = tables.map((t) => `<option value="${esc(t.schema)}|${esc(t.table)}">${esc(t.schema)}.${esc(t.table)}</option>`).join('');
  try {
    const ents = await api('/entitlements');
    $('#knownUsers2').innerHTML = [...new Set(ents.map((e) => e.user_email))].map((u) => `<option value="${esc(u)}">`).join('');
  } catch (_) {}

  let dirty = false;
  const setDirty = (d) => {
    dirty = d;
    $('#cPanel').classList.toggle('pending', d);
    $('#cPush').disabled = !d;
    $('#cState').textContent = d ? 'Unpushed changes — blush means not yet enforced.' : '';
  };

  $('#cLoad').onclick = async () => {
    $('#cOut').innerHTML = '';
    const [schema, table] = $('#cTable').value.split('|');
    const email = $('#cEmail').value.trim();
    if (!email) { $('#cOut').innerHTML = '<div class="err">Enter a user email first.</div>'; return; }
    try {
      const [cols, grants] = await Promise.all([
        api(`/columns?schema=${encodeURIComponent(schema)}&table=${encodeURIComponent(table)}`),
        api(`/column-rules?schema=${encodeURIComponent(schema)}&table=${encodeURIComponent(table)}`),
      ]);
      const mine = grants.find((g) => g.principal.toLowerCase() === email.toLowerCase());
      const has = (c) => (mine ? mine.tableWide || mine.columns.includes(c) : false);
      $('#cCols').innerHTML = `<div class="colgrid">${cols
        .map((c) => `<label><input type="checkbox" value="${esc(c.name)}" ${has(c.name) ? 'checked' : ''}/> ${esc(c.name)}
          ${c.masked ? '<span class="badge">MASKED</span>' : ''}</label>`)
        .join('')}</div>
        <div class="note">${mine ? (mine.tableWide ? 'Currently: all columns.' : `Currently: ${mine.columns.length} column(s).`) : 'Currently: no access to this table.'}</div>`;
      $('#cCols').querySelectorAll('input').forEach((i) => (i.onchange = () => setDirty(true)));
      setDirty(false);
    } catch (e) { $('#cOut').innerHTML = errBox(e); }
  };

  $('#cPush').onclick = async () => {
    const [schema, table] = $('#cTable').value.split('|');
    const email = $('#cEmail').value.trim();
    const columns = [...$('#cCols').querySelectorAll('input:checked')].map((i) => i.value);
    $('#cPush').disabled = true;
    try {
      const r = await api('/column-rules', { method: 'POST', body: { user_email: email, schema, table, columns } });
      $('#cOut').innerHTML = `<div class="sqllog">${esc(r.executed.join('\n'))}</div><div class="note">${esc(r.reminder)}</div>`;
      toast('PUSHED', `${email} → ${columns.length ? columns.length + ' column(s)' : 'no access'} on ${schema}.${table}`);
      setDirty(false);
    } catch (e) { $('#cOut').innerHTML = errBox(e); setDirty(true); }
  };
}

async function renderPreview() {
  view.innerHTML = `
    <h1>Preview as user</h1>
    <div class="pagesub">Simulated from grants + entitlement rows: the rows and columns this user would get. Masking is not simulated.</div>
    <div class="lbl">Who</div>
    <div class="panel">
      <div class="formrow">
        <input type="email" id="pEmail" placeholder="person@company.com" />
        <button id="pGo">Preview</button>
      </div>
    </div>
    <div id="pOut"></div>`;
  $('#pGo').onclick = async () => {
    $('#pOut').innerHTML = '<div class="note">Loading…</div>';
    try {
      const r = await api(`/preview?email=${encodeURIComponent($('#pEmail').value.trim())}`);
      const head = r.visibleColumns.map((c) => `<th>${esc(c)}</th>`).join('');
      const body = r.rows.map((row) => `<tr>${r.visibleColumns.map((c) => `<td>${esc(row[c] ?? '')}</td>`).join('')}</tr>`).join('');
      $('#pOut').innerHTML = `
        <div class="lbl">Regions</div><div class="panel">${r.regions.length ? r.regions.map((x) => `<span class="chip">${esc(x)}</span>`).join('') : '<span class="note">No row rules — this user sees zero rows.</span>'}</div>
        <div class="lbl">Columns</div><div class="panel">
          ${r.visibleColumns.map((c) => `<span class="chip">${esc(c)}</span>`).join('')}
          ${r.hiddenColumns.map((c) => `<span class="chip off">${esc(c)}</span>`).join('')}
          ${r.visibleColumns.length ? '' : '<div class="note">No column grants — queries would be denied.</div>'}
        </div>
        <div class="lbl">Rows (top 50, simulated)</div>
        <div class="panel">${r.rows.length ? `<table><tr>${head}</tr>${body}</table>` : '<span class="note">No rows.</span>'}
        <div class="note">${esc(r.note)}</div></div>`;
    } catch (e) { $('#pOut').innerHTML = errBox(e); }
  };
}

async function renderEvidence() {
  view.innerHTML = `
    <h1>Evidence</h1>
    <div class="pagesub">Every change made through this console, as recorded in gov.change_log — who, when, what, and the SQL that ran. Read-only.</div>
    <div class="lbl">Recent changes</div>
    <div class="panel">
      <div class="formrow"><button class="ghost small" id="eRefresh">Refresh</button></div>
      <div id="eList" style="margin-top:14px">Loading…</div>
    </div>`;
  const when = (iso) => { const d = new Date(iso); return isNaN(d) ? esc(iso) : d.toISOString().slice(0, 19).replace('T', ' ') + ' UTC'; };
  const detail = (s) => {
    const [summary, ...sql] = String(s).trimEnd().split('\n');
    return esc(summary) + (sql.length ? `<div class="sqllog">${esc(sql.join('\n'))}</div>` : '');
  };
  const load = async () => {
    try {
      const changes = await api('/changes?limit=100');
      $('#eList').innerHTML = changes.length
        ? `<table><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th></tr>${changes
            .map((c) => `<tr><td style="white-space:nowrap">${when(c.at)}</td><td>${esc(c.actor)}</td>
              <td style="white-space:nowrap"><span class="badge plain">${esc(String(c.action).toUpperCase())}</span></td><td>${detail(c.detail)}</td></tr>`)
            .join('')}</table>`
        : '<span class="note">No changes recorded yet. Run setup on the Overview page, then make a change.</span>';
    } catch (e) { $('#eList').innerHTML = errBox(e); }
  };
  $('#eRefresh').onclick = load;
  load();
}

/* ── Router ────────────────────────────────────────────── */
const routes = { overview: renderOverview, tables: renderTables, rows: renderRows, columns: renderColumns, preview: renderPreview, evidence: renderEvidence };
async function route() {
  const name = (location.hash || '#overview').slice(1);
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
  try { await (routes[name] || renderOverview)(); }
  catch (e) { view.innerHTML = errBox(e); }
}
window.addEventListener('hashchange', route);
(async () => { await refreshStatus(); route(); })();
