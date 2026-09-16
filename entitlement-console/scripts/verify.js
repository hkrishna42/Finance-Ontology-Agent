// npm run verify — persona verification harness (seed of "continuous assurance": access rules
// proven, not assumed). Static checks always run; warehouse checks need a connected .env; the
// persona matrix needs VERIFY_P3_EMAIL + VERIFY_P4_EMAIL (P2 optional). Anything it cannot
// prove is SKIP (with the reason) or MANUAL (with the exact SQL a human runs) — never omitted,
// except that a server which fails to boot ends the run (FAIL, exit 1).
const path = require('path');
const net = require('net');
const { spawn } = require('child_process');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const ROOT = path.join(__dirname, '..');
const { VERIFY_P2_EMAIL: P2, VERIFY_P3_EMAIL: P3, VERIFY_P4_EMAIL: P4 } = process.env;
const tally = { PASS: 0, FAIL: 0, SKIP: 0, MANUAL: 0 };
let base = ''; // http://127.0.0.1:<free port>
let skip = null; // when set, test() reports SKIP with this reason instead of running
let child = null;

function row(status, name, detail) {
  tally[status]++;
  console.log(`${status.padEnd(6)} ${name}${detail ? ' — ' + detail : ''}`);
}

// fn resolves to [ok, detail]; a thrown error is a FAIL carrying its message.
async function test(name, fn, why = skip) {
  if (why) return row('SKIP', name, why);
  try { const [ok, detail] = await fn(); row(ok ? 'PASS' : 'FAIL', name, detail); }
  catch (e) { row('FAIL', name, e.message); }
}

function manual(name, sqlText, expected) { row('MANUAL', name, `${sqlText} → ${expected}`); }

