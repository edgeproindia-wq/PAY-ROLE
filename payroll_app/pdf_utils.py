"""Real PDF payslip generation (reportlab). Replaces the old on-screen-only stub."""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def render_payslip_pdf(line):
    """Build a one-page payslip PDF for a PayrollRunLine. Returns raw PDF bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('PayslipTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=2)
    sub_style = ParagraphStyle('PayslipSub', parent=styles['Normal'], textColor=colors.HexColor('#64748b'))

    employee = line.employee
    company_name = employee.company.name if employee.company else 'EDGEPRO Payroll'

    story = [
        Paragraph(company_name, title_style),
        Paragraph(f"Payslip for {line.payroll_run.month}", sub_style),
        Spacer(1, 10 * mm),
    ]

    info_table = Table([
        ['Employee', employee.full_name, 'Employee Code', employee.employee_code],
        ['Department', employee.department, 'Designation', employee.designation],
        ['Pay Period', line.payroll_run.month, 'Status', line.payroll_run.get_status_display()],
    ], colWidths=[35 * mm, 55 * mm, 35 * mm, 55 * mm])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e6e9f0')),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 8 * mm))

    deductions = (line.gross_salary - line.net_pay) if line.gross_salary is not None else 0
    earnings_table = Table([
        ['Earnings', 'Amount (₹)', 'Deductions', 'Amount (₹)'],
        ['Basic', f'{line.basic:,.2f}', 'Statutory & other deductions', f'{deductions:,.2f}'],
        ['Gross Salary', f'{line.gross_salary:,.2f}', '', ''],
        ['Net Pay', f'{line.net_pay:,.2f}', '', ''],
    ], colWidths=[45 * mm, 35 * mm, 55 * mm, 35 * mm])
    earnings_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eef0fe')),
        ('FONTNAME', (0, 3), (1, 3), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e6e9f0')),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
    ]))
    story.append(earnings_table)
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph(
        'This is a system-generated payslip and does not require a signature.',
        ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#94a3b8')),
    ))

    doc.build(story)
    return buf.getvalue()
