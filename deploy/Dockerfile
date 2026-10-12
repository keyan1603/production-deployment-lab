# One image for all four services; the command chooses which one runs.
#   python scripts/vendor_system.py && podman build -f deploy/Dockerfile -t production-deployment-lab .   (docker build works the same)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements-runtime.txt .
RUN pip install -r requirements-runtime.txt

# The system under deployment (the AI System Design lab), exported by scripts/vendor_system.py.
COPY vendor/system /system
RUN pip install --no-deps /system

COPY deploylab ./deploylab
COPY services ./services
COPY releases ./releases
COPY sim ./sim

# Run as an unprivileged user; nothing in these services needs to write to the image.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

# Liveness only: the process answers. Readiness is what the orchestrator routes traffic on.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'),timeout=2)"

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
CMD ["python", "-m", "services.ticket_service"]
