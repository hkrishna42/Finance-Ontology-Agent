require('dotenv').config();

// Data access with two interchangeable backends behind one q(text, params) contract, because the
// pure-JS tedious/mssql driver cannot connect to a Fabric warehouse (Microsoft-confirmed:
// tediousjs/tedious#1563 — the login is dropped right after LOGIN7). Both backends are Entra-only.
//
//   sqlcmd  — shells out to the go-sqlcmd CLI (go-mssqldb, which DOES speak Fabric). Zero install
//             beyond the CLI you already have; values are validated+escaped into the SQL text.
//   odbc    — the `odbc` npm module + Microsoft ODBC Driver 18. Needs a driver install, but binds
//             parameters natively. Opt in with EC_DB_DRIVER=odbc.
//
// Auto-selection prefers sqlcmd when the CLI is present (zero install), else odbc. Force either with
// EC_DB_DRIVER. See RUNBOOK.md.

const { execFile } = require('child_process');
const { promisify } = require('util');
const { spawnSync } = require('child_process');
const execFileP = promisify(execFile);

const SEP = '\x1f'; // unit separator: never appears in sqlcmd diagnostics, Fabric info tokens, or our data
const SQLCMD = process.env.SQLCMD_BIN || 'sqlcmd';

// Active target: an in-memory override chosen at runtime (warehouse picker), else the .env values.
// Restart falls back to .env. Single target per process — fine for a single-operator console.
let override = null;
function target() {
  return override || { server: process.env.FABRIC_SQL_SERVER, database: process.env.FABRIC_SQL_DATABASE };
}
function getTarget() { return target(); }
async function setTarget(t) {
  override = t && t.server && t.database ? { server: t.server, database: t.database } : null;
  if (poolPromise) { try { const p = await poolPromise; if (p && p.close) await p.close(); } catch (_) {} poolPromise = null; }
}

function isConfigured() {
  const t = target();
  return Boolean(t.server && t.database);
}
function assertConfigured() {
  if (!isConfigured()) {
    const e = new Error('FABRIC_SQL_SERVER / FABRIC_SQL_DATABASE not set. Copy .env.example to .env and fill them in.');
    e.code = 'NOT_CONFIGURED';
    throw e;
  }
}
function usingSpn() {
  return Boolean(process.env.AZURE_CLIENT_ID && process.env.AZURE_CLIENT_SECRET && process.env.AZURE_TENANT_ID);
}

// ── backend selection (decided once) ──────────────────────────────
let backend = null;
function haveSqlcmd() {
  try { return !spawnSync(SQLCMD, ['-?'], { stdio: 'ignore' }).error; } catch (_) { return false; }
}
function haveOdbc() {
  try { require.resolve('odbc'); return true; } catch (_) { return false; }
}
function pickBackend() {
  if (backend) return backend;
  const forced = (process.env.EC_DB_DRIVER || '').toLowerCase();
  if (forced === 'odbc' || forced === 'sqlcmd') return (backend = forced);
  if (haveSqlcmd()) backend = 'sqlcmd';        // zero-install, proven against Fabric
  else if (haveOdbc()) backend = 'odbc';
  else {
    const e = new Error('No database backend available. Install the sqlcmd CLI (go-sqlcmd), or the Microsoft ODBC Driver 18 with `npm i odbc` and set EC_DB_DRIVER=odbc. See RUNBOOK.md.');
    e.code = 'NO_BACKEND';
    throw e;
  }
  return backend;
}
function authMode() {
  const auth = usingSpn() ? 'service-principal' : 'ActiveDirectoryDefault / az login';
  let b;
  try { b = pickBackend(); } catch (_) { b = (process.env.EC_DB_DRIVER || 'auto'); }
  return `${auth} (${b})`;
}

// ── @named -> value substitution, shared shape ────────────────────
// A single left-to-right scan so repeated/interleaved names keep order. An @name that is NOT a
// provided key is left untouched — critically @region inside setup's EXEC('CREATE FUNCTION …') DDL
// literal, which is run with no params. `subst` decides what a matched name becomes.
function rewrite(text, params, subst) {
  if (!params || !Object.keys(params).length) return text;
  return text.replace(/@([A-Za-z_][A-Za-z0-9_]*)/g, (m, name) =>
    Object.prototype.hasOwnProperty.call(params, name) ? subst(params[name]) : m
  );
}

// ── odbc backend ──────────────────────────────────────────────────
function buildConnectionString() {
  assertConfigured();
  const driver = process.env.ODBC_DRIVER || 'ODBC Driver 18 for SQL Server';
  const t = target();
  const base = `Driver={${driver}};Server=${t.server},1433;Database=${t.database};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;`;
  return usingSpn()
    ? base + `Authentication=ActiveDirectoryServicePrincipal;UID=${process.env.AZURE_CLIENT_ID};PWD=${process.env.AZURE_CLIENT_SECRET};`
    : base + 'Authentication=ActiveDirectoryDefault;';
}
let poolPromise = null;
function getPool() {
  if (!poolPromise) {
    const connectionString = buildConnectionString();
    let odbc;
    try { odbc = require('odbc'); }
    catch (_) { const e = new Error('EC_DB_DRIVER=odbc but the `odbc` module is not built. brew install unixodbc msodbcsql18 && npm ci. See RUNBOOK.md.'); e.code = 'ODBC_MISSING'; throw e; }
    poolPromise = odbc.pool({ connectionString, initialSize: 1, maxSize: 4 }).catch((err) => { poolPromise = null; throw err; });
  }
  return poolPromise;
}
function toPositional(text, params) {
  const args = [];
  const sql = rewrite(text, params, (v) => { args.push(v); return '?'; });
  return [sql, args];
}
async function qOdbc(text, params) {
  const pool = await getPool();
  const [sql, args] = toPositional(text, params);
  const result = await pool.query(sql, args);
  return { recordset: result, rowsAffected: [result.count > 0 ? result.count : 0] };
}

