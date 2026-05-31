"""
Migration script to add active column to cleaner table (archive staff from hours).
Existing rows get active=1 (True).
"""
from app import app, db
from sqlalchemy import text


def migrate_cleaner_active():
    """Add active column to cleaner table if it doesn't exist."""
    with app.app_context():
        try:
            result = db.session.execute(text("""
                SELECT COUNT(*) as cnt
                FROM pragma_table_info('cleaner')
                WHERE name='active'
            """))
            column_exists = result.fetchone()[0] > 0

            if not column_exists:
                print("Adding active column to cleaner table...")
                db.session.execute(text("""
                    ALTER TABLE cleaner
                    ADD COLUMN active BOOLEAN NOT NULL DEFAULT 1
                """))
                db.session.commit()
                print("Successfully added active column (existing staff remain active)")
            else:
                print("active column already exists")

        except Exception as e:
            print(f"Error: {e}")
            db.session.rollback()
            raise


if __name__ == "__main__":
    migrate_cleaner_active()
