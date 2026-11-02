"""Accounting Integration: journals, payment entries, employer contributions, ledger mapping, export and reconciliation.

Every page is for company owners only and every query is scoped to the owner's company.
"""
import csv
import io
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .accounting import HEADS, build_lines, run_figures
from .accounting_models import AccountingSettings, JournalEntry, JournalLine, JournalPaymentLink, LedgerMapping
from .models import AuditLog, BankPayment, CompanySettings, Employee, PayrollRun, PayrollRunLine
from .tests import make_company, make_employee, make_user

D = Decimal


def acc_employee(company, code, email, dept):
    e = make_employee(company, code, email)
    e.department = dept
    e.save()
    return e


def make_line(run, emp, earnings, pf=0, esi=0, tds=0, pt=0, loan=0, insurance=0, other=0, net=None, deductions=None):
    total = D(pf) + D(esi) + D(tds) + D(pt) + D(loan) + D(insurance) + D(other) if deductions is None else D(deductions)
    return PayrollRunLine.objects.create(
        payroll_run=run, employee=emp, payslip_number=f'PS-{run.pk}-{emp.employee_code}', basic=earnings, gross_salary=earnings,
        total_earnings=earnings, pf=pf, esi=esi, tds=tds, professional_tax=pt, loan_deduction=loan, insurance_deduction=insurance,
        other_deductions=other, total_deductions=total, net_pay=D(earnings) - total if net is None else net)


class AccBase(TestCase):
    def setUp(self):
        self.co = make_company('Acc Co')
        self.other = make_company('Other Acc Co')
        self.owner = make_user('acc_owner', 'COMPANY_OWNER', self.co)
        CompanySettings.objects.create(company=self.co, pf_percentage=D('12'), esi_percentage=D('0.75'), pf_wage_ceiling=D('15000'))
        self.e1 = acc_employee(self.co, 'E1', 'e1@a.test', 'Engineering')
        self.e2 = acc_employee(self.co, 'E2', 'e2@a.test', 'Admin')
        self.run = PayrollRun.objects.create(company=self.co, month='September 2026', status='RELEASED')
        self.l1 = make_line(self.run, self.e1, 30000, pf=1800, tds=500, pt=200, loan=1000)
        self.l2 = make_line(self.run, self.e2, 20000, pf=1800, esi=150, pt=200, other=100)
        self.client.force_login(self.owner)

    def post(self, run=None):
        return self.client.post(reverse('acc_journals'), {'action': 'post', 'run_id': (run or self.run).pk})

    def journal(self):
        self.post()
        return JournalEntry.objects.get(kind='SALARY', status='POSTED')

    def amounts(self, entry):
        return {l.head + (':' + l.department if l.department else ''): (l.debit, l.credit) for l in entry.lines.all()}


class FigureTests(AccBase):
    def test_the_figures_and_lines_balance(self):
        fig = run_figures(self.co, self.run)
        self.assertEqual((fig['t']['earnings'], fig['t']['net'], fig['t']['pf'], fig['count']), (D('50000'), D('44250'), D('3600'), 2))
        lines = build_lines(self.co, self.run, fig)
        self.assertEqual(sum(l[2] for l in lines), sum(l[3] for l in lines))
        self.assertEqual(fig['problems'], [])

    def test_payroll_that_does_not_add_up_is_reported_and_not_postable(self):
        PayrollRunLine.objects.filter(pk=self.l1.pk).update(net_pay=D('1'))
        fig = run_figures(self.co, self.run)
        self.assertEqual(len(fig['problems']), 1)
        self.assertIn('E1', fig['problems'][0])
        self.post()
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_a_difference_between_total_deductions_and_the_heads_goes_to_other_deductions(self):
        PayrollRunLine.objects.filter(pk=self.l2.pk).update(total_deductions=D('2300'), net_pay=D('17700'))      # 50 more than the heads
        e = self.journal()
        self.assertEqual(self.amounts(e)['OTHER_DEDUCTIONS'], (D('0'), D('150')))
        self.assertEqual(e.total_debit, e.total_credit)

    def test_employer_share_is_worked_out_from_the_employee_amounts(self):
        AccountingSettings.objects.create(company=self.co, employer_pf_percent=D('12'), employer_esi_percent=D('3.25'))
        fig = run_figures(self.co, self.run)
        self.assertEqual((fig['employer_pf'], fig['employer_esi']), (D('3600.00'), D('650.00')))

    def test_a_missing_company_percentage_gives_no_employer_share_and_a_note(self):
        CompanySettings.objects.filter(company=self.co).update(pf_percentage=0)
        AccountingSettings.objects.create(company=self.co, employer_pf_percent=D('12'), employer_esi_percent=D('0'))
        fig = run_figures(self.co, self.run)
        self.assertEqual(fig['employer_pf'], D('0'))
        self.assertTrue(any('missing in Company Settings' in n for n in fig['notes']))


