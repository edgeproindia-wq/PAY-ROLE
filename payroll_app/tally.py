"""Tally export for the Accounting Integration journals (company owners only).

Two files, both made from journals that are already posted in Accounting Integration:
  * Vouchers XML: every journal becomes one Journal voucher (debit lines negative, credit lines positive, as Tally expects).
  * Ledgers XML:  creates the payroll ledgers in Tally with a sensible parent group, using the names set in General Ledger Mapping.

Import in Tally Prime: Import Data -> Masters (ledgers first), then Import Data -> Vouchers.
Nothing is sent to Tally from here; the owner imports the file. Ledger names in the vouchers must exist in Tally, which is why the
ledgers file comes first.
"""
import re
import xml.etree.ElementTree as ET
from decimal import Decimal

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import path
from django.utils import timezone

from .accounting import _company, _run_from_request, ledger_map
from .accounting_models import JournalEntry
from .audit import log_action
from .permissions import company_owner_required

MAX_VOUCHERS = 5000
# Parent group in Tally for each payroll head. Payables sit under Current Liabilities so that Tally does not ask for a duty type.
PARENT_GROUP = {
    'SALARY_EXPENSE': 'Indirect Expenses', 'EMPLOYER_PF_EXPENSE': 'Indirect Expenses', 'EMPLOYER_ESI_EXPENSE': 'Indirect Expenses',
    'SALARY_PAYABLE': 'Current Liabilities', 'PF_PAYABLE': 'Current Liabilities', 'ESI_PAYABLE': 'Current Liabilities',
    'TDS_PAYABLE': 'Current Liabilities', 'PT_PAYABLE': 'Current Liabilities', 'INSURANCE_PAYABLE': 'Current Liabilities',
    'OTHER_DEDUCTIONS': 'Current Liabilities', 'LOAN_RECOVERY': 'Loans & Advances (Asset)', 'BANK': 'Bank Accounts',
}
_BAD_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')


def clean(text, limit=None):
    """Text that is safe inside XML: control characters removed, optionally cut to a length."""
    text = _BAD_XML.sub('', str(text if text is not None else '')).strip()
    return text[:limit] if limit else text


def _two(value):
    return f'{Decimal(value):.2f}'


def _envelope(report_name):
    env = ET.Element('ENVELOPE')
    header = ET.SubElement(env, 'HEADER')
    ET.SubElement(header, 'TALLYREQUEST').text = 'Import Data'
    body = ET.SubElement(env, 'BODY')
    imp = ET.SubElement(body, 'IMPORTDATA')
    desc = ET.SubElement(imp, 'REQUESTDESC')
    ET.SubElement(desc, 'REPORTNAME').text = report_name
    return env, ET.SubElement(imp, 'REQUESTDATA')


def _serialise(env):
    ET.indent(env)
    return ET.tostring(env, encoding='utf-8', xml_declaration=True)


def entry_is_balanced(entry):
    lines = list(entry.lines.all())
    return bool(lines) and sum((l.debit for l in lines), Decimal('0')) == sum((l.credit for l in lines), Decimal('0'))


