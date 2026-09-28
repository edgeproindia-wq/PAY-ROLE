"""Leave balance: yearly entitlement (from the company's Settings, falling
back to the model defaults) minus APPROVED leave days taken that year."""
from .models import CompanySettings, LeaveRequest

DEFAULT_ENTITLEMENT = {'CASUAL': 12, 'SICK': 12, 'EARNED': 15}


def entitlement_for(company):
    cs = CompanySettings.objects.filter(company=company).first() if company else None
    if not cs:
        return dict(DEFAULT_ENTITLEMENT)
    return {'CASUAL': cs.casual_leave_days, 'SICK': cs.sick_leave_days, 'EARNED': cs.earned_leave_days}


def leave_balance(employee, year):
    ent = entitlement_for(employee.company)
    labels = dict(LeaveRequest.LEAVE_TYPE_CHOICES)
    taken = {k: 0 for k in ent}
    for lv in LeaveRequest.objects.filter(employee=employee, status='APPROVED', from_date__year=year):
        taken[lv.leave_type] = taken.get(lv.leave_type, 0) + lv.days
    return {
        k: {'label': labels.get(k, k), 'entitled': ent[k], 'taken': taken.get(k, 0),
            'remaining': max(ent[k] - taken.get(k, 0), 0)}
        for k in ent
    }