// ── sqlcmd backend ────────────────────────────────────────────────
// No bind parameters over the CLI, so render each value as a safe T-SQL literal. Every value that
// reaches here is already validated (assertEmail forbids quotes/`;`, regions/columns whitelisted),
// and `'`-doubling + control-char rejection is defense-in-depth. \n/\t/\r are allowed (audit detail).
function lit(v) {
  if (v === null || v === undefined) return 'NULL';
  if (typeof v === 'number') { if (!Number.isFinite(v)) throw new Error('Non-finite numeric parameter'); return String(v); }
  if (typeof v === 'boolean') return v ? '1' : '0';
  if (typeof v !== 'string') throw new Error('Unsupported parameter type: ' + typeof v);
  // Reject every control char (incl. CR/LF and the SEP byte) so an inlined value cannot start a
  // sqlcmd ':' meta-command / GO on its own line, and reject the '$(' scripting-variable trigger.
  // Then double single quotes: that is the only escape a T-SQL N'…' literal needs.
  if (/[\x00-\x1f]/.test(v)) throw new Error('Control character in parameter');
  if (v.includes('$(')) throw new Error("'$(' not allowed in parameter");
  return `N'${v.replace(/'/g, "''")}'`;
}
function inlineParams(text, params) { return rewrite(text, params, lit); }

function sqlcmdArgs() {
  assertConfigured();
  // Note: sqlcmd's -Q text is preprocessed by sqlcmd itself ($(var) substitution, ':' meta-commands,
  // GO). We neutralize that at the value level instead of relying on -x/-X flags (whose spelling
  // varies across sqlcmd builds): lit() and bracket() reject control chars (CR/LF) and '$(', so no
  // attacker-controlled value can inject a preprocessor trigger. Every identifier is also bracket()ed.
  return [
    '-S', target().server,
    '-d', target().database,
    '--authentication-method', 'ActiveDirectoryDefault', // SPN vars, if set, are picked up by this chain
    '-s', SEP, '-W', '-b', '-l', '60',
  ];
}
// Pull the human-readable cause out of sqlcmd's output so the UI hint regex (login|denied|AADSTS…) can fire.
function sqlcmdError(text) {
  const lines = text.split('\n').map((s) => s.trim()).filter(Boolean);
  const i = lines.findIndex((l) => /^Msg \d+,/.test(l));
  if (i >= 0) return lines.slice(i, i + 2).join(' ');
  return lines.find((l) => /login|token|authentication|AADSTS|denied|permission/i.test(l)) || lines.slice(-2).join(' ');
}
function rowsAffected(text) {
  const m = [...text.matchAll(/\((\d+) rows? affected\)/g)];
  return m.length ? parseInt(m[m.length - 1][1], 10) : 0;
}
// go-sqlcmd prints: header, a ruler of dashes, then rows, then a blank line / "(N rows affected)".
// The ruler anchors us; SEP splits columns; a continuation line (no SEP) is an embedded newline in
// the last column (audit detail). Noise lines (Statement ID…, row counts) never contain SEP or dashes-only.
function parseSqlcmd(out) {
  const lines = out.split('\n').map((l) => l.replace(/\r$/, ''));
  let r = -1;
  for (let i = 1; i < lines.length; i++) {
    if (/-/.test(lines[i]) && new RegExp(`^[-${SEP} ]+$`).test(lines[i])) { r = i; break; }
  }
  if (r < 1) return [];
  const header = lines[r - 1].split(SEP).map((s) => s.trim());
  const multi = header.length > 1;
  const rows = [];
  for (let i = r + 1; i < lines.length; i++) {
    const L = lines[i];
    if (L === '' || /^\(\d+ rows? affected\)/.test(L)) break;
    if (!multi) rows.push([L]);
    else if (L.includes(SEP)) rows.push(L.split(SEP));
    else if (rows.length) rows[rows.length - 1][rows[rows.length - 1].length - 1] += '\n' + L;
    else rows.push([L]);
  }
  return rows.map((cells) => {
    const o = {};
    header.forEach((h, i) => { const v = cells[i] === undefined ? null : cells[i]; o[h] = v === 'NULL' ? null : v; });
    return o;
  });
}
async function qSqlcmd(text, params) {
  const sql = inlineParams(text, params);
  try {
    const { stdout } = await execFileP(SQLCMD, [...sqlcmdArgs(), '-Q', sql], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    return { recordset: parseSqlcmd(stdout), rowsAffected: [rowsAffected(stdout)] };
  } catch (e) {
    if (e.code === 'ENOENT') { const x = new Error('sqlcmd not found on PATH. Install go-sqlcmd, set SQLCMD_BIN, or use EC_DB_DRIVER=odbc. See RUNBOOK.md.'); x.code = 'SQLCMD_MISSING'; throw x; }
    const text2 = (e.stderr || '') + '\n' + (e.stdout || '');
    throw new Error(sqlcmdError(text2) || `sqlcmd failed: ${e.message}`);
  }
}

// ── dispatcher ────────────────────────────────────────────────────
async function q(text, params = {}) {
  assertConfigured(); // preserve NOT_CONFIGURED before touching any backend (keeps the no-DB path intact)
  return pickBackend() === 'odbc' ? qOdbc(text, params) : qSqlcmd(text, params);
}

module.exports = { q, getPool, authMode, getTarget, setTarget, toPositional, inlineParams, lit, parseSqlcmd, rowsAffected };