class PostingTests(AccBase):
    def test_a_balanced_journal_is_posted_with_a_voucher_and_department_lines(self):
        r = self.post()
        e = JournalEntry.objects.get()
        self.assertRedirects(r, reverse('acc_journal_detail', args=[e.pk]))
        self.assertRegex(e.voucher_no, r'^SJ-\d{4}-0001$')
        self.assertEqual((e.kind, e.status, e.total_debit, e.total_credit, e.created_by), ('SALARY', 'POSTED', D('50000'), D('50000'), self.owner))
        a = self.amounts(e)
        self.assertEqual(a['SALARY_EXPENSE:Engineering'], (D('30000'), D('0')))
        self.assertEqual(a['SALARY_EXPENSE:Admin'], (D('20000'), D('0')))
        self.assertEqual(a['SALARY_PAYABLE'], (D('0'), D('44250')))
        self.assertEqual((a['PF_PAYABLE'][1], a['ESI_PAYABLE'][1], a['TDS_PAYABLE'][1], a['PT_PAYABLE'][1], a['LOAN_RECOVERY'][1], a['OTHER_DEDUCTIONS'][1]),
                         (D('3600'), D('150'), D('500'), D('400'), D('1000'), D('100')))
        self.assertTrue(AuditLog.objects.filter(action='CREATE', model_name='JournalEntry', company=self.co).exists())

    def test_employer_contribution_entries_are_added_when_the_percentages_are_set(self):
        AccountingSettings.objects.create(company=self.co, employer_pf_percent=D('12'), employer_esi_percent=D('3.25'))
        e = self.journal()
        a = self.amounts(e)
        self.assertEqual((a['EMPLOYER_PF_EXPENSE'][0], a['EMPLOYER_ESI_EXPENSE'][0]), (D('3600'), D('650')))
        self.assertEqual((a['PF_PAYABLE'][1], a['ESI_PAYABLE'][1]), (D('7200'), D('800')))
        self.assertEqual((e.total_debit, e.total_credit), (D('54250'), D('54250')))

    def test_a_month_is_posted_only_once(self):
        self.post()
        self.post()
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_the_database_itself_refuses_a_second_live_salary_journal(self):
        from django.db import IntegrityError, transaction
        e = self.journal()
        with self.assertRaises(IntegrityError), transaction.atomic():
            JournalEntry.objects.create(company=self.co, payroll_run=self.run, kind='SALARY', voucher_no='SJ-X-1', entry_date=e.entry_date)

    def test_only_a_released_run_can_be_posted(self):
        run = PayrollRun.objects.create(company=self.co, month='October 2026', status='APPROVED')
        make_line(run, self.e1, 100)
        self.client.post(reverse('acc_journals'), {'action': 'post', 'run_id': run.pk})
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_a_run_with_no_payslips_is_not_posted(self):
        run = PayrollRun.objects.create(company=self.co, month='November 2026', status='RELEASED')
        self.client.post(reverse('acc_journals'), {'action': 'post', 'run_id': run.pk})
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_mapped_ledger_names_and_codes_are_used_and_kept_on_old_journals(self):
        e1 = self.journal()
        self.assertEqual(e1.lines.get(head='SALARY_PAYABLE').ledger_name, 'Salary Payable')
        self.client.post(reverse('acc_mapping'), {'name_SALARY_PAYABLE': 'Staff Payable', 'code_SALARY_PAYABLE': '2100'})
        self.client.post(reverse('acc_journal_detail', args=[e1.pk]), {'reason': 'repost'})
        e2 = self.journal()
        line = e2.lines.get(head='SALARY_PAYABLE')
        self.assertEqual((line.ledger_name, line.ledger_code), ('Staff Payable', '2100'))
        self.assertEqual(e1.lines.get(head='SALARY_PAYABLE').ledger_name, 'Salary Payable')

    def test_the_voucher_number_goes_up(self):
        e = self.journal()
        self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'again'})
        e2 = self.journal()
        self.assertTrue(e2.voucher_no.endswith('0002'))

    def test_an_employee_of_another_company_in_a_run_blocks_posting(self):
        stranger = acc_employee(self.other, 'S1', 's1@a.test', 'X')
        make_line(self.run, stranger, 100)
        self.post()
        self.assertEqual(JournalEntry.objects.count(), 0)


