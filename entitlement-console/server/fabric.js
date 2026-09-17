const { execFile } = require('child_process');
const { promisify } = require('util');
const execFileP = promisify(execFile);

// List the signed-in identity's Fabric warehouses (across all workspaces), for the runtime picker.
// Uses an az-CLI token for the Fabric REST API — same approach as scripts/diag-connect.js. This is the
// az-login path; a pure service-principal host would need a token via the SPN instead (not wired here).

async function fabricToken() {
  const { stdout } = await execFileP(
    'az',
    ['account', 'get-access-token', '--resource', 'https://api.fabric.microsoft.com', '-o', 'tsv', '--query', 'accessToken'],
    { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 }
  );
  const tok = stdout.trim();
  if (!tok) throw new Error('empty token');
  return tok;
}

async function fget(tok, path) {
  const r = await fetch('https://api.fabric.microsoft.com/v1' + path, { headers: { Authorization: `Bearer ${tok}` } });
  if (!r.ok) throw new Error(`Fabric API HTTP ${r.status} on ${path}`);
  return (await r.json()).value || [];
}

async function listWarehouses() {
  let tok;
  try {
    tok = await fabricToken();
  } catch (e) {
    const err = new Error(`Could not get a Fabric API token. Run \`az login\` as an account with access to your workspaces. (${e.message})`);
    err.code = 'FABRIC_TOKEN';
    throw err;
  }
  const workspaces = await fget(tok, '/workspaces');
  const out = [];
  for (const w of workspaces) {
    let whs = [];
    try { whs = await fget(tok, `/workspaces/${w.id}/warehouses`); } catch (_) { /* skip workspaces we can't enumerate */ }
    for (const x of whs) {
      const server = x.properties && x.properties.connectionString;
      if (server) out.push({ workspace: w.displayName, name: x.displayName, server, database: x.displayName });
    }
  }
  out.sort((a, b) => (a.workspace + '|' + a.name).localeCompare(b.workspace + '|' + b.name));
  return out;
}

module.exports = { listWarehouses };
