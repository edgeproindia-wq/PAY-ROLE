"""Tax and HR documents issued to an employee (Form 16, Form 22, ...).

The file is stored in the database (BinaryField) on purpose: Render's disk is
wiped on every deploy, so files saved to MEDIA_ROOT would disappear."""
from django.conf import settings
from django.db import models


class EmployeeDocument(models.Model):
    DOC_TYPES = [
        ('FORM16', 'Form 16'),
        ('FORM22', 'Form 22'),
        ('FORM12BB', 'Form 12BB'),
        ('ID_PROOF', 'ID proof'),
        ('ADDRESS', 'Address proof'),
        ('EDUCATION', 'Education certificate'),
        ('EXPERIENCE', 'Experience letter'),
        ('OTHER', 'Other document'),
    ]
    STATUS_CHOICES = [('DRAFT', 'Draft (not visible to employee)'), ('PENDING', 'Awaiting verification'),
                      ('VERIFIED', 'Available'), ('REJECTED', 'Rejected')]

    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='employee_documents')
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField(max_length=10, choices=DOC_TYPES)
    financial_year = models.CharField(max_length=9, help_text='e.g. 2025-26')
    title = models.CharField(max_length=150, blank=True)
    file_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100, default='application/pdf')
    data = models.BinaryField()
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='VERIFIED')
    expiry_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-financial_year', 'doc_type', '-uploaded_at']

    def __str__(self):
        return f'{self.get_doc_type_display()} {self.financial_year} - {self.employee}'

    @property
    def label(self):
        return self.title or self.get_doc_type_display()