const express = require('express');
const { q } = require('../db');
const { assertEmail, assertOrderId } = require('../validate');
const { logChange, changed } = require('../audit');

const router = express.Router();

// All row grants: (user_email, order_id). order_id = -1 means "all rows".
router.get('/entitlements', async (_req, res, next) => {
  try {
    const r = await q(`SELECT user_email, order_id FROM gov.entitlement ORDER BY user_email, order_id`);
    res.json(r.recordset.map((e) => ({ user_email: e.user_email, order_id: Number(e.order_id) })));
  } catch (err) { next(err); }
});

// The rows an operator ticks from — sales.orders, keyed on order_id (the row filter's dimension).
router.get('/rows', async (_req, res, next) => {
  try {
    const r = await q(`SELECT order_id, region, customer_name, amount, order_date FROM sales.orders ORDER BY order_id`);
    res.json(r.recordset.map((x) => ({ ...x, order_id: Number(x.order_id) })));
  } catch (err) { next(err); }
});

// Grant one row (or all) to a user. Applies instantly — the security policy reads this table on every query.
router.post('/entitlements', async (req, res, next) => {
  try {
    const email = assertEmail(String(req.body.user_email || '').trim());
    const orderId = assertOrderId(req.body.order_id);
    if (orderId !== -1) {
      const n = (await q(`SELECT COUNT(*) n FROM sales.orders WHERE order_id = @orderId`, { orderId })).recordset[0].n;
      if (Number(n) === 0) return res.status(400).json({ error: `No such row: order_id ${orderId}` });
    }
    const r = await q(
      `IF NOT EXISTS (SELECT 1 FROM gov.entitlement WHERE user_email = @email AND order_id = @orderId)
       INSERT INTO gov.entitlement (user_email, order_id) VALUES (@email, @orderId)`,
      { email, orderId }
    );
    if (changed(r)) await logChange('row-rule.add', `${email} → row ${orderId === -1 ? 'ALL' : orderId}`); // duplicate add = no-op, not evidence
    res.json({ ok: true, applied: 'instantly — the row filter reads this table on every query' });
  } catch (err) { next(err); }
});

router.delete('/entitlements', async (req, res, next) => {
  try {
    const email = assertEmail(String(req.body.user_email || '').trim());
    const orderId = assertOrderId(req.body.order_id);
    const r = await q(`DELETE FROM gov.entitlement WHERE user_email = @email AND order_id = @orderId`, { email, orderId });
    if (changed(r)) await logChange('row-rule.remove', `${email} → row ${orderId === -1 ? 'ALL' : orderId}`);
    res.json({ ok: true });
  } catch (err) { next(err); }
});

module.exports = router;
