FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first for layer caching, then the
# Playwright-managed Chromium with its system dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
  && playwright install --with-deps chromium \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python3", "-m", "hypercorn", "main:app", "--bind", "0.0.0.0:8000"]
