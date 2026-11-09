# Playwright's image ships Chromium and its system libraries; the booking
# worker needs them and Railway's default Python builder can't install them.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}
