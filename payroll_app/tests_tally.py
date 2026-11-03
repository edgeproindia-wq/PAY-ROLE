"""Tally export: payroll journals as Tally voucher XML, and the payroll ledgers as master XML. Owners only, company-scoped, balanced."""
import datetime
import xml.etree.ElementTree as ET
from decimal import Decimal

from django.urls import reverse

from .accounting_models import JournalEntry, JournalLine, LedgerMapping
from .models import AuditLog, BankPayment
from .tally import PARENT_GROUP, clean, vouchers_xml
from .tests import make_company, make_user
from .tests_accounting import AccBase

D = Decimal


def parse(response):
    return ET.fromstring(response.content)


def vouchers(root):
    return root.findall('.//VOUCHER')


def entries_of(voucher):
    return [(e.findtext('LEDGERNAME'), e.findtext('ISDEEMEDPOSITIVE'), D(e.findtext('AMOUNT'))) for e in voucher.findall('ALLLEDGERENTRIES.LIST')]


class VoucherTests(AccBase):
    def get(self, **params):
        return self.client.get(reverse('acc_tally_vouchers'), params)

    def test_a_posted_journal_becomes_a_balanced_tally_journal_voucher(self):
        e = self.journal()
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertIn('application/xml', r['Content-Type'])
        self.assertRegex(r['Content-Disposition'], r'attachment; filename="tally_vouchers_\d{8}\.xml"')
        root = parse(r)
        self.assertEqual((root.tag, root.findtext('HEADER/TALLYREQUEST'), root.findtext('.//REPORTNAME')), ('ENVELOPE', 'Import Data', 'Vouchers'))
        v, = vouchers(root)
        self.assertEqual((v.get('VCHTYPE'), v.get('ACTION'), v.findtext('VOUCHERTYPENAME'), v.findtext('VOUCHERNUMBER')), ('Journal', 'Create', 'Journal', e.voucher_no))
        self.assertEqual(v.findtext('DATE'), e.entry_date.strftime('%Y%m%d'))
        self.assertEqual(v.findtext('NARRATION'), e.narration)
        rows = entries_of(v)
        self.assertEqual(len(rows), e.lines.count())
        self.assertEqual(sum(a for _n, _f, a in rows), D('0.00'))                       # Tally needs the amounts of a voucher to add up to zero
        for name, flag, amount in rows:
            self.assertEqual(flag == 'Yes', amount < 0, name)                           # debits are negative and "deemed positive"
        by_name = {n: (f, a) for n, f, a in rows}
        self.assertEqual(by_name['Salary Payable'], ('No', D('44250.00')))
        self.assertEqual(by_name['Salaries and Wages'][0], 'Yes')

    def test_amounts_have_two_decimals_and_zero_lines_are_left_out(self):
        self.journal()
        text = self.get().content.decode()
        self.assertIn('<AMOUNT>-30000.00</AMOUNT>', text)
        self.assertIn('<AMOUNT>44250.00</AMOUNT>', text)
        self.assertNotIn('<AMOUNT>0.00</AMOUNT>', text)
        self.assertNotIn('<AMOUNT>-0.00</AMOUNT>', text)

    def test_names_with_xml_characters_are_escaped_and_come_back_unchanged(self):
        self.client.post(reverse('acc_mapping'), {'name_SALARY_PAYABLE': 'Salary & Wages <Staff> "A"', 'code_SALARY_PAYABLE': ''})
        e = self.journal()
        JournalEntry.objects.filter(pk=e.pk).update(narration='Salary Sep 2026 & bonus <ok> \'x\'')
        r = self.get()
        names = {n for n, _f, _a in entries_of(vouchers(parse(r))[0])}
        self.assertIn('Salary & Wages <Staff> "A"', names)
        self.assertEqual(vouchers(parse(r))[0].findtext('NARRATION'), "Salary Sep 2026 & bonus <ok> 'x'")
        self.assertIn('&amp;', r.content.decode())

    def test_control_characters_never_break_the_file(self):
        self.assertEqual(clean('a\x00b\x0bc\x1fd'), 'abcd')
        self.assertEqual(clean(None), '')
        self.assertEqual(len(clean('x' * 500, 250)), 250)
        JournalLine.objects.filter(entry=self.journal()).update(narration='bad\x0bchar')
        LedgerMapping.objects.create(company=self.co, head='BANK', ledger_name='Bank\x0b Account')
        self.assertEqual(self.get().status_code, 200)                                  # still well-formed XML

    def test_reversed_journals_are_left_out_unless_asked_for(self):
        e = self.journal()
        self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'wrong'})
        r = self.get()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse('acc_tally_vouchers'), {'status': 'REVERSED'}).status_code, 200)
        self.assertEqual(len(vouchers(parse(self.get(status='REVERSED')))), 1)

    def test_nothing_to_export_gives_a_message_not_an_empty_file(self):
        r = self.client.get(reverse('acc_tally_vouchers'), follow=True)
        self.assertRedirects(r, reverse('acc_export'))
        self.assertContains(r, 'nothing to export')

    def test_filters_by_month_and_by_type(self):
        e = self.journal()
        BankPayment.objects.create(payroll_line=self.l1, company=self.co, amount=D('26500'), account_no='123456789012', ifsc_code='HDFC0001234', status='PAID', reference_number='U1')
        self.client.post(reverse('acc_payable'), {'action': 'post_payment', 'run_id': self.run.pk})
        self.assertEqual(len(vouchers(parse(self.get()))), 2)
        only_payment = vouchers(parse(self.get(kind='PAYMENT')))
        self.assertEqual(len(only_payment), 1)
        self.assertEqual(sorted(entries_of(only_payment[0])), sorted([('Salary Payable', 'Yes', D('-26500.00')), ('Bank Account', 'No', D('26500.00'))]))
        self.assertEqual(len(vouchers(parse(self.get(run=self.run.pk, kind='SALARY')))), 1)
        self.assertEqual(self.get(run=99999999).status_code, 200)                          # an unknown month falls back to all months
        self.assertEqual(vouchers(parse(self.get(kind='SALARY')))[0].findtext('VOUCHERNUMBER'), e.voucher_no)

    def test_a_journal_that_does_not_balance_is_never_exported(self):
        good = self.journal()
        bad = JournalEntry.objects.create(company=self.co, payroll_run=self.run, kind='PAYMENT', voucher_no='PJ-BAD-1', entry_date=good.entry_date, total_debit=10, total_credit=5)
        JournalLine.objects.create(entry=bad, head='SALARY_PAYABLE', ledger_name='Salary Payable', debit=D('10'), credit=D('0'))
        JournalLine.objects.create(entry=bad, head='BANK', ledger_name='Bank Account', debit=D('0'), credit=D('5'))
        root = parse(self.get())
        self.assertEqual([v.findtext('VOUCHERNUMBER') for v in vouchers(root)], [good.voucher_no])
        xml, written, skipped = vouchers_xml(JournalEntry.objects.filter(company=self.co).prefetch_related('lines'))
        self.assertEqual((written, skipped), (1, 1))

    def test_the_export_is_recorded_in_the_audit_log(self):
        self.journal()
        self.get()
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', company=self.co, details__startswith='Tally vouchers XML exported (1 voucher').exists())

    def test_another_company_gets_nothing_of_ours(self):
        self.journal()
        self.client.force_login(make_user('tally_other', 'COMPANY_OWNER', self.other))
        r = self.get()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse('acc_tally_vouchers'), {'run': self.run.pk}).status_code, 302)


