(function () {
  var KEY = 'sbGroups', saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
  var here = location.pathname;
  document.querySelectorAll('.sidebar .sb-group').forEach(function (g) {
    var id = g.getAttribute('data-group'), head = g.querySelector('.sb-group-head'), active = false;
    g.querySelectorAll('.sb-sub a').forEach(function (a) { if (a.pathname === here || a.classList.contains('active')) { active = true; a.classList.add('active'); } });
    if (active) g.classList.add('has-active');
    var open = active || saved[id] === 1;
    g.classList.toggle('open', open);
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    head.addEventListener('click', function () {
      var now = !g.classList.contains('open');
      g.classList.toggle('open', now);
      head.setAttribute('aria-expanded', now ? 'true' : 'false');
      saved[id] = now ? 1 : 0;
      try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
    });
  });
  document.querySelectorAll('.sidebar a.sb-top').forEach(function (a) { if (a.pathname === here) a.classList.add('active'); });
})();
(function () {
  var wrap = document.getElementById('pfWrap'), btn = document.getElementById('pfBtn');
  if (!wrap || !btn) return;
  function setOpen(on) { wrap.classList.toggle('open', on); btn.setAttribute('aria-expanded', on ? 'true' : 'false'); }
  btn.addEventListener('click', function (e) { e.stopPropagation(); setOpen(!wrap.classList.contains('open')); });
  document.addEventListener('click', function (e) { if (!e.target.closest('#pfWrap')) setOpen(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setOpen(false); });
})();
