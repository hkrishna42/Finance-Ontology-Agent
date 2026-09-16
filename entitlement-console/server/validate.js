const { q } = require('./db');

const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;

function assertEmail(email) {
  if (typeof email !== 'string' || email.length > 256 || !EMAIL_RE.test(email)) {
    const e = new Error(`Not a valid email address: ${email}`);
    e.status = 400;
    throw e;
  }
  return email;
}

// Bracket-quote an identifier for T-SQL. Rejects ']' so the quote cannot be escaped.
function bracket(name) {
  if (typeof name !== 'string' || name.length === 0 || name.length > 128 || name.includes(']')) {
    const e = new Error(`Invalid identifier: ${name}`);
    e.status = 400;
    throw e;
  }
  return `[${name}]`;
}

async function assertTableExists(schema, table) {
  // Syntax first (rejects ']', empty, >128) — no DB round-trip for garbage input.
  bracket(schema); bracket(table);
  const r = await q(
    `SELECT 1 AS ok FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
     WHERE s.name = @schema AND t.name = @table`,
    { schema, table }
  );
  if (r.recordset.length === 0) {
    const e = new Error(`Table not found: ${schema}.${table}`);
    e.status = 404;
    throw e;
  }
}

async function assertColumnsExist(schema, table, columns) {
  columns.forEach(bracket);
  const r = await q(
    `SELECT c.name FROM sys.columns c
     JOIN sys.tables t ON t.object_id = c.object_id
     JOIN sys.schemas s ON s.schema_id = t.schema_id
     WHERE s.name = @schema AND t.name = @table`,
    { schema, table }
  );
  const known = new Set(r.recordset.map((x) => x.name));
  for (const c of columns) {
    if (!known.has(c)) {
      const e = new Error(`Column not found on ${schema}.${table}: ${c}`);
      e.status = 400;
      throw e;
    }
  }
  return known;
}

module.exports = { assertEmail, bracket, assertTableExists, assertColumnsExist };
