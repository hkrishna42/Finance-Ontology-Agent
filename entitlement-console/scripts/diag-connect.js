// node scripts/diag-connect.js — pinpoint why the console cannot log in to the warehouse.
// Times the Entra token, then tries the app's exact connection and two variants (TDS 8.0 strict
// encryption; a token fetched before the socket opens), printing the driver's state log on failure.
// Diagnostic only — nothing here is used by the app.
const path = require('path');
const { execFileSync } = require('child_process');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });
const { Connection, Request } = require('tedious');

const server = process.env.FABRIC_SQL_SERVER;
const database = process.env.FABRIC_SQL_DATABASE;
if (!server || !database) { console.log('FABRIC_SQL_SERVER / FABRIC_SQL_DATABASE not set in .env'); process.exit(1); }
const SCOPE = 'https://database.windows.net//.default';

function attempt(label, authentication, encrypt) {
  return new Promise((resolve) => {
    const log = [];
    const t0 = Date.now();
    let settled = false;
    const conn = new Connection({
      server, authentication,
      options: { database, port: 1433, encrypt, trustServerCertificate: false, connectTimeout: 30000, requestTimeout: 30000, debug: { packet: true, token: true } },
    });
    conn.on('debug', (m) => log.push(m));
    const done = (ok, msg) => { if (settled) return; settled = true; try { conn.close(); } catch (_) {} resolve({ label, ok, msg, ms: Date.now() - t0, log }); };
    conn.on('error', (e) => done(false, e.message));
    conn.connect((err) => {
      if (err) return done(false, err.message);
      let me = '?';
      const req = new Request('SELECT USER_NAME() AS me', (e) => done(!e, e ? e.message : `USER_NAME() = ${me}`));
      req.on('row', (cols) => { me = cols[0].value; });
      conn.execSql(req);
    });
  });
}

// Ask Fabric what this sign-in can see and whether .env names one of those warehouses exactly.
// A wrong hostname still resolves (wildcard DNS) and passes TLS (shared gateway cert) — the gateway
// only hangs up at LOGIN7 — so this is the check that catches a typo or the wrong tenant.
async function fabricInventory() {
  let tok;
  try {
    tok = execFileSync('az', ['account', 'get-access-token', '--resource', 'https://api.fabric.microsoft.com', '-o', 'tsv', '--query', 'accessToken'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (e) { return console.log(`Fabric API token        FAIL ${String(e.message).split('\n')[0]}`); }
  const get = async (p) => {
    const r = await fetch('https://api.fabric.microsoft.com/v1' + p, { headers: { Authorization: `Bearer ${tok}` } });
    if (!r.ok) throw new Error(`HTTP ${r.status} on ${p}`);
    return (await r.json()).value || [];
  };
  try {
    const workspaces = await get('/workspaces');
    console.log(`Fabric API              OK   ${workspaces.length} workspace(s) visible to this sign-in (tenant of \`az login\`)`);
    let capacities = [];
    try { capacities = await get('/capacities'); } catch (_) {}
    let match = false;
    for (const w of workspaces) {
      const cap = capacities.find((c) => c.id === w.capacityId);
      const capText = cap ? `${cap.displayName} (${cap.sku}, ${cap.region}) state ${cap.state}${cap.state === 'Active' ? '' : '  <-- not Active: SQL endpoints in this workspace are unreachable until it is resumed'}`
        : (w.capacityId ? `${w.capacityId} (not visible to this sign-in — ask an admin whether it is Active)` : 'none (Pro workspace, no Fabric capacity)');
      console.log(`  workspace "${w.displayName}" · capacity ${capText}`);
      const items = [];
      try { for (const x of await get(`/workspaces/${w.id}/warehouses`)) items.push(['warehouse', x.displayName, x.properties && x.properties.connectionString]); } catch (e) { items.push(['warehouses', e.message, '']); }
      try { for (const x of await get(`/workspaces/${w.id}/lakehouses`)) items.push(['lakehouse (read-only endpoint)', x.displayName, x.properties && x.properties.sqlEndpointProperties && x.properties.sqlEndpointProperties.connectionString]); } catch (_) {}
      for (const [kind, name, cs] of items) {
        const hit = kind === 'warehouse' && String(cs).toLowerCase() === server.toLowerCase() && String(name).toLowerCase() === database.toLowerCase();
        match = match || hit;
        console.log(`  ${hit ? '>>' : '  '} ${w.displayName} · ${kind} "${name}" · ${cs || '(no SQL endpoint yet)'}`);
      }
    }
    console.log(match
      ? '  .env matches a warehouse this sign-in can see.'
      : '  NO warehouse matches FABRIC_SQL_SERVER + FABRIC_SQL_DATABASE in .env — copy the values from a warehouse line above, or `az login --tenant <id>` to the tenant that holds it.');
  } catch (e) { console.log(`Fabric API              FAIL ${e.message}`); }
}

(async () => {
  console.log(`Target: ${server} / ${database}  (node ${process.version}, tedious ${require('tedious/package.json').version})\n`);
  await fabricInventory();
  console.log('');
  let token = null;
  try {
    const t0 = Date.now();
    token = JSON.parse(execFileSync('az', ['account', 'get-access-token', '--resource', 'https://database.windows.net/', '-o', 'json'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })).accessToken;
    console.log(`az CLI token            OK   ${Date.now() - t0} ms`);
  } catch (e) { console.log(`az CLI token            FAIL ${String(e.message).split('\n')[0]}`); }
  try {
    const { DefaultAzureCredential } = require('@azure/identity'); // what the app's auth mode uses, via tedious
    const t0 = Date.now();
    await new DefaultAzureCredential().getToken(SCOPE);
    console.log(`DefaultAzureCredential  OK   ${Date.now() - t0} ms  (the driver fetches this mid-login)`);
  } catch (e) { console.log(`DefaultAzureCredential  FAIL ${String(e.message).split('\n')[0]}`); }

  const variants = [
    ['app config: encrypt true, azure-active-directory-default', { type: 'azure-active-directory-default', options: {} }, true],
    ['TDS 8.0: encrypt strict, azure-active-directory-default', { type: 'azure-active-directory-default', options: {} }, 'strict'],
  ];
  if (token) variants.push(['token fetched before connecting: encrypt true, azure-active-directory-access-token', { type: 'azure-active-directory-access-token', options: { token } }, true]);
  for (const [label, auth, encrypt] of variants) {
    const r = await attempt(label, auth, encrypt);
    console.log(`\n${r.ok ? 'OK  ' : 'FAIL'} ${label}\n     ${r.msg} (${r.ms} ms)`);
    if (!r.ok) console.log(r.log.slice(-14).map((l) => '     | ' + l).join('\n'));
  }
  process.exit(0);
})();
