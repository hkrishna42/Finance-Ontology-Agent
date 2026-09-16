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

// LOGIN7 tweaks, applied by monkeypatching tedious's serializer. Compared with go-mssqldb (which logs
// in to this warehouse) tedious differs in: FEDAUTH workflow byte (0x02 vs 0x03), OptionFlags1
// (no fUseDB, plus INIT_DB_FATAL), OptionFlags2 (no fODBC), OptionFlags3 (UnknownCollationHandling),
// and it writes the FeatureExt block right after ServerName instead of last. Each probe flips some.
let tweak = {};
const Login7 = (m => m.default || m)(require('tedious/lib/login7-payload'));
const origFeatureExt = Login7.prototype.buildFeatureExt;
Login7.prototype.buildFeatureExt = function () {
  let b = origFeatureExt.call(this);
  if (tweak.workflow != null && this.fedAuth && this.fedAuth.type === 'ADAL') b[6] = tweak.workflow;
  if (tweak.noUtf8 && b[b.length - 7] === 0x0a) b = Buffer.concat([b.subarray(0, b.length - 7), b.subarray(b.length - 1)]); // drop the UTF8_SUPPORT entry
  return b;
};
const origToBuffer = Login7.prototype.toBuffer;
Login7.prototype.toBuffer = function () {
  if (tweak.libraryName) this.libraryName = tweak.libraryName;
  if (tweak.clientProgVer) this.clientProgVer = tweak.clientProgVer;
  let d = origToBuffer.call(this);
  if (tweak.flags1 != null) d.writeUInt8(tweak.flags1, 24);
  if (tweak.flags2 != null) d.writeUInt8(tweak.flags2, 25);
  if (tweak.flags3 != null) d.writeUInt8(tweak.flags3, 27);
  if (tweak.tz != null) d.writeInt32LE(tweak.tz, 28);
  if (tweak.extLast) {
    // Move the 4-byte pointer + FeatureExt block (currently right after ServerName) to the end.
    const E = d.readUInt16LE(56), L = d.readUInt16LE(60) - (E + 4), M = 4 + L;
    d = Buffer.concat([d.subarray(0, E), d.subarray(E + M), d.subarray(E, E + M)]);
    for (const ib of [60, 64, 68, 78, 82, 86]) { const v = d.readUInt16LE(ib); if (v >= E + M) d.writeUInt16LE(v - M, ib); }
    d.writeUInt16LE(d.length - M, 56);        // ibExtension -> pointer now sits just before the block
    d.writeUInt32LE(d.length - L, d.length - M); // pointer -> block at the very end
  }
  return d;
};

function attempt(label, authentication, encrypt, t = {}) {
  tweak = t;
  return new Promise((resolve) => {
    const log = [];
    const t0 = Date.now();
    let settled = false;
    const conn = new Connection({
      server, authentication,
      options: { database, port: 1433, encrypt, trustServerCertificate: false, connectTimeout: 30000, requestTimeout: 30000, debug: { packet: true, token: true }, ...(t.options || {}) },
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

  const AAD = { type: 'azure-active-directory-default', options: {} };
  const GO_FLAGS = { flags1: 0xA0, flags2: 0x02, flags3: 0x10 };            // go-mssqldb: fUseDB|fSetLang · fODBC · fExtension
  const variants = [
    ['app config (tedious as-is): encrypt true, azure-active-directory-default', AAD, true, {}],
    ['TDS 8.0 strict encryption, tedious as-is', AAD, 'strict', {}],
    ['fODBC flag only (OptionFlags2 0x02, as SqlClient and go-mssqldb send)', AAD, true, { flags2: 0x02 }],
    ['no UTF8_SUPPORT feature entry (go-mssqldb sends only FEDAUTH)', AAD, true, { noUtf8: true }],
    ['flags: exact go-mssqldb bytes (A0 / 02 / 10)', AAD, true, GO_FLAGS],
    ['FeatureExt (pointer + block) moved to the end of LOGIN7', AAD, true, { extLast: true }],
    ['FEDAUTH workflow 0x03 (SqlClient/go-mssqldb value for Default)', AAD, true, { workflow: 0x03 }],
    ['go-mssqldb flags + FeatureExt last + workflow 0x03', AAD, true, { ...GO_FLAGS, extLast: true, workflow: 0x03 }],
    ['full go-mssqldb mimic (+ CtlIntName go-mssqldb, appName sqlcmd, empty language, progver)', AAD, true,
      { ...GO_FLAGS, extLast: true, workflow: 0x03, noUtf8: true, tz: 0, libraryName: 'go-mssqldb', clientProgVer: 0x01000a00, options: { appName: 'sqlcmd', language: '' } }],
  ];
  if (token) variants.push(['token fetched before connecting (SECURITYTOKEN) + go-mssqldb flags + FeatureExt last', { type: 'azure-active-directory-access-token', options: { token } }, true, { ...GO_FLAGS, extLast: true }]);
  for (const [label, auth, encrypt, t] of variants) {
    const r = await attempt(label, auth, encrypt, t);
    console.log(`\n${r.ok ? 'OK  ' : 'FAIL'} ${label}\n     ${r.msg} (${r.ms} ms)`);
    if (!r.ok) console.log(r.log.slice(-14).map((l) => '     | ' + l).join('\n'));
  }
  process.exit(0);
})();
