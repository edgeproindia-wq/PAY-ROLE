"""Real PDF payslip generation (ReportLab)."""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import CompanySettings

RS = 'Rs. '  # the built-in PDF fonts have no rupee glyph


def _money(v):
    return f"- {RS}{abs(v):,.2f}" if v < 0 else f"{RS}{v:,.2f}"


def build_payslip_pdf(line):
    emp = line.employee
    run = line.payroll_run
    company = emp.company
    cs = CompanySettings.objects.filter(company=company).first() if company else None
    company_name = (cs.company_name if cs and cs.company_name != 'My Company' else None) or (company.name if company else 'Company')
    company_addr = (cs.address if cs else '') or (company.address if company else '')

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f'Payslip {line.payslip_number}')
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f'<b>{company_name}</b>', styles['Title']),
        Paragraph(company_addr or '&nbsp;', styles['Normal']),
        Spacer(1, 4 * mm),
        Paragraph(f'<b>Payslip for {run.month}</b>', styles['Heading2']),
    ]

    info = [
        ['Payslip No.', line.payslip_number or '-', 'Employee Code', emp.employee_code],
        ['Employee Name', emp.full_name, 'Department', emp.department],
        ['Designation', emp.designation, 'Date of Joining', emp.date_of_joining.strftime('%d %b %Y')],
        ['PAN', emp.pan_number or '-', 'Bank A/C', emp.masked_account_no or 'Missing Bank Details'],
        ['Days in Month', str(line.days_in_month or '-'), 'LOP Days', str(line.lop_days)],
    ]
    t = Table(info, colWidths=[32 * mm, 55 * mm, 32 * mm, 55 * mm])
    t.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.whitesmoke),
        ('BACKGROUND', (2, 0), (2, -1), colors.whitesmoke),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
    ]))
    story += [t, Spacer(1, 5 * mm)]

    earnings = [
        ('Basic', line.basic), ('HRA', line.hra), ('Conveyance', line.conveyance),
        ('Special Allowance', line.special_allowance), ('Less: Loss of Pay', -line.lop_amount),
        ('Arrears', line.arrears), ('Reimbursements', line.reimbursements),
    ]
    deductions = [('Provident Fund', line.pf), ('ESI', line.esi), ('TDS', line.tds)]
    for label, value in (('Professional Tax', getattr(line, 'professional_tax', 0)),
                         ('Loan EMI', getattr(line, 'loan_deduction', 0)),
                         ('Insurance', getattr(line, 'insurance_deduction', 0))):
        if value:
            deductions.append((label, value))
    rows = [['Earnings', 'Amount', 'Deductions', 'Amount']]
    for i in range(max(len(earnings), len(deductions))):
        e = earnings[i] if i < len(earnings) else ('', None)
        d = deductions[i] if i < len(deductions) else ('', None)
        rows.append([e[0], _money(e[1]) if e[1] is not None else '', d[0], _money(d[1]) if d[1] is not None else ''])
    rows.append(['Total Earnings', _money(line.total_earnings), 'Total Deductions', _money(line.total_deductions)])
    t2 = Table(rows, colWidths=[50 * mm, 37 * mm, 50 * mm, 37 * mm])
    t2.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e293b')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
        ('ALIGN', (3, 1), (3, -1), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
    ]))
    story += [t2, Spacer(1, 6 * mm),
              Paragraph(f'<b>Net Pay: {_money(line.net_pay)}</b>', styles['Heading2']),
              Spacer(1, 10 * mm),
              Paragraph('This is a system-generated payslip and does not require a signature.', styles['Italic'])]
    doc.build(story)
    return buf.getvalue()
