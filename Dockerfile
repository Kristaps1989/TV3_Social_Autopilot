FROM python:3.12-slim

WORKDIR /srv/autopilot
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt psycopg2-binary
# Chromium for the carousel card renderer (app/cards.py)
RUN playwright install --with-deps chromium
# ffmpeg for the slideshow reel builder (app/reels.py)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
