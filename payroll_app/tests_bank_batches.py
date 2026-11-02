"""Advanced bank payments: validation, batches, approval, bank files, reconciliation, duplicates, export and audit.

Every page is for company owners only and every query is scoped to the owner's company.
"""
import csv
import io
import time
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .bank_batch_models import BankBatch, BankBatchItem
from .bank_batches import analyse_statement, can_approve, check_bank_details
from .models import AuditLog, BankPayment, Employee, PayrollRun, PayrollRunLine
from .tests import make_company, make_employee, make_user


def bank_employee(company, code, email, acc='123456789012', ifsc='HDFC0001234', bank='HDFC Bank', name=None):
    e = make_employee(company, code, email)
    e.bank_name, e.bank_account_no, e.ifsc_code, e.account_holder_name = bank, acc, ifsc, name or f'Holder {code}'
    e.save()
    return e


def released_run(company, month, pays, status='RELEASED'):
    run = PayrollRun.objects.create(company=company, month=month, status=status)
    lines = [PayrollRunLine.objects.create(payroll_run=run, employee=e, payslip_number=f'PS-{run.pk}-{e.employee_code}',
                                           basic=net, gross_salary=net, net_pay=net) for e, net in pays]
    return run, lines


def csv_upload(rows, name='bank.csv'):
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(r)
    return SimpleUploadedFile(name, buf.getvalue().encode('utf-8'), content_type='text/csv')


class BankBase(TestCase):
    def setUp(self):
        self.co = make_company('Bank Co')
        self.other = make_company('Other Bank Co')
        self.owner = make_user('bank_owner', 'COMPANY_OWNER', self.co)
        self.e1 = bank_employee(self.co, 'E1', 'e1@b.test', acc='111111111111', name='Asha')
        self.e2 = bank_employee(self.co, 'E2', 'e2@b.test', acc='222222222222', ifsc='SBIN0004321', bank='SBI Bank')
        self.e3 = bank_employee(self.co, 'E3', 'e3@b.test', acc='', ifsc='', bank='')          # no bank details at all
        self.run, self.lines = released_run(self.co, 'September 2026', [(self.e1, '30000.00'), (self.e2, '25000.50'), (self.e3, '20000.00')])
        self.client.force_login(self.owner)

    def create(self, lines=None, mode='NEFT', run=None):
        ids = [l.pk for l in (lines if lines is not None else self.lines[:2])]
        return self.client.post(reverse('bank_batch_new'), {'run': (run or self.run).pk, 'mode': mode, 'line': ids})

    def batch(self, **kw):
        self.create(**kw)
        return BankBatch.objects.filter(company=self.co).order_by('-id').first()

    def act(self, batch, action, **extra):
        return self.client.post(reverse('bank_batch_action', args=[batch.pk]), dict(action=action, **extra))

    def sent_batch(self):
        """A batch whose bank file has been generated (the state reconciliation needs)."""
        b = self.batch()
        self.act(b, 'approve')
        self.client.get(reverse('bank_batch_file', args=[b.pk]))
        b.refresh_from_db()
        self.p1 = BankPayment.objects.get(payroll_line=self.lines[0])
        self.p2 = BankPayment.objects.get(payroll_line=self.lines[1])
        return b


