"""Biometric punches: devices (one secret key each), device-ID mapping and the raw punch log.

New tables only. A punch is stored once (unique per company, device user ID and time); the attendance record is derived from the
punches of the day. Only a hash of the device key is stored, so a key can never be read back.
"""
import hashlib
import secrets

from django.db import models


class BiometricDevice(models.Model):
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='biometric_devices')
    name = models.CharField(max_length=80)
    key_hash = models.CharField(max_length=64, unique=True)
    key_prefix = models.CharField(max_length=12)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_batch = models.PositiveIntegerField(default=0)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['name', 'id']

    @staticmethod
    def hash_key(key):
        return hashlib.sha256(key.encode('utf-8')).hexdigest()

    @staticmethod
    def new_key():
        return 'bio_' + secrets.token_urlsafe(32)

    def __str__(self):
        return f'{self.name} ({self.key_prefix}...)'


class BiometricIdMap(models.Model):
    """When the number on the device is not the Employee ID, this says which employee it is."""
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='biometric_id_maps')
    device_user_id = models.CharField(max_length=30)
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.CASCADE, related_name='biometric_ids')

    class Meta:
        app_label = 'payroll_app'
        constraints = [models.UniqueConstraint(fields=['company', 'device_user_id'], name='one_biometric_map_per_device_id')]


class BiometricPunch(models.Model):
    company = models.ForeignKey('payroll_app.Company', on_delete=models.CASCADE, related_name='biometric_punches')
    device = models.ForeignKey(BiometricDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name='punches')
    device_user_id = models.CharField(max_length=30)
    employee = models.ForeignKey('payroll_app.Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name='biometric_punches')
    punched_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-punched_at', '-id']
        constraints = [models.UniqueConstraint(fields=['company', 'device_user_id', 'punched_at'], name='one_punch_per_user_and_time')]
        indexes = [models.Index(fields=['company', 'punched_at']), models.Index(fields=['employee', 'punched_at'])]
