"""Month-end biometric summary: counts, hours, missing check-outs, export, isolation and access."""
import csv
import datetime
import io
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .biometric import IST
from .biometric_models import BiometricPunch
from .biometric_month import COLUMNS, parse_month, summary_rows
from .models import Attendance, AuditLog, Employee
from .tests import make_company, make_employee, make_user

D = Decimal
OCT = (datetime.date(2026, 10, 1), datetime.date(2026, 10, 31))


def at(day, hh, mm=0):
    return datetime.datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


class MonthBase(TestCase):
    def setUp(self):
        self.co = make_company('Month Co')
        self.other = make_company('Other Month Co')
        self.owner = make_user('month_owner', 'COMPANY_OWNER', self.co)
        self.e1 = make_employee(self.co, 'E1', 'e1@m.test')
        self.e2 = make_employee(self.co, 'E2', 'e2@m.test')
        for e, dept in ((self.e1, 'Detailing'), (self.e2, 'Admin')):
            e.department = dept
            e.save()
        self.client.force_login(self.owner)

    def att(self, emp, day, status='PRESENT', cin=None, cout=None):
        return Attendance.objects.create(employee=emp, date=datetime.date(2026, 10, day), status=status, check_in=cin, check_out=cout)

    def punch(self, emp, day, hh, mm=0, uid=None):
        return BiometricPunch.objects.create(company=self.co, device_user_id=uid or emp.employee_code, employee=emp, punched_at=at(datetime.date(2026, 10, day), hh, mm))

    def rows(self, month='2026-10', **params):
        r = self.client.get(reverse('bio_month'), dict(params, month=month))
        return r, r.context['rows'] if r.status_code == 200 else None

    def data(self, month='2026-10'):
        rows, totals = summary_rows(self.co, *parse_month(month))
        return {r['code']: r for r in rows}, totals


