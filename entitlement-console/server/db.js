require('dotenv').config();
const sql = require('mssql');

function authMode() {
  const spn = process.env.AZURE_CLIENT_ID && process.env.AZURE_CLIENT_SECRET && process.env.AZURE_TENANT_ID;
  return spn ? 'service-principal' : 'azure-cli / default';
}

function buildConfig() {
  const server = process.env.FABRIC_SQL_SERVER;
  const database = process.env.FABRIC_SQL_DATABASE;
  if (!server || !database) {
    const e = new Error('FABRIC_SQL_SERVER / FABRIC_SQL_DATABASE not set. Copy .env.example to .env and fill them in.');
    e.code = 'NOT_CONFIGURED';
    throw e;
  }
  const spn = process.env.AZURE_CLIENT_ID && process.env.AZURE_CLIENT_SECRET && process.env.AZURE_TENANT_ID;
  return {
    server,
    database,
    port: 1433,
    connectionTimeout: 30000,
    requestTimeout: 90000,
    options: { encrypt: true, trustServerCertificate: false },
    authentication: spn
      ? {
          type: 'azure-active-directory-service-principal-secret',
          options: {
            clientId: process.env.AZURE_CLIENT_ID,
            clientSecret: process.env.AZURE_CLIENT_SECRET,
            tenantId: process.env.AZURE_TENANT_ID,
          },
        }
      : { type: 'azure-active-directory-default', options: {} },
  };
}

let poolPromise = null;
function getPool() {
  if (!poolPromise) {
    poolPromise = new sql.ConnectionPool(buildConfig())
      .connect()
      .catch((err) => { poolPromise = null; throw err; });
  }
  return poolPromise;
}

async function q(text, params = {}) {
  const pool = await getPool();
  const req = pool.request();
  for (const [k, v] of Object.entries(params)) req.input(k, v);
  return req.query(text);
}

module.exports = { q, getPool, authMode, sql };
