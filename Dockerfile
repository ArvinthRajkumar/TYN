# Python 3.12.10 to match local development and CI; pinned by digest.
FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime dependencies only, hash-checked.
COPY requirements.txt .
RUN python -m pip install --require-hashes -r requirements.txt

# Unprivileged user; reports/, state/ and inputs/ are mounted at run time.
RUN useradd --create-home --uid 10001 tyn \
    && mkdir -p /app/reports /app/state /app/inputs \
    && chown -R tyn:tyn /app

COPY --chown=tyn:tyn tyn ./tyn

USER tyn

# Configuration is supplied at run time with --env-file; no secrets are
# copied into the image (.env is excluded by .dockerignore).
ENTRYPOINT ["python", "-m", "tyn"]
CMD ["--help"]