class ReverseTests(AccBase):
    def test_reverse_needs_a_reason_then_lets_the_month_be_posted_again(self):
        e = self.journal()
        self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': ''})
        e.refresh_from_db()
        self.assertEqual(e.status, 'POSTED')
        self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'wrong month'})
        e.refresh_from_db()
        self.assertEqual((e.status, e.reverse_reason, e.reversed_by), ('REVERSED', 'wrong month', self.owner))
        self.post()
        self.assertEqual(JournalEntry.objects.filter(status='POSTED').count(), 1)

    def test_a_reversed_entry_cannot_be_reversed_twice(self):
        e = self.journal()
        self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'x'})
        r = self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'y'}, follow=True)
        self.assertContains(r, 'already reversed')

    def test_the_detail_page_shows_lines_and_the_reverse_form(self):
        e = self.journal()
        r = self.client.get(reverse('acc_journal_detail', args=[e.pk]))
        self.assertContains(r, 'Reverse this entry')
        self.assertContains(r, 'Salaries and Wages')


class PaymentEntryTests(AccBase):
    def pay(self, line, amount=None, status='PAID', ref=None):
        return BankPayment.objects.create(payroll_line=line, company=self.co, amount=amount or line.net_pay, account_no='123456789012', ifsc_code='HDFC0001234',
                                          status=status, reference_number=ref or f'UTR{line.pk}' if status == 'PAID' else '')

    def post_payment(self):
        return self.client.post(reverse('acc_payable'), {'action': 'post_payment', 'run_id': self.run.pk}, follow=True)

    def test_payment_entry_needs_the_salary_journal_first(self):
        self.pay(self.l1)
        self.post_payment()
        self.assertEqual(JournalEntry.objects.filter(kind='PAYMENT').count(), 0)

    def test_paid_payments_are_posted_once_each(self):
        self.journal()
        self.pay(self.l1)
        self.post_payment()
        pe = JournalEntry.objects.get(kind='PAYMENT')
        self.assertEqual((pe.total_debit, pe.total_credit), (D('26500'), D('26500')))
        self.assertEqual(self.amounts(pe), {'SALARY_PAYABLE': (D('26500'), D('0')), 'BANK': (D('0'), D('26500'))})
        self.assertEqual(JournalPaymentLink.objects.count(), 1)
        self.post_payment()                                           # nothing new to post
        self.assertEqual(JournalEntry.objects.filter(kind='PAYMENT').count(), 1)
        self.pay(self.l2)
        self.post_payment()
        self.assertEqual(JournalEntry.objects.filter(kind='PAYMENT').count(), 2)
        self.assertEqual(JournalPaymentLink.objects.count(), 2)

    def test_payments_that_are_not_paid_or_belong_to_another_company_are_not_posted(self):
        self.journal()
        self.pay(self.l1, status='PENDING')
        other_emp = acc_employee(self.other, 'S1', 's1@a.test', 'X')
        orun = PayrollRun.objects.create(company=self.other, month='September 2026', status='RELEASED')
        ol = make_line(orun, other_emp, 500)
        BankPayment.objects.create(payroll_line=ol, company=self.other, amount=500, account_no='123456789012', ifsc_code='HDFC0001234', status='PAID', reference_number='OTHERUTR')
        self.post_payment()
        self.assertEqual(JournalEntry.objects.filter(kind='PAYMENT').count(), 0)

    def test_reversing_the_salary_journal_is_blocked_while_payment_entries_stand(self):
        e = self.journal()
        self.pay(self.l1)
        self.post_payment()
        r = self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'x'}, follow=True)
        self.assertContains(r, 'Reverse the payment entries')
        e.refresh_from_db()
        self.assertEqual(e.status, 'POSTED')

    def test_reversing_a_payment_entry_frees_its_payments(self):
        self.journal()
        self.pay(self.l1)
        self.post_payment()
        pe = JournalEntry.objects.get(kind='PAYMENT')
        self.client.post(reverse('acc_journal_detail', args=[pe.pk]), {'reason': 'wrong date'})
        self.assertEqual(JournalPaymentLink.objects.count(), 0)
        self.post_payment()
        self.assertEqual(JournalEntry.objects.filter(kind='PAYMENT', status='POSTED').count(), 1)


