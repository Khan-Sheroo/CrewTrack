"""
Seed the database with test data for April and May 2026.

Usage:
    python seed.py           # add seed data (keeps existing rows)
    python seed.py --clear   # wipe staff + logs, then seed fresh data
"""

from __future__ import annotations

import argparse
import random
from calendar import monthrange
from datetime import date, datetime, time, timedelta

from app import (
    Cleaner,
    TimeLog,
    User,
    app,
    calculate_hours_worked,
    db,
    ensure_cleaner_schema,
    ensure_time_log_schema,
    ensure_user_schema,
)

SEED_RANDOM = random.Random(202604)

FIRST_NAMES = [
    "Thabo", "Zanele", "Liam", "Priya", "Jordan",
    "Amahle", "Connor", "Nomsa", "Ethan", "Lerato",
]
LAST_NAMES = [
    "Mokoena", "Ndlovu", "Pillay", "Botha", "Khumalo",
    "Smith", "van Wyk", "Dlamini", "Jacobs", "Nkosi",
]

CATEGORIES = ["Bartenders", "Barbacks", "Waiters", "Runners", "Manager", "Retail"]

RATE_PROFILES = [
    ("hourly", 52.0, 78.0),
    ("hourly", 48.0, 72.0),
    ("daily", 380.0, 520.0),
    ("daily", 400.0, 550.0),
    ("monthly", 5500.0, 7500.0),
    ("monthly", 6000.0, 8500.0),
    ("monthly", 7000.0, 9500.0),
    ("monthly", 8000.0, 11000.0),
    ("monthly", 9000.0, 12000.0),
    ("monthly", 5000.0, 6500.0),
]

SEED_MONTHS = [(2026, 4), (2026, 5)]


def round_rate(rate_type: str, amount: float) -> float:
    if rate_type == "hourly":
        return round(amount / 5) * 5 or 45.0
    if rate_type == "daily":
        return round(amount / 50) * 50 or 350.0
    return round(amount / 100) * 100 or 5000.0


def build_staff(owner_id: int) -> list[Cleaner]:
    names = list(zip(FIRST_NAMES, LAST_NAMES))
    SEED_RANDOM.shuffle(names)

    cleaners = []
    for index in range(10):
        first, last = names[index]
        category = CATEGORIES[index % len(CATEGORIES)]
        rate_type, rate_min, rate_max = RATE_PROFILES[index]
        rate_amount = round_rate(rate_type, SEED_RANDOM.uniform(rate_min, rate_max))
        flat_monthly = rate_type == "monthly" and index == 9

        cleaners.append(
            Cleaner(
                owner_id=owner_id,
                name=f"{first} {last}",
                category=category,
                rate_type=rate_type,
                rate_amount=rate_amount,
                flat_monthly=flat_monthly,
                active=True,
            )
        )
    return cleaners


def work_days_for_month(year: int, month: int, min_days: int = 18, max_days: int = 24) -> list[date]:
    """Pick a random subset of days in the month."""
    _, days_in_month = monthrange(year, month)
    all_days = [date(year, month, day) for day in range(1, days_in_month + 1)]
    count = SEED_RANDOM.randint(min_days, max_days)
    return sorted(SEED_RANDOM.sample(all_days, count))


def random_shift_times() -> tuple[time, time, float]:
    start_hour = SEED_RANDOM.randint(9, 16)
    duration = SEED_RANDOM.choice([4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0])
    start = time(start_hour, SEED_RANDOM.choice([0, 15, 30, 45]))
    end_dt = datetime.combine(date.today(), start) + timedelta(hours=duration)
    end = end_dt.time()
    hours = calculate_hours_worked(start, end)
    return start, end, hours


def logs_for_cleaner(cleaner: Cleaner, work_days: list[date]) -> list[TimeLog]:
    logs: list[TimeLog] = []
    rate_type = cleaner.rate_type or "monthly"

    if cleaner.flat_monthly:
        first_day = min(work_days)
        logs.append(
            TimeLog(
                cleaner_id=cleaner.id,
                date=first_day,
                log_type="shift",
                notes="Seed: flat monthly mark",
            )
        )
        time_days = SEED_RANDOM.sample(work_days, k=min(len(work_days), SEED_RANDOM.randint(8, 14)))
        for work_date in sorted(time_days):
            start, end, hours = random_shift_times()
            logs.append(
                TimeLog(
                    cleaner_id=cleaner.id,
                    date=work_date,
                    log_type="time",
                    start_time=start,
                    end_time=end,
                    hours_worked=hours,
                    notes="Seed: hours tracked",
                )
            )
        return logs

    if rate_type == "hourly":
        for work_date in work_days:
            start, end, hours = random_shift_times()
            logs.append(
                TimeLog(
                    cleaner_id=cleaner.id,
                    date=work_date,
                    log_type="time",
                    start_time=start,
                    end_time=end,
                    hours_worked=hours,
                )
            )
        return logs

    for work_date in work_days:
        logs.append(
            TimeLog(
                cleaner_id=cleaner.id,
                date=work_date,
                log_type="shift",
            )
        )
    return logs


def clear_database() -> None:
    TimeLog.query.delete()
    Cleaner.query.delete()
    User.query.delete()
    db.session.commit()


def ensure_seed_user(email: str = 'demo@crewtrack.local', password: str = 'demo12345') -> User:
    """Create or return the demo account used for seed data."""
    user = User.query.filter_by(email=email).first()
    if user:
        return user
    user = User(email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    print(f'Created login user: {email} / {password}')
    return user


def seed(clear: bool = False) -> None:
    with app.app_context():
        db.create_all()
        ensure_cleaner_schema()
        ensure_time_log_schema()
        ensure_user_schema()

        seed_user = ensure_seed_user()

        if clear:
            tenant_ids = [
                row[0] for row in db.session.query(Cleaner.id).filter_by(owner_id=seed_user.id).all()
            ]
            if tenant_ids:
                TimeLog.query.filter(TimeLog.cleaner_id.in_(tenant_ids)).delete(
                    synchronize_session=False
                )
            Cleaner.query.filter_by(owner_id=seed_user.id).delete(synchronize_session=False)
            db.session.commit()
            print("Cleared existing seed staff and logs for the demo account.")
        else:
            existing = Cleaner.query.filter_by(owner_id=seed_user.id).count()
            if existing:
                print(f"Demo account already has {existing} staff member(s).")
                print("Run with --clear to replace seed data for that account.")
                return

        cleaners = build_staff(seed_user.id)
        db.session.add_all(cleaners)
        db.session.commit()
        print(f"Created {len(cleaners)} staff members for {seed_user.email}.")

        total_logs = 0
        for year, month in SEED_MONTHS:
            month_label = date(year, month, 1).strftime("%B %Y")
            month_logs = 0
            for cleaner in cleaners:
                work_days = work_days_for_month(year, month)
                entries = logs_for_cleaner(cleaner, work_days)
                db.session.add_all(entries)
                month_logs += len(entries)
            db.session.commit()
            total_logs += month_logs
            print(f"  {month_label}: {month_logs} log entries")

        print(f"\nDone. {total_logs} total logs for April & May 2026.")
        print("Staff summary:")
        for cleaner in cleaners:
            flat = " (flat monthly)" if cleaner.flat_monthly else ""
            print(
                f"  - {cleaner.name}: {cleaner.category}, "
                f"{cleaner.rate_type} R{cleaner.rate_amount:.0f}{flat}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test data for CrewTrack")
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete all staff and logs before seeding",
    )
    args = parser.parse_args()
    seed(clear=args.clear)


if __name__ == "__main__":
    main()