def vouchers_xml(entries):
    """(xml bytes, number of vouchers written, vouchers skipped because they do not balance)."""
    env, data = _envelope('Vouchers')
    written = skipped = 0
    for entry in entries:
        lines = [l for l in entry.lines.all() if l.debit or l.credit]
        if not entry_is_balanced(entry):
            skipped += 1
            continue
        msg = ET.SubElement(data, 'TALLYMESSAGE', {'xmlns:UDF': 'TallyUDF'})
        v = ET.SubElement(msg, 'VOUCHER', {'VCHTYPE': 'Journal', 'ACTION': 'Create', 'OBJVIEW': 'Accounting Voucher View'})
        ET.SubElement(v, 'DATE').text = entry.entry_date.strftime('%Y%m%d')
        ET.SubElement(v, 'VOUCHERTYPENAME').text = 'Journal'
        ET.SubElement(v, 'VOUCHERNUMBER').text = clean(entry.voucher_no, 30)
        ET.SubElement(v, 'NARRATION').text = clean(entry.narration, 250)
        for l in lines:
            e = ET.SubElement(v, 'ALLLEDGERENTRIES.LIST')
            ET.SubElement(e, 'LEDGERNAME').text = clean(l.ledger_name, 100)
            if l.debit:                                          # Tally: a debit is "deemed positive" and carries a negative amount
                ET.SubElement(e, 'ISDEEMEDPOSITIVE').text = 'Yes'
                ET.SubElement(e, 'AMOUNT').text = '-' + _two(l.debit)
            else:
                ET.SubElement(e, 'ISDEEMEDPOSITIVE').text = 'No'
                ET.SubElement(e, 'AMOUNT').text = _two(l.credit)
        written += 1
    return _serialise(env), written, skipped


def ledgers_xml(company):
    """Master XML that creates every payroll ledger (default or mapped name) with its parent group."""
    env, data = _envelope('All Masters')
    seen = set()
    for head, (name, _code, _custom) in ledger_map(company).items():
        name = clean(name, 100)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        msg = ET.SubElement(data, 'TALLYMESSAGE', {'xmlns:UDF': 'TallyUDF'})
        ledger = ET.SubElement(msg, 'LEDGER', {'NAME': name, 'ACTION': 'Create'})
        ET.SubElement(ET.SubElement(ledger, 'NAME.LIST'), 'NAME').text = name
        ET.SubElement(ledger, 'PARENT').text = PARENT_GROUP.get(head, 'Current Liabilities')
    return _serialise(env), len(seen)


def _xml_response(content, stem):
    resp = HttpResponse(content, content_type='application/xml')
    resp['Content-Disposition'] = f'attachment; filename="{stem}_{timezone.localdate():%Y%m%d}.xml"'
    return resp


@company_owner_required
def acc_tally_vouchers(request):
    """Posted journals as Tally Journal vouchers. Filters are the same as on the Accounting Export page."""
    company = _company(request)
    run = _run_from_request(request, company)
    status = request.GET.get('status') if request.GET.get('status') in dict(JournalEntry.STATUS_CHOICES) else JournalEntry.POSTED
    kind = request.GET.get('kind') if request.GET.get('kind') in dict(JournalEntry.KIND_CHOICES) else ''
    entries = JournalEntry.objects.filter(company=company, status=status).prefetch_related('lines').order_by('entry_date', 'id')
    if run is not None:
        entries = entries.filter(payroll_run=run)
    if kind:
        entries = entries.filter(kind=kind)
    entries = list(entries[:MAX_VOUCHERS])
    if not entries:
        messages.error(request, 'No journals match, so there is nothing to export. Post a salary journal first.')
        return redirect('acc_export')
    content, written, skipped = vouchers_xml(entries)
    if not written:
        messages.error(request, 'None of the journals balance, so no file was made.')
        return redirect('acc_export')
    log_action(request, 'EXPORT', company=company, details=f'Tally vouchers XML exported ({written} voucher(s)' + (f', {skipped} skipped' if skipped else '') + ')')
    return _xml_response(content, 'tally_vouchers')


@company_owner_required
def acc_tally_ledgers(request):
    """The payroll ledgers, to be imported into Tally before the vouchers."""
    company = _company(request)
    content, count = ledgers_xml(company)
    log_action(request, 'EXPORT', company=company, details=f'Tally ledgers XML exported ({count} ledger(s))')
    return _xml_response(content, 'tally_ledgers')


urlpatterns = [
    path('accounting/tally/vouchers.xml', acc_tally_vouchers, name='acc_tally_vouchers'),
    path('accounting/tally/ledgers.xml', acc_tally_ledgers, name='acc_tally_ledgers'),
]
