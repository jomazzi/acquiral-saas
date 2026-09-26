# Portable container image for Acquiral -- works unmodified on Render,
# Railway, Fly.io, DigitalOcean App Platform, or any other host that can
# run a Docker image. See DEPLOYMENT.md for the actual deploy steps.
FROM python:3.11-slim

# psycopg2-binary ships its own libpq, so no extra system packages are
# needed for Postgres connectivity. libpango/libcairo etc. are NOT
# needed either -- reportlab (PDF generation) is pure Python, which was
# chosen specifically so this image stays small and dependency-free.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    FLASK_APP=run.py

EXPOSE 8000

# Migrations + RLS setup are run as a separate release/pre-deploy step
# (see DEPLOYMENT.md and scripts/release.sh) -- NOT here, so that a
# container that fails to start doesn't leave the database mid-migration,
# and so scaling to multiple instances doesn't run migrations more than
# once per deploy.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60", "run:app"]