class ReconcileTests(AccBase):
    def row(self):
        return self.client.get(reverse('acc_reconcile')).context['rows'][0]

    def test_states_follow_the_work_done(self):
        self.assertEqual(self.row()['state'], 'Salary journal not posted')
        self.journal()
        self.assertEqual(self.row()['state'], 'Part paid, matches')
        BankPayment.objects.create(payroll_line=self.l1, company=self.co, amount=26500, account_no='123456789012', ifsc_code='HDFC0001234', status='PAID', reference_number='U1')
        self.assertEqual(self.row()['state'], 'Paid payments not posted')
        self.client.post(reverse('acc_payable'), {'action': 'post_payment', 'run_id': self.run.pk})
        self.assertEqual(self.row()['state'], 'Part paid, matches')
        BankPayment.objects.create(payroll_line=self.l2, company=self.co, amount=17750, account_no='123456789012', ifsc_code='HDFC0001234', status='PAID', reference_number='U2')
        self.client.post(reverse('acc_payable'), {'action': 'post_payment', 'run_id': self.run.pk})
        r = self.row()
        self.assertEqual((r['state'], r['outstanding'], r['paid_posted']), ('Fully paid and posted', D('0'), D('44250')))

    def test_payroll_changed_after_posting_is_flagged(self):
        self.journal()
        PayrollRunLine.objects.filter(pk=self.l1.pk).update(net_pay=D('26000'), total_deductions=D('4000'))
        self.assertEqual(self.row()['state'], 'Payroll changed after posting')
        self.assertContains(self.client.get(reverse('acc_reconcile')), 'need attention')


class MappingAndSettingsTests(AccBase):
    def test_the_mapping_page_lists_every_head_and_saves_changes(self):
        r = self.client.get(reverse('acc_mapping'))
        for head, info in HEADS.items():
            self.assertContains(r, info[0])
        self.client.post(reverse('acc_mapping'), {'name_BANK': 'HDFC Current', 'code_BANK': '1010'})
        m = LedgerMapping.objects.get()
        self.assertEqual((m.head, m.ledger_name, m.ledger_code), ('BANK', 'HDFC Current', '1010'))
        self.client.post(reverse('acc_mapping'), {'name_BANK': '', 'code_BANK': ''})
        self.assertEqual(LedgerMapping.objects.count(), 0)

    def test_a_code_without_a_name_and_too_long_values_are_refused(self):
        self.client.post(reverse('acc_mapping'), {'code_BANK': '1010'})
        self.client.post(reverse('acc_mapping'), {'name_BANK': 'x' * 101})
        self.assertEqual(LedgerMapping.objects.count(), 0)

    def test_mapping_is_per_company(self):
        LedgerMapping.objects.create(company=self.other, head='BANK', ledger_name='Other Bank')
        self.assertNotContains(self.client.get(reverse('acc_mapping')), 'Other Bank')

    def test_employer_percentages_are_validated_and_saved(self):
        self.client.post(reverse('acc_employer'), {'employer_pf_percent': '12', 'employer_esi_percent': '3.25'})
        s = AccountingSettings.objects.get(company=self.co)
        self.assertEqual((s.employer_pf_percent, s.employer_esi_percent), (D('12.00'), D('3.25')))
        for bad in ('150', '-1', 'abc', 'NaN'):
            self.client.post(reverse('acc_employer'), {'employer_pf_percent': bad, 'employer_esi_percent': '0'})
        s.refresh_from_db()
        self.assertEqual(s.employer_pf_percent, D('12.00'))
        self.assertTrue(AuditLog.objects.filter(action='UPDATE', details__contains='Employer contribution').exists())


