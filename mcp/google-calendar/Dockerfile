FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src


FROM base AS test

RUN pip install --no-cache-dir -e ".[test]"

COPY tests ./tests

CMD ["python", "-m", "pytest"]


FROM base AS runtime

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["python", "-m", "google_calendar_mcp"]
