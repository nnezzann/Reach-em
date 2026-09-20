FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# EXPOSE is only a hint for the default; the app binds the PORT env var
# (Render sets it), falling back to 8000.
EXPOSE 8000

# One process runs BOTH the Socket Mode handler and the /health server
# (src/reach_bot/app.py main() via asyncio.gather).
CMD ["python", "app.py"]
