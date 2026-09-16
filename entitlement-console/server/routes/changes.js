const express = require('express');
const { q } = require('../db');

const router = express.Router();

// Evidence feed: newest first. Nothing from the client touches an identifier — limit is a parameter.
router.get('/changes', async (req, res, next) => {
  try {
    const n = parseInt(req.query.limit, 10);
    const limit = Number.isNaN(n) ? 50 : Math.min(500, Math.max(1, n));
    const r = await q(`SELECT TOP (@limit) at, actor, action, detail FROM gov.change_log ORDER BY at DESC`, { limit });
    res.json(r.recordset);
  } catch (err) { next(err); }
});

module.exports = router;
