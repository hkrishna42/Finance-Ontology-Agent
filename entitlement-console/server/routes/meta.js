const express = require('express');
const { q } = require('../db');
const { assertTableExists } = require('../validate');

const router = express.Router();

router.get('/tables', async (_req, res, next) => {
  try {
    const r = await q(
      `SELECT s.name AS [schema], t.name AS [table]
       FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
       ORDER BY s.name, t.name`
    );
    res.json(
      r.recordset.map((x) => ({
        schema: x.schema,
        table: x.table,
        governance: x.schema === 'gov' || x.schema === 'sec',
      }))
    );
  } catch (err) { next(err); }
});

router.get('/columns', async (req, res, next) => {
  try {
    const schema = String(req.query.schema || '');
    const table = String(req.query.table || '');
    await assertTableExists(schema, table);
    const r = await q(
      `SELECT c.name, ty.name AS type, c.max_length,
              CASE WHEN mc.column_id IS NOT NULL THEN 1 ELSE 0 END AS masked
       FROM sys.columns c
       JOIN sys.tables t ON t.object_id = c.object_id
       JOIN sys.schemas s ON s.schema_id = t.schema_id
       JOIN sys.types ty ON ty.user_type_id = c.user_type_id
       LEFT JOIN sys.masked_columns mc ON mc.object_id = c.object_id AND mc.column_id = c.column_id
       WHERE s.name = @schema AND t.name = @table
       ORDER BY c.column_id`,
      { schema, table }
    );
    res.json(r.recordset.map((c) => ({ name: c.name, type: c.type, masked: Number(c.masked) === 1 })));
  } catch (err) { next(err); }
});

module.exports = router;
