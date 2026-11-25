FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system bms \
    && useradd --system --gid bms bms

COPY --chown=bms:bms manage.py .
COPY --chown=bms:bms config config
COPY --chown=bms:bms accounts accounts
COPY --chown=bms:bms bookings bookings
COPY --chown=bms:bms mailimport mailimport
COPY --chown=bms:bms tests tests
USER bms

CMD ["gunicorn", "--no-control-socket", "--bind", "0.0.0.0:8000", "config.wsgi:application"]
