const express = require('express');
const { q } = require('../db');
const { assertEmail, bracket, assertTableExists, assertColumnsExist } = require('../validate');
const { logChange, AUDIT_SQL } = require('../audit');

const router = express.Router();

// Current SELECT grants on a table, per principal (table-wide vs column list).
router.get('/column-rules', async (req, res, next) => {
  try {
    const schema = String(req.query.schema || '');
    const table = String(req.query.table || '');
    await assertTableExists(schema, table);
    const r = await q(
      `SELECT pr.name AS principal, p.minor_id, c.name AS col
       FROM sys.database_permissions p
       JOIN sys.database_principals pr ON pr.principal_id = p.grantee_principal_id
       JOIN sys.objects o ON o.object_id = p.major_id
       JOIN sys.schemas s ON s.schema_id = o.schema_id
       LEFT JOIN sys.columns c ON c.object_id = p.major_id AND c.column_id = p.minor_id
       WHERE p.class = 1 AND p.permission_name = 'SELECT' AND p.state IN ('G','W')
         AND s.name = @schema AND o.name = @table
       ORDER BY pr.name`,
      { schema, table }
    );
    const byPrincipal = {};
    for (const row of r.recordset) {
      byPrincipal[row.principal] ||= { principal: row.principal, tableWide: false, columns: [] };
      if (Number(row.minor_id) === 0) byPrincipal[row.principal].tableWide = true;
      else if (row.col) byPrincipal[row.principal].columns.push(row.col);
    }
    res.json(Object.values(byPrincipal));
  } catch (err) { next(err); }
});

// Push a column rule: exact allow-list of columns for one user on one table.
// Empty list = revoke everything on that table for that user.
router.post('/column-rules', async (req, res, next) => {
  const log = [];
  const run = async (sqlText) => { log.push(sqlText); await q(sqlText); };
  try {
    const email = assertEmail(String(req.body.user_email || '').trim());
    const schema = String(req.body.schema || '');
    const table = String(req.body.table || '');
    const columns = Array.isArray(req.body.columns) ? [...new Set(req.body.columns.map(String))] : [];
    columns.forEach(bracket); // Syntax before the table lookup — fail fast on garbage columns.
    await assertTableExists(schema, table);
    const known = await assertColumnsExist(schema, table, columns);

    const u = bracket(email);
    const obj = `${bracket(schema)}.${bracket(table)}`;

    // 1) The principal must already exist. Fabric creates the SQL principal when the warehouse is
    //    shared with the user (Fabric portal → Share → their email); it does NOT support
    //    CREATE USER … FROM EXTERNAL PROVIDER (Msg 22424). So if it's missing, tell the operator to
    //    share first rather than emitting an unsupported statement.
    const exists = (
      await q(`SELECT COUNT(*) n FROM sys.database_principals WHERE name = @email`, { email })
    ).recordset[0].n > 0;
    if (!exists) {
      const e = new Error(`${email} is not a user in this warehouse yet. In the Fabric portal, Share the warehouse with them (their email, no extra checkboxes) — Fabric creates the SQL principal on share — then push again.`);
      e.status = 400;
      throw e;
    }

    // 2) Clear existing SELECT grants (object-level, then any column-level).
    await run(`REVOKE SELECT ON OBJECT::${obj} FROM ${u};`);
    const colGrants = (
      await q(
        `SELECT c.name AS col
         FROM sys.database_permissions p
         JOIN sys.database_principals pr ON pr.principal_id = p.grantee_principal_id
         JOIN sys.columns c ON c.object_id = p.major_id AND c.column_id = p.minor_id
         JOIN sys.objects o ON o.object_id = p.major_id
         JOIN sys.schemas s ON s.schema_id = o.schema_id
         WHERE p.class = 1 AND p.permission_name='SELECT' AND p.minor_id > 0
           AND s.name = @schema AND o.name = @table AND pr.name = @email`,
        { schema, table, email }
      )
    ).recordset.map((x) => x.col);
    if (colGrants.length) {
      await run(`REVOKE SELECT ON OBJECT::${obj} (${colGrants.map(bracket).join(', ')}) FROM ${u};`);
    }

    // 3) Grant the new allow-list.
    if (columns.length === known.size) {
      await run(`GRANT SELECT ON OBJECT::${obj} TO ${u};`);
    } else if (columns.length > 0) {
      await run(`GRANT SELECT ON OBJECT::${obj} (${columns.map(bracket).join(', ')}) TO ${u};`);
    }

    // 4) Evidence: one change_log row carrying the SQL above. Logged first so it shows in `executed` too.
    const summary = columns.length === known.size ? 'all columns' : columns.length ? columns.join(', ') : 'no columns (access revoked)';
    log.push(`${AUDIT_SQL}; -- @action = 'column-rule.push'`);
    await logChange('column-rule.push', `${email} on ${schema}.${table} → ${summary}\n${log.join('\n')}`);

    res.json({
      ok: true,
      executed: log,
      reminder: 'SQL grants control what they can see. The user still needs the warehouse shared with them (no extra checkboxes) to connect at all.',
    });
  } catch (err) {
    err.executed = log;
    next(err);
  }
});

module.exports = router;
