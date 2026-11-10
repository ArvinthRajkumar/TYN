# Python 3.12.10 to match local development and CI, pinned by digest.
FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --require-hashes -r requirements.txt


# Lint, type-check and test against the same base and pins.
# Build with --target test; this stage is never part of the runtime image.
FROM base AS test

COPY requirements-dev.txt .
RUN python -m pip install --require-hashes -r requirements-dev.txt

COPY pyproject.toml ./
COPY tyn ./tyn
COPY tests ./tests
COPY scripts ./scripts

CMD ["sh", "-c", "python -m ruff check . && python -m ruff format --check . && python -m mypy tyn tests scripts && python -m pytest -q -p no:cacheprovider"]


# Runtime image (default target).
FROM base AS runtime

# The code stays root-owned and read-only for the service user; only the
# reports/ and state/ volumes are writable.
RUN useradd --create-home --uid 10001 tyn \
    && mkdir -p /app/reports /app/state /app/inputs \
    && chown tyn:tyn /app/reports /app/state

COPY tyn ./tyn

USER tyn

# Configuration comes from the environment at run time (docker run --env-file).
ENTRYPOINT ["python", "-m", "tyn"]
CMD ["--help"]
