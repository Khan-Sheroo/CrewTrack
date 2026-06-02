from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, time, timedelta
from werkzeug.security import check_password_hash, generate_password_hash
import hashlib
import json
import logging
import os
import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-before-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///cleaning_logs.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['PASSWORD_RESET_EXPIRY_HOURS'] = int(os.environ.get('PASSWORD_RESET_EXPIRY_HOURS', '1'))
app.config['APP_BASE_URL'] = (os.environ.get('APP_BASE_URL') or '').rstrip('/')
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', '')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', '1').lower() in ('1', 'true', 'yes')
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_FROM'] = os.environ.get('MAIL_FROM', '') or os.environ.get('MAIL_USERNAME', '')

db = SQLAlchemy(app)
logger = logging.getLogger(__name__)

# Database Models
class Cleaner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=True)  # e.g. Bartenders, Barbacks, Runners, Waiters, Manager
    rate_type = db.Column(db.String(20), nullable=True, default='monthly')
    rate_amount = db.Column(db.Float, nullable=True)
    flat_monthly = db.Column(db.Boolean, default=False, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)  # False = archived from hours (no new shifts)
    time_logs = db.relationship('TimeLog', backref='cleaner', lazy=True)

    __table_args__ = (
        db.UniqueConstraint('owner_id', 'name', name='uq_cleaner_owner_name'),
    )

class TimeLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cleaner_id = db.Column(db.Integer, db.ForeignKey('cleaner.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    log_type = db.Column(db.String(20), nullable=False, default='shift')
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)
    hours_worked = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    cleaners = db.relationship('Cleaner', backref='owner', lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class PasswordResetToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship('User', backref=db.backref('password_reset_tokens', lazy=True))


def normalize_email(raw_email: str) -> str | None:
    """Normalize and validate an email address."""
    email = (raw_email or '').strip().lower()
    if not email or '@' not in email or len(email) > 255:
        return None
    return email


def get_current_user():
    """Return the logged-in user or None."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    return db.session.get(User, user_id)


def validate_password(password: str) -> str | None:
    """Return an error message if the password is invalid, else None."""
    if len(password) < 8:
        return 'Password must be at least 8 characters.'
    return None


def _hash_reset_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def mail_is_configured() -> bool:
    return bool(app.config['MAIL_SERVER'] and app.config['MAIL_FROM'])


def get_app_base_url() -> str:
    """Public base URL for links in emails (falls back to the current request host)."""
    configured = app.config.get('APP_BASE_URL') or ''
    if configured:
        return configured.rstrip('/')
    if request:
        return request.host_url.rstrip('/')
    return 'http://127.0.0.1:5000'


def build_password_reset_url(raw_token: str) -> str:
    base = get_app_base_url()
    with app.test_request_context(base_url=f"{base}/"):
        return url_for('reset_password', token=raw_token, _external=True)


def create_password_reset_token(user: User) -> str:
    """Create a one-time reset token and return the raw value for the reset link."""
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).delete(
        synchronize_session=False
    )
    raw_token = secrets.token_urlsafe(32)
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_reset_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(hours=app.config['PASSWORD_RESET_EXPIRY_HOURS']),
    )
    db.session.add(reset)
    db.session.commit()
    return raw_token


def get_user_for_reset_token(raw_token: str) -> User | None:
    """Return the user for a valid, unused reset token."""
    if not raw_token:
        return None
    record = PasswordResetToken.query.filter_by(
        token_hash=_hash_reset_token(raw_token),
        used_at=None,
    ).first()
    if not record or record.expires_at < datetime.utcnow():
        return None
    return db.session.get(User, record.user_id)


def mark_reset_token_used(raw_token: str) -> None:
    record = PasswordResetToken.query.filter_by(
        token_hash=_hash_reset_token(raw_token),
        used_at=None,
    ).first()
    if record:
        record.used_at = datetime.utcnow()
        db.session.commit()


def send_password_reset_email(user: User, reset_url: str) -> bool:
    """Send the password reset email. Returns True when sent."""
    if not mail_is_configured():
        return False

    subject = 'Reset your CrewTrack password'
    text_body = (
        f"Hello,\n\n"
        f"We received a request to reset the password for your CrewTrack account ({user.email}).\n\n"
        f"Open this link to choose a new password (expires in "
        f"{app.config['PASSWORD_RESET_EXPIRY_HOURS']} hour(s)):\n\n"
        f"{reset_url}\n\n"
        f"If you did not request this, you can ignore this email.\n"
    )
    html_body = (
        f"<p>Hello,</p>"
        f"<p>We received a request to reset the password for your CrewTrack account "
        f"(<strong>{user.email}</strong>).</p>"
        f"<p><a href=\"{reset_url}\">Reset your password</a> "
        f"(link expires in {app.config['PASSWORD_RESET_EXPIRY_HOURS']} hour(s)).</p>"
        f"<p>If you did not request this, you can ignore this email.</p>"
    )

    message = MIMEMultipart('alternative')
    message['Subject'] = subject
    message['From'] = app.config['MAIL_FROM']
    message['To'] = user.email
    message.attach(MIMEText(text_body, 'plain'))
    message.attach(MIMEText(html_body, 'html'))

    with smtplib.SMTP(app.config['MAIL_SERVER'], app.config['MAIL_PORT']) as smtp:
        if app.config['MAIL_USE_TLS']:
            smtp.starttls()
        if app.config['MAIL_USERNAME']:
            smtp.login(app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'])
        smtp.sendmail(app.config['MAIL_FROM'], [user.email], message.as_string())
    return True


def initiate_password_reset(email: str) -> None:
    """Start a password reset if the account exists (always appears successful to the caller)."""
    user = User.query.filter_by(email=email).first()
    if not user:
        return

    raw_token = create_password_reset_token(user)
    reset_url = build_password_reset_url(raw_token)

    if send_password_reset_email(user, reset_url):
        return

    logger.warning(
        'Password reset for %s (email not configured). Reset link: %s',
        user.email,
        reset_url,
    )


def get_current_owner_id() -> int | None:
    """Return the current tenant (account) id."""
    user = get_current_user()
    return user.id if user else None


def cleaners_query():
    """Staff query scoped to the logged-in account."""
    owner_id = get_current_owner_id()
    if owner_id is None:
        return Cleaner.query.filter(db.false())
    return Cleaner.query.filter_by(owner_id=owner_id)


def timelogs_query():
    """Time log query scoped to the logged-in account."""
    owner_id = get_current_owner_id()
    if owner_id is None:
        return TimeLog.query.filter(db.false())
    return TimeLog.query.join(Cleaner).filter(Cleaner.owner_id == owner_id)


def get_cleaner_for_owner(cleaner_id: int, owner_id: int | None = None):
    """Return a staff member if it belongs to the current account."""
    owner_id = owner_id if owner_id is not None else get_current_owner_id()
    if owner_id is None:
        return None
    return cleaners_query().filter_by(id=cleaner_id).first()


def get_time_log_for_owner(log_id: int):
    """Return a log entry if it belongs to the current account."""
    return timelogs_query().filter(TimeLog.id == log_id).first()


def get_tenant_log_year_month_options() -> tuple[list[int], list[int]]:
    """Distinct years and months from logs for the current account."""
    owner_id = get_current_owner_id()
    year_query = db.session.query(db.func.strftime('%Y', TimeLog.date).label('year'))
    month_query = db.session.query(db.func.strftime('%m', TimeLog.date).label('month'))
    if owner_id is not None:
        year_query = year_query.join(Cleaner, TimeLog.cleaner_id == Cleaner.id).filter(
            Cleaner.owner_id == owner_id
        )
        month_query = month_query.join(Cleaner, TimeLog.cleaner_id == Cleaner.id).filter(
            Cleaner.owner_id == owner_id
        )
    else:
        year_query = year_query.filter(db.false())
        month_query = month_query.filter(db.false())

    years = [
        int(row.year)
        for row in year_query.distinct().order_by(db.func.strftime('%Y', TimeLog.date).desc()).all()
        if row.year is not None
    ]
    months = [
        int(row.month)
        for row in month_query.distinct().order_by(db.func.strftime('%m', TimeLog.date)).all()
        if row.month is not None
    ]
    return years, months


DEFAULT_STAFF_CATEGORIES = [
    "Bartenders", "Barbacks", "Waiters", "Runners", "Manager", "Retail"
]

DISPLAY_CATEGORY_GROUPS = {
    "Bar": ["Bartenders", "Barbacks"],
    "Waiters": ["Waiters"],
    "Runners": ["Runners"],
    "Manager": ["Manager"],
    "Retail": ["Retail"],
}

MONTHLY_REPORT_CATEGORY_ORDER = [
    'Headbartenders', 'Bartenders', 'Barbacks', 'Waiters', 'Runners', 'Manager', 'Retail'
]


def normalize_category_name(raw_category: str) -> str | None:
    """Normalize a staff category name from form input."""
    if not raw_category:
        return None
    category = ' '.join(raw_category.strip().split())
    if not category:
        return None
    return category[:50]


def get_all_staff_categories() -> list:
    """Return default categories plus any custom ones already in use for this account."""
    owner_id = get_current_owner_id()
    query = db.session.query(Cleaner.category).filter(
        Cleaner.category.isnot(None),
        Cleaner.category != ''
    )
    if owner_id is not None:
        query = query.filter(Cleaner.owner_id == owner_id)
    db_categories = {
        row[0] for row in query.distinct().all()
        if row[0]
    }
    categories = set(DEFAULT_STAFF_CATEGORIES)
    categories.update(db_categories)
    return sorted(categories, key=lambda name: (name not in DEFAULT_STAFF_CATEGORIES, name.lower()))


def resolve_display_group(staff_category: str) -> str:
    """Map a stored staff category to the display group used in reports."""
    if not staff_category:
        return "Uncategorized"
    for group_name, categories in DISPLAY_CATEGORY_GROUPS.items():
        if staff_category in categories:
            return group_name
    return staff_category


def sort_display_categories(category_names) -> list:
    """Return display categories in a stable order with custom groups at the end."""
    preferred = list(DISPLAY_CATEGORY_GROUPS.keys())
    category_set = set(category_names)
    order = [name for name in preferred if name in category_set]
    order.extend(sorted(name for name in category_set if name not in preferred and name != "Uncategorized"))
    if "Uncategorized" in category_set:
        order.append("Uncategorized")
    return order


def resolve_staff_category_from_form(category_field: str, new_category_field: str) -> str | None:
    """Resolve the category submitted from the add-staff form."""
    if category_field == '__new__':
        return normalize_category_name(new_category_field)
    return normalize_category_name(category_field)

# Helper Functions
def normalize_log_type(log_type: str) -> str:
    """Normalize a log type to a supported value."""
    return 'time' if log_type == 'time' else 'shift'


def parse_time_value(value: str):
    """Parse an HH:MM string into a time object."""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%H:%M').time()
    except (ValueError, TypeError):
        return None


def calculate_hours_worked(start_time: time, end_time: time) -> float:
    """Return the duration in hours, supporting overnight shifts."""
    start_dt = datetime.combine(date.today(), start_time)
    end_dt = datetime.combine(date.today(), end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    duration_hours = (end_dt - start_dt).total_seconds() / 3600
    return round(duration_hours, 2)


def get_log_hours(log: "TimeLog") -> float:
    """Return hours for a time entry, or 0 for shift entries."""
    return float(log.hours_worked or 0)


SUNDAY_HOURLY_MULTIPLIER = 1.5
PUBLIC_HOLIDAY_HOURLY_MULTIPLIER = 2.0

_SA_PUBLIC_HOLIDAY_CACHE: dict[int, set[date]] = {}


def calculate_western_easter(year: int) -> date:
    """Return Easter Sunday for a given year."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def get_sa_public_holidays(year: int) -> set[date]:
    """Return South African public holidays for a year, including observed Mondays."""
    holidays: set[date] = set()

    def add_holiday(holiday_date: date) -> None:
        holidays.add(holiday_date)
        if holiday_date.weekday() == 6:
            holidays.add(holiday_date + timedelta(days=1))

    for month, day in (
        (1, 1),    # New Year's Day
        (3, 21),   # Human Rights Day
        (4, 27),   # Freedom Day
        (5, 1),    # Workers' Day
        (6, 16),   # Youth Day
        (8, 9),    # National Women's Day
        (9, 24),   # Heritage Day
        (12, 16),  # Day of Reconciliation
        (12, 25),  # Christmas Day
        (12, 26),  # Day of Goodwill
    ):
        add_holiday(date(year, month, day))

    easter_sunday = calculate_western_easter(year)
    add_holiday(easter_sunday - timedelta(days=2))  # Good Friday
    add_holiday(easter_sunday + timedelta(days=1))  # Family Day

    return holidays


def is_sa_public_holiday(work_date: date) -> bool:
    """Return True when a date is a South African public holiday."""
    year = work_date.year
    if year not in _SA_PUBLIC_HOLIDAY_CACHE:
        _SA_PUBLIC_HOLIDAY_CACHE[year] = get_sa_public_holidays(year)
    return work_date in _SA_PUBLIC_HOLIDAY_CACHE[year]


def is_sunday(work_date: date) -> bool:
    """Return True when a date falls on Sunday."""
    return work_date.weekday() == 6


def get_hourly_pay_multiplier(work_date: date) -> float:
    """Return the hourly pay multiplier for a work date."""
    if is_sa_public_holiday(work_date):
        return PUBLIC_HOLIDAY_HOURLY_MULTIPLIER
    if is_sunday(work_date):
        return SUNDAY_HOURLY_MULTIPLIER
    return 1.0


def split_log_hours(log: "TimeLog") -> tuple[float, float, float]:
    """Return regular, Sunday, and public holiday hours tracked in a time log."""
    if normalize_log_type(log.log_type) != 'time':
        return 0.0, 0.0, 0.0
    hours = get_log_hours(log)
    if is_sa_public_holiday(log.date):
        return 0.0, 0.0, hours
    if is_sunday(log.date):
        return 0.0, hours, 0.0
    return hours, 0.0, 0.0


def calculate_hourly_pay(
    regular_hours: float,
    sunday_hours: float,
    public_holiday_hours: float,
    rate_amount: float
) -> float:
    """Calculate hourly pay with Sunday and public holiday premiums."""
    return round(
        rate_amount * (
            regular_hours
            + sunday_hours * SUNDAY_HOURLY_MULTIPLIER
            + public_holiday_hours * PUBLIC_HOLIDAY_HOURLY_MULTIPLIER
        ),
        2
    )


def format_hourly_rate_label(
    rate_amount: float,
    include_sunday_rate: bool = False,
    include_public_holiday_rate: bool = False
) -> str:
    """Format an hourly rate, optionally including premium rates."""
    extras = []
    if include_sunday_rate:
        extras.append(f"Sun R{round(rate_amount * SUNDAY_HOURLY_MULTIPLIER, 2):.2f}/hr")
    if include_public_holiday_rate:
        extras.append(f"PH R{round(rate_amount * PUBLIC_HOLIDAY_HOURLY_MULTIPLIER, 2):.2f}/hr")
    base_label = format_rate_label('hourly', rate_amount)
    if extras:
        return f"{base_label} ({', '.join(extras)})"
    return base_label


def ensure_time_log_schema() -> None:
    """Add newer time-tracking columns to the SQLite table if they are missing."""
    existing_tables = db.session.execute(
        db.text("SELECT name FROM sqlite_master WHERE type='table' AND name='time_log'")
    ).fetchall()
    if not existing_tables:
        return

    existing_columns = {
        row[1] for row in db.session.execute(db.text("PRAGMA table_info(time_log)")).fetchall()
    }

    if 'log_type' not in existing_columns:
        db.session.execute(
            db.text("ALTER TABLE time_log ADD COLUMN log_type VARCHAR(20) NOT NULL DEFAULT 'shift'")
        )
    if 'start_time' not in existing_columns:
        db.session.execute(db.text("ALTER TABLE time_log ADD COLUMN start_time TIME"))
    if 'end_time' not in existing_columns:
        db.session.execute(db.text("ALTER TABLE time_log ADD COLUMN end_time TIME"))
    if 'hours_worked' not in existing_columns:
        db.session.execute(db.text("ALTER TABLE time_log ADD COLUMN hours_worked FLOAT"))

    db.session.commit()


def ensure_cleaner_schema() -> None:
    """Add newer cleaner pay columns to the SQLite table if they are missing."""
    existing_tables = db.session.execute(
        db.text("SELECT name FROM sqlite_master WHERE type='table' AND name='cleaner'")
    ).fetchall()
    if not existing_tables:
        return

    existing_columns = {
        row[1] for row in db.session.execute(db.text("PRAGMA table_info(cleaner)")).fetchall()
    }

    if 'rate_type' not in existing_columns:
        db.session.execute(
            db.text("ALTER TABLE cleaner ADD COLUMN rate_type VARCHAR(20) DEFAULT 'monthly'")
        )
    if 'rate_amount' not in existing_columns:
        db.session.execute(db.text("ALTER TABLE cleaner ADD COLUMN rate_amount FLOAT"))
    if 'flat_monthly' not in existing_columns:
        db.session.execute(
            db.text("ALTER TABLE cleaner ADD COLUMN flat_monthly BOOLEAN NOT NULL DEFAULT 0")
        )

    db.session.execute(
        db.text("UPDATE cleaner SET category = 'Manager' WHERE category = 'Shop'")
    )

    if 'owner_id' not in existing_columns:
        db.session.execute(db.text("ALTER TABLE cleaner ADD COLUMN owner_id INTEGER"))
        first_user = db.session.query(User).order_by(User.id).first()
        if first_user:
            db.session.execute(
                db.text("UPDATE cleaner SET owner_id = :owner_id WHERE owner_id IS NULL"),
                {'owner_id': first_user.id},
            )

    db.session.commit()


def add_time_log(
    cleaner_id: int,
    date: date,
    notes: str = None,
    log_type: str = 'shift',
    start_time: time = None,
    end_time: time = None,
    hours_worked: float = None
) -> None:
    """Insert a new shift or time log entry."""
    cleaner = get_cleaner_for_owner(cleaner_id)
    if not cleaner:
        raise ValueError('Staff member not found for this account')

    new_log = TimeLog(
        cleaner_id=cleaner_id,
        date=date,
        log_type=normalize_log_type(log_type),
        start_time=start_time,
        end_time=end_time,
        hours_worked=hours_worked,
        notes=notes
    )
    db.session.add(new_log)
    db.session.commit()

def get_daily_logs(cleaner_id: int = None):
    """Retrieve daily logs (optionally filtered by cleaner)."""
    query = timelogs_query()
    if cleaner_id:
        query = query.filter(TimeLog.cleaner_id == cleaner_id)
    return query.order_by(TimeLog.date.desc()).all()


def get_time_log_by_id(log_id: int):
    """Retrieve a specific time log by ID for the current account."""
    return get_time_log_for_owner(log_id)

def get_monthly_rate(cleaner_name: str, cleaner_category: str, log_date: date = None) -> float:
    """Get monthly rate for a cleaner based on name and category.
    Returns monthly rate in the base currency.
    
    Args:
        cleaner_name: Name of the cleaner
        cleaner_category: Category of the cleaner
        log_date: Optional date of the log entry. If provided, used to determine date-based rates.
    """
    cleaner_name_upper = cleaner_name.upper()
    
    # Seth Elinam: 25000/month before January 2026, 6000/month (waiter rate) from January 2026 onwards
    if cleaner_name_upper.startswith('SETH ELINAM'):
        if log_date is not None:
            # From January 2026 onwards, Seth is on waiter rate
            if log_date >= date(2026, 1, 1):
                return 6000.0
        # Before January 2026, Seth makes 25000/month
        return 25000.0
    
    # Craig van der Lith, Sandra Viljoen, and Taylor Johanssen: 8000/month from January 2026 onwards
    special_waiters_8000 = ['CRAIG VAN DER LITH', 'SANDRA VILJOEN', 'TAYLOR JOHANSSEN']
    for waiter_name in special_waiters_8000:
        if cleaner_name_upper.startswith(waiter_name.upper()):
            if log_date is not None:
                # From January 2026 onwards, these waiters get 8000/month
                if log_date >= date(2026, 1, 1):
                    return 8000.0
            # Before January 2026, they get standard waiter rate
            return 6000.0
    
    # Head barman make 15000/month
    head_barman_names = ['EDSON', 'NICKI', 'COLLIN (bar)', 'COLLIN bar', 'MUKETIWA']
    
    # Check if name matches head barman (case-insensitive, supports surnames)
    for head_name in head_barman_names:
        if cleaner_name_upper.startswith(head_name.upper()):
            return 15000.0
    
    # Bartenders make 9000/month (excluding head barman)
    if cleaner_category == 'Bartenders':
        return 9000.0
    
    # Waiters, Runners, Barbacks, Managers, and Retail make 6000/month
    if cleaner_category in ['Waiters', 'Runners', 'Barbacks', 'Manager', 'Retail']:
        return 6000.0
    
    # Default rate for uncategorized
    return 0.0


def normalize_rate_type(rate_type: str) -> str:
    """Normalize a stored rate type to a supported value."""
    return rate_type if rate_type in {'hourly', 'daily', 'monthly'} else 'monthly'


def get_cleaner_rate_config(cleaner: "Cleaner", log_date: date = None):
    """Return the effective rate type and amount for a cleaner."""
    rate_type = normalize_rate_type(cleaner.rate_type)
    if cleaner.rate_amount is not None:
        return rate_type, float(cleaner.rate_amount)
    return 'monthly', float(get_monthly_rate(cleaner.name, cleaner.category, log_date))


def is_flat_monthly(cleaner: "Cleaner") -> bool:
    """Return True when a monthly-paid cleaner uses one shift for the full month."""
    return normalize_rate_type(cleaner.rate_type) == 'monthly' and bool(cleaner.flat_monthly)


def format_rate_label(rate_type: str, rate_amount: float, flat_monthly: bool = False) -> str:
    """Format a configured rate for display."""
    normalized_rate_type = normalize_rate_type(rate_type)
    suffix = {
        'hourly': '/hr',
        'daily': '/day',
        'monthly': '/month flat' if flat_monthly else '/month'
    }[normalized_rate_type]
    return f"R{rate_amount:.2f}{suffix}"


def calculate_period_total(
    rate_type: str,
    rate_amount: float,
    entry_count: int,
    hours_count: float,
    days_in_month: int,
    flat_monthly: bool = False,
    sunday_hours: float = 0.0,
    public_holiday_hours: float = 0.0
) -> float:
    """Calculate total pay for a period from the cleaner's pay basis."""
    normalized_rate_type = normalize_rate_type(rate_type)
    if normalized_rate_type == 'hourly':
        regular_hours = round(hours_count - sunday_hours - public_holiday_hours, 2)
        return calculate_hourly_pay(
            regular_hours,
            sunday_hours,
            public_holiday_hours,
            rate_amount
        )
    if normalized_rate_type == 'daily':
        return round(entry_count * rate_amount, 2)
    if flat_monthly:
        return round(rate_amount, 2) if entry_count >= 1 else 0.0
    return round(entry_count * (rate_amount / float(days_in_month)), 2)


def get_allowed_log_types_for_cleaner(cleaner: "Cleaner") -> frozenset:
    """Return log types a cleaner may use."""
    if normalize_rate_type(cleaner.rate_type) == 'hourly':
        return frozenset({'time'})
    if is_flat_monthly(cleaner):
        return frozenset({'shift', 'time'})
    return frozenset({'shift'})


def get_required_log_type_for_cleaner(cleaner: "Cleaner") -> str:
    """Return the log type to use when only one method is allowed."""
    allowed = get_allowed_log_types_for_cleaner(cleaner)
    if len(allowed) == 1:
        return next(iter(allowed))
    return 'time'


def get_rate_days_in_month(target_date: date) -> int:
    """Return the configured number of paid days in a month."""
    return 26 if target_date.month == 12 else 24


def build_invoice_line_item(cleaner: "Cleaner", log: "TimeLog") -> dict:
    """Build a single invoice line item from a log entry."""
    rate_type, rate_amount = get_cleaner_rate_config(cleaner, log.date)
    normalized_rate_type = normalize_rate_type(rate_type)
    time_window = None
    if log.start_time and log.end_time:
        time_window = f"{log.start_time.strftime('%H:%M')} - {log.end_time.strftime('%H:%M')}"

    if normalized_rate_type == 'hourly':
        quantity = get_log_hours(log)
        multiplier = get_hourly_pay_multiplier(log.date)
        effective_rate = round(rate_amount * multiplier, 2)
        if is_sa_public_holiday(log.date):
            rate_display = f"R{effective_rate:.2f}/hr (Public holiday 2x)"
            description = f"Public holiday time entry{f' ({time_window})' if time_window else ''}"
        elif is_sunday(log.date):
            rate_display = f"R{effective_rate:.2f}/hr (Sun 1.5x)"
            description = f"Sunday time entry{f' ({time_window})' if time_window else ''}"
        else:
            rate_display = format_rate_label(rate_type, rate_amount)
            description = f"Time entry{f' ({time_window})' if time_window else ''}"
        line_total = round(quantity * effective_rate, 2)
        quantity_display = f"{quantity:.2f} hrs"
    elif normalized_rate_type == 'daily':
        quantity = 1.0
        rate_display = format_rate_label(rate_type, rate_amount)
        line_total = round(rate_amount, 2)
        description = f"Worked day{f' ({time_window})' if time_window else ''}"
        quantity_display = "1 day"
    elif is_flat_monthly(cleaner):
        quantity = 1.0
        rate_display = format_rate_label(rate_type, rate_amount, flat_monthly=True)
        line_total = round(rate_amount, 2)
        month_name = log.date.strftime('%B %Y')
        if normalize_log_type(log.log_type) == 'time' and log.hours_worked:
            description = f"Hours worked - {log.hours_worked:.2f} hrs{f' ({time_window})' if time_window else ''}"
            quantity_display = f"{log.hours_worked:.2f} hrs"
        else:
            description = f"Monthly flat rate - {month_name}"
            quantity_display = "1 month"
    else:
        quantity = 1.0
        days_in_month = get_rate_days_in_month(log.date)
        daily_equivalent = round(rate_amount / float(days_in_month), 2)
        rate_display = f"R{daily_equivalent:.2f}/day from {format_rate_label(rate_type, rate_amount)}"
        line_total = daily_equivalent
        description = f"Worked day ({days_in_month}-day rule){f' ({time_window})' if time_window else ''}"
        quantity_display = "1 day"

    if log.notes:
        description = f"{description} - {log.notes}"

    return {
        'date': log.date,
        'description': description,
        'quantity': quantity,
        'quantity_display': quantity_display,
        'rate_display': rate_display,
        'amount': round(line_total, 2)
    }


def build_staff_invoice_data(cleaner: "Cleaner", date_from: date, date_to: date) -> dict:
    """Build printable invoice data for one staff member over a date range."""
    logs = timelogs_query().filter(
        TimeLog.cleaner_id == cleaner.id,
        TimeLog.date >= date_from,
        TimeLog.date <= date_to
    ).order_by(TimeLog.date.asc(), TimeLog.created_at.asc()).all()

    if is_flat_monthly(cleaner):
        months_seen = {}
        for log in logs:
            month_key = f"{log.date.year}-{log.date.month:02d}"
            if month_key not in months_seen:
                months_seen[month_key] = log
        line_items = [
            build_invoice_line_item(cleaner, months_seen[month_key])
            for month_key in sorted(months_seen.keys())
        ]
    else:
        line_items = [build_invoice_line_item(cleaner, log) for log in logs]
    total_hours = round(sum(get_log_hours(log) for log in logs), 2)
    total_amount = round(sum(item['amount'] for item in line_items), 2)
    rate_type, rate_amount = get_cleaner_rate_config(cleaner, date_from)

    return {
        'invoice_number': f"INV-{cleaner.id}-{date_from.strftime('%Y%m%d')}-{date_to.strftime('%Y%m%d')}",
        'issue_date': date.today(),
        'cleaner': cleaner,
        'period_start': date_from,
        'period_end': date_to,
        'line_items': line_items,
        'entries_count': len(logs),
        'hours_total': total_hours,
        'amount_total': total_amount,
        'rate_label': format_rate_label(
            rate_type,
            rate_amount,
            flat_monthly=is_flat_monthly(cleaner)
        ),
        'rate_type': normalize_rate_type(rate_type)
    }

def _filter_timelog_query(query, filter_year=None, filter_month=None, date_from=None, date_to=None):
    """Apply year/month or custom date range filters to a TimeLog query."""
    if date_from is not None or date_to is not None:
        if date_from is not None:
            query = query.filter(TimeLog.date >= date_from)
        if date_to is not None:
            query = query.filter(TimeLog.date <= date_to)
    elif filter_year is not None and filter_month is not None:
        from calendar import monthrange
        start_date = date(filter_year, filter_month, 1)
        end_date = date(filter_year, filter_month, monthrange(filter_year, filter_month)[1])
        query = query.filter(TimeLog.date >= start_date, TimeLog.date <= end_date)
    elif filter_year is not None:
        start_date = date(filter_year, 1, 1)
        end_date = date(filter_year, 12, 31)
        query = query.filter(TimeLog.date >= start_date, TimeLog.date <= end_date)
    elif filter_month is not None:
        query = query.filter(db.func.strftime('%m', TimeLog.date) == f"{filter_month:02d}")
    return query


def _calculate_category_totals(ordered_data, period_bucket_key):
    """Sum pay totals by report category from grouped staff period data."""
    category_totals = {
        'bartenders': 0.0,
        'waiters': 0.0,
        'runners': 0.0,
        'managers': 0.0,
        'retail': 0.0,
        'custom': {},
        'grand_total': 0.0
    }
    known_report_categories = set(MONTHLY_REPORT_CATEGORY_ORDER)

    for category_name, staff_members in ordered_data.items():
        category_total = 0.0
        for cleaner_data in staff_members.values():
            for period_data in cleaner_data[period_bucket_key].values():
                category_total += period_data['total']

        if category_name in ['Headbartenders', 'Bartenders']:
            category_totals['bartenders'] += category_total
        elif category_name == 'Waiters':
            category_totals['waiters'] += category_total
        elif category_name == 'Runners':
            category_totals['runners'] += category_total
        elif category_name == 'Manager':
            category_totals['managers'] += category_total
        elif category_name == 'Retail':
            category_totals['retail'] += category_total
        elif category_name not in known_report_categories and category_name != 'Uncategorized':
            category_totals['custom'][category_name] = category_total

        category_totals['grand_total'] += category_total

    category_totals['display_rows'] = []
    for category_name, staff_members in ordered_data.items():
        category_total = round(
            sum(
                period_data['total']
                for cleaner_data in staff_members.values()
                for period_data in cleaner_data[period_bucket_key].values()
            ),
            2,
        )
        category_totals['display_rows'].append({
            'label': category_name,
            'amount': category_total,
        })

    return category_totals


def _previous_calendar_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _month_period_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime('%B %Y')


def _parse_month_key(value: str | None) -> tuple[int, int] | tuple[None, None]:
    if not value:
        return None, None
    try:
        year_str, month_str = value.split('-', 1)
        return int(year_str), int(month_str)
    except (ValueError, TypeError):
        return None, None


def _month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


STANDARD_WAGE_CATEGORY_ROWS = [
    ('bartenders', 'Bartenders'),
    ('waiters', 'Waiters'),
    ('runners', 'Runners'),
    ('managers', 'Manager'),
    ('retail', 'Retail'),
]


def build_wage_category_comparison(
    totals_a: dict,
    totals_b: dict,
    label_a: str,
    label_b: str,
) -> dict:
    """Align two months of category totals for side-by-side comparison."""
    rows = []

    def pct(amount: float, grand_total: float) -> float:
        return (amount / grand_total * 100) if grand_total > 0 else 0.0

    def change_pct(amount_a: float, amount_b: float) -> float | None:
        if amount_a == 0:
            return None if amount_b == 0 else 100.0
        return ((amount_b - amount_a) / amount_a) * 100

    for key, label in STANDARD_WAGE_CATEGORY_ROWS:
        amount_a = totals_a.get(key, 0.0)
        amount_b = totals_b.get(key, 0.0)
        if amount_a > 0 or amount_b > 0:
            rows.append({
                'label': label,
                'amount_a': amount_a,
                'amount_b': amount_b,
                'pct_a': pct(amount_a, totals_a['grand_total']),
                'pct_b': pct(amount_b, totals_b['grand_total']),
                'change': amount_b - amount_a,
                'change_pct': change_pct(amount_a, amount_b),
            })

    custom_names = sorted(
        set(totals_a.get('custom', {})) | set(totals_b.get('custom', {}))
    )
    for name in custom_names:
        amount_a = totals_a.get('custom', {}).get(name, 0.0)
        amount_b = totals_b.get('custom', {}).get(name, 0.0)
        if amount_a > 0 or amount_b > 0:
            rows.append({
                'label': name,
                'amount_a': amount_a,
                'amount_b': amount_b,
                'pct_a': pct(amount_a, totals_a['grand_total']),
                'pct_b': pct(amount_b, totals_b['grand_total']),
                'change': amount_b - amount_a,
                'change_pct': change_pct(amount_a, amount_b),
            })

    grand_a = totals_a['grand_total']
    grand_b = totals_b['grand_total']
    rows.append({
        'label': 'Grand Total',
        'amount_a': grand_a,
        'amount_b': grand_b,
        'pct_a': 100.0 if grand_a > 0 else 0.0,
        'pct_b': 100.0 if grand_b > 0 else 0.0,
        'change': grand_b - grand_a,
        'change_pct': change_pct(grand_a, grand_b),
        'is_grand_total': True,
    })

    return {
        'label_a': label_a,
        'label_b': label_b,
        'rows': rows,
        'totals_a': totals_a,
        'totals_b': totals_b,
        'has_data': grand_a > 0 or grand_b > 0,
    }


def get_wage_category_comparison(
    year_a: int,
    month_a: int,
    year_b: int,
    month_b: int,
) -> dict:
    _, totals_a = get_monthly_totals(filter_year=year_a, filter_month=month_a)
    _, totals_b = get_monthly_totals(filter_year=year_b, filter_month=month_b)
    return build_wage_category_comparison(
        totals_a,
        totals_b,
        _month_period_label(year_a, month_a),
        _month_period_label(year_b, month_b),
    )


def get_available_log_months() -> list[tuple[int, int]]:
    """Return distinct (year, month) pairs that have logged hours, newest first."""
    owner_id = get_current_owner_id()
    month_query = db.session.query(
        db.func.strftime('%Y', TimeLog.date).label('year'),
        db.func.strftime('%m', TimeLog.date).label('month'),
    )
    if owner_id is not None:
        month_query = month_query.join(Cleaner, TimeLog.cleaner_id == Cleaner.id).filter(
            Cleaner.owner_id == owner_id
        )
    else:
        month_query = month_query.filter(db.false())
    rows = month_query.distinct().order_by(
        db.func.strftime('%Y', TimeLog.date).desc(),
        db.func.strftime('%m', TimeLog.date).desc(),
    ).all()
    return [
        (int(row.year), int(row.month))
        for row in rows
        if row.year is not None and row.month is not None
    ]


def resolve_wage_compare_params(args, today: date | None = None) -> tuple[int, int, int, int]:
    """Resolve the two months used for wage bill comparison."""
    today = today or date.today()
    has_explicit = 'compare_a' in args or 'compare_b' in args

    if not has_explicit:
        year_a, month_a = today.year, today.month
        year_b, month_b = _previous_calendar_month(year_a, month_a)
        return year_a, month_a, year_b, month_b

    year_a, month_a = _parse_month_key(args.get('compare_a'))
    year_b, month_b = _parse_month_key(args.get('compare_b'))

    if year_a is None or month_a is None:
        year_a, month_a = today.year, today.month
    if year_b is None or month_b is None:
        year_b, month_b = _previous_calendar_month(year_a, month_a)

    return year_a, month_a, year_b, month_b


def get_weekly_totals(filter_year=None, filter_month=None, date_from=None, date_to=None):
    """Group logs by cleaner and week with pay totals and tracked hours."""
    query = _filter_timelog_query(
        timelogs_query(),
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=date_from,
        date_to=date_to,
    )
    logs = query.all()
    weekly_totals = {}
    
    # First pass: collect shifts and track months per week
    for log in logs:
        cleaner = log.cleaner
        cleaner_name = cleaner.name
        cleaner_category = cleaner.category or "Uncategorized"
        # Get ISO week number
        year, week, _ = log.date.isocalendar()
        week_key = f"{year}-W{week:02d}"
        # Get month from the log date to determine daily rate
        month = log.date.month
        
        if cleaner_name not in weekly_totals:
            weekly_totals[cleaner_name] = {
                'category': cleaner_category,
                'weeks': {}
            }
        
        if week_key not in weekly_totals[cleaner_name]['weeks']:
            weekly_totals[cleaner_name]['weeks'][week_key] = {
                'entries': 0,
                'hours': 0.0,
                'regular_hours': 0.0,
                'sunday_hours': 0.0,
                'public_holiday_hours': 0.0,
                'months': [],  # Track all months in this week
                'log_date': log.date  # Store first log date for rate calculation
            }

        weekly_totals[cleaner_name]['weeks'][week_key]['entries'] += 1
        if normalize_log_type(log.log_type) == 'time':
            regular_hours, sunday_hours, public_holiday_hours = split_log_hours(log)
            weekly_totals[cleaner_name]['weeks'][week_key]['hours'] += regular_hours + sunday_hours + public_holiday_hours
            weekly_totals[cleaner_name]['weeks'][week_key]['regular_hours'] += regular_hours
            weekly_totals[cleaner_name]['weeks'][week_key]['sunday_hours'] += sunday_hours
            weekly_totals[cleaner_name]['weeks'][week_key]['public_holiday_hours'] += public_holiday_hours
        # Track month (avoid duplicates)
        if month not in weekly_totals[cleaner_name]['weeks'][week_key]['months']:
            weekly_totals[cleaner_name]['weeks'][week_key]['months'].append(month)
    
    # Calculate totals for each week with month-specific daily rates
    for cleaner_name, cleaner_data in weekly_totals.items():
        cleaner_category = cleaner_data['category']
        for week_key, week_data in cleaner_data['weeks'].items():
            entry_count = week_data['entries']
            hours_count = round(week_data['hours'], 2)
            sunday_hours = round(week_data.get('sunday_hours', 0.0), 2)
            public_holiday_hours = round(week_data.get('public_holiday_hours', 0.0), 2)
            # Use the first month (or most common if week spans months)
            # For simplicity, use the first month encountered
            month = week_data['months'][0] if week_data['months'] else 1
            # Determine days per month: December = 26, others = 24
            days_in_month = 26 if month == 12 else 24
            log_date = week_data['log_date']
            cleaner = cleaners_query().filter_by(name=cleaner_name).first()
            rate_type, rate_amount = get_cleaner_rate_config(cleaner, log_date) if cleaner else ('monthly', get_monthly_rate(cleaner_name, cleaner_category, log_date))
            flat_monthly = is_flat_monthly(cleaner) if cleaner else False
            if flat_monthly:
                week_total = 0.0
                week_rate_label = f"{format_rate_label(rate_type, rate_amount, flat_monthly=True)} (see monthly)"
            else:
                week_total = calculate_period_total(
                    rate_type,
                    rate_amount,
                    entry_count,
                    hours_count,
                    days_in_month,
                    sunday_hours=sunday_hours,
                    public_holiday_hours=public_holiday_hours
                )
                if normalize_rate_type(rate_type) == 'hourly':
                    week_rate_label = format_hourly_rate_label(
                        rate_amount,
                        include_sunday_rate=sunday_hours > 0,
                        include_public_holiday_rate=public_holiday_hours > 0
                    )
                else:
                    week_rate_label = format_rate_label(rate_type, rate_amount)
            cleaner_data['weeks'][week_key] = {
                'entries': entry_count,
                'hours': hours_count,
                'rate_label': week_rate_label,
                'total': week_total
            }
    
    # Group by category and sort, separating head bartenders
    head_barman_names = ['EDSON', 'NICKI', 'COLLIN (bar)', 'COLLIN bar', 'MUKETIWA']
    category_order = ['Headbartenders', 'Bartenders', 'Barbacks', 'Waiters', 'Runners', 'Manager', 'Retail']
    categorized_data = {}
    
    # Initialize categories
    for category in category_order:
        categorized_data[category] = {}
    
    # Add uncategorized if needed
    categorized_data['Uncategorized'] = {}
    
    # Group staff by category, separating head bartenders
    for cleaner_name, cleaner_data in weekly_totals.items():
        category = cleaner_data['category']
        
        # Check if this is a head bartender (supports surnames)
        cleaner_name_upper = cleaner_name.upper()
        is_head_bartender = any(cleaner_name_upper.startswith(name.upper()) for name in head_barman_names)
        
        if is_head_bartender and category == 'Bartenders':
            # Put head bartenders in their own category
            if 'Headbartenders' not in categorized_data:
                categorized_data['Headbartenders'] = {}
            categorized_data['Headbartenders'][cleaner_name] = cleaner_data
        else:
            # Regular category grouping
            if category not in categorized_data:
                categorized_data[category] = {}
            categorized_data[category][cleaner_name] = cleaner_data
    
    # Sort staff alphabetically within each category
    for category in categorized_data:
        categorized_data[category] = dict(sorted(categorized_data[category].items()))
    
    # Return in the specified order, then any custom categories
    ordered_data = {}
    for category in category_order:
        if category in categorized_data and categorized_data[category]:
            ordered_data[category] = categorized_data[category]

    custom_categories = sorted(
        category for category in categorized_data
        if category not in category_order and category != 'Uncategorized' and categorized_data[category]
    )
    for category in custom_categories:
        ordered_data[category] = categorized_data[category]
    
    # Add any remaining uncategorized
    if 'Uncategorized' in categorized_data and categorized_data['Uncategorized']:
        ordered_data['Uncategorized'] = categorized_data['Uncategorized']
    
    category_totals = _calculate_category_totals(ordered_data, 'weeks')
    return ordered_data, category_totals

def get_monthly_totals(filter_year=None, filter_month=None, date_from=None, date_to=None):
    """Group logs by cleaner and month with pay totals and tracked hours, organized by category.
    
    Args:
        filter_year: Optional year to filter by (e.g., 2024)
        filter_month: Optional month to filter by (1-12)
        date_from: Optional start date for custom range (inclusive)
        date_to: Optional end date for custom range (inclusive)
    """
    query = _filter_timelog_query(
        timelogs_query(),
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=date_from,
        date_to=date_to,
    )
    
    logs = query.all()
    monthly_totals = {}
    
    for log in logs:
        cleaner = log.cleaner
        cleaner_id = cleaner.id
        cleaner_name = cleaner.name
        cleaner_category = cleaner.category or "Uncategorized"
        # Get year and month
        year = log.date.year
        month = log.date.month
        month_key = f"{year}-{month:02d}"
        
        # Group by cleaner_id to prevent duplicates from name changes
        if cleaner_id not in monthly_totals:
            monthly_totals[cleaner_id] = {
                'name': cleaner_name,
                'category': cleaner_category,
                'months': {}
            }
        
        if month_key not in monthly_totals[cleaner_id]['months']:
            monthly_totals[cleaner_id]['months'][month_key] = {
                'entries': 0,
                'hours': 0.0,
                'regular_hours': 0.0,
                'sunday_hours': 0.0,
                'public_holiday_hours': 0.0
            }

        monthly_totals[cleaner_id]['months'][month_key]['entries'] += 1
        if normalize_log_type(log.log_type) == 'time':
            regular_hours, sunday_hours, public_holiday_hours = split_log_hours(log)
            month_bucket = monthly_totals[cleaner_id]['months'][month_key]
            month_bucket['hours'] += regular_hours + sunday_hours + public_holiday_hours
            month_bucket['regular_hours'] += regular_hours
            month_bucket['sunday_hours'] += sunday_hours
            month_bucket['public_holiday_hours'] += public_holiday_hours
    
    # Calculate totals for each month with month-specific daily rates
    for cleaner_id, cleaner_data in monthly_totals.items():
        cleaner_name = cleaner_data['name']
        cleaner_category = cleaner_data['category']
        for month_key, month_entry in cleaner_data['months'].items():
            entry_count = month_entry['entries']
            hours_count = round(month_entry['hours'], 2)
            sunday_hours = round(month_entry.get('sunday_hours', 0.0), 2)
            public_holiday_hours = round(month_entry.get('public_holiday_hours', 0.0), 2)
            year, month = month_key.split('-')
            year_int = int(year)
            month_int = int(month)
            days_in_month = 26 if month_int == 12 else 24
            month_date = date(year_int, month_int, 1)
            cleaner = get_cleaner_for_owner(cleaner_id)
            rate_type, rate_amount = get_cleaner_rate_config(cleaner, month_date) if cleaner else ('monthly', get_monthly_rate(cleaner_name, cleaner_category, month_date))
            flat_monthly = is_flat_monthly(cleaner) if cleaner else False
            normalized_rate_type = normalize_rate_type(rate_type)
            if normalized_rate_type == 'hourly':
                rate_label = format_hourly_rate_label(
                    rate_amount,
                    include_sunday_rate=sunday_hours > 0,
                    include_public_holiday_rate=public_holiday_hours > 0
                )
            else:
                rate_label = format_rate_label(rate_type, rate_amount, flat_monthly=flat_monthly)
            cleaner_data['months'][month_key] = {
                'entries': entry_count,
                'hours': hours_count,
                'sunday_hours': sunday_hours,
                'public_holiday_hours': public_holiday_hours,
                'rate_label': rate_label,
                'total': calculate_period_total(
                    rate_type,
                    rate_amount,
                    entry_count,
                    hours_count,
                    days_in_month,
                    flat_monthly=flat_monthly,
                    sunday_hours=sunday_hours,
                    public_holiday_hours=public_holiday_hours
                )
            }
    
    # Group by category and sort, separating head bartenders
    head_barman_names = ['EDSON', 'NICKI', 'COLLIN (bar)', 'COLLIN bar', 'MUKETIWA']
    category_order = ['Headbartenders', 'Bartenders', 'Barbacks', 'Waiters', 'Runners', 'Manager', 'Retail']
    categorized_data = {}
    
    # Initialize categories
    for category in category_order:
        categorized_data[category] = {}
    
    # Add uncategorized if needed
    categorized_data['Uncategorized'] = {}
    
    # Group staff by category, separating head bartenders
    for cleaner_id, cleaner_data in monthly_totals.items():
        cleaner_name = cleaner_data['name']
        category = cleaner_data['category']
        
        # Check if this is a head bartender (supports surnames)
        cleaner_name_upper = cleaner_name.upper()
        is_head_bartender = any(cleaner_name_upper.startswith(name.upper()) for name in head_barman_names)
        
        if is_head_bartender and category == 'Bartenders':
            # Put head bartenders in their own category
            if 'Headbartenders' not in categorized_data:
                categorized_data['Headbartenders'] = {}
            categorized_data['Headbartenders'][cleaner_name] = cleaner_data
        else:
            # Regular category grouping
            if category not in categorized_data:
                categorized_data[category] = {}
            categorized_data[category][cleaner_name] = cleaner_data
    
    # Sort staff alphabetically within each category by surname, then first name
    def get_sort_key(name):
        """Extract surname and first name for sorting."""
        name_parts = name.split()
        if len(name_parts) > 1:
            surname = name_parts[-1]
            first_name = ' '.join(name_parts[:-1])
            return (surname.upper(), first_name.upper())
        else:
            # If no surname, use the name as both
            return (name.upper(), '')
    
    for category in categorized_data:
        categorized_data[category] = dict(sorted(categorized_data[category].items(), key=lambda x: get_sort_key(x[0])))
    
    # Return in the specified order, then any custom categories
    ordered_data = {}
    for category in category_order:
        if category in categorized_data and categorized_data[category]:
            ordered_data[category] = categorized_data[category]

    custom_categories = sorted(
        category for category in categorized_data
        if category not in category_order and category != 'Uncategorized' and categorized_data[category]
    )
    for category in custom_categories:
        ordered_data[category] = categorized_data[category]
    
    # Add any remaining uncategorized
    if 'Uncategorized' in categorized_data and categorized_data['Uncategorized']:
        ordered_data['Uncategorized'] = categorized_data['Uncategorized']
    
    category_totals = _calculate_category_totals(ordered_data, 'months')
    return ordered_data, category_totals

def get_monthly_totals_with_details():
    """Group logs by cleaner and month with individual log details."""
    logs = timelogs_query().order_by(TimeLog.date.desc()).all()
    monthly_data = {}
    
    for log in logs:
        cleaner_name = log.cleaner.name
        cleaner_id = log.cleaner_id
        year = log.date.year
        month = log.date.month
        month_key = f"{year}-{month:02d}"
        
        if cleaner_name not in monthly_data:
            monthly_data[cleaner_name] = {}
        
        if month_key not in monthly_data[cleaner_name]:
            monthly_data[cleaner_name][month_key] = {
                'entry_total': 0,
                'hours_total': 0.0,
                'entries': []
            }

        monthly_data[cleaner_name][month_key]['entry_total'] += 1
        if normalize_log_type(log.log_type) == 'time':
            monthly_data[cleaner_name][month_key]['hours_total'] += get_log_hours(log)
        monthly_data[cleaner_name][month_key]['entries'].append({
            'id': log.id,
            'cleaner_id': cleaner_id,
            'date': log.date,
            'log_type': normalize_log_type(log.log_type),
            'start_time': log.start_time,
            'end_time': log.end_time,
            'hours_worked': get_log_hours(log),
            'notes': log.notes,
            'created_at': log.created_at
        })
    
    return monthly_data

def get_monthly_totals_by_category():
    """Group monthly totals by category with individual log details."""
    logs = timelogs_query().order_by(TimeLog.date.desc()).all()
    category_data = {}

    for log in logs:
        cleaner = log.cleaner
        cleaner_name = cleaner.name
        cleaner_id = log.cleaner_id
        cleaner_category = cleaner.category or "Uncategorized"
        year = log.date.year
        month = log.date.month
        month_key = f"{year}-{month:02d}"
        category_name = resolve_display_group(cleaner_category)

        if category_name not in category_data:
            category_data[category_name] = {}

        if cleaner_name not in category_data[category_name]:
            category_data[category_name][cleaner_name] = {}

        if month_key not in category_data[category_name][cleaner_name]:
            category_data[category_name][cleaner_name][month_key] = {
                'entry_total': 0,
                'hours_total': 0.0,
                'entries': []
            }

        category_data[category_name][cleaner_name][month_key]['entry_total'] += 1
        if normalize_log_type(log.log_type) == 'time':
            category_data[category_name][cleaner_name][month_key]['hours_total'] += get_log_hours(log)
        category_data[category_name][cleaner_name][month_key]['entries'].append({
            'id': log.id,
            'cleaner_id': cleaner_id,
            'date': log.date,
            'log_type': normalize_log_type(log.log_type),
            'start_time': log.start_time,
            'end_time': log.end_time,
            'hours_worked': get_log_hours(log),
            'notes': log.notes,
            'created_at': log.created_at
        })

    return category_data

def get_cleaners_by_category(active_only=True, archived_only=False):
    """Group cleaners by category for display in forms and staff lists."""
    query = cleaners_query()
    if active_only:
        query = query.filter_by(active=True)
    elif archived_only:
        query = query.filter_by(active=False)
    all_cleaners = query.all()

    grouped = {}
    for cleaner in all_cleaners:
        group_name = resolve_display_group(cleaner.category or "Uncategorized")
        grouped.setdefault(group_name, []).append(cleaner)

    ordered = {}
    for group_name in sort_display_categories(grouped.keys()):
        if grouped[group_name]:
            ordered[group_name] = grouped[group_name]
    return ordered

def get_logs_by_staff_and_date(filter_year=None, filter_month=None, date_from=None, date_to=None):
    """Group logs by staff member, showing dates they worked. 
    Returns a dictionary with cleaner_id -> {name, category, dates: [list of date objects]}."""
    query = _filter_timelog_query(
        timelogs_query().order_by(TimeLog.date.desc()),
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=date_from,
        date_to=date_to,
    )
    logs = query.all()
    staff_data = {}
    
    for log in logs:
        cleaner = log.cleaner
        cleaner_id = cleaner.id
        
        if cleaner_id not in staff_data:
            staff_data[cleaner_id] = {
                'name': cleaner.name,
                'category': cleaner.category or "Uncategorized",
                'dates': []
            }
        
        # Add date if not already in the list (avoid duplicates)
        if log.date not in staff_data[cleaner_id]['dates']:
            staff_data[cleaner_id]['dates'].append(log.date)
    
    # Sort dates in descending order for each staff member
    for cleaner_id in staff_data:
        staff_data[cleaner_id]['dates'].sort(reverse=True)
    
    return staff_data

def get_staff_by_category_with_dates(filter_year=None, filter_month=None, date_from=None, date_to=None):
    """Group staff members by category with their dates."""
    staff_data = get_logs_by_staff_and_date(
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=date_from,
        date_to=date_to,
    )
    grouped = {}

    for cleaner_id, data in staff_data.items():
        group_name = resolve_display_group(data['category'])
        grouped.setdefault(group_name, {})
        grouped[group_name][cleaner_id] = {
            'name': data['name'],
            'dates': data['dates']
        }

    ordered = {}
    for group_name in sort_display_categories(grouped.keys()):
        if grouped[group_name]:
            ordered[group_name] = grouped[group_name]
    return ordered

def get_staff_by_date(filter_year=None, filter_month=None, date_from=None, date_to=None):
    """Group logs by date, showing which staff members worked on each date."""
    query = _filter_timelog_query(
        timelogs_query().order_by(TimeLog.date.desc()),
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=date_from,
        date_to=date_to,
    )
    logs = query.all()
    date_data = {}
    
    for log in logs:
        work_date = log.date
        
        if work_date not in date_data:
            date_data[work_date] = []
        
        date_data[work_date].append({
            'cleaner_id': log.cleaner.id,
            'name': log.cleaner.name,
            'category': log.cleaner.category or "Uncategorized",
            'log_id': log.id,
            'log_type': normalize_log_type(log.log_type),
            'start_time': log.start_time.strftime('%H:%M') if log.start_time else None,
            'end_time': log.end_time.strftime('%H:%M') if log.end_time else None,
            'hours_worked': get_log_hours(log),
            'notes': log.notes
        })
    
    return date_data

def parse_log_batches_from_form(form) -> list:
    """Parse queued log batches submitted from the multi-date calendar UI."""
    raw_batches = form.get('log_batches')
    if not raw_batches:
        return []

    batches = json.loads(raw_batches)
    if not isinstance(batches, list):
        raise ValueError('Invalid batch payload')

    parsed_batches = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError('Invalid batch entry')

        dates = batch.get('dates') or []
        if not dates:
            continue

        batch_log_type = normalize_log_type(batch.get('log_type'))
        start_time = parse_time_value(batch.get('start_time'))
        end_time = parse_time_value(batch.get('end_time'))
        hours_worked = None

        if batch_log_type == 'time':
            if not start_time or not end_time:
                raise ValueError('Time batches require start and end times')
            hours_worked = calculate_hours_worked(start_time, end_time)

        parsed_batches.append({
            'dates': [datetime.strptime(date_str, '%Y-%m-%d').date() for date_str in dates],
            'log_type': batch_log_type,
            'start_time': start_time,
            'end_time': end_time,
            'hours_worked': hours_worked,
        })

    return parsed_batches


def create_logs_for_batch(cleaner_ids, batch, notes):
    """Create log entries for one batch across all selected staff and dates."""
    entries_created = 0
    for work_date in batch['dates']:
        for cleaner_id in cleaner_ids:
            add_time_log(
                cleaner_id,
                work_date,
                notes,
                log_type=batch['log_type'],
                start_time=batch['start_time'],
                end_time=batch['end_time'],
                hours_worked=batch['hours_worked']
            )
            entries_created += 1
    return entries_created

def get_dashboard_stats():
    """Build summary metrics for the admin dashboard (current calendar month)."""
    today = date.today()
    filter_year, filter_month = today.year, today.month
    _, category_totals = get_monthly_totals(
        filter_year=filter_year,
        filter_month=filter_month,
    )

    from calendar import monthrange
    month_start = date(filter_year, filter_month, 1)
    month_end = date(filter_year, filter_month, monthrange(filter_year, filter_month)[1])

    month_logs = timelogs_query().filter(
        TimeLog.date >= month_start,
        TimeLog.date <= month_end,
    ).all()
    logs_count = len(month_logs)
    hours_count = round(
        sum(
            get_log_hours(log)
            for log in month_logs
            if normalize_log_type(log.log_type) == 'time'
        ),
        2,
    )

    staff_working_today = (
        timelogs_query()
        .filter(TimeLog.date == today)
        .with_entities(TimeLog.cleaner_id)
        .distinct()
        .count()
    )

    recent_logs = (
        timelogs_query()
        .order_by(TimeLog.date.desc(), TimeLog.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        'period_label': today.strftime('%B %Y'),
        'active_staff': cleaners_query().filter_by(active=True).count(),
        'archived_staff': cleaners_query().filter_by(active=False).count(),
        'logs_this_month': logs_count,
        'hours_this_month': hours_count,
        'wage_bill_this_month': category_totals['grand_total'],
        'staff_working_today': staff_working_today,
        'category_totals': category_totals,
        'recent_logs': recent_logs,
    }


def ensure_user_schema() -> None:
    """Create the user table and migrate legacy username columns to email."""
    db.create_all()
    existing_tables = db.session.execute(
        db.text("SELECT name FROM sqlite_master WHERE type='table' AND name='user'")
    ).fetchall()
    if not existing_tables:
        return

    existing_columns = {
        row[1] for row in db.session.execute(db.text("PRAGMA table_info(user)")).fetchall()
    }

    if 'email' not in existing_columns:
        db.session.execute(db.text("ALTER TABLE user ADD COLUMN email VARCHAR(255)"))

    existing_columns = {
        row[1] for row in db.session.execute(db.text("PRAGMA table_info(user)")).fetchall()
    }

    if 'username' in existing_columns:
        db.session.execute(
            db.text(
                "UPDATE user SET email = CASE "
                "WHEN instr(lower(trim(username)), '@') > 0 THEN lower(trim(username)) "
                "ELSE lower(trim(username)) || '@legacy.local' "
                "END "
                "WHERE email IS NULL OR trim(email) = ''"
            )
        )

    db.session.commit()


def ensure_password_reset_schema() -> None:
    """Create the password reset token table if needed."""
    db.create_all()


PUBLIC_ENDPOINTS = frozenset({
    'login', 'register', 'forgot_password', 'reset_password', 'static',
})


# Routes
@app.before_request
def ensure_schema_before_request():
    """Keep the SQLite schema aligned with the current model before handling requests."""
    ensure_cleaner_schema()
    ensure_time_log_schema()
    ensure_user_schema()
    ensure_password_reset_schema()


@app.before_request
def require_login():
    """Redirect unauthenticated users to the login page."""
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    if not session.get('user_id'):
        flash('Please log in to continue.', 'error')
        return redirect(url_for('login', next=request.path))


@app.context_processor
def inject_current_user():
    return {'current_user': get_current_user()}


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Sign in to CrewTrack."""
    if session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password') or ''

        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('login.html')

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash('Invalid email or password.', 'error')
            return render_template('login.html')

        session.clear()
        session['user_id'] = user.id
        session.permanent = bool(request.form.get('remember'))
        flash(f'Welcome back!', 'success')

        next_url = request.args.get('next') or request.form.get('next')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect(url_for('index'))

    return render_template('login.html', next=request.args.get('next', ''))


@app.route('/register', methods=['GET', 'POST'])
def register():
    """Create a new CrewTrack account."""
    if session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm_password') or ''

        if not email:
            flash('Please enter a valid email address.', 'error')
            return render_template('register.html')
        password_error = validate_password(password)
        if password_error:
            flash(password_error, 'error')
            return render_template('register.html')
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'error')
            return render_template('register.html')

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        session.clear()
        session['user_id'] = user.id
        flash('Account created. You are now signed in.', 'success')
        return redirect(url_for('index'))

    return render_template('register.html')


@app.route('/logout', methods=['POST'])
def logout():
    """Sign out."""
    session.clear()
    flash('You have been signed out.', 'success')
    return redirect(url_for('login'))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Request a password reset link by email."""
    if session.get('user_id'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        if not email:
            flash('Please enter a valid email address.', 'error')
            return render_template('forgot_password.html')

        initiate_password_reset(email)

        if mail_is_configured():
            flash(
                'If an account exists for that email, we sent a link to reset your password. '
                'Check your inbox (and spam folder).',
                'success',
            )
        else:
            flash(
                'If an account exists for that email, a reset link was written to the server console '
                '(email is not configured yet).',
                'success',
            )
        return redirect(url_for('login'))

    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Set a new password using a reset token from email."""
    if session.get('user_id'):
        return redirect(url_for('index'))

    user = get_user_for_reset_token(token)
    if not user:
        flash('This reset link is invalid or has expired. Please request a new one.', 'error')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm_password') or ''
        password_error = validate_password(password)
        if password_error:
            flash(password_error, 'error')
            return render_template('reset_password.html', token=token)
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('reset_password.html', token=token)

        user.set_password(password)
        mark_reset_token_used(token)
        db.session.commit()

        session.clear()
        flash('Your password has been updated. You can sign in now.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)


@app.route('/')
def index():
    """Admin dashboard home page."""
    today = date.today()
    dashboard = get_dashboard_stats()
    year_a, month_a, year_b, month_b = resolve_wage_compare_params(request.args, today)
    wage_comparison = get_wage_category_comparison(year_a, month_a, year_b, month_b)
    available_log_months = get_available_log_months()
    cleaners_by_category = get_cleaners_by_category(active_only=True)
    archived_by_category = get_cleaners_by_category(active_only=False, archived_only=True)
    has_archived = any(cleaners for cleaners in archived_by_category.values() if cleaners)
    return render_template(
        'index.html',
        dashboard=dashboard,
        wage_comparison=wage_comparison,
        compare_a_key=_month_key(year_a, month_a),
        compare_b_key=_month_key(year_b, month_b),
        available_log_months=available_log_months,
        today=today,
        cleaners_by_category=cleaners_by_category,
        archived_by_category=archived_by_category,
        has_archived=has_archived,
        staff_categories=get_all_staff_categories(),
    )


@app.route('/manage')
def manage_logs():
    """Detailed log management with edit and delete."""
    cleaners_by_category = get_cleaners_by_category(active_only=True)
    archived_by_category = get_cleaners_by_category(active_only=False, archived_only=True)
    has_archived = any(cleaners for cleaners in archived_by_category.values() if cleaners)
    cleaners_by_category_all = get_cleaners_by_category(active_only=False)
    monthly_data_by_category = get_monthly_totals_by_category()
    all_cleaners = cleaners_query().order_by(Cleaner.name).all()
    display_categories = sort_display_categories(monthly_data_by_category.keys())
    return render_template(
        'manage_logs.html',
        cleaners_by_category=cleaners_by_category,
        archived_by_category=archived_by_category,
        has_archived=has_archived,
        cleaners_by_category_all=cleaners_by_category_all,
        monthly_data_by_category=monthly_data_by_category,
        display_categories=display_categories,
        staff_categories=get_all_staff_categories(),
        all_cleaners=all_cleaners,
    )


@app.route('/log-staff', methods=['GET'])
def log_form():
    """Show form to add a new log entry."""
    cleaners_by_category = get_cleaners_by_category(active_only=True)
    today = date.today().isoformat()
    return render_template('log_form.html', cleaners_by_category=cleaners_by_category, today=today)


@app.route('/api/staff-logged-dates')
def staff_logged_dates():
    """Return dates that already have logs for the selected staff members."""
    raw_cleaner_ids = request.args.getlist('cleaner_ids[]') or request.args.getlist('cleaner_ids')
    if not raw_cleaner_ids:
        return jsonify({'dates': [], 'details': {}})

    try:
        cleaner_ids = [int(cleaner_id) for cleaner_id in raw_cleaner_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid staff selection'}), 400

    allowed_ids = {
        row[0] for row in cleaners_query().filter(Cleaner.id.in_(cleaner_ids)).with_entities(Cleaner.id).all()
    }
    cleaner_ids = [cid for cid in cleaner_ids if cid in allowed_ids]
    if not cleaner_ids:
        return jsonify({'dates': [], 'details': {}})

    logs = timelogs_query().filter(TimeLog.cleaner_id.in_(cleaner_ids)).order_by(
        TimeLog.date.asc(),
        TimeLog.created_at.asc()
    ).all()

    details = {}
    for log in logs:
        date_key = log.date.isoformat()
        entry = {
            'log_type': normalize_log_type(log.log_type),
            'cleaner_id': log.cleaner_id,
        }
        if log.start_time and log.end_time:
            entry['start'] = log.start_time.strftime('%H:%M')
            entry['end'] = log.end_time.strftime('%H:%M')
        if log.hours_worked:
            entry['hours'] = round(float(log.hours_worked), 2)
        details.setdefault(date_key, []).append(entry)

    return jsonify({
        'dates': sorted(details.keys()),
        'details': details,
    })

@app.route('/log-staff', methods=['POST'])
def submit_log():
    """Handle submission of new shift or time log(s)."""
    cleaner_ids = request.form.getlist('cleaner_ids[]')
    date_mode = request.form.get('date_mode')
    log_type = normalize_log_type(request.form.get('log_type'))
    notes = request.form.get('notes')

    if not cleaner_ids:
        flash('Please select at least one FOH staff member', 'error')
        return redirect(url_for('log_form'))

    try:
        cleaner_ids = [int(cid) for cid in cleaner_ids]
        selected_cleaners = cleaners_query().filter(Cleaner.id.in_(cleaner_ids)).all()
        if len(selected_cleaners) != len(set(cleaner_ids)):
            flash('One or more selected staff members could not be found', 'error')
            return redirect(url_for('log_form'))

        allowed_log_types = set.intersection(*[
            set(get_allowed_log_types_for_cleaner(cleaner))
            for cleaner in selected_cleaners
        ])
        if not allowed_log_types:
            flash('Hourly staff and daily/monthly staff must be logged separately', 'error')
            return redirect(url_for('log_form'))

        requested_log_type = normalize_log_type(request.form.get('log_type'))
        if len(allowed_log_types) == 1:
            log_type = next(iter(allowed_log_types))
        elif requested_log_type not in allowed_log_types:
            flash('Invalid log type for the selected staff', 'error')
            return redirect(url_for('log_form'))
        else:
            log_type = requested_log_type

        start_time = None
        end_time = None
        hours_worked = None

        if log_type == 'time' and date_mode == 'single':
            start_time = parse_time_value(request.form.get('start_time'))
            end_time = parse_time_value(request.form.get('end_time'))
            if not start_time or not end_time:
                flash('Please provide both a start time and end time', 'error')
                return redirect(url_for('log_form'))
            hours_worked = calculate_hours_worked(start_time, end_time)

        if date_mode == 'single':
            # Single date mode
            date_str = request.form.get('date')
            if not date_str:
                flash('Please select a date', 'error')
                return redirect(url_for('log_form'))

            work_date = datetime.strptime(date_str, '%Y-%m-%d').date()

            # Create a log for each selected staff member
            for cleaner_id in cleaner_ids:
                add_time_log(
                    cleaner_id,
                    work_date,
                    notes,
                    log_type=log_type,
                    start_time=start_time,
                    end_time=end_time,
                    hours_worked=hours_worked
                )

            entry_label = 'Time entr' if log_type == 'time' else 'Shift'
            suffix = 'ies' if log_type == 'time' else '(s)'
            flash(f'{entry_label}{suffix} logged successfully for {len(cleaner_ids)} FOH staff member(s)!', 'success')

        elif date_mode == 'multiple':
            batches = parse_log_batches_from_form(request.form)
            if not batches:
                flash('Please select at least one date on the calendar', 'error')
                return redirect(url_for('log_form'))

            entries_created = 0
            for batch in batches:
                if batch['log_type'] != log_type:
                    flash('All timeslots must use the same log type', 'error')
                    return redirect(url_for('log_form'))
                entries_created += create_logs_for_batch(cleaner_ids, batch, notes)

            entry_label = 'Time entries' if log_type == 'time' else 'Shifts'
            flash(
                f'{entry_label} logged successfully for {entries_created} staff/date combinations!',
                'success'
            )

    except (ValueError, TypeError):
        flash('Invalid data provided', 'error')
        return redirect(url_for('log_form'))

    return redirect(url_for('index'))

@app.route('/update-log', methods=['POST'])
def update_log():
    """Update a shift or time log entry."""
    log_id = request.form.get('log_id')
    cleaner_id = request.form.get('cleaner_id')
    date_str = request.form.get('date')
    log_type = normalize_log_type(request.form.get('log_type'))
    notes = request.form.get('notes')
    
    if not all([log_id, cleaner_id, date_str]):
        flash('Please fill in all required fields', 'error')
        return redirect(url_for('index'))
    
    try:
        log_id = int(log_id)
        cleaner_id = int(cleaner_id)
        work_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        cleaner = get_cleaner_for_owner(cleaner_id)
        if not cleaner:
            flash('Staff member not found', 'error')
            return redirect(url_for('index'))

        allowed_log_types = get_allowed_log_types_for_cleaner(cleaner)
        requested_log_type = normalize_log_type(request.form.get('log_type'))
        if requested_log_type in allowed_log_types:
            log_type = requested_log_type
        elif len(allowed_log_types) == 1:
            log_type = next(iter(allowed_log_types))
        else:
            flash('Invalid log type for this staff member', 'error')
            return redirect(url_for('index'))
        
        time_log = get_time_log_by_id(log_id)
        if not time_log:
            flash('Shift not found', 'error')
            return redirect(url_for('index'))
        
        # Update the log entry
        time_log.cleaner_id = cleaner_id
        time_log.date = work_date
        time_log.log_type = log_type
        if log_type == 'time':
            start_time = parse_time_value(request.form.get('start_time'))
            end_time = parse_time_value(request.form.get('end_time'))
            if not start_time or not end_time:
                flash('Please provide both a start time and end time', 'error')
                return redirect(url_for('index'))
            time_log.start_time = start_time
            time_log.end_time = end_time
            time_log.hours_worked = calculate_hours_worked(start_time, end_time)
        else:
            time_log.start_time = None
            time_log.end_time = None
            time_log.hours_worked = None
        time_log.notes = notes if notes else None
        
        db.session.commit()
        
        flash('Log updated successfully!', 'success')
        
    except (ValueError, TypeError) as e:
        flash('Invalid data provided', 'error')
    except Exception as e:
        flash('Error updating shift', 'error')
        db.session.rollback()
    
    return redirect(url_for('index'))

@app.route('/delete-log', methods=['POST'])
def delete_log():
    """Delete a shift or time log entry."""
    log_id = request.form.get('log_id')
    
    if not log_id:
        flash('Invalid log ID', 'error')
        return redirect(url_for('index'))
    
    try:
        log_id = int(log_id)
        time_log = get_time_log_by_id(log_id)
        
        if not time_log:
            flash('Time log not found', 'error')
            return redirect(url_for('index'))
        
        # Store log details for flash message
        cleaner_name = time_log.cleaner.name
        log_date = time_log.date.strftime('%Y-%m-%d')
        
        # Delete the log entry
        db.session.delete(time_log)
        db.session.commit()
        
        flash(f'Log deleted successfully! (FOH Staff: {cleaner_name}, Date: {log_date})', 'success')
        
    except (ValueError, TypeError):
        flash('Invalid log ID', 'error')
    except Exception as e:
        flash('Error deleting time log', 'error')
        db.session.rollback()
    
    return redirect(url_for('index'))

@app.route('/weekly')
def weekly_totals():
    """Show weekly totals per cleaner."""
    filter_year, filter_month, filter_date_from, filter_date_to, using_default_filter = (
        _resolve_monthly_filter_params(request.args)
    )

    weekly_data, category_totals = get_weekly_totals(
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=filter_date_from,
        date_to=filter_date_to,
    )

    years, months = get_tenant_log_year_month_options()

    filter_query_string = (
        request.query_string.decode()
        if request.query_string
        else _monthly_filter_query_string(
            filter_year, filter_month, filter_date_from, filter_date_to
        )
    )

    return render_template(
        'weekly.html',
        weekly_data=weekly_data,
        category_totals=category_totals,
        filter_year=filter_year,
        filter_month=filter_month,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
        using_default_filter=using_default_filter,
        filter_query_string=filter_query_string,
        available_years=years,
        available_months=months,
    )

def _parse_date_arg(value):
    """Parse YYYY-MM-DD string to date or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _resolve_monthly_filter_params(args):
    """Parse monthly view filters; default to the current calendar month."""
    filter_date_from = _parse_date_arg(args.get('date_from'))
    filter_date_to = _parse_date_arg(args.get('date_to'))
    has_explicit_filter = any(
        key in args for key in ('year', 'month', 'date_from', 'date_to')
    )

    if not has_explicit_filter:
        today = date.today()
        return today.year, today.month, None, None, True

    filter_year = args.get('year', type=int)
    filter_month = args.get('month', type=int)
    return filter_year, filter_month, filter_date_from, filter_date_to, False


def _monthly_filter_query_string(filter_year, filter_month, filter_date_from, filter_date_to):
    """Build a query string reflecting the active monthly filters."""
    parts = []
    if filter_date_from or filter_date_to:
        if filter_date_from:
            parts.append(f'date_from={filter_date_from.isoformat()}')
        if filter_date_to:
            parts.append(f'date_to={filter_date_to.isoformat()}')
    else:
        if filter_year is not None:
            parts.append(f'year={filter_year}')
        if filter_month is not None:
            parts.append(f'month={filter_month}')
    return '&'.join(parts)


def _get_default_invoice_dates():
    """Return a sensible default invoice period."""
    today = date.today()
    return today.replace(day=1), today


@app.route('/monthly')
def monthly_totals():
    """Show monthly totals per cleaner."""
    filter_year, filter_month, filter_date_from, filter_date_to, using_default_filter = (
        _resolve_monthly_filter_params(request.args)
    )
    
    monthly_data, category_totals = get_monthly_totals(
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=filter_date_from,
        date_to=filter_date_to
    )
    
    years, months = get_tenant_log_year_month_options()

    filter_query_string = (
        request.query_string.decode()
        if request.query_string
        else _monthly_filter_query_string(
            filter_year, filter_month, filter_date_from, filter_date_to
        )
    )
    
    return render_template('monthly.html', 
                         monthly_data=monthly_data,
                         category_totals=category_totals,
                         filter_year=filter_year,
                         filter_month=filter_month,
                         filter_date_from=filter_date_from,
                         filter_date_to=filter_date_to,
                         using_default_filter=using_default_filter,
                         filter_query_string=filter_query_string,
                         available_years=years,
                         available_months=months)


@app.route('/invoices')
def invoices():
    """Preview invoices for individual staff members."""
    cleaners = cleaners_query().order_by(Cleaner.name).all()
    default_from, default_to = _get_default_invoice_dates()
    cleaner_id = request.args.get('cleaner_id', type=int)
    filter_date_from = _parse_date_arg(request.args.get('date_from')) or default_from
    filter_date_to = _parse_date_arg(request.args.get('date_to')) or default_to

    invoice_data = None
    selected_cleaner = None

    if filter_date_from > filter_date_to:
        flash('The invoice start date must be before the end date', 'error')
        filter_date_from, filter_date_to = default_from, default_to

    if cleaner_id:
        selected_cleaner = get_cleaner_for_owner(cleaner_id)
        if not selected_cleaner:
            flash('Staff member not found', 'error')
        else:
            invoice_data = build_staff_invoice_data(selected_cleaner, filter_date_from, filter_date_to)

    return render_template(
        'invoices.html',
        cleaners=cleaners,
        selected_cleaner_id=cleaner_id,
        selected_cleaner=selected_cleaner,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
        invoice_data=invoice_data,
        reportlab_available=REPORTLAB_AVAILABLE
    )


@app.route('/invoices/pdf')
def invoice_pdf():
    """Generate a PDF invoice for a staff member."""
    cleaner_id = request.args.get('cleaner_id', type=int)
    filter_date_from = _parse_date_arg(request.args.get('date_from'))
    filter_date_to = _parse_date_arg(request.args.get('date_to'))

    if not cleaner_id or not filter_date_from or not filter_date_to:
        flash('Please choose a staff member and invoice date range', 'error')
        return redirect(url_for('invoices'))

    if filter_date_from > filter_date_to:
        flash('The invoice start date must be before the end date', 'error')
        return redirect(url_for('invoices', cleaner_id=cleaner_id, date_from=filter_date_from.isoformat(), date_to=filter_date_to.isoformat()))

    cleaner = get_cleaner_for_owner(cleaner_id)
    if not cleaner:
        flash('Staff member not found', 'error')
        return redirect(url_for('invoices'))

    if not REPORTLAB_AVAILABLE:
        flash('PDF generation requires reportlab. Use the print button to save the invoice as PDF for now.', 'error')
        return redirect(url_for('invoices', cleaner_id=cleaner_id, date_from=filter_date_from.isoformat(), date_to=filter_date_to.isoformat()))

    invoice_data = build_staff_invoice_data(cleaner, filter_date_from, filter_date_to)

    from io import BytesIO
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch
    )

    elements = []
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'InvoiceTitle',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#000000'),
        spaceAfter=12,
        alignment=TA_CENTER
    )
    meta_style = ParagraphStyle(
        'InvoiceMeta',
        parent=styles['BodyText'],
        fontSize=9,
        leading=12,
        alignment=TA_LEFT
    )

    elements.append(Paragraph("Staff Invoice", title_style))
    elements.append(Paragraph(f"Invoice No: {invoice_data['invoice_number']}", meta_style))
    elements.append(Paragraph(f"Issue Date: {invoice_data['issue_date'].strftime('%d %b %Y')}", meta_style))
    elements.append(Paragraph(f"Staff Member: {cleaner.name}", meta_style))
    elements.append(Paragraph(f"Category: {cleaner.category or 'Uncategorized'}", meta_style))
    elements.append(Paragraph(f"Period: {filter_date_from.strftime('%d %b %Y')} to {filter_date_to.strftime('%d %b %Y')}", meta_style))
    elements.append(Paragraph(f"Pay Basis: {invoice_data['rate_label']}", meta_style))
    elements.append(Spacer(1, 0.2 * inch))

    table_data = [['Date', 'Details', 'Qty', 'Rate', 'Amount']]
    if invoice_data['line_items']:
        for item in invoice_data['line_items']:
            table_data.append([
                item['date'].strftime('%Y-%m-%d'),
                Paragraph(item['description'], styles['BodyText']),
                item['quantity_display'],
                item['rate_display'],
                f"R{item['amount']:.2f}"
            ])
    else:
        table_data.append(['-', 'No logs found for this period', '-', '-', 'R0.00'])

    table_data.append(['', '', '', 'Total', f"R{invoice_data['amount_total']:.2f}"])

    table = Table(table_data, colWidths=[0.9 * inch, 3.7 * inch, 0.8 * inch, 1.3 * inch, 1.0 * inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('BACKGROUND', (0, 1), (-1, -2), colors.beige),
        ('FONTNAME', (-2, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (-2, -1), (-1, -1), colors.lightgrey)
    ]))
    elements.append(table)
    elements.append(Spacer(1, 0.2 * inch))
    elements.append(Paragraph(f"Entries: {invoice_data['entries_count']} | Hours: {invoice_data['hours_total']:.2f}", meta_style))

    doc.build(elements)
    buffer.seek(0)
    pdf_data = buffer.getvalue()
    buffer.close()

    filename = f"invoice_{cleaner.name.replace(' ', '_').lower()}_{filter_date_from.strftime('%Y%m%d')}_{filter_date_to.strftime('%Y%m%d')}.pdf"
    return Response(
        pdf_data,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/monthly/pdf')
def monthly_totals_pdf():
    """Generate PDF of monthly totals."""
    if not REPORTLAB_AVAILABLE:
        flash('PDF generation requires reportlab package. Install with: pip install reportlab', 'error')
        return redirect(url_for('monthly_totals'))
    
    filter_year, filter_month, filter_date_from, filter_date_to, _ = (
        _resolve_monthly_filter_params(request.args)
    )
    
    monthly_data, _ = get_monthly_totals(
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=filter_date_from,
        date_to=filter_date_to
    )
    
    # Create PDF in memory
    from io import BytesIO
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), 
                            rightMargin=0.5*inch, leftMargin=0.5*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    
    # Container for the 'Flowable' objects
    elements = []
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#000000'),
        spaceAfter=20,
        alignment=TA_CENTER
    )
    
    # Add title
    title = Paragraph("Monthly Totals - Cleaning Time Logger", title_style)
    elements.append(title)
    elements.append(Spacer(1, 0.2*inch))
    
    # Prepare table data
    table_data = [['FOH Staff', 'Category', 'Month', 'Total Entries', 'Hours Logged', 'Rate', 'Total']]
    
    for category_name, staff_members in monthly_data.items():
        # Add category header row
        table_data.append([f"{category_name}:", '', '', '', '', '', ''])
        
        for cleaner_name, cleaner_data in sorted(staff_members.items()):
            for month_key, month_data in sorted(cleaner_data['months'].items()):
                year, month = month_key.split('-')
                month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June', 
                              'July', 'August', 'September', 'October', 'November', 'December']
                month_name = f"{month_names[int(month)]} {year}"
                
                table_data.append([
                    cleaner_name,
                    cleaner_data['category'],
                    month_name,
                    str(month_data['entries']),
                    f"{month_data['hours']:.2f}",
                    month_data['rate_label'],
                    f"R{month_data['total']:.2f}"
                ])
    
    # Create table
    table = Table(table_data, colWidths=[1.5*inch, 1*inch, 1.2*inch, 0.8*inch, 0.8*inch, 1*inch, 1*inch])
    
    # Style the table
    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
    ])
    
    # Style category headers
    row_idx = 1  # Start after header row
    for category_name, staff_members in monthly_data.items():
        # Style the category header row
        table_style.add('BACKGROUND', (0, row_idx), (-1, row_idx), colors.lightblue)
        table_style.add('FONTNAME', (0, row_idx), (-1, row_idx), 'Helvetica-Bold')
        table_style.add('FONTSIZE', (0, row_idx), (-1, row_idx), 10)
        row_idx += 1
        # Count data rows for this category
        for cleaner_name, cleaner_data in sorted(staff_members.items()):
            row_idx += len(cleaner_data['months'])
    
    table.setStyle(table_style)
    elements.append(table)
    
    # Build PDF
    doc.build(elements)
    
    # Get PDF data
    buffer.seek(0)
    pdf_data = buffer.getvalue()
    buffer.close()
    
    # Generate filename with current date
    filename = f"monthly_totals_{datetime.now().strftime('%Y%m%d')}.pdf"
    
    return Response(
        pdf_data,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/by-date')
def by_date_view():
    """Show dates categorized by staff member. Clicking on a date shows all staff who worked that day."""
    filter_year, filter_month, filter_date_from, filter_date_to, using_default_filter = (
        _resolve_monthly_filter_params(request.args)
    )

    staff_by_category = get_staff_by_category_with_dates(
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=filter_date_from,
        date_to=filter_date_to,
    )
    date_data = get_staff_by_date(
        filter_year=filter_year,
        filter_month=filter_month,
        date_from=filter_date_from,
        date_to=filter_date_to,
    )
    display_categories = sort_display_categories(staff_by_category.keys())

    years, months = get_tenant_log_year_month_options()

    return render_template(
        'by_date.html',
        staff_by_category=staff_by_category,
        display_categories=display_categories,
        date_data=date_data,
        filter_year=filter_year,
        filter_month=filter_month,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
        using_default_filter=using_default_filter,
        available_years=years,
        available_months=months,
    )

@app.route('/archive-cleaner', methods=['POST'])
def archive_cleaner():
    """Archive a FOH staff member from hours (they won't appear for new shifts; past hours kept)."""
    cleaner_id = request.form.get('cleaner_id')
    if not cleaner_id:
        flash('Invalid request', 'error')
        return redirect(url_for('index'))
    try:
        cid = int(cleaner_id)
        cleaner = get_cleaner_for_owner(cid)
        if not cleaner:
            flash('Staff member not found', 'error')
            return redirect(url_for('index'))
        cleaner.active = False
        db.session.commit()
        flash(f'"{cleaner.name}" has been archived from hours. They will not appear when logging new shifts; past hours are unchanged.', 'success')
    except (ValueError, TypeError):
        flash('Invalid request', 'error')
    except Exception:
        db.session.rollback()
        flash('Error archiving staff', 'error')
    return redirect(url_for('index'))


@app.route('/restore-cleaner', methods=['POST'])
def restore_cleaner():
    """Restore an archived FOH staff member so they can receive new shifts again."""
    cleaner_id = request.form.get('cleaner_id')
    if not cleaner_id:
        flash('Invalid request', 'error')
        return redirect(url_for('index'))
    try:
        cid = int(cleaner_id)
        cleaner = get_cleaner_for_owner(cid)
        if not cleaner:
            flash('Staff member not found', 'error')
            return redirect(url_for('index'))
        cleaner.active = True
        db.session.commit()
        flash(f'"{cleaner.name}" has been restored. They will appear when logging shifts again.', 'success')
    except (ValueError, TypeError):
        flash('Invalid request', 'error')
    except Exception:
        db.session.rollback()
        flash('Error restoring staff', 'error')
    return redirect(url_for('index'))


@app.route('/add-staff', methods=['POST'])
def add_staff():
    """Add a new FOH staff member."""
    name = request.form.get('name')
    category_field = request.form.get('category')
    new_category_field = request.form.get('new_category', '')
    raw_rate_type = request.form.get('rate_type')
    rate_amount_raw = request.form.get('rate_amount')
    
    category = resolve_staff_category_from_form(category_field, new_category_field)
    if not name or not category or not rate_amount_raw:
        flash('Please fill in all required fields', 'error')
        return redirect(url_for('index'))
    
    if category_field == '__new__' and not normalize_category_name(new_category_field):
        flash('Please enter a name for the new category', 'error')
        return redirect(url_for('index'))
    
    if raw_rate_type not in ['hourly', 'daily', 'monthly']:
        flash('Invalid rate type selected', 'error')
        return redirect(url_for('index'))
    
    try:
        rate_type = normalize_rate_type(raw_rate_type)
        rate_amount = float(rate_amount_raw)
        flat_monthly = request.form.get('flat_monthly') == 'on'
        if rate_amount <= 0:
            flash('Rate amount must be greater than zero', 'error')
            return redirect(url_for('index'))
        if flat_monthly and rate_type != 'monthly':
            flash('Flat rate only applies to monthly staff', 'error')
            return redirect(url_for('index'))

        # Check if staff member already exists
        owner_id = get_current_owner_id()
        if owner_id is None:
            flash('Please log in to add staff.', 'error')
            return redirect(url_for('login'))

        existing = cleaners_query().filter_by(name=name).first()
        if existing:
            flash(f'Staff member "{name}" already exists!', 'error')
            return redirect(url_for('index'))
        
        # Create new staff member
        new_cleaner = Cleaner(
            owner_id=owner_id,
            name=name,
            category=category,
            rate_type=rate_type,
            rate_amount=rate_amount,
            flat_monthly=flat_monthly
        )
        db.session.add(new_cleaner)
        db.session.commit()
        
        flash(
            f'Staff member "{name}" added successfully as {category} with '
            f'{format_rate_label(rate_type, rate_amount, flat_monthly=flat_monthly)}.',
            'success'
        )
        
    except (ValueError, TypeError):
        flash('Invalid rate amount provided', 'error')
    except Exception as e:
        flash('Error adding staff member', 'error')
        db.session.rollback()
    
    return redirect(url_for('index'))

@app.route('/clear-db', methods=['POST'])
def clear_db():
    """Delete all staff and logs. For testing only."""
    try:
        owner_id = get_current_owner_id()
        if owner_id is None:
            flash('Please log in to clear data.', 'error')
            return redirect(url_for('login'))

        tenant_cleaner_ids = [
            row[0] for row in db.session.query(Cleaner.id).filter_by(owner_id=owner_id).all()
        ]
        log_count = (
            TimeLog.query.filter(TimeLog.cleaner_id.in_(tenant_cleaner_ids)).count()
            if tenant_cleaner_ids else 0
        )
        staff_count = len(tenant_cleaner_ids)
        if tenant_cleaner_ids:
            TimeLog.query.filter(TimeLog.cleaner_id.in_(tenant_cleaner_ids)).delete(
                synchronize_session=False
            )
        Cleaner.query.filter_by(owner_id=owner_id).delete(synchronize_session=False)
        db.session.commit()
        flash(f'Database cleared ({log_count} log(s), {staff_count} staff member(s) removed).', 'success')
    except Exception:
        db.session.rollback()
        flash('Error clearing database', 'error')
    return redirect(url_for('index'))


@app.route('/init-db')
def init_db():
    """Initialize database with sample data."""
    db.create_all()
    ensure_cleaner_schema()
    ensure_time_log_schema()
    
    owner_id = get_current_owner_id()
    if owner_id is None:
        flash('Please log in to initialize sample data.', 'error')
        return redirect(url_for('login'))

    # Add sample cleaners if none exist for this account
    if not cleaners_query().first():
        sample_cleaners = [
            Cleaner(owner_id=owner_id, name='Nancy Z'),
            Cleaner(owner_id=owner_id, name='Ettie T'),
            Cleaner(owner_id=owner_id, name='Daniel B'),
            Cleaner(owner_id=owner_id, name='Lina Y'),
            Cleaner(owner_id=owner_id, name='Stephan M')
        ]
        db.session.add_all(sample_cleaners)
        db.session.commit()
        flash('Database initialized with your cleaners!', 'success')
    
    return redirect(url_for('index'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        ensure_cleaner_schema()
        ensure_time_log_schema()
        ensure_user_schema()
        ensure_password_reset_schema()
    app.run(debug=True)
