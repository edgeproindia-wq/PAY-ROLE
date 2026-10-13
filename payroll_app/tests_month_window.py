"""The payroll month dropdown allows planning up to 12 months ahead and 5 years back, and nothing beyond."""
import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import PayrollRun
from .tests import make_company, make_user


def month_label(offset):
    today = timezone.localdate()
    y, m = divmod(today.year * 12 + today.month - 1 + offset, 12)
    return datetime.date(y, m + 1, 1).strftime('%B %Y')


class MonthWindowTests(TestCase):
    def test_the_allowed_window_and_the_refused_months(self):
        co = make_company('Window Co')
        self.client.force_login(make_user('windowowner', 'COMPANY_OWNER', co))
        for off in (12, 0, -60):
            self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month_label(off)})
            self.assertTrue(PayrollRun.objects.filter(company=co, month=month_label(off)).exists(), f'offset {off} should be allowed')
        for off in (13, -61):
            self.client.post(reverse('payroll_combined'), {'action': 'create', 'month': month_label(off)})
            self.assertFalse(PayrollRun.objects.filter(company=co, month=month_label(off)).exists(), f'offset {off} should be refused')