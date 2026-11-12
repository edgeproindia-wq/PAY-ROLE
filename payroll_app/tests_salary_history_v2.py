"""Salary history: the new-salary form opens only on a button, the all-employees list with filters and select-all, and salary changes for many employees at once."""
import datetime
import re
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import salary_history_bulk as bulk
from .models import AuditLog, Employee, PayrollRun, PayrollRunLine, SalaryRevision, SalaryStructure
from .tests import make_company, make_user

D = datetime.date


def person(company, code, dept='Engineering', desig='Developer', status='ACTIVE', first=None, last='Person'):
    return Employee.objects.create(employee_code=code, first_name=first or code, last_name=last, email='%s@sh2.test' % code.lower(), date_of_joining=D(2020, 1, 1),
                                   department=dept, designation=desig, employment_status=status, company=company)


def rev(emp, eff, basic, hra=0, conv=0, special=0, note=''):
    return SalaryRevision.objects.create(employee=emp, effective_from=eff, basic=basic, hra=hra, conveyance=conv, special_allowance=special, note=note)


def months_from(today, n):
    y, m = divmod(today.year * 12 + today.month - 1 + n, 12)
    return D(y, m + 1, 1)


class Base(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.this_month = self.today.replace(day=1)
        self.next_month = months_from(self.today, 1)
        self.last_month = months_from(self.today, -1)
        self.co, self.other = make_company('SH Alpha'), make_company('SH Beta')
        self.owner = make_user('sh_owner', 'COMPANY_OWNER', self.co)
        self.a = person(self.co, 'A1', 'Engineering', 'Developer', first='Asha')
        self.b = person(self.co, 'A2', 'Engineering', 'Tester', first='Bala')
        self.c = person(self.co, 'A3', 'Accounts', 'Clerk', first='Chitra')
        self.gone = person(self.co, 'A4', 'Accounts', 'Clerk', status='RESIGNED', first='Dev')
        self.stranger = person(self.other, 'B1', first='Zed')
        rev(self.a, D(2020, 1, 1), 20000, 8000, 1600, 10400)                   # gross 40,000
        rev(self.b, D(2020, 1, 1), 30000, 12000, 0, 0)                         # gross 42,000
        rev(self.c, D(2020, 1, 1), 15000, 0, 0, 0)                             # gross 15,000
        rev(self.stranger, D(2020, 1, 1), 99000)
        self.client.force_login(self.owner)
        self.url = reverse('salary_history')

    def page(self, **params):
        r = self.client.get(self.url, params)
        self.assertEqual(r.status_code, 200)
        return r

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def preview(self, ids, method='percent', eff=None, **extra):
        data = {'action': 'bulk_preview', 'ids': [e.pk for e in ids], 'effective_from': (eff or self.next_month).isoformat(), 'method': method}
        data.update(extra)
        return self.client.post(self.url, data)

    def save(self, ids, method='percent', eff=None, **extra):
        data = {'action': 'bulk_save', 'ids': [e.pk for e in ids], 'effective_from': (eff or self.next_month).isoformat(), 'method': method}
        data.update(extra)
        return self.client.post(self.url, data)


class SinglePageTests(Base):
    def test_the_new_salary_form_stays_closed_until_the_button_is_pressed(self):
        h = self.page(employee=self.a.pk).content.decode()
        self.assertIn('+ New salary', h)
        tag = re.search(r'<details class="sh-new"[^>]*>', h).group(0)
        self.assertNotIn(' open', tag)
        self.assertIn('Save new salary', h)                                         # the form is in the page, just folded away

    def test_a_failed_save_opens_the_form_again_with_the_error_and_the_typed_values(self):
        r = self.client.post(self.url, {'employee': self.a.pk, 'effective_from': self.today.replace(day=15).isoformat(), 'basic': '31000',
                                        'hra': '0', 'conveyance': '0', 'special_allowance': '0'})
        h = r.content.decode()
        self.assertIn('must start on the first day of a month', h)
        self.assertIn(' open', re.search(r'<details class="sh-new"[^>]*>', h).group(0))
        self.assertIn('value="31000"', h)

    def test_a_good_save_still_works_and_the_form_is_closed_again(self):
        r = self.client.post(self.url, {'employee': self.a.pk, 'effective_from': self.next_month.isoformat(), 'basic': '25000', 'hra': '0', 'conveyance': '0', 'special_allowance': '0'}, follow=True)
        self.assertTrue(SalaryRevision.objects.filter(employee=self.a, effective_from=self.next_month, basic=25000).exists())
        self.assertNotIn(' open', re.search(r'<details class="sh-new"[^>]*>', r.content.decode()).group(0))

    def test_the_timeline_page_links_back_to_the_list(self):
        self.assertIn('All employees', self.page(employee=self.a.pk).content.decode())


class ListTests(Base):
    def rows(self, **params):
        r = self.page(**params)
        return [row['emp'].employee_code for row in r.context['rows']], r

    def test_the_page_opens_with_everyone_of_my_company_and_nobody_else(self):
        codes, r = self.rows()
        self.assertEqual(codes, ['A1', 'A2', 'A3', 'A4'])
        self.assertNotContains(r, 'Zed Person')                                                   # (not a short fragment: the page holds a random security code that could contain it)
        self.assertNotContains(r, 'B1 - ')
        self.assertContains(r, 'id="shAll"')
        self.assertEqual(r.content.decode().count('name="ids"'), 4)
        self.assertContains(r, 'Select all')

    def test_current_salary_and_since_are_shown(self):
        rev(self.a, self.this_month, 33000, 8000, 1600, 10400)
        r = self.page()
        row = {x['emp'].employee_code: x for x in r.context['rows']}
        self.assertEqual(row['A1']['current']['rev'].gross_salary, Decimal('53000.00'))
        self.assertEqual(row['A2']['current']['rev'].gross_salary, Decimal('42000.00'))

    def test_people_without_a_salary_or_whose_salary_starts_later_are_labelled(self):
        person(self.co, 'A5')
        late = person(self.co, 'A6')
        rev(late, self.next_month, 10000)
        h = self.page().content.decode()
        self.assertIn('No salary saved', h)
        self.assertIn('Starts later', h)

    def test_each_filter_narrows_the_list(self):
        self.assertEqual(self.rows(q='asha')[0], ['A1'])
        self.assertEqual(self.rows(q='a3')[0], ['A3'])
        self.assertEqual(self.rows(q='Bala Person')[0], ['A2'])                          # two words: each must match
        self.assertEqual(self.rows(department='Accounts')[0], ['A3', 'A4'])
        self.assertEqual(self.rows(designation='Tester')[0], ['A2'])
        self.assertEqual(self.rows(status='RESIGNED')[0], ['A4'])
        self.assertEqual(self.rows(department='Accounts', status='ACTIVE')[0], ['A3'])
        self.assertEqual(self.rows(q='nobody-at-all')[0], [])
        self.assertEqual(self.rows(status='NONSENSE')[0], ['A1', 'A2', 'A3', 'A4'])      # an unknown status is ignored
        r = self.page(department='Accounts')
        self.assertContains(r, 'Matching employees')
        self.assertContains(r, 'Clear')

    def test_the_filter_lists_only_my_companys_departments(self):
        person(self.other, 'B2', dept='Secret Dept')
        ctx = self.page().context
        self.assertEqual(ctx['departments'], ['Accounts', 'Engineering'])
        self.assertNotIn('Secret Dept', self.page().content.decode())

    def test_the_all_changes_view_lists_every_salary_change_newest_first(self):
        rev(self.a, self.this_month, 33000, note='Increment')
        r = self.page(view='changes')
        self.assertEqual(r.context['view'], 'changes')
        dates = [c['rev'].effective_from for c in r.context['changes']]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(len(dates), 4)                                                  # A1 twice, A2, A3 (the resigned one has none)
        h = r.content.decode()
        self.assertIn('Increment', h)
        self.assertIn('In force now', h)
        self.assertNotIn('Zed Person', h)

    def test_a_long_list_is_cut_and_says_so(self):
        with mock.patch.object(bulk, 'LIST_LIMIT', 2):
            r = self.page()
        self.assertEqual((r.context['shown'], r.context['total'], r.context['capped']), (2, 4, True))
        self.assertContains(r, 'use the filters to narrow it down')

    def test_the_list_does_not_run_a_query_per_employee(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.page(view='changes')
            return len(ctx)

        for i in range(5):
            rev(person(self.co, 'Q%d' % i), D(2020, 1, 1), 10000)
        few = queries()
        for i in range(5, 40):
            rev(person(self.co, 'Q%d' % i), D(2020, 1, 1), 10000)
        self.assertEqual(few, queries())

    def test_names_are_escaped(self):
        person(self.co, 'X1', first='<script>alert(1)</script>')
        h = self.page().content.decode()
        self.assertNotIn('<script>alert(1)</script>', h)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', h)

    def test_only_the_owner_can_use_it(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        emp_user = make_user('sh_emp', 'EMPLOYEE', self.co)
        Employee.objects.filter(pk=self.a.pk).update(user=emp_user)
        self.client.force_login(emp_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.preview([self.a], percent='5').status_code, 403)
        self.assertEqual(self.save([self.a], percent='5').status_code, 403)


class BulkPreviewTests(Base):
    def test_a_percentage_preview_shows_each_new_salary_and_saves_nothing(self):
        r = self.preview([self.a, self.b], percent='10')
        self.assertEqual(r.status_code, 200)
        rows = {x['emp'].employee_code: x for x in r.context['bulk']['rows']}
        self.assertEqual(rows['A1']['new'], {'basic': Decimal('22000.00'), 'hra': Decimal('8800.00'), 'conveyance': Decimal('1760.00'), 'special_allowance': Decimal('11440.00')})
        self.assertEqual(rows['A1']['gross'], Decimal('44000.00'))
        self.assertEqual(rows['A2']['before'], Decimal('42000.00'))
        self.assertEqual((r.context['bulk']['ok'], r.context['bulk']['skipped']), (2, 0))
        self.assertEqual(r.context['bulk']['total_after'], Decimal('44000.00') + Decimal('46200.00'))
        self.assertContains(r, 'nothing is saved yet')
        self.assertContains(r, 'Confirm and save 2 salaries')
        self.assertEqual(SalaryRevision.objects.filter(effective_from=self.next_month).count(), 0)

    def test_the_three_methods(self):
        same = self.preview([self.a, self.c], method='same', basic='50000', hra='20000', conveyance='0', special_allowance='5000')
        self.assertEqual([r['gross'] for r in same.context['bulk']['rows']], [Decimal('75000.00')] * 2)
        amount = self.preview([self.a], method='amount', amount='2500')
        self.assertEqual(amount.context['bulk']['rows'][0]['new']['basic'], Decimal('22500.00'))
        self.assertEqual(amount.context['bulk']['rows'][0]['new']['hra'], Decimal('8000.00'))          # only Basic changes
        self.assertEqual(self.preview([self.a], percent='0.5').context['bulk']['rows'][0]['new']['basic'], Decimal('20100.00'))

    def test_percentages_are_rounded_half_up_to_paise(self):
        rev(self.c, D(2021, 1, 1), Decimal('10000.05'))
        row = self.preview([self.c], percent='8.333').context['bulk']['rows'][0]
        self.assertEqual(row['new']['basic'], (Decimal('10000.05') * Decimal('1.08333')).quantize(Decimal('0.01'), rounding='ROUND_HALF_UP'))

    def test_someone_who_already_has_a_change_that_month_is_skipped_not_overwritten(self):
        rev(self.b, self.next_month, 31111)
        r = self.preview([self.a, self.b], percent='10')
        rows = {x['emp'].employee_code: x for x in r.context['bulk']['rows']}
        self.assertEqual(rows['A2']['skip'], 'already has a salary starting %s' % self.next_month.strftime('%B %Y'))
        self.assertIsNone(rows['A2']['new'])
        self.assertEqual((r.context['bulk']['ok'], r.context['bulk']['skipped']), (1, 1))

    def test_a_percentage_needs_a_salary_to_start_from(self):
        nobody = person(self.co, 'A9')
        rows = {x['emp'].employee_code: x for x in self.preview([nobody], percent='10').context['bulk']['rows']}
        self.assertEqual(rows['A9']['skip'], 'no salary in force before that month')
        first = self.preview([nobody], method='same', basic='20000').context['bulk']['rows'][0]               # but a first salary can be set the same-for-all way
        self.assertEqual(first['gross'], Decimal('20000.00'))

    def test_the_base_is_the_salary_in_force_that_month_not_a_later_one(self):
        rev(self.a, months_from(self.today, 3), 99999)
        row = self.preview([self.a], percent='10').context['bulk']['rows'][0]
        self.assertEqual(row['new']['basic'], Decimal('22000.00'))

    def test_a_huge_result_is_skipped_instead_of_failing(self):
        rev(self.c, D(2021, 1, 1), Decimal('99999999.00'))
        row = self.preview([self.c], percent='50').context['bulk']['rows'][0]
        self.assertEqual(row['skip'], 'the new amount is too large')

    def test_the_confirm_button_carries_everything_needed_and_no_computed_numbers(self):
        h = self.preview([self.a, self.b], percent='10', note='Annual').content.decode()
        self.assertEqual(h.count('name="ids"'), 2)
        for needle in ('name="action" value="bulk_save"', 'name="method" value="percent"', 'name="percent" value="10"', 'name="note" value="Annual"'):
            self.assertIn(needle, h)
        self.assertNotIn('name="gross"', h)


class BulkSaveTests(Base):
    def test_saving_creates_one_revision_per_person_from_the_chosen_month(self):
        r = self.save([self.a, self.b, self.c], percent='10')
        self.assertEqual(r.status_code, 302)
        made = {x.employee.employee_code: x.gross_salary for x in SalaryRevision.objects.filter(effective_from=self.next_month)}
        self.assertEqual(made, {'A1': Decimal('44000.00'), 'A2': Decimal('46200.00'), 'A3': Decimal('16500.00')})
        self.assertTrue(all(x.created_by == self.owner for x in SalaryRevision.objects.filter(effective_from=self.next_month)))
        self.assertEqual(SalaryRevision.objects.filter(employee=self.gone).count(), 0)                  # the resigned one was not ticked
        follow = self.client.get(r['Location'])
        self.assertIn('New salary saved for 3 employees from %s.' % self.next_month.strftime('%B %Y'), [str(m) for m in follow.context['messages']])

    def test_a_change_from_this_month_updates_the_current_salary_and_a_later_one_does_not(self):
        SalaryStructure.objects.create(employee=self.a, basic=1, hra=0, conveyance=0, special_allowance=0)
        self.save([self.a], percent='10', eff=self.next_month)
        self.assertEqual(SalaryStructure.objects.get(employee=self.a).basic, Decimal('20000.00'))
        self.save([self.a], percent='10', eff=self.this_month)
        self.assertEqual(SalaryStructure.objects.get(employee=self.a).basic, Decimal('22000.00'))

    def test_the_same_for_everyone_and_the_fixed_amount_methods_save_correctly(self):
        self.save([self.a, self.c], method='same', basic='50000', hra='1000', conveyance='0', special_allowance='0', eff=months_from(self.today, 2))
        self.assertEqual({x.basic for x in SalaryRevision.objects.filter(effective_from=months_from(self.today, 2))}, {Decimal('50000.00')})
        self.save([self.b], method='amount', amount='1500')
        r = SalaryRevision.objects.get(employee=self.b, effective_from=self.next_month)
        self.assertEqual((r.basic, r.hra), (Decimal('31500.00'), Decimal('12000.00')))

    def test_a_skipped_person_keeps_their_existing_change_and_the_rest_are_saved(self):
        rev(self.b, self.next_month, 31111, note='Hand entered')
        r = self.save([self.a, self.b, self.c], percent='10')
        self.assertEqual(SalaryRevision.objects.get(employee=self.b, effective_from=self.next_month).basic, Decimal('31111.00'))
        self.assertEqual(SalaryRevision.objects.get(employee=self.b, effective_from=self.next_month).note, 'Hand entered')
        self.assertEqual(SalaryRevision.objects.filter(effective_from=self.next_month).count(), 3)
        follow = self.client.get(r['Location'])
        text = ' '.join(str(m) for m in follow.context['messages'])
        self.assertIn('Skipped 1: A2 already has a salary starting', text)

    def test_it_is_all_or_nothing(self):
        real_create = SalaryRevision.objects.create
        calls = {'n': 0}

        def flaky(*a, **k):
            calls['n'] += 1
            if calls['n'] == 2:
                raise IntegrityError('another request saved it first')
            return real_create(*a, **k)

        with mock.patch.object(SalaryRevision.objects, 'create', side_effect=flaky):
            r = self.save([self.a, self.b, self.c], percent='10')
        self.assertEqual(SalaryRevision.objects.filter(effective_from=self.next_month).count(), 0)
        self.assertIn('nothing was saved', ' '.join(str(m) for m in self.client.get(r['Location']).context['messages']))

    def test_a_crash_half_way_leaves_nothing_behind(self):
        self.client.raise_request_exception = False
        with mock.patch.object(bulk, 'sync_current_structure', side_effect=[None, RuntimeError('boom')]):
            r = self.save([self.a, self.b, self.c], percent='10')
        self.assertEqual(r.status_code, 500)
        self.assertEqual(SalaryRevision.objects.filter(effective_from=self.next_month).count(), 0)

    def test_every_change_is_written_to_the_audit_log(self):
        before = AuditLog.objects.filter(action='UPDATE').count()
        self.save([self.a, self.b], percent='10')
        logs = AuditLog.objects.filter(action='UPDATE').order_by('-id')[:2]
        self.assertEqual(AuditLog.objects.filter(action='UPDATE').count() - before, 2)
        self.assertTrue(all('(bulk)' in x.details and 'Salary revised from' in x.details for x in logs))

    def test_released_payslips_never_change_and_the_owner_is_told(self):
        released_month = months_from(self.today, 0)
        run = PayrollRun.objects.create(company=self.co, month=released_month.strftime('%B %Y'), status='RELEASED')
        line = PayrollRunLine.objects.create(payroll_run=run, employee=self.a, basic=20000, gross_salary=40000, net_pay=39000)
        draft = PayrollRun.objects.create(company=self.co, month=months_from(self.today, 1).strftime('%B %Y'), status='DRAFT')
        r = self.save([self.a], percent='10', eff=self.this_month)
        self.assertEqual(PayrollRunLine.objects.get(pk=line.pk).gross_salary, Decimal('40000.00'))
        text = ' '.join(str(m) for m in self.client.get(r['Location']).context['messages'])
        self.assertIn('Already released and paid at the old salary (unchanged): %s' % run.month, text)
        self.assertIn('Not released yet - reprocess to use the new salary: %s' % draft.month, text)

    def test_nothing_to_save_gives_a_clear_message(self):
        rev(self.a, self.next_month, 1)
        r = self.save([self.a], percent='10')
        self.assertEqual(r.status_code, 302)
        self.assertIn('every ticked employee was skipped', ' '.join(str(m) for m in self.client.get(r['Location']).context['messages']))

    def test_saving_twice_does_not_duplicate_anything(self):
        self.save([self.a], percent='10')
        self.save([self.a], percent='10')
        self.assertEqual(SalaryRevision.objects.filter(employee=self.a, effective_from=self.next_month).count(), 1)
        self.assertEqual(SalaryRevision.objects.get(employee=self.a, effective_from=self.next_month).basic, Decimal('22000.00'))


class BulkSafetyTests(Base):
    def errors(self, response):
        self.assertEqual(response.status_code, 200)
        return response.context['bulk_errors']

    def test_bad_input_is_refused_with_a_reason_and_saves_nothing(self):
        cases = [
            (dict(ids=[]), 'Tick at least one employee.'),
            (dict(eff='not-a-date'), 'Choose the month the new salary starts.'),
            (dict(eff=self.this_month.replace(day=15).isoformat()), 'must start on the first day of a month'),
            (dict(method='nonsense'), 'Choose how the new salary is worked out.'),
            (dict(percent='0'), 'Percentage must be more than zero.'),
            (dict(percent='150'), 'cannot be more than 100'),
            (dict(percent='abc'), 'Percentage must be a number.'),
            (dict(method='amount', amount=''), 'Amount must be more than zero.'),
            (dict(method='same', basic='0'), 'Basic must be more than zero.'),
        ]
        for case, expected in cases:
            data = {'action': 'bulk_preview', 'ids': case.get('ids', [self.a.pk]), 'effective_from': case.get('eff', self.next_month.isoformat()), 'method': case.get('method', 'percent'), 'percent': case.get('percent', '10')}
            if 'amount' in case:
                data['amount'] = case['amount']
            if case.get('method') == 'same':
                data['basic'] = case['basic']
            r = self.client.post(self.url, data)
            self.assertIn(expected, ' '.join(self.errors(r)), case)
        self.assertEqual(SalaryRevision.objects.filter(effective_from__gte=self.this_month).count(), 0)

    def test_after_an_error_the_panel_is_open_and_the_ticks_and_values_are_kept(self):
        r = self.preview([self.a, self.c], percent='150')
        h = r.content.decode()
        self.assertIn(' open', re.search(r'<details class="sh-new"[^>]*>', h).group(0))
        self.assertEqual(r.context['picked'], {self.a.pk, self.c.pk})
        self.assertEqual(len(re.findall(r'name="ids" value="\d+" checked', h)), 2)
        self.assertIn('value="150"', h)

    def test_too_many_at_once_is_refused(self):
        with mock.patch.object(bulk, 'MAX_BULK', 2):
            r = self.preview([self.a, self.b, self.c], percent='10')
        self.assertIn('at most 2 at a time', ' '.join(self.errors(r)))

    def test_another_companys_employees_can_never_be_read_or_changed(self):
        r = self.preview([self.stranger], percent='10')
        self.assertIn('None of the ticked employees belong to your company.', ' '.join(self.errors(r)))
        r = self.save([self.a, self.stranger], percent='10')
        self.assertEqual(r.status_code, 302)
        self.assertFalse(SalaryRevision.objects.filter(employee=self.stranger, effective_from=self.next_month).exists())
        self.assertTrue(SalaryRevision.objects.filter(employee=self.a, effective_from=self.next_month).exists())
        prev = self.preview([self.a, self.stranger], percent='10')
        self.assertEqual([x['emp'].employee_code for x in prev.context['bulk']['rows']], ['A1'])
        self.assertNotContains(prev, 'Zed Person')

    def test_the_old_single_form_post_is_not_mistaken_for_a_bulk_one(self):
        r = self.client.post(self.url, {'employee': self.a.pk, 'effective_from': self.next_month.isoformat(), 'basic': '25000', 'hra': '0', 'conveyance': '0', 'special_allowance': '0'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SalaryRevision.objects.filter(effective_from=self.next_month).count(), 1)

    def test_numbers_posted_by_a_browser_never_override_the_calculation(self):
        self.save([self.a], percent='10', basic='1', hra='1', gross='1', new_basic='1', hack='1')
        made = SalaryRevision.objects.get(employee=self.a, effective_from=self.next_month)
        self.assertEqual((made.basic, made.hra, made.gross_salary), (Decimal('22000.00'), Decimal('8800.00'), Decimal('44000.00')))

    def test_the_filters_come_back_after_saving(self):
        r = self.save([self.c], percent='10', department='Accounts', status='ACTIVE')
        self.assertIn('department=Accounts', r['Location'])
        self.assertIn('status=ACTIVE', r['Location'])