class LedgerTests(AccBase):
    def get(self):
        return self.client.get(reverse('acc_tally_ledgers'))

    def test_every_payroll_ledger_is_created_with_its_parent_group(self):
        r = self.get()
        root = parse(r)
        self.assertEqual((root.findtext('.//REPORTNAME'), root.findtext('HEADER/TALLYREQUEST')), ('All Masters', 'Import Data'))
        ledgers = {l.get('NAME'): (l.findtext('PARENT'), l.get('ACTION'), l.findtext('NAME.LIST/NAME')) for l in root.findall('.//LEDGER')}
        self.assertEqual(len(ledgers), 12)
        self.assertEqual(ledgers['Salaries and Wages'], ('Indirect Expenses', 'Create', 'Salaries and Wages'))
        self.assertEqual(ledgers['Salary Payable'][0], 'Current Liabilities')
        self.assertEqual(ledgers['Bank Account'][0], 'Bank Accounts')
        self.assertEqual(ledgers['Employee Loans and Advances'][0], 'Loans & Advances (Asset)')
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertEqual(set(PARENT_GROUP), {'SALARY_EXPENSE', 'EMPLOYER_PF_EXPENSE', 'EMPLOYER_ESI_EXPENSE', 'SALARY_PAYABLE', 'PF_PAYABLE', 'ESI_PAYABLE', 'TDS_PAYABLE',
                                             'PT_PAYABLE', 'INSURANCE_PAYABLE', 'OTHER_DEDUCTIONS', 'LOAN_RECOVERY', 'BANK'})

    def test_mapped_names_are_used_escaped_and_never_repeated(self):
        LedgerMapping.objects.create(company=self.co, head='BANK', ledger_name='HDFC & Co Current')
        LedgerMapping.objects.create(company=self.co, head='SALARY_PAYABLE', ledger_name='Salaries and Wages')               # same name as another head
        root = parse(self.get())
        names = [l.get('NAME') for l in root.findall('.//LEDGER')]
        self.assertIn('HDFC & Co Current', names)
        self.assertEqual(len(names), len({n.lower() for n in names}))
        self.assertNotIn('Bank Account', names)

    def test_vouchers_use_exactly_the_ledger_names_the_ledger_file_creates(self):
        LedgerMapping.objects.create(company=self.co, head='SALARY_PAYABLE', ledger_name='Staff Payable')
        self.journal()
        created = {l.get('NAME') for l in parse(self.get()).findall('.//LEDGER')}
        used = {n for v in vouchers(parse(self.client.get(reverse('acc_tally_vouchers')))) for n, _f, _a in entries_of(v)}
        self.assertTrue(used <= created, used - created)

    def test_only_this_companys_mapping_is_used(self):
        LedgerMapping.objects.create(company=self.other, head='BANK', ledger_name='Other Bank')
        self.assertNotIn('Other Bank', self.get().content.decode())

    def test_the_export_is_recorded(self):
        self.get()
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', details__startswith='Tally ledgers XML exported (12').exists())


class AccessTests(AccBase):
    def test_only_company_owners_can_download(self):
        self.journal()
        names = ['acc_tally_vouchers', 'acc_tally_ledgers']
        self.client.logout()
        for n in names:
            self.assertEqual(self.client.get(reverse(n)).status_code, 302, n)
        self.client.force_login(make_user('tally_emp', 'EMPLOYEE', self.co))
        for n in names:
            self.assertIn(self.client.get(reverse(n)).status_code, (302, 403), n)
        self.client.force_login(make_user('tally_admin', 'ADMIN'))
        for n in names:
            self.assertEqual(self.client.get(reverse(n)).status_code, 403, n)

    def test_the_export_page_has_the_two_tally_buttons_and_the_steps(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse('acc_export'), {'run': self.run.pk})
        self.assertContains(r, reverse('acc_tally_ledgers'))
        self.assertContains(r, reverse('acc_tally_vouchers'))
        self.assertContains(r, 'Import Data')
