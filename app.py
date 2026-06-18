# app.py - Work Schedule Management System

import os
import datetime
import smtplib
import io
import logging
import traceback as tb
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('shift_app')
# Show SMTP handshake details in the console
logging.getLogger('smtplib').setLevel(logging.DEBUG)

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from PIL import Image, ImageDraw, ImageFont

# --- Configuration ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_super_secret_key_here'
# Update the URI to include connection arguments
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://neondb_owner:npg_4sLTVfeAD0gt@ep-sweet-night-aowpnooc.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require'
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True, # This prevents the SSL closed error
    "pool_recycle": 300,
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


# --- Email Configuration (Using Brevo on Port 2525) ---
SMTP_SERVER   = 'smtp-relay.brevo.com'
SMTP_PORT     = 2525                        # Use 2525 to bypass Render's port blocks
SMTP_USERNAME = 'ae379c001@smtp-brevo.com'  
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')  # Hidden securely from GitHub's scanners
SENDER_EMAIL  = 'bibiyanaj8@gmail.com'      # Your verified sender email

# --- Shift metadata ---
SHIFT_LABELS = {
    'M': 'Morning',
    'A': 'Afternoon',
    'N': 'Night',
    'L': 'Leave',
    'O': 'OFF',
    'G': 'General',
}

# --- Many-to-Many Association Table ---
employee_team = db.Table('employee_team',
    db.Column('employee_id', db.Integer, db.ForeignKey('employee.id'), primary_key=True),
    db.Column('team_id', db.Integer, db.ForeignKey('team.id'), primary_key=True)
)