class PageTests(AccBase):
    def test_every_page_opens_and_shows_its_figures(self):
        self.journal()
        for name in ('acc_journals', 'acc_payable', 'acc_employer', 'acc_statutory', 'acc_loans', 'acc_allocation', 'acc_mapping', 'acc_export', 'acc_reconcile'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertContains(self.client.get(reverse('acc_statutory')), '3600.00')
        self.assertContains(self.client.get(reverse('acc_loans'), {'run': self.run.pk}), 'E1')

    def test_cost_allocation_splits_by_department(self):
        r = self.client.get(reverse('acc_allocation'), {'run': self.run.pk})
        rows = {x['dept']: x for x in r.context['rows']}
        self.assertEqual((rows['Engineering']['earnings'], rows['Engineering']['share']), (D('30000'), D('60.00')))
        self.assertEqual(rows['Admin']['count'], 1)

    def test_unassigned_department_is_named(self):
        Employee.objects.filter(pk=self.e2.pk).update(department='')
        rows = {x['dept'] for x in self.client.get(reverse('acc_allocation'), {'run': self.run.pk}).context['rows']}
        self.assertIn('Unassigned', rows)

    def test_loan_recovery_lists_only_people_with_a_deduction(self):
        people = self.client.get(reverse('acc_loans'), {'run': self.run.pk}).context['people']
        self.assertEqual([p['emp'].employee_code for p in people], ['E1'])

    def test_journal_search_and_status_filter(self):
        e = self.journal()
        self.assertContains(self.client.get(reverse('acc_journals'), {'q': e.voucher_no}), e.voucher_no)
        self.assertNotContains(self.client.get(reverse('acc_journals'), {'status': 'REVERSED'}), f'>{e.voucher_no}</a></td><td>Salary journal')


class ExportTests(AccBase):
    def test_csv_and_excel_export_contain_posted_lines_only_and_balance(self):
        from openpyxl import load_workbook
        e = self.journal()
        body = self.client.get(reverse('acc_export'), {'export': 'csv'}).content.decode()
        rows = list(csv.reader(io.StringIO(body)))
        self.assertEqual(rows[0][:4], ['Voucher No', 'Date', 'Type', 'Month'])
        self.assertEqual(len(rows) - 1, e.lines.count())
        self.assertEqual(sum(D(r[7]) for r in rows[1:]), sum(D(r[8]) for r in rows[1:]))
        ws = load_workbook(io.BytesIO(self.client.get(reverse('acc_export'), {'export': 'xlsx'}).content)).active
        self.assertEqual(ws.max_row, e.lines.count() + 1)
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', details__startswith='Accounting export').exists())
        self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'x'})
        self.assertEqual(len(list(csv.reader(io.StringIO(self.client.get(reverse('acc_export'), {'export': 'csv'}).content.decode())))), 1)
        self.assertGreater(len(list(csv.reader(io.StringIO(self.client.get(reverse('acc_export'), {'export': 'csv', 'status': 'REVERSED'}).content.decode())))), 1)

    def test_text_that_looks_like_a_formula_is_neutralised(self):
        self.client.post(reverse('acc_mapping'), {'name_SALARY_PAYABLE': '=cmd|calc', 'code_SALARY_PAYABLE': ''})
        self.journal()
        body = self.client.get(reverse('acc_export'), {'export': 'csv'}).content.decode()
        self.assertIn("'=cmd|calc", body)
        self.assertNotIn(',=cmd|calc', body)

    def test_export_is_scoped_to_the_company(self):
        self.journal()
        other_owner = make_user('acc_owner2', 'COMPANY_OWNER', self.other)
        self.client.force_login(other_owner)
        self.assertEqual(len(list(csv.reader(io.StringIO(self.client.get(reverse('acc_export'), {'export': 'csv'}).content.decode())))), 1)


class IsolationTests(AccBase):
    def test_another_company_cannot_see_or_change_these_journals_or_runs(self):
        e = self.journal()
        other_owner = make_user('acc_owner3', 'COMPANY_OWNER', self.other)
        self.client.force_login(other_owner)
        self.assertEqual(self.client.get(reverse('acc_journal_detail', args=[e.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('acc_journal_detail', args=[e.pk]), {'reason': 'x'}).status_code, 404)
        self.client.post(reverse('acc_journals'), {'action': 'post', 'run_id': self.run.pk})
        self.client.post(reverse('acc_payable'), {'action': 'post_payment', 'run_id': self.run.pk})
        e.refresh_from_db()
        self.assertEqual((e.status, JournalEntry.objects.count()), ('POSTED', 1))
        self.assertNotContains(self.client.get(reverse('acc_journals')), 'September 2026')

    def test_only_company_owners_can_open_the_pages(self):
        e = self.journal()
        names = [(n, []) for n in ('acc_journals', 'acc_payable', 'acc_employer', 'acc_statutory', 'acc_loans', 'acc_allocation', 'acc_mapping', 'acc_export', 'acc_reconcile')]
        names.append(('acc_journal_detail', [e.pk]))
        self.client.logout()
        for name, args in names:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302, name)
        self.client.force_login(make_user('acc_emp', 'EMPLOYEE', self.co))
        for name, args in names:
            self.assertIn(self.client.get(reverse(name, args=args)).status_code, (302, 403), name)
        self.client.force_login(make_user('acc_admin', 'ADMIN'))
        for name, args in names:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)
        self.assertIn(self.client.post(reverse('acc_journals'), {'action': 'post', 'run_id': self.run.pk}).status_code, (302, 403))
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_posting_does_not_change_payroll_or_payments(self):
        before = list(PayrollRunLine.objects.order_by('pk').values_list('net_pay', 'total_deductions'))
        self.journal()
        self.assertEqual(list(PayrollRunLine.objects.order_by('pk').values_list('net_pay', 'total_deductions')), before)
        self.assertEqual(PayrollRun.objects.get(pk=self.run.pk).status, 'RELEASED')
        self.assertEqual(JournalLine.objects.filter(entry__company=self.other).count(), 0)
