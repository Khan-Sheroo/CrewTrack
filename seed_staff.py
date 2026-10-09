"""
Copy active staff members between databases (e.g. local -> production).

Step 1, on the machine with the source data:
    python seed_staff.py export --email sheroo-khan@outlook.com --out staff_seed.json

Step 2, copy staff_seed.json to the production server, then:
    python seed_staff.py import --email sheroo-khan@outlook.com --file staff_seed.json --dry-run
    python seed_staff.py import --email sheroo-khan@outlook.com --file staff_seed.json

The account must already exist in the target database (register / log in once first).
Staff that already exist (same name) are skipped unless --update is given.
Shifts, time logs and leave balances are not copied.
Set DATABASE_URL to point at a different database if needed.
"""
import argparse
import json
import sys

from app import (
    app,
    db,
    Cleaner,
    User,
    ensure_cleaner_schema,
    ensure_time_log_schema,
    ensure_user_schema,
    normalize_email,
    normalize_employment_type,
    normalize_rate_type,
    normalize_residency,
)

STAFF_FIELDS = (
    'name',
    'id_number',
    'category',
    'rate_type',
    'rate_amount',
    'flat_monthly',
    'residency',
    'employment_type',
)


def find_user(email: str) -> User:
    normalized = normalize_email(email)
    user = User.query.filter(db.func.lower(User.email) == normalized).first() if normalized else None
    if not user:
        sys.exit(f'No account found for {email}. Register / log in once on this server first.')
    return user


def prepare_schema() -> None:
    db.create_all()
    ensure_user_schema()
    ensure_cleaner_schema()
    ensure_time_log_schema()


def export_staff(email: str, out_path: str) -> None:
    user = find_user(email)
    staff = Cleaner.query.filter_by(owner_id=user.id, active=True).order_by(Cleaner.name).all()
    payload = [{field: getattr(cleaner, field) for field in STAFF_FIELDS} for cleaner in staff]
    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f'Exported {len(payload)} active staff for {user.email} to {out_path}')


def import_staff(email: str, file_path: str, update: bool, dry_run: bool) -> None:
    user = find_user(email)
    with open(file_path, encoding='utf-8') as handle:
        records = json.load(handle)

    created = updated = skipped = 0
    for record in records:
        name = (record.get('name') or '').strip()[:100]
        if not name:
            continue
        values = {
            'id_number': (record.get('id_number') or None),
            'category': record.get('category') or None,
            'rate_type': normalize_rate_type(record.get('rate_type')),
            'rate_amount': record.get('rate_amount'),
            'flat_monthly': bool(record.get('flat_monthly')),
            'residency': normalize_residency(record.get('residency')),
            'employment_type': normalize_employment_type(record.get('employment_type')),
        }

        if values['id_number']:
            clash = Cleaner.query.filter(
                Cleaner.owner_id == user.id,
                Cleaner.id_number == values['id_number'],
                Cleaner.name != name,
            ).first()
            if clash:
                print(f'  ! {name}: ID number {values["id_number"]} already belongs to {clash.name}; leaving it blank')
                values['id_number'] = None

        existing = Cleaner.query.filter_by(owner_id=user.id, name=name).first()
        if existing:
            if not update:
                print(f'  - {name}: already exists, skipped')
                skipped += 1
                continue
            for field, value in values.items():
                setattr(existing, field, value)
            existing.active = True
            print(f'  ~ {name}: updated')
            updated += 1
            continue

        db.session.add(Cleaner(owner_id=user.id, name=name, active=True, **values))
        print(f'  + {name}: added ({values["category"]}, {values["rate_type"]} {values["rate_amount"]})')
        created += 1

    if dry_run:
        db.session.rollback()
        print(f'DRY RUN - nothing saved. Would add {created}, update {updated}, skip {skipped}.')
    else:
        db.session.commit()
        print(f'Done for {user.email}: added {created}, updated {updated}, skipped {skipped}.')


def main() -> None:
    parser = argparse.ArgumentParser(description='Export / import active staff between databases.')
    sub = parser.add_subparsers(dest='command', required=True)

    export_parser = sub.add_parser('export', help='Write active staff to a JSON file')
    export_parser.add_argument('--email', required=True)
    export_parser.add_argument('--out', default='staff_seed.json')

    import_parser = sub.add_parser('import', help='Load staff from a JSON file')
    import_parser.add_argument('--email', required=True)
    import_parser.add_argument('--file', default='staff_seed.json')
    import_parser.add_argument('--update', action='store_true', help='Overwrite details of staff that already exist')
    import_parser.add_argument('--dry-run', action='store_true', help='Show what would happen without saving')

    args = parser.parse_args()
    with app.app_context():
        prepare_schema()
        if args.command == 'export':
            export_staff(args.email, args.out)
        else:
            import_staff(args.email, args.file, args.update, args.dry_run)


if __name__ == '__main__':
    main()