class ValidationUnitTests(TestCase):
    def emp(self, **kw):
        base = dict(bank_account_no='123456789012', ifsc_code='HDFC0001234', account_holder_name='A B', bank_status='VERIFIED', employment_status='ACTIVE')
        base.update(kw)
        return Employee(**base)

    def test_a_good_employee_has_no_errors(self):
        self.assertEqual(check_bank_details(self.emp(), Decimal('100'), 'NEFT'), ([], []))

    def test_missing_and_malformed_account_numbers_are_errors(self):
        self.assertIn('Account number missing', check_bank_details(self.emp(bank_account_no=''), Decimal('1'))[0])
        self.assertTrue(check_bank_details(self.emp(bank_account_no='ABC12345678'), Decimal('1'))[0])
        self.assertTrue(check_bank_details(self.emp(bank_account_no='12345'), Decimal('1'))[0])

    def test_spaces_inside_an_account_number_are_not_an_error(self):
        self.assertEqual(check_bank_details(self.emp(bank_account_no='1234 5678 9012'), Decimal('1'))[0], [])

    def test_ifsc_is_checked_and_lower_case_is_accepted(self):
        self.assertEqual(check_bank_details(self.emp(ifsc_code='hdfc0001234'), Decimal('1'))[0], [])
        self.assertTrue(check_bank_details(self.emp(ifsc_code='HDFC1001234'), Decimal('1'))[0])
        self.assertIn('IFSC missing', check_bank_details(self.emp(ifsc_code=''), Decimal('1'))[0])

    def test_zero_or_negative_pay_inactive_account_and_transfer_limits(self):
        self.assertTrue(check_bank_details(self.emp(), Decimal('0'))[0])
        self.assertTrue(check_bank_details(self.emp(), Decimal('-5'))[0])
        self.assertTrue(check_bank_details(self.emp(bank_status='INACTIVE'), Decimal('10'))[0])
        self.assertTrue(check_bank_details(self.emp(), Decimal('100000'), 'RTGS')[0])
        self.assertEqual(check_bank_details(self.emp(), Decimal('200000'), 'RTGS')[0], [])
        self.assertTrue(check_bank_details(self.emp(), Decimal('500001'), 'IMPS')[0])
        self.assertEqual(check_bank_details(self.emp(), Decimal('500001'), 'NEFT')[0], [])

    def test_missing_holder_name_and_left_employee_are_warnings_only(self):
        errors, warnings = check_bank_details(self.emp(account_holder_name='', employment_status='RESIGNED'), Decimal('10'))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2)


class ValidationPageTests(BankBase):
    def test_the_page_lists_problems_and_can_show_only_problems(self):
        r = self.client.get(reverse('bank_validation'))
        self.assertContains(r, 'Account number missing')
        self.assertContains(r, 'Must fix (errors)')
        only = self.client.get(reverse('bank_validation'), {'issues': '1'})
        self.assertContains(only, 'E3')
        self.assertNotContains(only, 'Holder E1')

    def test_search_and_month_filter(self):
        self.assertContains(self.client.get(reverse('bank_validation'), {'q': 'SBIN'}), 'E2')
        self.assertNotContains(self.client.get(reverse('bank_validation'), {'q': 'SBIN'}), 'Holder E1')
        self.assertEqual(self.client.get(reverse('bank_validation'), {'run': self.run.pk}).status_code, 200)

    def test_a_shared_account_is_a_warning(self):
        bank_employee(self.co, 'E4', 'e4@b.test', acc='111111111111')
        released_run(self.co, 'October 2026', [(Employee.objects.get(employee_code='E4'), '100')])
        self.assertContains(self.client.get(reverse('bank_validation')), 'Same account as')

    def test_paid_rows_are_not_listed(self):
        BankPayment.objects.create(payroll_line=self.lines[0], company=self.co, amount='30000', account_no='111111111111', ifsc_code='HDFC0001234', status='PAID')
        self.assertNotContains(self.client.get(reverse('bank_validation')), 'Holder E1')


