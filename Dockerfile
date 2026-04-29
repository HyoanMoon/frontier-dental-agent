# syntax=docker/dockerfile:1.7

# Stage 1 — install Python deps in a builder layer so we can cache pip
# downloads independently from app source changes.
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# Stage 2 — runtime image. Slim, non-root, deterministic entrypoint.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Create an unprivileged user. The scraper does not need root for any of its
# operations and avoiding root is a basic production hygiene step.
RUN groupadd --system --gid 1000 scraper \
 && useradd  --system --uid 1000 --gid scraper --create-home scraper

WORKDIR /app

# Bring installed packages over from the builder stage.
COPY --from=builder /install /usr/local

# Copy only what the runtime needs. .dockerignore strips out venv/, .git, etc.
COPY --chown=scraper:scraper src/    ./src/
COPY --chown=scraper:scraper config/ ./config/
COPY --chown=scraper:scraper README.md requirements.txt ./

# Ensure /app/output is writable by the unprivileged user. Mount a host
# directory here at runtime to extract the SQLite DB and the JSON/CSV exports.
RUN mkdir -p /app/output && chown -R scraper:scraper /app/output
VOLUME ["/app/output"]

USER scraper

# Default command runs the full pipeline against the seed categories defined
# in config/config.yaml. Override with `docker run ... <cmd>` to run other
# subcommands (e.g. `docker run ... export --format csv`).
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["run"]
