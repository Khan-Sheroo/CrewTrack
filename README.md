# CrewTrack

Web app for logging FOH staff shifts and hours, calculating wages, and producing reports for a hospitality team.

## Features

- **Admin dashboard** — staff counts, hours, wage bill, month-over-month category comparison
- **Log shifts / time** — single or multi-date calendar logging with timeslot queue
- **Weekly & monthly totals** — filtered by current month by default, with category breakdowns and pie charts
- **Work by date** — see who worked on which days, grouped by category
- **Invoices** — PDF invoices per staff member (requires ReportLab)
- **Manage logs** — edit, delete, and browse entries by category
- **Pay rules** — hourly, daily, and monthly rates; Sunday 1.5× and SA public holiday 2× for hourly staff

## Tech stack

- Python 3.10+
- Flask + SQLAlchemy
- SQLite
- Bootstrap 5 + Chart.js

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/YOUR_USERNAME/cleaning_time_logger.git
   cd cleaning_time_logger
   ```

2. **Create a virtual environment**

   ```bash
   python -m venv venv
   ```

   Windows:

   ```bash
   venv\Scripts\activate
   ```

   macOS / Linux:

   ```bash
   source venv/bin/activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**

   ```bash
   copy .env.example .env
   ```

   Edit `.env` and set a strong `SECRET_KEY`.

   For **forgot password** emails, configure SMTP in `.env` (see `.env.example`). Without SMTP, reset links are printed to the terminal when you request a reset (fine for local development).

5. **Run the app**

   ```bash
   python app.py
   ```

   Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

## Seed test data

Populate the database with 10 staff members and two months of sample logs (April & May 2026):

```bash
python seed.py --clear
```

This also creates a demo login **`demo@crewtrack.local`** / **`demo12345`** if that account does not exist yet. Each registered email has its own isolated staff and logs.

## Project layout

| Path | Purpose |
|------|---------|
| `app.py` | Flask app, models, pay logic, routes |
| `seed.py` | Test data generator |
| `templates/` | HTML templates |
| `instance/` | Local SQLite database (created at runtime, not committed) |

## GitHub push (first time)

After cloning locally or initializing git:

```bash
git init
git add .
git commit -m "Initial commit: CrewTrack staff time logger"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/cleaning_time_logger.git
git push -u origin main
```

Create an empty repository on GitHub first (no README or .gitignore — this repo includes them).

## Notes

- The SQLite database is gitignored. Use `seed.py` or log entries through the UI on a fresh install.
- PDF export needs `reportlab` (included in `requirements.txt`).
- `FLASK_DEBUG=1` in `.env` is for local development only.
