# Agent server image. Build context is the repo root (see docker-compose.yml) so the
# `agent` and `cli` packages install from pyproject.toml. pytest is installed too
# because the sandbox runs the target repo's tests inside this container.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY agent ./agent
COPY cli ./cli
RUN pip install --no-cache-dir . && mkdir -p /tmp/fixit /workspace
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