# --- Database Models ---

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    password = db.Column(db.String(120), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    # legacy column kept for DB compatibility – do not use for queries
    team_id = db.Column(db.Integer, nullable=True)
    teams = db.relationship('Team', secondary=employee_team, backref='members')

    def __repr__(self):
        return f'<Employee {self.name}>'

class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

class Shift(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    shift_type = db.Column(db.String(2), nullable=False)  # M, A, N, O, L, G

    def __repr__(self):
        return f'<Shift {self.employee_id} {self.date} {self.shift_type}>'

class ShiftTemplate(db.Model):
    id  = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    mon = db.Column(db.String(2), nullable=True)
    tue = db.Column(db.String(2), nullable=True)
    wed = db.Column(db.String(2), nullable=True)
    thu = db.Column(db.String(2), nullable=True)
    fri = db.Column(db.String(2), nullable=True)
    sat = db.Column(db.String(2), nullable=True)
    sun = db.Column(db.String(2), nullable=True)

# --- Utility Functions ---

def get_week_range(date=None):
    if date is None:
        date = datetime.date.today()
    start_of_week = date - datetime.timedelta(days=date.weekday())
    end_of_week = start_of_week + datetime.timedelta(days=6)
    return start_of_week, end_of_week

def get_calendar_view_range(date=None):
    if date is None:
        date = datetime.date.today()
    start_of_month = date.replace(day=1)
    if date.month == 12:
        end_of_month = date.replace(day=31)
    else:
        end_of_month = date.replace(month=date.month + 1, day=1) - datetime.timedelta(days=1)
    start_of_calendar_view = start_of_month - datetime.timedelta(days=start_of_month.weekday())
    end_of_calendar_view = end_of_month + datetime.timedelta(days=(6 - end_of_month.weekday()))
    return start_of_calendar_view, end_of_calendar_view

def get_employees_for_team(team_id):
    team = db.session.get(Team, team_id)
    if not team:
        return []
    return team.members

# --- Shift Validation ---

def validate_shift(employee_id, date, shift_type):
    start_of_week, end_of_week = get_week_range(date)
    existing_shifts = Shift.query.filter_by(employee_id=employee_id) \
                                 .filter(Shift.date >= start_of_week, Shift.date <= end_of_week) \
                                 .all()
    off_days_count = sum(1 for s in existing_shifts if s.shift_type == 'O' and s.date != date)
    if shift_type == 'O' and off_days_count >= 2:
        return False, "Maximum 2 OFF days allowed per week for this employee."
    night_shifts_count = sum(1 for s in existing_shifts if s.shift_type == 'N' and s.date != date)
    if shift_type == 'N' and night_shifts_count >= 1:
        return False, "Only ONE Night Shift allowed per employee per week."
    return True, "Success"

# --- Image & Email Helpers ---

def generate_roster_image(employees, dates, schedule_data, title, team_name='', period_label=''):
    """Generate a PNG image of the roster table for emailing."""

    col_w    = 72 if len(dates) <= 7 else 52
    row_h    = 70
    date_h   = 90
    wk_h     = 55
    name_w   = 300
    header_h = 130
    padding  = 30
    legend_h = 60

    SHIFT_COLORS = {
        'M': (255, 215, 0),
        'A': (255, 152, 0),
        'N': (26, 35, 126),
        'O': (76, 175, 80),
        'L': (244, 67, 54),
        'G': (103, 58, 183),
        '':  (255, 255, 255),
    }
    SHIFT_TEXT_COLORS = {
        'M': (30, 30, 30),
        'A': (255, 255, 255),
        'N': (255, 255, 255),
        'O': (255, 255, 255),
        'L': (255, 255, 255),
        'G': (255, 255, 255),
        '':  (200, 200, 200),
    }

    total_cols_w = col_w * len(dates)
    width  = padding + name_w + total_cols_w + padding
    height = header_h + wk_h + date_h + row_h * len(employees) + legend_h + padding * 2

    img  = Image.new('RGB', (width, height), (240, 242, 245))
    draw = ImageDraw.Draw(img)

    # ── Load fonts ─────────────────────────────────────────────────
    def load_font(size, bold=False):
        if bold:
            font_names = [
                'arialbd.ttf', 'Arial_Bold.ttf', 
                'DejaVuSans-Bold.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
                'arial.ttf', 'Arial.ttf'
            ]
        else:
            font_names = [
                'arial.ttf', 'Arial.ttf', 
                'DejaVuSans.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
                'arialbd.ttf'
            ]
        for fname in font_names:
            try:
                return ImageFont.truetype(fname, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)
        except Exception:
            return ImageFont.load_default()

    font_title   = load_font(42, bold=True)
    font_sub     = load_font(28)
    font_wk      = load_font(30, bold=True)
    font_datenum = load_font(28, bold=True)
    font_dayname = load_font(22)
    font_empname = load_font(28, bold=True)
    font_cell    = load_font(28, bold=True)
    font_legend  = load_font(22)

    # ── Helper: centered text in a box ────────────────────────────
    def draw_centered(text, x, y, w, h, font, color):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((x + (w - tw) // 2, y + (h - th) // 2), text, fill=color, font=font)

    # ── Title bar ──────────────────────────────────────────────────
    draw.rectangle([0, 0, width, header_h], fill=(60, 90, 155))
    # Title text
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text((padding, 20), title, fill=(255, 255, 255), font=font_title)
    # Subtitle
    if team_name or period_label:
        sub = f"Team: {team_name}   |   Period: {period_label}" if team_name else period_label
        draw.text((padding, 78), sub, fill=(190, 215, 255), font=font_sub)

    x_start = padding
    y_wk    = header_h
    y_date  = y_wk + wk_h
    y_data  = y_date + date_h

    # ── "Employee" column header (spans wk + date rows) ───────────
    draw.rectangle([x_start, y_wk, x_start + name_w - 1, y_date + date_h - 1],
                   fill=(60, 90, 155))
    draw_centered("Employee", x_start, y_wk, name_w, wk_h + date_h,
                  font_wk, (255, 255, 255))

    # ── Week-group header row ──────────────────────────────────────
    groups = []
    for i, d in enumerate(dates):
        wk = d.isocalendar()[1]
        if groups and groups[-1]['wk'] == wk:
            groups[-1]['count'] += 1
        else:
            groups.append({'wk': wk, 'start': i, 'count': 1,
                           'label': f"Wk {len(groups)+1}"})

    ALT_DARK = (60, 90, 155)
    ALT_MED  = (80, 112, 178)

    for gi, g in enumerate(groups):
        gx = x_start + name_w + g['start'] * col_w
        gw = g['count'] * col_w
        bg = ALT_DARK if gi % 2 == 0 else ALT_MED
        draw.rectangle([gx, y_wk, gx + gw - 1, y_wk + wk_h - 1], fill=bg)
        draw_centered(g['label'], gx, y_wk, gw, wk_h, font_wk, (255, 255, 255))
        # right divider
        draw.line([gx + gw - 1, y_wk, gx + gw - 1, y_wk + wk_h - 1],
                  fill=(0, 0, 0), width=2)
    draw.line([x_start, y_date, x_start + name_w + total_cols_w, y_date],
              fill=(0, 0, 0), width=2)
    draw.line([x_start, y_data, x_start + name_w + total_cols_w, y_data],
              fill=(0, 0, 0), width=2)
    draw.line([x_start + name_w, y_wk, x_start + name_w, y_data + row_h * len(employees)],
              fill=(0, 0, 0), width=3)
    # ── Date + day header row ──────────────────────────────────────
    HEADER_DATE_BG  = (92, 122, 190)
    HEADER_DATE_BG2 = (108, 138, 205)

    for i, d in enumerate(dates):
        x  = x_start + name_w + i * col_w
        bg = HEADER_DATE_BG if i % 2 == 0 else HEADER_DATE_BG2
        draw.rectangle([x, y_date, x + col_w - 1, y_date + date_h - 1], fill=bg)
        num_str = str(d.day)
        day_str = d.strftime('%a')
        # date number (top half)
        draw_centered(num_str, x, y_date, col_w, date_h // 2, font_datenum, (255, 255, 255))
        # day name (bottom half)
        draw_centered(day_str, x, y_date + date_h // 2, col_w, date_h // 2,
                      font_dayname, (210, 230, 255))
        # column divider
        draw.line([x + col_w - 1, y_date, x + col_w - 1, y_date + date_h - 1],
                  fill=(0, 0, 0), width=2)

    # ── Employee data rows ─────────────────────────────────────────
    for row_i, emp in enumerate(employees):
        y   = y_data + row_h * row_i
        bg  = (250, 251, 253) if row_i % 2 == 0 else (238, 243, 252)
        # name cell
        draw.rectangle([x_start, y, x_start + name_w - 1, y + row_h - 1], fill=bg)
        # vertically center name
        bbox = draw.textbbox((0, 0), emp.name[:28], font=font_empname)
        th = bbox[3] - bbox[1]
        draw.text((x_start + 12, y + (row_h - th) // 2),
                  emp.name[:28], fill=(25, 35, 60), font=font_empname)

        for col_i, d in enumerate(dates):
            x        = x_start + name_w + col_i * col_w
            date_str = d.strftime('%Y-%m-%d')
            shift    = schedule_data.get(emp.id, {}).get(date_str, '')
            fill     = SHIFT_COLORS.get(shift, (255, 255, 255))
            text_col = SHIFT_TEXT_COLORS.get(shift, (220, 220, 220))
            draw.rectangle([x, y, x + col_w - 1, y + row_h - 1], fill=fill)
            if shift:
                draw_centered(shift, x, y, col_w, row_h, font_cell, text_col)

        # row bottom border
        draw.line([x_start, y + row_h - 1,
                   x_start + name_w + total_cols_w, y + row_h - 1],
                  fill=(0, 0, 0), width=2)

    # Vertical grid lines
    # Vertical grid lines - BLACK
    for col_i in range(len(dates) + 1):
        lx = x_start + name_w + col_i * col_w
        draw.line([lx, y_date, lx, y_data + row_h * len(employees)],
                fill=(0, 0, 0), width=2)

    # Outer border - BLACK thick
    draw.rectangle([x_start, y_wk,
                    x_start + name_w + total_cols_w,
                    y_data + row_h * len(employees)],
                outline=(0, 0, 0), width=3)

    # ── Legend footer ──────────────────────────────────────────────
    y_legend = y_data + row_h * len(employees) + padding
    legend_items = [
        ('M', (255, 215, 0),   (30, 30, 30),   'Morning'),
        ('A', (255, 152, 0),   (255, 255, 255), 'Afternoon'),
        ('N', (26, 35, 126),   (255, 255, 255), 'Night'),
        ('L', (244, 67, 54),   (255, 255, 255), 'Leave'),
        ('O', (76, 175, 80),   (255, 255, 255), 'OFF'),
        ('G', (103, 58, 183),  (255, 255, 255), 'General'),
    ]
    lx = x_start
    box_size = 36
    gap      = 10
    for code, bg, fg, label in legend_items:
        draw.rectangle([lx, y_legend, lx + box_size, y_legend + box_size], fill=bg)
        draw_centered(code, lx, y_legend, box_size, box_size, font_legend, fg)
        draw.text((lx + box_size + 6, y_legend + 7), label,
                  fill=(50, 50, 60), font=font_legend)
        label_w = draw.textbbox((0, 0), label, font=font_legend)[2]
        lx += box_size + 6 + label_w + gap + 18

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.read()


def _smtp_send(msg, to_label):
    """Send via SMTP (port 2525 with STARTTLS)."""
    log.info("SMTP ▶ connecting to %s:%s", SMTP_SERVER, SMTP_PORT)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as s:
            s.ehlo()
            s.starttls()  # Secure connection via STARTTLS
            s.ehlo()
            s.login(SMTP_USERNAME, SMTP_PASSWORD)
            s.send_message(msg)
            log.info("SMTP ▶ sent OK to %s", to_label)
    except Exception as e:
        log.error("SMTP ▶ failed: %s", e)
        raise


def send_roster_email(employee, image_bytes, subject, body_text, team_name='', period_label=''):
    """Send HTML email with the roster image displayed inline."""
    msg = MIMEMultipart('related')
    msg['Subject'] = subject
    msg['From']    = f"ShiftRoaster  <{SENDER_EMAIL}>"
    msg['To']      = employee.email

    alt = MIMEMultipart('alternative')
    msg.attach(alt)

    # Plain-text fallback
    alt.attach(MIMEText(body_text, 'plain'))

    # HTML body with inline image
    mode_word  = 'Weekly' if 'Weekly' in subject else 'Monthly'
    team_line  = f"<strong>Team:</strong> {team_name}" if team_name else "<strong>Team:</strong> All Teams"
    period_line = f"<strong>Period:</strong> {period_label}" if period_label else ""

    html = f"""
<html>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;">
  <div style="max-width:960px;margin:20px auto;background:#fff;border-radius:10px;
              overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <!-- Header bar -->
    <div style="background:#4c66a4;padding:18px 24px;">
      <h2 style="margin:0;color:#fff;font-size:1.15em;">{mode_word} Shift Roster</h2>
      <p style="margin:4px 0 0;color:#c5d5f5;font-size:0.88em;">{period_label}</p>
    </div>

    <!-- Body -->
    <div style="padding:20px 24px;">
      <p style="margin-top:0;">Hi <strong>{employee.name}</strong>,</p>
      <p>Your {mode_word.lower()} shift roster is shown below.</p>

      <!-- Roster image -->
      <div style="overflow-x:auto;border:1px solid #ddd;border-radius:6px;">
        <img src="cid:roster_image" alt="Roster" style="display:block;max-width:100%;height:auto;">
      </div>

      <!-- Details below the image -->
      <div style="margin-top:14px;background:#f0f4ff;border-left:4px solid #4c66a4;
                  border-radius:4px;padding:10px 16px;font-size:0.9em;color:#333;">
        {team_line} &nbsp;&nbsp;|&nbsp;&nbsp; {period_line}
      </div>

      <!-- Legend -->
      <div style="margin-top:14px;font-size:0.82em;color:#555;">
        <strong>Shift Legend:</strong>&nbsp;
        <span style="background:#ffd700;color:#333;padding:2px 8px;border-radius:3px;margin:0 2px;">M – Morning</span>
        <span style="background:#ff9800;color:#fff;padding:2px 8px;border-radius:3px;margin:0 2px;">A – Afternoon</span>
        <span style="background:#1a237e;color:#fff;padding:2px 8px;border-radius:3px;margin:0 2px;">N – Night</span>
        <span style="background:#f44336;color:#fff;padding:2px 8px;border-radius:3px;margin:0 2px;">L – Leave</span>
        <span style="background:#4caf50;color:#fff;padding:2px 8px;border-radius:3px;margin:0 2px;">O – OFF</span>
        <span style="background:#673ab7;color:#fff;padding:2px 8px;border-radius:3px;margin:0 2px;">G – General/span>
      </div>

      <p style="margin-top:20px;margin-bottom:0;color:#888;font-size:0.82em;">
        This is an automated email from the Work Schedule Management System.
      </p>
    </div>
  </div>
</body>
</html>
"""
    alt.attach(MIMEText(html, 'html'))

    # Inline image attachment (Content-ID referenced in HTML)
    img_part = MIMEImage(image_bytes, _subtype='png')
    img_part.add_header('Content-ID', '<roster_image>')
    img_part.add_header('Content-Disposition', 'inline', filename='roster.png')
    msg.attach(img_part)

    _smtp_send(msg, employee.email)


def generate_schedule_image(employee, schedule):
    img = Image.new('RGB', (800, 500), 'white')
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), f"{employee.name} - Schedule", fill="black")
    y = 60
    for date, shift in schedule.items():
        draw.text((20, y), f"{date} : {shift}", fill="black")
        y += 25
    file_path = f"static/schedule_{employee.id}.png"
    img.save(file_path)
    return file_path


def send_email(to_email, file_path, employee_name):
    log.info("SMTP ▶ sending individual schedule to %s", to_email)
    msg = EmailMessage()
    msg['Subject'] = 'Updated Monthly Shift Roster'
    msg['From']    = f"ShiftRoaster  <{SENDER_EMAIL}>" # Changed from SMTP_USERNAME to SENDER_EMAIL
    msg['To']      = to_email
    msg.set_content(f"Hi {employee_name},\n\nYour updated monthly shift roster is attached.\n\nRegards,\nAdmin")
    with open(file_path, 'rb') as f:
        msg.add_attachment(f.read(), maintype='image', subtype='png', filename='schedule.png')
    _smtp_send(msg, to_email)

# --- Search Route ---

@app.route('/search_shift')
def search_shift():
    team_id = request.args.get('team_id')
    day = request.args.get('day')
    shift = request.args.get('shift')

    if team_id:
        employees = get_employees_for_team(int(team_id))
    else:
        employees = Employee.query.all()

    result_names = []
    for emp in employees:
        shifts = Shift.query.filter_by(employee_id=emp.id).all()
        for s in shifts:
            if day is not None and s.date.weekday() == int(day) and s.shift_type == shift:
                result_names.append(emp.name)
                break
            elif day is None and s.shift_type == shift:
                result_names.append(emp.name)
                break

    return jsonify({"count": len(result_names), "names": result_names})


@app.route('/api/roster_search')
def roster_search():
    if not session.get('is_admin'):
        return jsonify({'error': 'Permission denied'}), 403

    team_id = request.args.get('team_id')
    shift_type = request.args.get('shift_type')
    dates_param = request.args.get('dates')

    if not dates_param:
        return jsonify({'error': 'date required'}), 400

    dates = [datetime.datetime.strptime(ds.strip(), '%Y-%m-%d').date() for ds in dates_param.split(',')]

    # Query only non-admin employees
    query = Employee.query.filter_by(is_admin=False)
    if team_id and team_id != '':
        query = query.join(employee_team).filter(employee_team.c.team_id == int(team_id))
    
    employees = query.all()

    # Get shifts for these employees on the selected date (using the first date in the list for simplicity)
    search_date = dates[0]
    shifts = Shift.query.filter(
        Shift.date == search_date,
        Shift.employee_id.in_([e.id for e in employees])
    ).all()
    shift_map = {s.employee_id: s.shift_type for s in shifts}

    if shift_type and shift_type != '':
        employees = [e for e in employees if shift_map.get(e.id) == shift_type]

    return jsonify({
        'mode': 'filtered',
        'count': len(employees),
        'results': [
            {'name': e.name, 'shift': shift_map.get(e.id, 'N/A')}
            for e in employees
            if shift_map.get(e.id) or not (shift_type and shift_type != '')
        ]
    })

@app.route('/team_details_view')
def team_details_view():
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    
    # Get all teams
    teams = Team.query.all()
    # Filter members to exclude admin
    team_data = []
    for team in teams:
        members = [m for m in team.members if not m.is_admin]
        team_data.append({'name': team.name, 'members': members})
        
    return render_template('team_details.html', team_data=team_data)
# --- Send Schedule (individual) ---

@app.route('/send_schedule/<int:employee_id>', methods=['POST'])
def send_schedule(employee_id):
    """Send individual employee schedule with the same professional format as roster emails."""
    try:
        employee = db.session.get(Employee, employee_id)
        if not employee:
            return jsonify({"status": "error", "message": "Employee not found"})

        # Get current month range
        today = datetime.date.today()
        start_of_month = today.replace(day=1)
        if start_of_month.month == 12:
            end_of_month = start_of_month.replace(year=start_of_month.year + 1, month=1) - datetime.timedelta(days=1)
        else:
            end_of_month = start_of_month.replace(month=start_of_month.month + 1) - datetime.timedelta(days=1)

        # Build date list for the month
        dates = []
        d = start_of_month
        while d <= end_of_month:
            dates.append(d)
            d += datetime.timedelta(days=1)

        # Get shifts for this employee
        shifts = Shift.query.filter_by(employee_id=employee_id) \
                            .filter(Shift.date >= start_of_month, Shift.date <= end_of_month) \
                            .all()
        
        schedule_data = {employee.id: {}}
        for s in shifts:
            schedule_data[employee.id][s.date.strftime('%Y-%m-%d')] = s.shift_type

        # Get employee's teams
        team_names = ', '.join([t.name for t in employee.teams]) if employee.teams else 'No Team'
        period_label = today.strftime('%B %Y')
        
        # Generate roster image (same as weekly/monthly)
        title = f"Individual Schedule – {employee.name}"
        image_bytes = generate_roster_image(
            [employee],  # Single employee
            dates,
            schedule_data,
            title,
            team_name=team_names,
            period_label=period_label
        )

        # Today's shift info
        today_shift = schedule_data.get(employee.id, {}).get(today.strftime('%Y-%m-%d'), '')
        today_line = ''
        if today_shift:
            today_line = f"\nToday ({today.strftime('%A, %d %b')}) you are assigned: {SHIFT_LABELS.get(today_shift, today_shift)} shift."

        # Email body
        body = (
            f"Hi {employee.name},\n\n"
            f"Please find your updated shift schedule for {period_label}.\n"
            f"{today_line}\n\n"
            f"Shift legend:  M=Morning  A=Afternoon  N=Night  L=Leave  O=Off  G=General\n\n"
            f"Regards,\nAdmin"
        )
        subject = f"Your Updated Schedule – {period_label}"

        # Send email with roster image
        log.info("SMTP ▶ sending individual schedule to %s", employee.email)
        send_roster_email(employee, image_bytes, subject, body,
                         team_name=team_names, period_label=period_label)

        return jsonify({"status": "success", "message": f"Schedule sent to {employee.email}"})
    
    except Exception as e:
        log.error("Failed to send individual schedule: %s", e)
        tb.print_exc()
        return jsonify({"status": "error", "message": str(e)})

# --- Authentication ---

@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        employee = Employee.query.filter_by(email=email).first()
        if employee:
            session['employee_id'] = employee.id
            session['is_admin'] = employee.is_admin
            return redirect(url_for('admin_dashboard') if employee.is_admin else url_for('employee_dashboard'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- Admin Routes ---

@app.route('/admin_dashboard')
def admin_dashboard():
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    teams = Team.query.all()
    team_counts = {t.id: len(t.members) for t in teams}
    today = datetime.date.today()
    total_employees = Employee.query.filter_by(is_admin=False).count()
    rosters_active  = db.session.query(Shift.employee_id).filter(
        Shift.date >= today.replace(day=1)).distinct().count()
    employees = Employee.query.order_by(Employee.name).all()

    # Today's shift breakdown
    today_rows = (db.session.query(Shift, Employee)
                  .join(Employee, Shift.employee_id == Employee.id)
                  .filter(Shift.date == today)
                  .order_by(Employee.name)
                  .all())
    today_by_shift = {'M': [], 'A': [], 'N': [], 'L': [], 'O': [], 'G': []}
    assigned_ids = set()
    for shift, emp in today_rows:
        if shift.shift_type in today_by_shift:
            today_by_shift[shift.shift_type].append({'id': emp.id, 'name': emp.name})
        assigned_ids.add(emp.id)
    unassigned = [{'id': e.id, 'name': e.name} for e in employees if e.id not in assigned_ids]

    return render_template('admin_dashboard.html',
        teams=teams, team_counts=team_counts,
        employees=employees,
        total_employees=total_employees,
        active_teams=len(teams),
        rosters_active=rosters_active,
        today=today,
        today_by_shift=today_by_shift,
        unassigned=unassigned)

@app.route('/team_members/<int:team_id>')
def team_members(team_id):
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    team = db.session.get(Team, team_id)
    if not team:
        return redirect(url_for('admin_dashboard'))
    employees = team.members
    all_teams = Team.query.all()
    return render_template('team_members.html', team=team, employees=employees, all_teams=all_teams)

@app.route('/employee_calendar/<int:employee_id>')
def employee_calendar(employee_id):
    if not session.get('employee_id'):
        return redirect(url_for('login'))
    current_employee_id = session.get('employee_id')
    is_admin = session.get('is_admin')
    if not is_admin and current_employee_id != employee_id:
        return redirect(url_for('login'))

    employee = db.session.get(Employee, employee_id)
    if not employee:
        return redirect(url_for('admin_dashboard'))

    ref_team_id = request.args.get('ref_team_id')

    try:
        current_date_str = request.args.get('date', datetime.date.today().strftime('%Y-%m-%d'))
        current_date = datetime.datetime.strptime(current_date_str, '%Y-%m-%d').date()
    except ValueError:
        current_date = datetime.date.today()

    next_month = (current_date.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    prev_month = current_date.replace(day=1) - datetime.timedelta(days=1)

    start_of_calendar_view, end_of_calendar_view = get_calendar_view_range(current_date)
    shifts = Shift.query.filter_by(employee_id=employee_id) \
                        .filter(Shift.date >= start_of_calendar_view, Shift.date <= end_of_calendar_view) \
                        .all()
    schedule_data = {s.date.strftime('%Y-%m-%d'): s.shift_type for s in shifts}

    calendar_dates = []
    d = start_of_calendar_view
    while d <= end_of_calendar_view:
        calendar_dates.append(d)
        d += datetime.timedelta(days=1)

    return render_template('employee_calendar.html',
                           employee=employee,
                           schedule_data=schedule_data,
                           calendar_dates=calendar_dates,
                           current_date=current_date,
                           prev_month=prev_month,
                           next_month=next_month,
                           is_admin=is_admin,
                           ref_team_id=ref_team_id)

# --- Update Shifts (multi-date, single employee) ---

@app.route('/api/update_employee_shift', methods=['POST'])
def update_employee_shift():
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    try:
        data = request.json
        employee_id = data['employee_id']
        dates = data['dates']
        shift_type = data['shift_type']

        results = []
        for date_str in dates:
            date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            if shift_type == 'O':
                valid, msg = validate_shift(employee_id, date, shift_type)
                if not valid:
                    results.append({'date': date_str, 'status': 'warning', 'message': msg})
                    continue
            with db.session.no_autoflush:
                existing = Shift.query.filter_by(employee_id=employee_id, date=date).first()
            if shift_type == '':
                if existing:
                    db.session.delete(existing)
                results.append({'date': date_str, 'status': 'success'})
                continue
            if existing:
                existing.shift_type = shift_type
            else:
                db.session.add(Shift(employee_id=employee_id, date=date, shift_type=shift_type))
            results.append({'date': date_str, 'status': 'success'})

        db.session.commit()
        success_count = sum(1 for r in results if r['status'] == 'success')
        fail_count = len(results) - success_count
        if success_count == 0 and fail_count > 0:
            first_msg = next((r['message'] for r in results if r.get('message')), 'Shift limit reached')
            return jsonify({'status': 'warning', 'message': first_msg, 'results': results})
        if fail_count > 0:
            return jsonify({'status': 'warning', 'message': f'{success_count} shift(s) updated, {fail_count} skipped — max 2 OFF per week reached', 'results': results})
        return jsonify({'status': 'success', 'message': f'{success_count} shifts updated', 'results': results})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

# --- Update Roster Shifts (multi-employee + multi-date) ---

@app.route('/api/update_roster_shifts', methods=['POST'])
def update_roster_shifts():
    """Used by monthly/weekly roster tables. Accepts list of {employee_id, date} pairs."""
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    try:
        data = request.json
        shift_type = data['shift_type']
        cells = data['cells']  # [{employee_id, date}, ...]

        results = []
        for cell in cells:
            employee_id = cell['employee_id']
            date_str = cell['date']
            date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
            with db.session.no_autoflush:
                existing = Shift.query.filter_by(employee_id=employee_id, date=date).first()
            if shift_type == '':
                if existing:
                    db.session.delete(existing)
                results.append({'cell': cell, 'status': 'success'})
                continue
            if existing:
                existing.shift_type = shift_type
            else:
                db.session.add(Shift(employee_id=employee_id, date=date, shift_type=shift_type))
            results.append({'cell': cell, 'status': 'success'})

        db.session.commit()
        success_count = sum(1 for r in results if r['status'] == 'success')
        return jsonify({'status': 'success', 'message': f'{success_count} shifts updated', 'results': results})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

# --- Monthly Roster ---

@app.route('/team_dashboard/<int:team_id>')
def team_dashboard(team_id):
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    team = db.session.get(Team, team_id)

    try:
        current_date_str = request.args.get('date', datetime.date.today().strftime('%Y-%m-%d'))
        current_date = datetime.datetime.strptime(current_date_str, '%Y-%m-%d').date()
    except ValueError:
        current_date = datetime.date.today()

    start_of_month = current_date.replace(day=1)
    if start_of_month.month == 12:
        next_month_start = start_of_month.replace(year=start_of_month.year + 1, month=1)
    else:
        next_month_start = start_of_month.replace(month=start_of_month.month + 1)
    end_of_month = next_month_start - datetime.timedelta(days=1)

    month_dates = []
    d = start_of_month
    while d <= end_of_month:
        month_dates.append(d)
        d += datetime.timedelta(days=1)

    employees = get_employees_for_team(team_id)
    all_shifts = Shift.query.filter(
        Shift.employee_id.in_([e.id for e in employees]),
        Shift.date >= start_of_month,
        Shift.date <= end_of_month
    ).all()

    schedule_data = {e.id: {} for e in employees}
    for s in all_shifts:
        schedule_data[s.employee_id][s.date.strftime('%Y-%m-%d')] = s.shift_type

    return render_template('team_dashboard.html',
                           team=team,
                           employees=employees,
                           month_dates=month_dates,
                           schedule_data=schedule_data,
                           current_date=current_date,
                           next_month=next_month_start,
                           prev_month=(start_of_month - datetime.timedelta(days=1)).replace(day=1))

# --- Weekly Roster ---

@app.route('/weekly_roster/<int:team_id>')
def weekly_roster(team_id):
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    team = db.session.get(Team, team_id)
    if not team:
        return redirect(url_for('admin_dashboard'))

    try:
        current_date_str = request.args.get('date', datetime.date.today().strftime('%Y-%m-%d'))
        current_date = datetime.datetime.strptime(current_date_str, '%Y-%m-%d').date()
    except ValueError:
        current_date = datetime.date.today()

    start_of_week, end_of_week = get_week_range(current_date)
    week_dates = [start_of_week + datetime.timedelta(days=i) for i in range(7)]

    employees = get_employees_for_team(team_id)
    all_shifts = Shift.query.filter(
        Shift.employee_id.in_([e.id for e in employees]),
        Shift.date >= start_of_week,
        Shift.date <= end_of_week
    ).all()

    schedule_data = {e.id: {} for e in employees}
    for s in all_shifts:
        schedule_data[s.employee_id][s.date.strftime('%Y-%m-%d')] = s.shift_type

    return render_template('weekly_roster.html',
                           team=team,
                           employees=employees,
                           week_dates=week_dates,
                           schedule_data=schedule_data,
                           current_date=current_date,
                           start_of_week=start_of_week,
                           end_of_week=end_of_week,
                           prev_week=start_of_week - datetime.timedelta(days=7),
                           next_week=start_of_week + datetime.timedelta(days=7))

# --- Single Shift Update (legacy) ---

@app.route('/api/update_shift', methods=['POST'])
def update_shift():
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    try:
        data = request.json
        employee_id = data['employee_id']
        date = datetime.datetime.strptime(data['date'], '%Y-%m-%d').date()
        shift_type = data['shift_type']
        valid, message = validate_shift(employee_id, date, shift_type)
        if not valid:
            return jsonify({'status': 'warning', 'message': message})
        existing = Shift.query.filter_by(employee_id=employee_id, date=date).first()
        if existing:
            if shift_type == 'O':
                db.session.delete(existing)
            else:
                existing.shift_type = shift_type
        elif shift_type != 'O':
            db.session.add(Shift(employee_id=employee_id, date=date, shift_type=shift_type))
        db.session.commit()
        return jsonify({'status': 'success', 'message': 'Shift updated'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

# --- Team & Employee Management ---

@app.route('/create_team', methods=['POST'])
def create_team():
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    team_name = request.form.get('team_name', '').strip()
    if not team_name:
        return jsonify({'status': 'warning', 'message': 'Team name required.'})
    if Team.query.filter_by(name=team_name).first():
        return jsonify({'status': 'warning', 'message': f"Team '{team_name}' already exists."})
    db.session.add(Team(name=team_name))
    db.session.commit()
    return jsonify({'status': 'success', 'message': f"Team '{team_name}' created."})

@app.route('/create_employee', methods=['POST'])
def create_employee():
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    password = 'password123'  # default password
    team_ids = request.form.getlist('team_ids')  # multiple teams

    if not all([name, email]):
        return jsonify({'status': 'warning', 'message': 'Name, email and password are required.'})
    if Employee.query.filter_by(email=email).first():
        return jsonify({'status': 'warning', 'message': f"Employee '{email}' already exists."})

    new_emp = Employee(name=name, email=email, phone=phone or None, password=password)
    db.session.add(new_emp)
    db.session.flush()

    for tid in team_ids:
        team = db.session.get(Team, int(tid))
        if team:
            new_emp.teams.append(team)

    db.session.commit()
    return jsonify({'status': 'success', 'message': f"Employee '{name}' created."})

@app.route('/api/assign_teams/<int:employee_id>', methods=['POST'])
def assign_teams(employee_id):
    """Add an employee to additional teams."""
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    employee = db.session.get(Employee, employee_id)
    if not employee:
        return jsonify({'status': 'error', 'message': 'Employee not found'}), 404
    team_ids = request.json.get('team_ids', [])
    for tid in team_ids:
        team = db.session.get(Team, int(tid))
        if team and team not in employee.teams:
            employee.teams.append(team)
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'Teams assigned.'})

@app.route('/delete_team/<int:team_id>', methods=['POST'])
def delete_team(team_id):
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    team = db.session.get(Team, team_id)
    if not team:
        return jsonify({'status': 'error', 'message': 'Team not found'}), 404
    team_name = team.name
    team.members.clear()
    db.session.delete(team)
    db.session.commit()
    return jsonify({'status': 'success', 'message': f"Team '{team_name}' deleted."})

@app.route('/delete_employee/<int:employee_id>', methods=['POST'])
def delete_employee(employee_id):
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    employee = db.session.get(Employee, employee_id)
    if not employee:
        return jsonify({'status': 'error', 'message': 'Employee not found'}), 404
    emp_name = employee.name
    Shift.query.filter_by(employee_id=employee_id).delete()
    employee.teams.clear()
    db.session.delete(employee)
    db.session.commit()
    return jsonify({'status': 'success', 'message': f"Employee '{emp_name}' deleted."})

# --- Shift Templates ---

@app.route('/api/shift_templates', methods=['GET'])
def get_shift_templates():
    templates = ShiftTemplate.query.order_by(ShiftTemplate.name).all()
    return jsonify([{
        'id': t.id, 'name': t.name,
        'pattern': {'mon': t.mon, 'tue': t.tue, 'wed': t.wed,
                    'thu': t.thu, 'fri': t.fri, 'sat': t.sat, 'sun': t.sun}
    } for t in templates])

@app.route('/api/shift_templates', methods=['POST'])
def create_shift_template():
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'status': 'warning', 'message': 'Template name is required.'})
    if ShiftTemplate.query.filter_by(name=name).first():
        return jsonify({'status': 'warning', 'message': f"Template '{name}' already exists."})
    valid_shifts = {'M', 'A', 'N', 'O', 'L', 'G', '', None}
    t = ShiftTemplate(
        name=name,
        mon=data.get('mon') or None,
        tue=data.get('tue') or None,
        wed=data.get('wed') or None,
        thu=data.get('thu') or None,
        fri=data.get('fri') or None,
        sat=data.get('sat') or None,
        sun=data.get('sun') or None,
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({'status': 'success', 'message': f"Template '{name}' saved.", 'id': t.id})

@app.route('/api/shift_templates/<int:template_id>', methods=['DELETE'])
def delete_shift_template(template_id):
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    t = db.session.get(ShiftTemplate, template_id)
    if not t:
        return jsonify({'status': 'error', 'message': 'Template not found'}), 404
    db.session.delete(t)
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'Template deleted.'})

@app.route('/api/apply_template', methods=['POST'])
def apply_template():
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    data = request.json
    template = db.session.get(ShiftTemplate, data.get('template_id'))
    if not template:
        return jsonify({'status': 'error', 'message': 'Template not found'}), 404
    employee_id = data.get('employee_id')
    try:
        start = datetime.datetime.strptime(data['start_date'], '%Y-%m-%d').date()
        end   = datetime.datetime.strptime(data['end_date'],   '%Y-%m-%d').date()
    except (KeyError, ValueError):
        return jsonify({'status': 'error', 'message': 'Invalid date range.'}), 400
    if end < start:
        return jsonify({'status': 'warning', 'message': 'End date must be after start date.'})
    # weekday() returns 0=Mon … 6=Sun
    day_map = {0: template.mon, 1: template.tue, 2: template.wed, 3: template.thu,
               4: template.fri, 5: template.sat, 6: template.sun}
    updated = skipped = 0
    current = start
    while current <= end:
        shift_type = day_map.get(current.weekday())
        if shift_type:
            if shift_type == 'O':
                valid, _ = validate_shift(employee_id, current, shift_type)
                if not valid:
                    skipped += 1
                    current += datetime.timedelta(days=1)
                    continue
            existing = Shift.query.filter_by(employee_id=employee_id, date=current).first()
            if existing:
                existing.shift_type = shift_type
            else:
                db.session.add(Shift(employee_id=employee_id, date=current, shift_type=shift_type))
            updated += 1
        current += datetime.timedelta(days=1)
    db.session.commit()
    msg = f'{updated} shift(s) applied from template "{template.name}"'
    if skipped:
        msg += f', {skipped} skipped (max 2 OFF/week reached)'
    return jsonify({'status': 'success', 'message': msg})

@app.route('/edit_team/<int:team_id>', methods=['POST'])
def edit_team(team_id):
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    team = db.session.get(Team, team_id)
    if not team:
        return jsonify({'status': 'error', 'message': 'Team not found'}), 404
    new_name = request.json.get('name', '').strip()
    if not new_name:
        return jsonify({'status': 'warning', 'message': 'Team name required.'})
    if Team.query.filter(Team.name == new_name, Team.id != team_id).first():
        return jsonify({'status': 'warning', 'message': f"Team '{new_name}' already exists."})
    team.name = new_name
    db.session.commit()
    return jsonify({'status': 'success', 'message': f"Team renamed to '{new_name}'."})

@app.route('/edit_employee/<int:employee_id>', methods=['POST'])
def edit_employee_route(employee_id):
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403
    employee = db.session.get(Employee, employee_id)
    if not employee:
        return jsonify({'status': 'error', 'message': 'Employee not found'}), 404
    data = request.json
    new_name = data.get('name', '').strip()
    new_email = data.get('email', '').strip()
    new_phone = data.get('phone', '').strip()
    if not new_name or not new_email:
        return jsonify({'status': 'warning', 'message': 'Name and email are required.'})
    if Employee.query.filter(Employee.email == new_email, Employee.id != employee_id).first():
        return jsonify({'status': 'warning', 'message': f"Email '{new_email}' is already in use."})
    employee.name = new_name
    employee.email = new_email
    employee.phone = new_phone or None
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'Employee updated successfully.'})

# --- Employee Dashboard ---

@app.route('/employee_dashboard')
def employee_dashboard():
    employee_id = session.get('employee_id')
    if not employee_id:
        return redirect(url_for('login'))
    employee = db.session.get(Employee, employee_id)
    start_of_week, end_of_week = get_week_range()
    dates = [start_of_week + datetime.timedelta(days=i) for i in range(7)]
    shifts = Shift.query.filter_by(employee_id=employee_id) \
                        .filter(Shift.date >= start_of_week, Shift.date <= end_of_week).all()
    schedule_data = {s.date: s.shift_type for s in shifts}
    return render_template('employee_dashboard.html', employee=employee, dates=dates, schedule_data=schedule_data)

@app.route('/all_employees')
def all_employees():
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    
    search = request.args.get('search', '').lower()
    # Query only non-admin employees
    query = Employee.query.filter_by(is_admin=False) 
    
    if search:
        query = query.filter(
            (Employee.name.contains(search)) | 
            (Employee.email.contains(search))
        )
    employees = query.order_by(Employee.name).all()
    return render_template('all_employees.html', employees=employees, search=search)
# --- Send Schedules (with roster image) ---

@app.route('/send_schedules', methods=['POST'])
def send_schedules():
    if not session.get('is_admin'):
        return jsonify({'status': 'error', 'message': 'Permission denied'}), 403

    # Determine context: weekly or monthly, and which team
    mode = request.json.get('mode', 'weekly') if request.is_json else request.form.get('mode', 'weekly')
    team_id = request.json.get('team_id') if request.is_json else request.form.get('team_id')

    try:
        today = datetime.date.today()

        if mode == 'weekly':
            start, end = get_week_range(today)
            dates = [start + datetime.timedelta(days=i) for i in range(7)]
            period_label = f"Week of {start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
        else:
            start = today.replace(day=1)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1) - datetime.timedelta(days=1)
            else:
                end = start.replace(month=start.month + 1) - datetime.timedelta(days=1)
            dates = []
            d = start
            while d <= end:
                dates.append(d)
                d += datetime.timedelta(days=1)
            period_label = today.strftime('%B %Y')

        team_name = ''
        if team_id:
            team_obj = db.session.get(Team, int(team_id))
            team_name = team_obj.name if team_obj else ''
            employees = get_employees_for_team(int(team_id))
        else:
            employees = Employee.query.filter_by(is_admin=False).all()

        all_shifts = Shift.query.filter(
            Shift.employee_id.in_([e.id for e in employees]),
            Shift.date >= start,
            Shift.date <= end
        ).all()

        schedule_data = {e.id: {} for e in employees}
        for s in all_shifts:
            schedule_data[s.employee_id][s.date.strftime('%Y-%m-%d')] = s.shift_type

        mode_label = 'Weekly' if mode == 'weekly' else 'Monthly'
        title = f"{mode_label} Roster – {period_label}"
        image_bytes = generate_roster_image(
            employees, dates, schedule_data, title,
            team_name=team_name, period_label=period_label
        )

        sent = 0
        errors = []
        for emp in employees:
            today_shift = schedule_data.get(emp.id, {}).get(today.strftime('%Y-%m-%d'), '')
            today_line = ''
            if today_shift:
                today_line = f"\nToday ({today.strftime('%A, %d %b')}) you are assigned: {SHIFT_LABELS.get(today_shift, today_shift)} shift."

            body = (
                f"Hi {emp.name},\n\n"
                f"Please find your {mode_label.lower()} shift roster for {period_label}.\n"
                f"{today_line}\n\n"
                f"Shift legend:  M=Morning  A=Afternoon  N=Night  L=Leave  O=Off  G=General\n\n"
                f"Regards,\nAdmin"
            )
            subject = f"Your {mode_label} Shift Roster – {period_label}"
            try:
                log.info("Sending roster email to %s (%s)", emp.name, emp.email)
                send_roster_email(emp, image_bytes, subject, body,
                                  team_name=team_name, period_label=period_label)
                sent += 1
                log.info("✓ Email sent to %s", emp.email)
            except Exception as email_err:
                err_detail = f"{type(email_err).__name__}: {email_err}"
                log.error("✗ Failed to send to %s — %s", emp.email, err_detail)
                tb.print_exc()
                errors.append({'employee': emp.name, 'email': emp.email, 'error': err_detail})

        if errors:
            first_err = errors[0]['error']
            return jsonify({
                'status': 'error',
                'message': f'Sent {sent}, failed {len(errors)}. First error: {first_err}',
                'errors': errors,
            }), 500

        return jsonify({'status': 'success', 'message': f'Roster email sent to {sent} employees.'})

    except Exception as e:
        tb.print_exc()
        err_detail = f"{type(e).__name__}: {e}"
        log.error("send_schedules top-level error: %s", err_detail)
        return jsonify({'status': 'error', 'message': err_detail}), 500

# --- App Initialization ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Create default admin user if it doesn't exist
        if not Employee.query.filter_by(email="admin@exmaplegmail.com").first():
            admin = Employee(
                name="Admin User",
                email="admin@exmaplegmail.com",
                is_admin=True,
                password="admin123"
            )
            db.session.add(admin)
            db.session.commit()
        
        # Migrate legacy team_id
        for emp in Employee.query.all():
            if emp.team_id and len(emp.teams) == 0:
                team = db.session.get(Team, emp.team_id)
                if team:
                    emp.teams.append(team)
        db.session.commit()
    
    app.run(debug=True)
# --- App Initialization (Move this OUTSIDE of the if __name__ block) ---
with app.app_context():
    db.create_all()
    # Check if admin exists, if not create them
    if not Employee.query.filter_by(email="admin@exmaplegmail.com").first():
        admin = Employee(
            name="Admin User",
            email="admin@exmaplegmail.com",
            is_admin=True,
            password="admin123" # Added a default password for you
        )
        db.session.add(admin)
        db.session.commit()