class BatchCreationTests(BankBase):
    def test_a_batch_is_created_with_payments_totals_and_a_reference(self):
        r = self.create()
        b = BankBatch.objects.get()
        self.assertRedirects(r, reverse('bank_batch_detail', args=[b.pk]))
        self.assertEqual((b.status, b.item_count, b.total_amount, b.mode, b.created_by), ('PENDING_APPROVAL', 2, Decimal('55000.50'), 'NEFT', self.owner))
        self.assertRegex(b.reference, r'^BB-\d{4}-0001$')
        p = BankPayment.objects.get(payroll_line=self.lines[1])
        self.assertEqual((p.account_no, p.ifsc_code, p.amount, p.status, p.company), ('222222222222', 'SBIN0004321', Decimal('25000.50'), 'PENDING', self.co))
        self.assertEqual(BankBatchItem.objects.filter(batch=b).count(), 2)
        self.assertTrue(AuditLog.objects.filter(action='CREATE', model_name='BankBatch', company=self.co).exists())

    def test_an_employee_with_bad_bank_details_is_left_out_with_the_reason(self):
        r = self.client.post(reverse('bank_batch_new'), {'run': self.run.pk, 'mode': 'NEFT', 'line': [l.pk for l in self.lines]}, follow=True)
        self.assertContains(r, 'E3')
        self.assertContains(r, 'Account number missing')
        self.assertEqual(BankBatch.objects.get().item_count, 2)

    def test_no_batch_when_nobody_can_be_paid(self):
        r = self.create(lines=[self.lines[2]])
        self.assertEqual(BankBatch.objects.count(), 0)
        self.assertEqual(BankPayment.objects.count(), 0)
        self.assertEqual(r.status_code, 302)

    def test_nothing_ticked_creates_nothing(self):
        self.client.post(reverse('bank_batch_new'), {'run': self.run.pk, 'mode': 'NEFT'})
        self.assertEqual(BankBatch.objects.count(), 0)

    def test_a_payment_in_an_open_batch_cannot_be_added_to_another(self):
        self.create()
        self.create()
        self.assertEqual(BankBatch.objects.count(), 1)
        self.assertEqual(BankBatchItem.objects.count(), 2)

    def test_a_paid_payment_can_never_be_added(self):
        BankPayment.objects.create(payroll_line=self.lines[0], company=self.co, amount='30000', account_no='111111111111', ifsc_code='HDFC0001234',
                                   status='PAID', reference_number='UTR1')
        self.create(lines=[self.lines[0]])
        self.assertEqual(BankBatch.objects.count(), 0)

    def test_a_payment_marked_initiated_outside_a_batch_is_not_added(self):
        BankPayment.objects.create(payroll_line=self.lines[0], company=self.co, amount='30000', account_no='111111111111', ifsc_code='HDFC0001234', status='INITIATED')
        self.create(lines=[self.lines[0]])
        self.assertEqual(BankBatch.objects.count(), 0)

    def test_a_failed_payment_is_retried_with_fresh_bank_details(self):
        p = BankPayment.objects.create(payroll_line=self.lines[0], company=self.co, amount='30000', account_no='999999999999', ifsc_code='HDFC0009999',
                                       status='FAILED', failure_reason='Wrong account', attempts=1)
        self.create(lines=[self.lines[0]])
        p.refresh_from_db()
        self.assertEqual((p.status, p.attempts, p.failure_reason, p.account_no, p.ifsc_code), ('PENDING', 2, '', '111111111111', 'HDFC0001234'))
        self.assertTrue(AuditLog.objects.filter(action='PAYMENT_STATUS_CHANGE', details__contains='retry in').exists())

    def test_cancelling_a_batch_frees_its_payments(self):
        b = self.batch()
        self.act(b, 'cancel')
        self.create()
        self.assertEqual(BankBatch.objects.count(), 2)
        self.assertEqual(BankBatch.objects.order_by('id').last().reference[-4:], '0002')
        self.assertEqual(BankPayment.objects.count(), 2)            # the same two payments, not duplicates

    def test_rtgs_batch_refuses_small_payments(self):
        self.create(mode='RTGS')
        self.assertEqual(BankBatch.objects.count(), 0)

    def test_an_unreleased_run_cannot_be_used(self):
        run, lines = released_run(self.co, 'October 2026', [(self.e1, '100')], status='APPROVED')
        self.create(lines=lines, run=run)
        self.assertEqual(BankBatch.objects.count(), 0)

    def test_the_create_page_lists_employees_with_a_select_all_box(self):
        r = self.client.get(reverse('bank_batch_new'), {'run': self.run.pk})
        self.assertContains(r, 'Select all shown')
        self.assertContains(r, 'name="line"')
        self.assertContains(r, 'Account number missing')

    def test_another_companys_lines_and_runs_are_ignored(self):
        oe = bank_employee(self.other, 'O1', 'o1@b.test')
        orun, olines = released_run(self.other, 'September 2026', [(oe, '500')])
        self.client.post(reverse('bank_batch_new'), {'run': self.run.pk, 'mode': 'NEFT', 'line': [olines[0].pk]})
        self.client.post(reverse('bank_batch_new'), {'run': orun.pk, 'mode': 'NEFT', 'line': [olines[0].pk]})
        self.assertEqual(BankBatch.objects.count(), 0)
        self.assertEqual(BankPayment.objects.count(), 0)
        self.assertNotContains(self.client.get(reverse('bank_batch_new'), {'run': orun.pk}), 'Holder O1')

    def test_a_batch_of_another_company_is_not_found_or_actionable(self):
        other_owner = make_user('other_bank_owner', 'COMPANY_OWNER', self.other)
        b = self.batch()
        self.client.force_login(other_owner)
        self.assertEqual(self.client.get(reverse('bank_batch_detail', args=[b.pk])).status_code, 404)
        self.assertEqual(self.act(b, 'approve').status_code, 404)
        self.assertEqual(self.client.get(reverse('bank_batch_file', args=[b.pk])).status_code, 404)
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING_APPROVAL')

    def test_only_company_owners_can_use_the_pages(self):
        names = [('bank_validation', []), ('bank_batches', []), ('bank_batch_new', []), ('bank_approvals', []), ('bank_export', []),
                 ('bank_duplicates', []), ('bank_reconcile', []), ('bank_audit', [])]
        b = self.batch()
        names += [('bank_batch_detail', [b.pk]), ('bank_batch_file', [b.pk])]
        self.client.logout()
        for name, args in names:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302, name)
        self.client.force_login(make_user('bank_emp', 'EMPLOYEE', self.co))
        for name, args in names:
            self.assertIn(self.client.get(reverse(name, args=args)).status_code, (403, 302), name)
        self.client.force_login(make_user('bank_admin', 'ADMIN'))
        for name, args in names:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)
        self.assertIn(self.client.post(reverse('bank_batch_action', args=[b.pk]), {'action': 'approve'}).status_code, (403, 302))
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING_APPROVAL')

    def test_list_and_detail_pages_render(self):
        b = self.batch()
        self.assertContains(self.client.get(reverse('bank_batches')), b.reference)
        self.assertContains(self.client.get(reverse('bank_batches'), {'status': 'PENDING_APPROVAL', 'q': 'Sept'}), b.reference)
        self.assertContains(self.client.get(reverse('bank_batch_detail', args=[b.pk])), '55000.50')


