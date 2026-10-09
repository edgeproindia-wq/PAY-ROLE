/* Namma Payroll - password fields: show/hide eye button, Caps Lock warning, "please wait" on sign-in.
   Works on every page that has an <input type="password">; nothing else to configure. */
(function () {
  if (window.__nmPasswordToggle) return;
  window.__nmPasswordToggle = true;

  var style = document.createElement('style');
  style.textContent =
    '.pw-wrap{position:relative;display:block;width:100%}' +
    '.pw-wrap>input{padding-right:46px !important}' +
    '.pw-eye{position:absolute;top:50%;right:6px;transform:translateY(-50%);width:36px;height:36px;padding:0;border:0;border-radius:6px;' +
    'background:transparent;color:#6b7280;cursor:pointer;display:flex;align-items:center;justify-content:center}' +
    '.pw-eye:hover{color:#1f9348;background:rgba(31,147,72,.08)}' +
    '.pw-eye:focus-visible{outline:2px solid rgba(31,147,72,.5);outline-offset:1px}' +
    '.pw-eye svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}' +
    '.pw-caps{margin-top:6px;font-size:12.5px;font-weight:600;color:#b45309}' +
    'button[data-pw-busy]{opacity:.7;cursor:progress}';
  document.head.appendChild(style);

  var EYE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_OFF = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

  function enhance(input) {
    if (input.dataset.pw === '1' || input.type !== 'password' || input.hidden) return;
    input.dataset.pw = '1';
    var wrap = document.createElement('span');
    wrap.className = 'pw-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-eye';
    btn.setAttribute('aria-label', 'Show password');
    btn.setAttribute('aria-pressed', 'false');
    btn.title = 'Show password';
    btn.innerHTML = EYE;
    wrap.appendChild(btn);

    var hint = document.createElement('div');
    hint.className = 'pw-caps';
    hint.textContent = 'Caps Lock is on';
    hint.hidden = true;
    wrap.parentNode.insertBefore(hint, wrap.nextSibling);

    btn.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.innerHTML = show ? EYE_OFF : EYE;
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      btn.title = show ? 'Hide password' : 'Show password';
      input.focus();
    });
    function caps(e) { if (e.getModifierState) hint.hidden = !e.getModifierState('CapsLock'); }
    input.addEventListener('keydown', caps);
    input.addEventListener('keyup', caps);
    input.addEventListener('blur', function () { hint.hidden = true; });
  }

  function run() { document.querySelectorAll('input[type=password]').forEach(enhance); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();

  // when a form with a password is sent: hide the password again and show "please wait" on the button
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.querySelector || !form.querySelector('input[data-pw="1"]')) return;
    form.querySelectorAll('input[data-pw="1"]').forEach(function (i) { if (i.type === 'text') i.type = 'password'; });
    var btn = form.querySelector('button[type=submit], button:not([type]), input[type=submit]');
    if (!btn || btn.hasAttribute('data-pw-busy')) return;
    setTimeout(function () {
      var original = btn.tagName === 'INPUT' ? btn.value : btn.textContent;
      btn.setAttribute('data-pw-busy', '1');
      btn.disabled = true;
      if (btn.tagName === 'INPUT') btn.value = 'Please wait...'; else btn.textContent = 'Please wait...';
      setTimeout(function () {                       // never leave the button stuck (for example if the page stays open)
        btn.disabled = false; btn.removeAttribute('data-pw-busy');
        if (btn.tagName === 'INPUT') btn.value = original; else btn.textContent = original;
      }, 8000);
    }, 0);
  }, true);
})();