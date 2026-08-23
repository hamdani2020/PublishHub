ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm

FROM ${PYTHON_IMAGE} AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt ./

RUN pip install --requirement requirements.txt

FROM ${PYTHON_IMAGE} AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    
    PYTHONUNBUFFERED=1 \
    
    PYTHONDONTWRITEBYTECODE=1 \
    
    PYTHONHASHSEED=random \
    
    PYTHONPATH=/app/src

RUN groupadd --system --gid 10001 worker \
 && useradd --system --uid 10001 --gid 10001 \
      --home-dir /app --no-create-home --shell /usr/sbin/nologin worker

WORKDIR /app

COPY --from=deps --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root src ./src

USER worker

CMD ["python", "-m", "publishhub_worker"]