class ApprovalTests(BankBase):
    def test_with_two_owners_the_creator_cannot_approve_but_another_owner_can(self):
        second = make_user('bank_owner2', 'COMPANY_OWNER', self.co)
        b = self.batch()
        self.act(b, 'approve')
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING_APPROVAL')
        self.assertEqual(can_approve(self.owner, b), (False, False))
        self.client.force_login(second)
        self.act(b, 'approve')
        b.refresh_from_db()
        self.assertEqual((b.status, b.approved_by, b.self_approved), ('APPROVED', second, False))
        self.assertTrue(AuditLog.objects.filter(action='APPROVE', actor=second).exists())

    def test_a_single_owner_may_approve_and_it_is_flagged_as_self_approval(self):
        b = self.batch()
        self.act(b, 'approve')
        b.refresh_from_db()
        self.assertEqual((b.status, b.self_approved), ('APPROVED', True))
        self.assertTrue(AuditLog.objects.filter(action='APPROVE', details__contains='self-approved').exists())

    def test_an_inactive_second_owner_does_not_count(self):
        make_user('bank_owner_off', 'COMPANY_OWNER', self.co, is_active=False)
        self.assertEqual(can_approve(self.owner, self.batch()), (True, True))

    def test_reject_needs_a_reason_and_frees_the_payments(self):
        b = self.batch()
        self.act(b, 'reject')
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING_APPROVAL')
        self.act(b, 'reject', reason='Wrong month')
        b.refresh_from_db()
        self.assertEqual((b.status, b.rejection_reason), ('REJECTED', 'Wrong month'))
        self.create()
        self.assertEqual(BankBatch.objects.count(), 2)

    def test_approval_is_refused_if_a_payment_was_paid_meanwhile(self):
        b = self.batch()
        BankPayment.objects.filter(payroll_line=self.lines[0]).update(status='PAID', reference_number='UTRX')
        self.act(b, 'approve')
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING_APPROVAL')

    def test_a_batch_cannot_be_approved_twice_or_moved_out_of_order(self):
        b = self.batch()
        self.act(b, 'approve')
        v = BankBatch.objects.get(pk=b.pk).updated_at
        self.act(b, 'approve')
        self.act(b, 'mark_sent')                       # not possible before the file is generated
        b.refresh_from_db()
        self.assertEqual(b.status, 'APPROVED')
        self.assertEqual(b.updated_at, v)

    def test_unknown_action_is_refused(self):
        b = self.batch()
        self.act(b, 'delete_everything')
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING_APPROVAL')

    def test_the_approvals_page_and_its_buttons_work(self):
        b = self.batch()
        self.assertContains(self.client.get(reverse('bank_approvals')), b.reference)
        r = self.act(b, 'approve', next='bank_approvals')
        self.assertRedirects(r, reverse('bank_approvals'))
        self.assertContains(self.client.get(reverse('bank_approvals')), 'Nothing waiting for approval')


