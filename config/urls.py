from django.contrib import admin
from django.urls import path, include

# Uploaded files (reimbursement receipts) are deliberately NOT served from a
# public /media/ URL, not even in DEBUG — they are streamed only through the
# permission-checked view `reimbursement_receipt`.
urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('payroll_app.urls')),
]
