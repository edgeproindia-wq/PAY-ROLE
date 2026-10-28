"""Overview page of each numbered section of the payroll structure (see payroll_structure.py).

A section that has no real page yet opens this page from the sidebar; it lists every feature of the section
as Available (with an Open button) or Planned. Company owners only: the platform admin has no payroll menu.
"""
from django.http import Http404
from django.shortcuts import render
from django.urls import path

from .payroll_structure import SECTIONS, get_section, overview_name, rows
from .permissions import company_owner_required


@company_owner_required
def module_page(request, key):
    section = get_section(key)
    if section is None or not section['children']:
        raise Http404
    items = rows(section)
    main = next((r['url'] for r in items if r['url']), '')
    return render(request, 'structure/module_page.html', {
        'section': section, 'rows': items, 'total': len(items),
        'built': sum(1 for r in items if r['url']), 'main_url': main})


# One address per section. The key is passed as a default so the address needs no argument and np_nav can list it.
urlpatterns = [path('module/%s/' % s['key'], module_page, {'key': s['key']}, name=overview_name(s))
               for s in SECTIONS if s['children']]