class BankFileTests(BankBase):
    def test_no_file_before_approval(self):
        b = self.batch()
        r = self.client.get(reverse('bank_batch_file', args=[b.pk]))
        self.assertEqual(r.status_code, 302)
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING_APPROVAL')

    def test_the_csv_file_has_every_payment_and_moves_the_batch_forward(self):
        b = self.batch()
        self.act(b, 'approve')
        r = self.client.get(reverse('bank_batch_file', args=[b.pk]))
        self.assertEqual(r.status_code, 200)
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0][:6], ['Sr No', 'Payment ID', 'Employee Code', 'Beneficiary Name', 'Account Number', 'IFSC'])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2][7], '25000.50')
        b.refresh_from_db()
        self.assertEqual(b.status, 'FILE_GENERATED')
        self.assertIsNotNone(b.file_generated_at)
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', model_name='BankBatch').exists())

    def test_one_file_per_bank(self):
        b = self.batch()
        self.act(b, 'approve')
        rows = list(csv.reader(io.StringIO(self.client.get(reverse('bank_batch_file', args=[b.pk]), {'bank': 'SBIN'}).content.decode())))
        self.assertEqual([r[2] for r in rows[1:]], ['E2'])
        self.assertContains(self.client.get(reverse('bank_batch_detail', args=[b.pk])), 'SBI Bank (1)')

    def test_text_that_looks_like_a_formula_is_neutralised(self):
        self.e1.account_holder_name = '=HYPERLINK("http://x")'
        self.e1.save()
        b = self.batch()
        self.act(b, 'approve')
        body = self.client.get(reverse('bank_batch_file', args=[b.pk])).content.decode()
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn(',=HYPERLINK', body)

    def test_excel_file(self):
        from openpyxl import load_workbook
        b = self.batch()
        self.act(b, 'approve')
        r = self.client.get(reverse('bank_batch_file', args=[b.pk]), {'format': 'xlsx'})
        ws = load_workbook(io.BytesIO(r.content)).active
        self.assertEqual(ws.max_row, 3)
        self.assertEqual(ws.cell(2, 5).value, '111111111111')
        self.assertEqual(ws.cell(3, 8).value, 25000.5)

    def test_mark_sent_and_cancel_rules(self):
        b = self.batch()
        self.act(b, 'approve')
        self.client.get(reverse('bank_batch_file', args=[b.pk]))
        self.act(b, 'cancel')                           # file already generated: needs the confirm tick
        b.refresh_from_db()
        self.assertEqual(b.status, 'FILE_GENERATED')
        self.act(b, 'mark_sent')
        b.refresh_from_db()
        self.assertEqual((b.status, b.sent_by), ('SENT', self.owner))
        self.act(b, 'cancel', confirm='1')              # a sent batch cannot be cancelled at all
        b.refresh_from_db()
        self.assertEqual(b.status, 'SENT')

    def test_cancel_after_file_with_confirm(self):
        b = self.batch()
        self.act(b, 'approve')
        self.client.get(reverse('bank_batch_file', args=[b.pk]))
        self.act(b, 'cancel', confirm='1')
        b.refresh_from_db()
        self.assertEqual(b.status, 'CANCELLED')


