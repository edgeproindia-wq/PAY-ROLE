"""An employee dropdown that lists employees by ID only and shows the chosen employee's name next to it, by itself.

Used on pages where the box is narrow (for example Other allowances & deductions). The saved value is still the employee, exactly as before.
"""
import json

from django import forms
from django.utils.html import escape
from django.utils.safestring import mark_safe

SCRIPT = ('<script>(function(){var s=document.getElementById(%(id)s),o=document.getElementById(%(out)s);if(!s||!o)return;'
          'function show(){var p=s.options[s.selectedIndex],n=p&&p.getAttribute("data-name");o.textContent=n?("Name: "+n):"";}'
          's.addEventListener("change",show);show();})();</script>')


class EmployeeIdSelect(forms.Select):
    """Options show only the employee ID; each option carries the name in data-name; a line under the box shows the chosen name."""

    def __init__(self, names=None, attrs=None, choices=(), extra=None):
        super().__init__(attrs, choices)
        self.names = names or {}
        self.extra = extra or {}                  # {'basic': {employee pk: value}} becomes data-basic on each option

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        key = str(getattr(value, 'value', value))
        if key in self.names:
            option['attrs']['data-name'] = self.names[key]
        for attr, values in self.extra.items():
            option['attrs']['data-' + attr] = values.get(key, '')
        return option

    def render(self, name, value, attrs=None, renderer=None):
        html = super().render(name, value, attrs=attrs, renderer=renderer)
        field_id = (attrs or {}).get('id') or self.attrs.get('id') or ('id_' + name)
        out_id = field_id + '_name'
        shown = ('<div class="np-empname" id="%s" aria-live="polite" style="margin-top:6px;font-size:13px;font-weight:700;color:#14532d;min-height:18px"></div>' % escape(out_id))
        return mark_safe(html + shown + SCRIPT % {'id': json.dumps(field_id), 'out': json.dumps(out_id)})


def use_employee_id_select(form, field='employee', extra=None):
    """Switch one employee field of a form to the ID-only dropdown with the automatic name. Returns the form."""
    f = form.fields.get(field)
    if f is None:
        return form
    names = {str(e.pk): (getattr(e, 'full_name', '') or str(e)) for e in f.queryset}
    f.label_from_instance = lambda e: e.employee_code
    widget = EmployeeIdSelect(names=names, attrs=dict(f.widget.attrs), extra=extra)
    widget.is_required = f.widget.is_required
    f.widget = widget
    widget.choices = f.choices
    return form
