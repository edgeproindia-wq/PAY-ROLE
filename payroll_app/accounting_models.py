"""Accounting Integration: ledger mapping, payroll journals and the payment links that stop a payment being posted twice.

These are new tables. A journal is built from a RELEASED payroll run and its bank payments; nothing in payroll
or bank payments is changed by posting a journal.
"""
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


class AccountingSettings(models.Model):
    company = models.OneToOneField('payroll_app.Company', on_delete=models.CASCADE, related_name='accounting_settings')
    employer_pf_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0,
                                              validators=[MinValueValidator(0), MaxValueValidator(100)],
                                              help_text='Employer PF share, as a percent of the PF wage. 0 means no employer PF entry.')
    employer_esi_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0,
                                               validators=[MinValueValidator(0), MaxValueValidator(100)],
                                               help_text='Employer ESI share, as a percent of the ESI wage. 0 means no employer ESI entry.')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'payroll_app'


class LedgerMapping(models.Model):
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='ledger_mappings')
    head = models.CharField(max_length=30)
    ledger_name = models.CharField(max_length=100)
    ledger_code = models.CharField(max_length=30, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['head']
        constraints = [models.UniqueConstraint(fields=['company', 'head'], name='one_ledger_mapping_per_head')]


class JournalEntry(models.Model):
    SALARY, PAYMENT = 'SALARY', 'PAYMENT'
    KIND_CHOICES = [(SALARY, 'Salary journal'), (PAYMENT, 'Salary payment')]
    POSTED, REVERSED = 'POSTED', 'REVERSED'
    STATUS_CHOICES = [(POSTED, 'Posted'), (REVERSED, 'Reversed')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='journal_entries')
    payroll_run = models.ForeignKey('payroll_app.PayrollRun', on_delete=models.PROTECT, related_name='journal_entries')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    voucher_no = models.CharField(max_length=30)
    entry_date = models.DateField()
    narration = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=POSTED)
    total_debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='journals_created')
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='journals_reversed')
    reversed_at = models.DateTimeField(null=True, blank=True)
    reverse_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['company', 'voucher_no'], name='unique_voucher_per_company'),
            # A payroll run can have only one live salary journal: posting twice is refused by the database too.
            models.UniqueConstraint(fields=['payroll_run'], condition=Q(kind='SALARY', status='POSTED'), name='one_posted_salary_journal_per_run'),
        ]

    def __str__(self):
        return f'{self.voucher_no} ({self.get_status_display()})'


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    head = models.CharField(max_length=30)
    ledger_name = models.CharField(max_length=100)
    ledger_code = models.CharField(max_length=30, blank=True)
    department = models.CharField(max_length=200, blank=True)
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    narration = models.CharField(max_length=255, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['id']


class JournalPaymentLink(models.Model):
    """A bank payment can be part of only one payment journal."""
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='payment_links')
    payment = models.OneToOneField('payroll_app.BankPayment', on_delete=models.PROTECT, related_name='journal_link')

    class Meta:
        app_label = 'payroll_app'
