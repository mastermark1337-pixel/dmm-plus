FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7100

WORKDIR /app

# Stdlib only — no pip install step, no build deps, ~60 MB image.
COPY addon.py ./

# Non-root: this process makes outbound calls only, never writes to disk.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 7100

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','7100')+'/health', timeout=4)"

CMD ["python", "addon.py"]
