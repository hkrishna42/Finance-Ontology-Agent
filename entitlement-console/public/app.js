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
    <div class="lbl">Warehouse</div>
    <div class="panel">
      <div class="formrow">
        <select id="whSelect"><option value="">Loading warehouses…</option></select>
        <span class="note" id="whNote">Pick the warehouse to manage. From your Fabric workspaces (needs <code>az login</code>).</span>
      </div>
    </div>
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
        <span class="note">Safe to run more than once — each step checks itself first. Also entitles the connected identity to all rows.</span>
      </div>
      <div id="setupOut"></div>
    </div>`;
  // Warehouse picker: list the admin's warehouses; switching one reconnects the console to it.
  (async () => {
    try {
      const whs = await api('/warehouses');
      const opts = whs.map((w) => `<option value="${esc(w.server)}|${esc(w.database)}" ${w.current ? 'selected' : ''}>${esc(w.workspace)} · ${esc(w.name)}</option>`).join('');
      $('#whSelect').innerHTML = (whs.some((w) => w.current) ? '' : '<option value="">— select a warehouse —</option>') + opts;
    } catch (e) {
      $('#whSelect').innerHTML = '<option value="">unavailable</option>';
      $('#whNote').innerHTML = errBox(e);
    }
  })();
  $('#whSelect').onchange = async () => {
    const [server, database] = ($('#whSelect').value || '').split('|');
    if (!server || !database) return;
    $('#whNote').textContent = 'Switching…';
    try {
      const r = await api('/target', { method: 'POST', body: { server, database } });
      toast('WAREHOUSE', `Now managing ${database}${r.connected ? ' · ' + r.me : ''}.`);
      await refreshStatus();
      renderOverview();
    } catch (e) { $('#whNote').innerHTML = errBox(e); }
  };
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

async function renderAccess() {
  view.innerHTML = `
    <h1>User access</h1>
    <div class="pagesub">Pick a user, then choose the columns and the rows they may see. Column changes run REVOKE + GRANT on the warehouse; row changes update the entitlement table (the filter reads it live).</div>
    <div class="lbl">User</div>
    <div class="panel">
      <div class="formrow">
        <input type="email" id="aEmail" placeholder="person@company.com" list="knownUsers" />
        <button id="aLoad">Load access</button>
      </div>
      <datalist id="knownUsers"></datalist>
      <div id="aErr"></div>
    </div>
    <div id="aBody" hidden>
      <div class="lbl">Columns this user may see</div>
      <div class="panel" id="cPanel">
        <div id="aCols"></div>
        <div class="formrow" style="margin-top:14px"><button id="cPush" disabled>Push column access</button><span class="note" id="cState"></span></div>
        <div id="cOut"></div>
      </div>
      <div class="lbl">Rows this user may see</div>
      <div class="panel" id="rPanel">
        <label style="display:block; margin-bottom:10px"><input type="checkbox" id="rAll" /> All rows (full access)</label>
        <div id="aRows"></div>
        <div class="formrow" style="margin-top:14px"><button id="rApply" disabled>Apply row access</button><span class="note" id="rState"></span></div>
        <div id="rOut"></div>
      </div>
    </div>`;
  const SCHEMA = 'sales', TABLE = 'orders';
  const setColDirty = (d) => { $('#cPanel').classList.toggle('pending', d); $('#cPush').disabled = !d; $('#cState').textContent = d ? 'Unpushed — blush means not yet enforced.' : ''; };
  const setRowDirty = (d) => { $('#rPanel').classList.toggle('pending', d); $('#rApply').disabled = !d; $('#rState').textContent = d ? 'Unpushed — blush means not yet enforced.' : ''; };
  const syncRowDisabled = () => { const all = $('#rAll').checked; $('#aRows').querySelectorAll('.rowck').forEach((c) => (c.disabled = all)); };

  try {
    const ents = await api('/entitlements');
    $('#knownUsers').innerHTML = [...new Set(ents.map((e) => e.user_email))].map((u) => `<option value="${esc(u)}">`).join('');
  } catch (_) {}

  $('#aLoad').onclick = async () => {
    const email = $('#aEmail').value.trim();
    $('#aErr').innerHTML = '';
    if (!email) { $('#aErr').innerHTML = '<div class="err">Enter a user email first.</div>'; return; }
    try {
      const [cols, grants, rows, ents] = await Promise.all([
        api(`/columns?schema=${SCHEMA}&table=${TABLE}`),
        api(`/column-rules?schema=${SCHEMA}&table=${TABLE}`),
        api('/rows'),
        api('/entitlements'),
      ]);
      // Columns
      const mine = grants.find((g) => g.principal.toLowerCase() === email.toLowerCase());
      const hasCol = (c) => (mine ? mine.tableWide || mine.columns.includes(c) : false);
      $('#aCols').innerHTML = `<div class="colgrid">${cols
        .map((c) => `<label><input type="checkbox" value="${esc(c.name)}" ${hasCol(c.name) ? 'checked' : ''}/> ${esc(c.name)} ${c.masked ? '<span class="badge">MASKED</span>' : ''}</label>`)
        .join('')}</div>`;
      $('#aCols').querySelectorAll('input').forEach((i) => (i.onchange = () => setColDirty(true)));
      setColDirty(false);
      // Rows
      const mineRows = ents.filter((e) => e.user_email.toLowerCase() === email.toLowerCase()).map((e) => e.order_id);
      const all = mineRows.includes(-1);
      $('#rAll').checked = all;
      $('#aRows').innerHTML = `<table><tr><th></th><th>order_id</th><th>region</th><th>customer_name</th><th>amount</th><th>order_date</th></tr>${rows
        .map((r) => `<tr><td><input type="checkbox" class="rowck" value="${r.order_id}" ${all || mineRows.includes(r.order_id) ? 'checked' : ''} ${all ? 'disabled' : ''}/></td><td>${r.order_id}</td><td>${esc(r.region)}</td><td>${esc(r.customer_name)}</td><td>${esc(String(r.amount))}</td><td>${esc(String(r.order_date))}</td></tr>`)
        .join('')}</table>`;
      $('#rAll').onchange = () => { syncRowDisabled(); setRowDirty(true); };
      $('#aRows').querySelectorAll('.rowck').forEach((c) => (c.onchange = () => setRowDirty(true)));
      syncRowDisabled();
      setRowDirty(false);
      $('#aBody').hidden = false;
      $('#aBody').dataset.rowsBefore = JSON.stringify({ all, ids: mineRows.filter((id) => id !== -1) });
    } catch (e) { $('#aErr').innerHTML = errBox(e); }
  };

  $('#cPush').onclick = async () => {
    const email = $('#aEmail').value.trim();
    const columns = [...$('#aCols').querySelectorAll('input:checked')].map((i) => i.value);
    $('#cPush').disabled = true;
    try {
      const r = await api('/column-rules', { method: 'POST', body: { user_email: email, schema: SCHEMA, table: TABLE, columns } });
      $('#cOut').innerHTML = `<div class="sqllog">${esc(r.executed.join('\n'))}</div><div class="note">${esc(r.reminder)}</div>`;
      toast('PUSHED', `${email} → ${columns.length ? columns.length + ' column(s)' : 'no access'}`);
      setColDirty(false);
    } catch (e) { $('#cOut').innerHTML = errBox(e); setColDirty(true); }
  };

  $('#rApply').onclick = async () => {
    const email = $('#aEmail').value.trim();
    const before = JSON.parse($('#aBody').dataset.rowsBefore);
    const allNow = $('#rAll').checked;
    const idsNow = allNow ? [] : [...$('#aRows').querySelectorAll('.rowck:checked')].map((c) => Number(c.value));
    $('#rApply').disabled = true;
    const log = [];
    const grant = async (id, label) => { await api('/entitlements', { method: 'POST', body: { user_email: email, order_id: id } }); log.push('grant ' + label); };
    const revoke = async (id, label) => { await api('/entitlements', { method: 'DELETE', body: { user_email: email, order_id: id } }); log.push('revoke ' + label); };
    try {
      if (allNow && !before.all) await grant(-1, 'ALL rows');
      if (!allNow && before.all) await revoke(-1, 'ALL rows');
      if (allNow) {
        for (const id of before.ids) await revoke(id, 'row ' + id); // tidy explicit grants under all-access
      } else {
        for (const id of idsNow) if (!before.ids.includes(id)) await grant(id, 'row ' + id);
        for (const id of before.ids) if (!idsNow.includes(id)) await revoke(id, 'row ' + id);
      }
      $('#rOut').innerHTML = `<div class="sqllog">${esc(log.join('\n') || 'no change')}</div>`;
      toast('APPLIED', `${email} → ${allNow ? 'all rows' : idsNow.length + ' row(s)'}`);
      $('#aBody').dataset.rowsBefore = JSON.stringify({ all: allNow, ids: idsNow });
      setRowDirty(false);
    } catch (e) { $('#rOut').innerHTML = errBox(e); setRowDirty(true); }
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
        <div class="lbl">Columns</div><div class="panel">
          ${r.visibleColumns.map((c) => `<span class="chip">${esc(c)}</span>`).join('')}
          ${r.hiddenColumns.map((c) => `<span class="chip off">${esc(c)}</span>`).join('')}
          ${r.visibleColumns.length ? '' : '<div class="note">No column grants — queries would be denied.</div>'}
        </div>
        <div class="lbl">Rows (${r.allRows ? 'all rows' : r.rows.length + ' row(s)'}, simulated)</div>
        <div class="panel">${r.rows.length ? `<table><tr>${head}</tr>${body}</table>` : '<span class="note">No rows — this user has no row grants.</span>'}
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
const routes = { overview: renderOverview, tables: renderTables, access: renderAccess, preview: renderPreview, evidence: renderEvidence };
async function route() {
  const name = (location.hash || '#overview').slice(1);
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
  try { await (routes[name] || renderOverview)(); }
  catch (e) { view.innerHTML = errBox(e); }
}
window.addEventListener('hashchange', route);
(async () => { await refreshStatus(); route(); })();
