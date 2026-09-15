const express = require('express');
const { q } = require('../db');
const { assertEmail } = require('../validate');

const router = express.Router();

router.get('/entitlements', async (_req, res, next) => {
  try {
    const r = await q(`SELECT user_email, region FROM gov.entitlement ORDER BY user_email, region`);
    res.json(r.recordset);
  } catch (err) { next(err); }
});

router.get('/regions', async (_req, res, next) => {
  try {
    const r = await q(`SELECT DISTINCT region FROM sales.orders ORDER BY region`);
    const regions = r.recordset.map((x) => x.region);
    if (!regions.includes('All')) regions.push('All');
    res.json(regions);
  } catch (err) { next(err); }
});

// Add one (user, region) row. Row rules apply instantly — the security policy reads this table.
router.post('/entitlements', async (req, res, next) => {
  try {
    const email = assertEmail(String(req.body.user_email || '').trim());
    const region = String(req.body.region || '').trim();
    const legal = (await q(`SELECT DISTINCT region FROM sales.orders`)).recordset.map((x) => x.region);
    legal.push('All');
    if (!legal.includes(region)) {
      return res.status(400).json({ error: `Region must be one of: ${legal.join(', ')}` });
    }
    await q(
      `IF NOT EXISTS (SELECT 1 FROM gov.entitlement WHERE user_email = @email AND region = @region)
       INSERT INTO gov.entitlement (user_email, region) VALUES (@email, @region)`,
      { email, region }
    );
    res.json({ ok: true, applied: 'instantly — the row filter reads this table on every query' });
  } catch (err) { next(err); }
});

router.delete('/entitlements', async (req, res, next) => {
  try {
    const email = assertEmail(String(req.body.user_email || '').trim());
    const region = String(req.body.region || '').trim();
    await q(`DELETE FROM gov.entitlement WHERE user_email = @email AND region = @region`, { email, region });
    res.json({ ok: true });
  } catch (err) { next(err); }
});

module.exports = router;