class ReconcileTests(BankBase):
    def upload(self, rows, name='bank.csv'):
        return self.client.post(reverse('bank_reconcile'), {'step': 'upload', 'statement': csv_upload(rows, name)}, follow=True)

    def confirm(self):
        return self.client.post(reverse('bank_reconcile'), {'step': 'confirm'}, follow=True)

    def test_preview_changes_nothing_and_confirm_marks_paid_with_the_utr(self):
        b = self.sent_batch()
        r = self.upload([['Payment ID', 'UTR', 'Status', 'Amount'], [self.p1.pk, 'UTR0001', 'SUCCESS', '30000.00'], [self.p2.pk, 'UTR0002', 'Processed', '25,000.50']])
        self.assertContains(r, 'Will be marked Paid')
        self.assertContains(r, 'Confirm and save 2 changes')
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.status, 'PENDING')
        self.confirm()
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual((self.p1.status, self.p1.reference_number, self.p1.verified_by), ('PAID', 'UTR0001', self.owner))
        self.assertIsNotNone(self.p1.paid_at)
        self.assertEqual((self.p2.status, self.p2.reference_number), ('PAID', 'UTR0002'))
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETED')
        self.assertTrue(AuditLog.objects.filter(action='PAYMENT_STATUS_CHANGE', details__contains='bank reconciliation').exists())

    def test_a_failed_row_marks_the_payment_failed_with_the_reason(self):
        b = self.sent_batch()
        self.upload([['Payment ID', 'UTR', 'Status', 'Remarks'], [self.p1.pk, '', 'REJECTED', 'Invalid account'], [self.p2.pk, 'UTR0002', 'Success', '']])
        self.confirm()
        self.p1.refresh_from_db()
        self.assertEqual((self.p1.status, self.p1.failure_reason), ('FAILED', 'Invalid account'))
        b.refresh_from_db()
        self.assertEqual(b.status, 'FILE_GENERATED')            # not complete: one payment failed

    def test_an_amount_that_does_not_match_is_never_applied(self):
        self.sent_batch()
        r = self.upload([['Payment ID', 'UTR', 'Status', 'Amount'], [self.p1.pk, 'UTR0001', 'SUCCESS', '29000.00']])
        self.assertContains(r, 'is not the payment amount')
        self.assertNotContains(r, 'Confirm and save')
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.status, 'PENDING')

    def test_a_repeated_or_used_utr_is_not_applied(self):
        self.sent_batch()
        BankPayment.objects.filter(pk=self.p1.pk).update(reference_number='TAKEN')       # another payment already holds this UTR
        r = self.upload([['Payment ID', 'UTR'], [self.p2.pk, 'TAKEN']])
        self.assertContains(r, 'already used by another payment')
        self.assertNotContains(r, 'Confirm and save')
        rows, _err = analyse_statement(self.co, [['Payment ID', 'UTR'], [self.p1.pk, 'DUP'], [self.p2.pk, 'DUP']])
        self.assertEqual([r['outcome'] for r in rows], ['ok', 'review'])

    def test_matching_by_account_and_amount_without_a_payment_id(self):
        self.sent_batch()
        self.upload([['Beneficiary Account Number', 'Amount', 'UTR No'], ['111111111111', '30000', 'UTRA']])
        self.confirm()
        self.p1.refresh_from_db()
        self.assertEqual((self.p1.status, self.p1.reference_number), ('PAID', 'UTRA'))

    def test_a_payment_whose_file_was_not_generated_is_not_matched(self):
        b = self.batch()
        self.act(b, 'approve')                                   # approved, but no file yet
        p = BankPayment.objects.get(payroll_line=self.lines[0])
        r = self.upload([['Payment ID', 'UTR'], [p.pk, 'UTRZ']])
        self.assertContains(r, 'not in a generated or sent batch')
        p.refresh_from_db()
        self.assertEqual(p.status, 'PENDING')

    def test_another_companys_payment_id_is_not_matched(self):
        oe = bank_employee(self.other, 'O1', 'o1@b.test')
        _run, olines = released_run(self.other, 'September 2026', [(oe, '500')])
        op = BankPayment.objects.create(payroll_line=olines[0], company=self.other, amount='500', account_no='123456789012', ifsc_code='HDFC0001234')
        ob = BankBatch.objects.create(company=self.other, reference='BB-X-1', payroll_run=olines[0].payroll_run, status='SENT')
        BankBatchItem.objects.create(batch=ob, payment=op, amount='500')
        self.sent_batch()
        r = self.upload([['Payment ID', 'UTR'], [op.pk, 'UTRO']])
        self.assertContains(r, 'not in a generated or sent batch')
        self.assertEqual(BankPayment.objects.get(pk=op.pk).status, 'PENDING')

    def test_an_already_paid_payment_is_left_alone(self):
        self.sent_batch()
        BankPayment.objects.filter(pk=self.p1.pk).update(status='PAID', reference_number='OLD')
        r = self.upload([['Payment ID', 'UTR'], [self.p1.pk, 'NEW']])
        self.assertContains(r, 'Already marked paid')
        self.assertEqual(BankPayment.objects.get(pk=self.p1.pk).reference_number, 'OLD')

    def test_unreadable_files_are_refused_with_a_message(self):
        self.sent_batch()
        self.assertContains(self.upload([['Name', 'Value'], ['a', 'b']]), 'Could not find the columns')
        r = self.client.post(reverse('bank_reconcile'), {'step': 'upload', 'statement': SimpleUploadedFile('x.pdf', b'%PDF')}, follow=True)
        self.assertContains(r, 'Upload a .csv or .xlsx file')
        r = self.client.post(reverse('bank_reconcile'), {'step': 'upload', 'statement': SimpleUploadedFile('x.xlsx', b'not a zip')}, follow=True)
        self.assertContains(r, 'could not be read')
        r = self.client.post(reverse('bank_reconcile'), {'step': 'upload'}, follow=True)
        self.assertContains(r, 'Choose the bank file first')

    def test_a_preview_expires_and_cannot_be_confirmed_twice(self):
        self.sent_batch()
        self.upload([['Payment ID', 'UTR'], [self.p1.pk, 'UTR1']])
        s = self.client.session
        s['bank_recon']['at'] = time.time() - 3600
        s.save()
        self.assertContains(self.confirm(), 'preview has expired')
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.status, 'PENDING')
        self.upload([['Payment ID', 'UTR'], [self.p1.pk, 'UTR1']])
        self.confirm()
        self.assertContains(self.confirm(), 'preview has expired')

    def test_a_payment_that_changed_after_the_preview_is_skipped(self):
        self.sent_batch()
        self.upload([['Payment ID', 'UTR'], [self.p1.pk, 'UTR1']])
        BankPayment.objects.filter(pk=self.p1.pk).update(status='FAILED')
        r = self.confirm()
        self.assertContains(r, 'skipped because the payment changed')
        self.assertEqual(BankPayment.objects.get(pk=self.p1.pk).status, 'FAILED')

    def test_discard_clears_the_preview(self):
        self.sent_batch()
        self.upload([['Payment ID', 'UTR'], [self.p1.pk, 'UTR1']])
        self.client.post(reverse('bank_reconcile'), {'step': 'cancel'})
        self.assertNotContains(self.client.get(reverse('bank_reconcile')), 'Confirm and save')

    def test_excel_statement(self):
        from openpyxl import Workbook
        self.sent_batch()
        wb = Workbook()
        ws = wb.active
        ws.append(['Bank statement'])
        ws.append(['Payment ID', 'Transaction Reference', 'Transaction Status'])
        ws.append([self.p1.pk, 'XLUTR1', 'Success'])
        buf = io.BytesIO()
        wb.save(buf)
        r = self.client.post(reverse('bank_reconcile'), {'step': 'upload', 'statement': SimpleUploadedFile('s.xlsx', buf.getvalue())}, follow=True)
        self.assertContains(r, 'Confirm and save 1 change')
        self.confirm()
        self.assertEqual(BankPayment.objects.get(pk=self.p1.pk).reference_number, 'XLUTR1')

    def test_status_and_success_words(self):
        self.sent_batch()
        rows, _ = analyse_statement(self.co, [['Payment ID', 'UTR', 'Status'], [self.p1.pk, 'U1', 'Unsuccessful'], [self.p2.pk, 'U2', 'maybe']])
        self.assertEqual([(r['action'], r['outcome']) for r in rows], [('FAILED', 'ok'), ('REVIEW', 'review')])


