FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml README.md uv.lock ./
COPY clauseai ./clauseai
COPY templates ./templates
COPY skills ./skills
COPY examples ./examples

RUN uv sync --no-dev --frozen

ENV ENVIRONMENT=production
ENV PORT=8000
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "clauseai.main:app", "--host", "0.0.0.0", "--port", "8000"]