class SummaryTests(MonthBase):
    def setUp(self):
        super().setUp()
        t = datetime.time
        self.att(self.e1, 1, 'PRESENT', t(9, 0), t(18, 0))
        self.att(self.e1, 2, 'PRESENT', t(9, 30), t(17, 30))
        self.att(self.e1, 3, 'PRESENT', t(9, 15), None)             # missing check-out
        self.att(self.e1, 4, 'HALF_DAY', t(9, 0), t(13, 0))
        self.att(self.e1, 5, 'ABSENT')
        self.att(self.e1, 6, 'LEAVE')
        self.att(self.e2, 1, 'PRESENT', None, None)                  # marked present by hand, no punches
        self.punch(self.e1, 1, 9)
        self.punch(self.e1, 1, 18)
        self.punch(self.e1, 2, 9, 30)
        self.punch(self.e2, 1, 10)

    def test_counts_hours_and_missing_checkouts(self):
        d, totals = self.data()
        a = d['E1']
        self.assertEqual((a['present'], a['half'], a['absent'], a['leave'], a['recorded'], a['missing']), (3, 1, 1, 1, 6, 1))
        self.assertEqual(a['hours'], D('21.00'))                      # 9 + 8 + 4 hours; the day without a check-out has no hours
        self.assertEqual(a['avg'], D('7.00'))
        self.assertEqual((a['punches'], a['punch_days']), (3, 2))
        self.assertEqual((d['E2']['present'], d['E2']['hours'], d['E2']['avg'], d['E2']['punches']), (1, D('0.00'), D('0.00'), 1))
        self.assertEqual((totals['present'], totals['recorded'], totals['hours'], totals['punches']), (4, 7, D('21.00'), 4))

    def test_the_page_shows_a_row_per_employee_a_total_and_the_month(self):
        r, rows = self.rows()
        self.assertContains(r, 'October 2026')
        self.assertEqual(len(rows), 2)
        self.assertEqual([c[0] for c in rows[0]][:4], ['E1', 'Test Employee', 'Detailing', 3])
        self.assertEqual(r.context['totals'][0][0], 'Total')
        self.assertEqual((r.context['prev_month'], r.context['next_month']), ('2026-09', '2026-11'))

    def test_other_months_and_bad_months(self):
        self.assertEqual(self.data('2026-09')[0]['E1']['recorded'], 0)
        self.assertEqual(parse_month('2026-12'), (datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)))
        self.assertEqual(parse_month('2028-02')[1], datetime.date(2028, 2, 29))
        today = timezone.localdate()
        for bad in ('', None, 'garbage', '2026-13', '1999-01', '2026-1'):
            self.assertEqual(parse_month(bad)[0], today.replace(day=1), bad)
        r = self.client.get(reverse('bio_month'), {'month': 'junk'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['next_month'] > r.context['prev_month'], True)

    def test_december_rolls_into_january(self):
        r = self.client.get(reverse('bio_month'), {'month': '2026-12'})
        self.assertEqual((r.context['prev_month'], r.context['next_month']), ('2026-11', '2027-01'))

    def test_a_punch_at_midnight_india_time_belongs_to_the_right_day_and_month(self):
        BiometricPunch.objects.create(company=self.co, device_user_id='E2', employee=self.e2, punched_at=datetime.datetime(2026, 10, 31, 23, 59, 30, tzinfo=IST))
        BiometricPunch.objects.create(company=self.co, device_user_id='E2', employee=self.e2, punched_at=datetime.datetime(2026, 11, 1, 0, 0, 10, tzinfo=IST))
        self.assertEqual(self.data('2026-10')[0]['E2']['punches'], 2)           # 1 from setUp + the 31 Oct punch
        self.assertEqual(self.data('2026-11')[0]['E2']['punches'], 1)

    def test_days_with_a_punch_are_counted_in_india_time_not_utc(self):
        for hh, mm in ((0, 10), (8, 0)):                                       # 00:10 IST is still the day before in UTC
            BiometricPunch.objects.create(company=self.co, device_user_id='E2', employee=self.e2, punched_at=at(datetime.date(2026, 10, 12), hh, mm))
        d, _ = self.data()
        self.assertEqual((d['E2']['punch_days'], d['E2']['punches']), (2, 3))     # the 1 from setUp is on the 1st; the two new ones share one India day

    def test_a_check_out_before_the_check_in_never_gives_negative_hours(self):
        self.att(self.e2, 8, 'PRESENT', datetime.time(18, 0), datetime.time(9, 0))
        d, totals = self.data()
        self.assertEqual(d['E2']['hours'], D('0.00'))
        self.assertGreaterEqual(totals['hours'], D('0.00'))
        self.assertEqual(d['E2']['missing'], 0)

    def test_employees_who_left_with_no_data_are_not_listed_but_active_ones_always_are(self):
        gone = make_employee(self.co, 'E3', 'e3@m.test')
        Employee.objects.filter(pk=gone.pk).update(employment_status='RESIGNED')
        quiet = make_employee(self.co, 'E4', 'e4@m.test')
        d, _ = self.data()
        self.assertNotIn('E3', d)
        self.assertEqual(d['E4']['recorded'], 0)
        self.att(gone, 7, 'PRESENT', datetime.time(9), datetime.time(18))
        self.assertIn('E3', self.data()[0])                                      # a leaver with attendance this month is shown

    def test_search_filters_the_rows(self):
        r, rows = self.rows(q='admin')
        self.assertEqual([row[0][0] for row in rows], ['E2'])
        self.assertEqual(len(self.rows(q='nobody')[1]), 0)
        self.assertContains(self.rows(q='nobody')[0], 'No employees to show')

    def test_punches_from_unmatched_numbers_are_counted_in_a_notice(self):
        BiometricPunch.objects.create(company=self.co, device_user_id='999', employee=None, punched_at=at(datetime.date(2026, 10, 9), 9))
        r, _ = self.rows()
        self.assertEqual(r.context['waiting'], 1)
        self.assertContains(r, 'not matched to an employee')


class ExportTests(MonthBase):
    def test_csv_and_excel_match_the_page(self):
        from openpyxl import load_workbook
        t = datetime.time
        self.att(self.e1, 1, 'PRESENT', t(9), t(18))
        r = self.client.get(reverse('bio_month'), {'month': '2026-10', 'export': 'csv'})
        self.assertIn('biometric_summary_2026_10.csv', r['Content-Disposition'])
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0], [label for _k, label in COLUMNS])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][COLUMNS.index(('hours', 'Hours worked'))], '9.00')
        x = self.client.get(reverse('bio_month'), {'month': '2026-10', 'export': 'xlsx'})
        ws = load_workbook(io.BytesIO(x.content)).active
        self.assertEqual((ws.max_row, ws.cell(2, 11).value), (3, 9.0))
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', company=self.co, details__startswith='Biometric month summary exported (2026-10, csv').exists())

    def test_text_that_looks_like_a_formula_is_neutralised(self):
        Employee.objects.filter(pk=self.e1.pk).update(first_name='=HYPERLINK("http://x")')
        body = self.client.get(reverse('bio_month'), {'month': '2026-10', 'export': 'csv'}).content.decode()
        self.assertIn("'=HYPERLINK", body)


class AccessTests(MonthBase):
    def test_another_company_sees_none_of_our_data(self):
        self.att(self.e1, 1, 'PRESENT', datetime.time(9), datetime.time(18))
        self.punch(self.e1, 1, 9)
        self.client.force_login(make_user('month_other', 'COMPANY_OWNER', self.other))
        r, rows = self.rows()
        self.assertEqual(rows, [])
        self.assertEqual(r.context['totals'][3][0], 0)
        self.assertNotContains(r, 'Detailing')
        self.assertEqual(len(list(csv.reader(io.StringIO(self.client.get(reverse('bio_month'), {'export': 'csv'}).content.decode())))), 1)

    def test_only_company_owners_can_open_it(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('bio_month')).status_code, 302)
        self.client.force_login(make_user('month_emp', 'EMPLOYEE', self.co))
        self.assertIn(self.client.get(reverse('bio_month')).status_code, (302, 403))
        self.client.force_login(make_user('month_admin', 'ADMIN'))
        self.assertEqual(self.client.get(reverse('bio_month')).status_code, 403)

    def test_the_biometric_page_links_to_the_summary(self):
        self.assertContains(self.client.get(reverse('bio_setup')), reverse('bio_month'))

    def test_the_page_changes_nothing(self):
        self.att(self.e1, 1, 'ABSENT')
        before = list(Attendance.objects.values_list('pk', 'status', 'check_in'))
        for params in ({}, {'month': '2026-10'}, {'month': '2026-10', 'export': 'csv'}):
            self.client.get(reverse('bio_month'), params)
        self.assertEqual(list(Attendance.objects.values_list('pk', 'status', 'check_in')), before)