class DuplicateExportAuditTests(BankBase):
    def test_the_duplicates_page_shows_shared_accounts_and_the_rules(self):
        self.assertContains(self.client.get(reverse('bank_duplicates')), 'No duplicates found')
        bank_employee(self.co, 'E9', 'e9@b.test', acc='111111111111', name='Twin')
        r = self.client.get(reverse('bank_duplicates'))
        self.assertContains(r, 'Same bank account used by more than one employee')
        self.assertContains(r, 'E9')
        self.assertContains(r, 'One payment per payslip')

    def test_two_payments_for_one_employee_and_month_are_reported(self):
        run2 = PayrollRun.objects.create(company=self.co, month='september 2026', status='RELEASED')
        l2 = PayrollRunLine.objects.create(payroll_run=run2, employee=self.e1, payslip_number='PS-X', basic=1, gross_salary=1, net_pay=1)
        for line in (self.lines[0], l2):
            BankPayment.objects.create(payroll_line=line, company=self.co, amount='1', account_no='111111111111', ifsc_code='HDFC0001234')
        self.assertContains(self.client.get(reverse('bank_duplicates')), 'same employee and month')

    def test_a_payment_in_two_open_batches_is_reported(self):
        b = self.batch()
        other = BankBatch.objects.create(company=self.co, reference='BB-T-9', payroll_run=self.run, status='SENT')
        BankBatchItem.objects.create(batch=other, payment=b.items.first().payment, amount='1')
        self.assertContains(self.client.get(reverse('bank_duplicates')), 'more than one open batch')

    def test_instruction_export_csv_excel_and_scope(self):
        from openpyxl import load_workbook
        oe = bank_employee(self.other, 'O1', 'o1@b.test')
        _r, ol = released_run(self.other, 'September 2026', [(oe, '500')])
        BankPayment.objects.create(payroll_line=ol[0], company=self.other, amount='500', account_no='777777777777', ifsc_code='HDFC0001234')
        self.batch()
        r = self.client.get(reverse('bank_export'), {'export': 'csv'})
        body = r.content.decode()
        self.assertIn('111111111111', body)
        self.assertNotIn('777777777777', body)
        self.assertEqual(len(list(csv.reader(io.StringIO(body)))), 3)
        ws = load_workbook(io.BytesIO(self.client.get(reverse('bank_export'), {'export': 'xlsx'}).content)).active
        self.assertEqual(ws.max_row, 3)
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', details__startswith='Bank payment instructions').exists())
        self.assertContains(self.client.get(reverse('bank_export'), {'status': 'PENDING', 'bank': 'SBIN', 'run': self.run.pk}), '222222222222'[-4:])

    def test_the_audit_page_shows_this_companys_bank_activity_only(self):
        b = self.batch()
        self.act(b, 'approve')
        other_owner = make_user('audit_other', 'COMPANY_OWNER', self.other)
        oe = bank_employee(self.other, 'O1', 'o1@b.test')
        orun, ol = released_run(self.other, 'September 2026', [(oe, '500')])
        self.client.logout()
        self.client.force_login(other_owner)
        self.client.post(reverse('bank_batch_new'), {'run': orun.pk, 'mode': 'NEFT', 'line': [ol[0].pk]})
        mine = self.client.get(reverse('bank_audit')).content.decode()
        self.assertIn('created: 1 payments', mine)                  # the other company sees only its own batch
        self.assertNotIn('created: 2 payments', mine)
        self.assertNotIn('approved', mine.split('<tbody>')[1])
        self.client.logout()
        self.client.force_login(self.owner)
        r = self.client.get(reverse('bank_audit'))
        self.assertContains(r, b.reference)
        self.assertContains(r, 'approved')
        self.assertContains(self.client.get(reverse('bank_audit'), {'action': 'APPROVE'}), 'approved')
        self.assertNotContains(self.client.get(reverse('bank_audit'), {'action': 'REJECT'}), b.reference)
        self.assertContains(self.client.get(reverse('bank_audit'), {'q': b.reference, 'from': '2020-01-01', 'to': '2099-01-01'}), b.reference)
        self.assertEqual(self.client.get(reverse('bank_audit'), {'from': 'garbage'}).status_code, 200)


class ExistingPagesStillWorkTests(BankBase):
    def test_nothing_in_the_new_module_changes_an_existing_payment_row_until_a_batch_is_made(self):
        p = BankPayment.objects.create(payroll_line=self.lines[0], company=self.co, amount='30000', account_no='111111111111', ifsc_code='HDFC0001234')
        version = p.version
        for name in ('bank_validation', 'bank_batches', 'bank_duplicates', 'bank_export', 'bank_audit', 'bank_reconcile', 'bank_approvals'):
            self.client.get(reverse(name))
        p.refresh_from_db()
        self.assertEqual(p.version, version)
