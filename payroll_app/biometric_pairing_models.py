"""Pairing a sync program without copying a key.

The sync program asks the website for a short code and shows it on its own screen. The owner types the code on the Biometric page and
presses Approve. The sync program then collects its device key once. Only hashes of the code and of the program's secret are stored, and the
key itself is created at the moment it is handed over, so it is never stored, shown or typed anywhere.
"""
import hashlib
import secrets

from django.db import models

CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'      # no I, O, L, 0 or 1: nothing to mix up when typing
CODE_LENGTH = 8
PAIR_MINUTES = 10                                        # how long a code can wait to be approved
COLLECT_MINUTES = 5                                      # how long the program has to collect its key after the approval


def normalize_code(text):
    return ''.join(ch for ch in str(text or '').upper() if ch.isalnum())


class BiometricPairing(models.Model):
    code_hash = models.CharField(max_length=64, unique=True)
    nonce_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True, blank=True)
    company = models.ForeignKey('payroll_app.Company', null=True, blank=True, on_delete=models.CASCADE, related_name='biometric_pairings')
    device_name = models.CharField(max_length=80, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'payroll_app'
        ordering = ['-created_at']

    @staticmethod
    def hash_text(text):
        return hashlib.sha256(str(text).encode('utf-8')).hexdigest()

    @classmethod
    def hash_code(cls, code):
        return cls.hash_text(normalize_code(code))

    @staticmethod
    def new_code():
        return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))

    @staticmethod
    def show_code(code):
        return code[:4] + '-' + code[4:]
