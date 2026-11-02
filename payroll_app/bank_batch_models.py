"""Bank payment batches: a batch is a group of salary payments sent to the bank together.

A batch moves PENDING_APPROVAL -> APPROVED -> FILE_GENERATED -> SENT -> COMPLETED
(or REJECTED / CANCELLED). A batch never changes the money details of a BankPayment;
it only groups payments so they can be approved, exported and reconciled together.
These are new tables: no existing table or row is touched.
"""
from django.conf import settings
from django.db import models


class BankBatch(models.Model):
    PENDING_APPROVAL, APPROVED, FILE_GENERATED, SENT, COMPLETED, REJECTED, CANCELLED = (
        'PENDING_APPROVAL', 'APPROVED', 'FILE_GENERATED', 'SENT', 'COMPLETED', 'REJECTED', 'CANCELLED')
    STATUS_CHOICES = [
        (PENDING_APPROVAL, 'Waiting for approval'), (APPROVED, 'Approved'), (FILE_GENERATED, 'Bank file generated'),
        (SENT, 'Sent to bank'), (COMPLETED, 'Completed'), (REJECTED, 'Rejected'), (CANCELLED, 'Cancelled'),
    ]
    # While a batch is in one of these states its payments are "locked": they cannot be put in another batch.
    ACTIVE_STATUSES = (PENDING_APPROVAL, APPROVED, FILE_GENERATED, SENT)
    MODE_CHOICES = [('NEFT', 'NEFT'), ('RTGS', 'RTGS'), ('IMPS', 'IMPS')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='bank_batches')
    reference = models.CharField(max_length=30)
    payroll_run = models.ForeignKey('payroll_app.PayrollRun', on_delete=models.PROTECT, related_name='bank_batches')
    mode = models.CharField(max_length=5, choices=MODE_CHOICES, default='NEFT')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING_APPROVAL)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    item_count = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=255, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='bank_batches_created')
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='bank_batches_approved')
    approved_at = models.DateTimeField(null=True, blank=True)
    self_approved = models.BooleanField(default=False, help_text='Approved by the person who created it (only allowed when the company has one owner login).')
    file_generated_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='bank_batches_sent')
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['company', 'reference'], name='unique_bank_batch_reference_per_company'),
        ]

    def __str__(self):
        return f'{self.reference} ({self.get_status_display()})'

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def file_allowed(self):
        """The bank file can only be produced once the batch is approved."""
        return self.status in (self.APPROVED, self.FILE_GENERATED, self.SENT, self.COMPLETED)


class BankBatchItem(models.Model):
    batch = models.ForeignKey(BankBatch, on_delete=models.CASCADE, related_name='items')
    payment = models.ForeignKey('payroll_app.BankPayment', on_delete=models.PROTECT, related_name='batch_items')
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text='Amount when the batch was created.')

    class Meta:
        app_label = 'payroll_app'
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['batch', 'payment'], name='one_batch_item_per_payment'),
        ]

    def __str__(self):
        return f'{self.batch.reference} / payment {self.payment_id}'
