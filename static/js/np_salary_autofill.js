/* Namma Payroll - Add Salary Structure: fill HRA, Conveyance and Special Allowance from Basic.
 * Works only on a form that has employee, basic, hra, conveyance and special_allowance and NO effective_from field (so the Salary history form is not touched).
 * The numbers are suggestions: the boxes stay editable, a box you type in yourself is never overwritten, and nothing is saved until you press Save.
 * Rules used by default: HRA 50%, Conveyance 10% and Special allowance 40% of Basic. Changed percentages are kept in this browser only (localStorage). No data is sent anywhere. */
(function () {
  'use strict';
  var KEY = 'np_salary_autofill_rules_v2';
  var DEFAULT_RULES = { hra: 50, conv: 10, special: 40 };

  function num(v) {
    var n = parseFloat(String(v === undefined || v === null ? '' : v).replace(/,/g, ''));
    return isFinite(n) ? n : 0;
  }
  function pct(v) {
    var n = num(v);
    return n < 0 ? 0 : (n > 100 ? 100 : n);
  }
  function compute(basic, rules) {
    var b = num(basic);
    if (!(b > 0)) { return null; }
    return {
      hra: Math.round(b * pct(rules.hra) / 100),
      conv: Math.round(b * pct(rules.conv) / 100),
      special: Math.round(b * pct(rules.special) / 100)
    };
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { compute: compute, num: num, pct: pct, DEFAULT_RULES: DEFAULT_RULES };
  }
  if (typeof document === 'undefined') { return; }

  var suppress = false;

  function loadRules() {
    var r = { hra: DEFAULT_RULES.hra, conv: DEFAULT_RULES.conv, special: DEFAULT_RULES.special };
    try {
      var saved = JSON.parse(window.localStorage.getItem(KEY) || 'null');
      if (saved && typeof saved === 'object') {
        ['hra', 'conv', 'special'].forEach(function (k) { if (saved[k] !== undefined) { r[k] = pct(saved[k]); } });
      }
    } catch (e) { /* storage may be blocked: the defaults are used */ }
    return r;
  }
  function saveRules(r) {
    try { window.localStorage.setItem(KEY, JSON.stringify(r)); } catch (e) { /* ignore */ }
  }
  function fmt(n) { return Number(n).toFixed(2); }
  function money(n) { return Math.round(n).toLocaleString('en-IN'); }

  function structureForm(el) {
    var f = el && el.form;
    if (!f) { return null; }
    var q = function (n) { return f.querySelector('[name="' + n + '"]'); };
    if (q('basic') && q('hra') && q('conveyance') && q('special_allowance') && q('employee') && !q('effective_from')) { return f; }
    return null;
  }
  function fields(f) {
    return {
      basic: f.querySelector('[name="basic"]'), hra: f.querySelector('[name="hra"]'),
      conv: f.querySelector('[name="conveyance"]'), special: f.querySelector('[name="special_allowance"]')
    };
  }
  function setValue(input, value) {
    suppress = true;
    try {
      input.value = value;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    } finally { suppress = false; }
  }
  function rulesOf(box) {
    return { hra: pct(box.querySelector('[data-np="hra"]').value), conv: pct(box.querySelector('[data-np="conv"]').value), special: pct(box.querySelector('[data-np="special"]').value) };
  }
  function updateGross(f) {
    var box = f.querySelector('.np-sal-rules');
    if (!box) { return; }
    var x = fields(f);
    var total = num(x.basic.value) + num(x.hra.value) + num(x.conv.value) + num(x.special.value);
    box.querySelector('.np-sal-gross').textContent = 'Gross per month: \u20B9' + money(total);
  }
  function recalc(f, force) {
    var box = f.querySelector('.np-sal-rules');
    if (!box) { return; }
    var x = fields(f);
    var c = compute(x.basic.value, rulesOf(box));
    [['hra', x.hra], ['conv', x.conv], ['special', x.special]].forEach(function (pair) {
      var input = pair[1];
      if (force) { delete input.dataset.npTouched; }
      if (input.dataset.npTouched === '1') { return; }                  // typed by hand: never overwritten
      setValue(input, c ? fmt(c[pair[0]]) : '0.00');
    });
    updateGross(f);
  }
  function ensureUi(f) {
    if (f.querySelector('.np-sal-rules')) { return; }
    var rules = loadRules();
    var box = document.createElement('div');
    box.className = 'np-sal-rules';
    box.setAttribute('style', 'margin:14px 0 0;padding:10px 12px;border:1px solid #d5e8dc;border-radius:10px;background:#f4faf6;font-size:12.5px;color:#244a35');
    var input = function (key, value, label) {
      return '<label style="display:inline-flex;align-items:center;gap:5px;margin-right:12px;white-space:nowrap">' + label +
        ' <input type="number" min="0" max="100" step="0.01" data-np="' + key + '" value="' + (value === 0 ? '' : value) + '" placeholder="0" aria-label="' + label + ' percent of Basic" ' +
        'style="width:62px;height:28px;padding:0 6px;border:1px solid #cfe3d6;border-radius:8px;font-size:12.5px"> %</label>';
    };
    box.innerHTML = '<div style="font-weight:700;margin-bottom:6px">Auto-fill from Basic</div>' +
      input('hra', rules.hra, 'HRA') + input('conv', rules.conv, 'Conveyance') + input('special', rules.special, 'Special allowance') +
      '<button type="button" data-np="recalc" style="height:28px;padding:0 10px;border:1px solid #16a34a;border-radius:8px;background:#fff;color:#15803d;font-size:12.5px;font-weight:600;cursor:pointer">Recalculate</button>' +
      '<div class="np-sal-gross" style="margin-top:8px;font-weight:700"></div>' +
      '<div style="margin-top:4px;color:#4b6b58">These are suggestions. You can change any box; a box you type in is kept as you typed it.</div>';
    var submit = f.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
    var holder = submit && (submit.closest('.modal-footer, .form-actions, .modal-actions') || submit.parentElement);
    if (holder && f.contains(holder) && holder !== f) { holder.parentNode.insertBefore(box, holder); } else { f.appendChild(box); }
    updateGross(f);
  }

  document.addEventListener('input', function (e) {
    var t = e.target;
    if (!t) { return; }
    if (t.getAttribute && t.getAttribute('data-np') && t.closest && t.closest('.np-sal-rules')) {          // a percentage box changed
      var form = t.closest('form');
      saveRules(rulesOf(t.closest('.np-sal-rules')));
      if (form) { recalc(form, false); }
      return;
    }
    if (!t.name) { return; }
    var f = structureForm(t);
    if (!f) { return; }
    ensureUi(f);
    if (t.name === 'basic') {
      if (!String(t.value).trim()) { ['hra', 'conveyance', 'special_allowance'].forEach(function (n) { delete f.querySelector('[name="' + n + '"]').dataset.npTouched; }); }
      recalc(f, false);
    } else if (t.name === 'hra' || t.name === 'conveyance' || t.name === 'special_allowance') {
      if (!suppress) { t.dataset.npTouched = '1'; }
      updateGross(f);
    }
  }, true);
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-np="recalc"]') : null;
    if (!b) { return; }
    var form = b.closest('form');
    if (form) { recalc(form, true); }
  });
  document.addEventListener('reset', function (e) {
    var f = e.target;
    if (f && f.querySelectorAll) { Array.prototype.forEach.call(f.querySelectorAll('[data-np-touched]'), function (i) { delete i.dataset.npTouched; }); }
  }, true);
  document.addEventListener('focusin', function (e) {
    var f = structureForm(e.target);
    if (f) { ensureUi(f); }
  });
  function scan() {
    Array.prototype.forEach.call(document.querySelectorAll('input[name="basic"]'), function (b) {
      var f = structureForm(b);
      if (f) { ensureUi(f); }
    });
  }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', scan); } else { scan(); }
})();