async function http(method, p, body) {
  const res = await fetch(base + p, { method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
  const text = await res.text();
  let json = null; try { json = JSON.parse(text); } catch (_) {}
  return { status: res.status, text, json };
}

// Same contract as the UI's api(): non-2xx throws the server's error (+ its executed SQL log).
async function api(method, p, body) {
  const r = await http(method, p, body);
  if (r.status < 400) return r.json;
  const d = r.json || {};
  throw new Error(`${d.error || 'HTTP ' + r.status}${d.executed ? ` [executed: ${d.executed.join(' | ')}]` : ''}`);
}

const J = JSON.stringify;
const same = (a, b) => J(a) === J(b);
const errOf = (r) => (r.json && r.json.error) || `HTTP ${r.status}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer().listen(0, () => { const { port } = s.address(); s.close(() => resolve(port)); });
    s.on('error', reject);
  });
}

// Spawn the real server on a free port (cwd = project dir so its own dotenv sees .env); resolve to /api/health.
async function boot() {
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  let stderr = '', dead = false;
  child = spawn(process.execPath, ['server/index.js'], { cwd: ROOT, env: { ...process.env, PORT: String(port) }, stdio: ['ignore', 'ignore', 'pipe'] });
  child.stderr.on('data', (d) => { stderr += d; });
  child.on('close', () => { dead = true; });
  const t0 = Date.now();
  while (!dead && Date.now() - t0 < 10000) {
    const r = await http('GET', '/api/health').catch(() => null);
    if (r && r.status === 200) return r.json;
    await sleep(200);
  }
  const lines = stderr.split('\n').filter(Boolean);
  const why = lines.find((l) => /error/i.test(l)) || lines.pop();
  throw new Error(`server did not boot${dead ? ` (exited ${child.exitCode})` : ' within 10 s'}${why ? ': ' + why : ''}`);
}

async function main() {
  console.log('Static checks');
  let health;
  try { health = await boot(); } catch (e) { return row('FAIL', 'boot', e.message); }
  row('PASS', 'boot', `GET /api/health 200 on ${base} (configured: ${health.configured})`);
  await test('GET / serves the UI', async () => {
    const r = await http('GET', '/');
    const missing = ['<title>Entitlement Console', 'app.js'].filter((s) => !r.text.includes(s));
    return [r.status === 200 && !missing.length, `status ${r.status}${missing.length ? ', missing ' + missing.join(', ') : ', has <title> + app.js'}`];
  });
  for (const f of ['/app.js', '/styles.css']) {
    await test(`GET ${f} → 200`, async () => { const r = await http('GET', f); return [r.status === 200, `status ${r.status}`]; });
  }
  await test('GET /api/preview?email=not-an-email → 400', async () => {
    const r = await http('GET', '/api/preview?email=not-an-email');
    return [r.status === 400, `status ${r.status}: ${errOf(r)}`];
  });
  await test('POST /api/entitlements with bad email → 400', async () => {
    const r = await http('POST', '/api/entitlements', { user_email: 'nope', region: 'EU' });
    return [r.status === 400, `status ${r.status}: ${errOf(r)}`];
  });
  await test('POST /api/column-rules with table "orders]" → 400 Invalid identifier', async () => {
    const r = await http('POST', '/api/column-rules', { user_email: 'p3@example.com', schema: 'sales', table: 'orders]', columns: [] });
    return [r.status === 400 && /Invalid identifier/.test(errOf(r)), `status ${r.status}: ${errOf(r)}`];
  });
  await test('GET /api/preview with table "orders]" → 400', async () => {
    const r = await http('GET', '/api/preview?email=p3@example.com&schema=sales&table=orders%5D');
    return [r.status === 400, `status ${r.status}: ${errOf(r)}`];
  });
  let status = {};
  await test('GET /api/status → 200 with boolean connected', async () => {
    const r = await http('GET', '/api/status');
    status = r.json || {};
    return [r.status === 200 && typeof status.connected === 'boolean', `status ${r.status}, connected ${status.connected}${status.error ? ' (' + status.error + ')' : ''}`];
  });
  await test('GET /api/nope → 404', async () => { const r = await http('GET', '/api/nope'); return [r.status === 404, `status ${r.status}`]; });

  console.log('\nWarehouse checks');
  if (!health.configured) skip = 'not configured — no FABRIC_SQL_SERVER/DATABASE (.env)';
  else if (!status.connected) skip = `not connected — ${status.error}`;
  const setup = async () => {
    const r = await api('POST', '/api/setup');
    const failed = r.results.filter((s) => !s.ok).map((s) => `${s.step}: ${s.error}`);
    return [r.ok === true, failed.length ? `failed steps: ${failed.join('; ')}` : `${r.results.length} steps ok`];
  };
  await test('POST /api/setup → ok (run 1)', setup);
  await test('POST /api/setup again → ok (idempotent)', setup);
  await test('GET /api/status → every object present', async () => {
    const s = await api('GET', '/api/status');
    const objects = Object.entries(s.objects || {}); // not hardcoded: a future object is covered too
    const missing = objects.filter(([, v]) => v !== true).map(([k]) => k);
    const ok = s.connected === true && objects.length > 0 && !missing.length;
    return [ok, !s.connected ? `not connected — ${s.error}` : missing.length ? `false: ${missing.join(', ')}` : `all true: ${objects.map(([k]) => k).join(', ')}`];
  });
  await test('GET /api/entitlements has operator → All', async () => {
    const rows = await api('GET', '/api/entitlements');
    const ok = rows.some((e) => e.user_email === status.me && e.region === 'All');
    return [ok, `${status.me} → ${ok ? 'All' : 'no All row'}`];
  });

  console.log('\nPersona matrix (simulated via /api/preview)');
  if (!skip && !(P3 && P4)) skip = 'set VERIFY_P3_EMAIL + VERIFY_P4_EMAIL in .env (real Entra users the warehouse is shared with)';
  const ps = [P2, P3, P4].filter(Boolean); // never converge the operator, never two personas on one identity
  if (!skip && (ps.includes(status.me) || new Set(ps).size !== ps.length)) skip = 'VERIFY_*_EMAIL must be distinct and not the operator';
  const p2skip = skip || (P2 ? null : 'VERIFY_P2_EMAIL not set (optional)');
  const preview = (email) => api('GET', `/api/preview?email=${encodeURIComponent(email)}&schema=sales&table=orders`);
  // Converge a persona to exactly {region} (or no row rules) + an exact column allow-list, through
  // the same endpoints the Row rules / Column rules tabs use. Idempotent — safe to re-run.
  const converge = async (email, region, columns) => {
    const mine = (await api('GET', '/api/entitlements')).filter((e) => e.user_email === email);
    for (const e of mine) if (e.region !== region) await api('DELETE', '/api/entitlements', { user_email: email, region: e.region });
    if (region) await api('POST', '/api/entitlements', { user_email: email, region });
    const r = await api('POST', '/api/column-rules', { user_email: email, schema: 'sales', table: 'orders', columns });
    return [r.ok === true, `rows: ${region || 'none'}; columns: ${columns.join(', ') || 'none'}; executed ${r.executed.length} statements`];
  };
  // What the preview must show: only {region}, exactly {hidden} struck out, 2 rows, no hidden column leaking into a row.
  const sees = (p, region, hidden) => {
    const leak = p.rows.some((r) => hidden.some((h) => h in r));
    const ok = same(p.regions, [region]) && same(p.hiddenColumns, hidden) && p.rows.length === 2 && p.rows.every((r) => r.region === region) && !leak;
    return [ok, `regions ${J(p.regions)}, hidden ${J(p.hiddenColumns)}, ${p.rows.length} rows [${p.rows.map((r) => r.region).join(', ')}]${leak ? ', hidden column leaked into rows' : ''}`];
  };
  let cols = [];
  await test('GET /api/columns sales.orders lists customer_ssn', async () => {
    cols = (await api('GET', '/api/columns?schema=sales&table=orders')).map((c) => c.name);
    if (!cols.length) skip = 'sales.orders columns unavailable — not converging personas';
    return [cols.includes('customer_ssn'), cols.join(', ')];
  });
  await test('P3 converge: rows EU, all columns', () => converge(P3, 'EU', cols));
  await test('P4 converge: rows APAC, all columns minus customer_ssn', () => converge(P4, 'APAC', cols.filter((c) => c !== 'customer_ssn')));
  await test('P2 converge: no rows, no columns', () => converge(P2, null, []), p2skip);
  await test('P2 preview: no regions, no visible columns', async () => {
    const p = await preview(P2);
    return [same(p.regions, []) && same(p.visibleColumns, []), `regions ${J(p.regions)}, visibleColumns ${J(p.visibleColumns)} (changes who sees data without seeing data)`];
  }, p2skip);
  await test('P3 preview: EU only, all columns, 2 rows', async () => sees(await preview(P3), 'EU', []));
  await test('P4 preview: APAC only, customer_ssn hidden, 2 rows', async () => sees(await preview(P4), 'APAC', ['customer_ssn']));
  await test('headline: flip P3 EU → US', async () => {
    await api('DELETE', '/api/entitlements', { user_email: P3, region: 'EU' });
    await api('POST', '/api/entitlements', { user_email: P3, region: 'US' });
    const [ok, detail] = sees(await preview(P3), 'US', []);
    return [ok, `preview shows US on the next query after the flip; ${detail}`];
  });
  await test('headline: restore P3 US → EU', async () => {
    await api('DELETE', '/api/entitlements', { user_email: P3, region: 'US' });
    await api('POST', '/api/entitlements', { user_email: P3, region: 'EU' });
    const [ok, detail] = sees(await preview(P3), 'EU', []);
    return [ok, `restored; ${detail}`];
  });
  console.log('Manual — each persona signs in themselves (warehouse Shared with them, no workspace roles) and runs:');
  manual('P1 operator sees everything', 'SELECT COUNT(*) FROM sales.orders', '6');
  manual('P2 access admin cannot read data', 'SELECT * FROM sales.orders', 'error 229, permission denied');
  manual('P2 access admin can edit entitlements', `INSERT INTO gov.entitlement VALUES ('x@example.com','EU')`,
    `succeeds; needs the manual grant: GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::gov.entitlement TO [${P2 || '<p2 email>'}]`);
  manual('P3 sees masked account numbers', 'SELECT account_number FROM sales.orders', "values begin 'XXXX-' (masking only shows on a real sign-in)");
  manual('P4 denied customer_ssn', 'SELECT customer_ssn FROM sales.orders', 'error 230, column permission denied');
}

process.on('exit', () => { if (child) child.kill(); });
for (const s of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(s, () => process.exit(130));
main().catch((e) => row('FAIL', 'harness', e.message)).then(() => {
  console.log(`\n${tally.PASS} passed · ${tally.FAIL} failed · ${tally.SKIP} skipped · ${tally.MANUAL} manual`);
  if (child) child.kill();
  process.exitCode = tally.FAIL ? 1 : 0; // let stdout drain instead of exiting mid-write
});
