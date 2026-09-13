# Control-plane image. Build context is the repo root (see docker-compose.yml) so the
# `api` package installs from pyproject.toml. Dependencies are installed in their own
# layer *before* the source is copied, so a code change rebuilds in seconds.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
# Do not emit the *_created companion gauges: they double the series count for no value.
ENV PROMETHEUS_DISABLE_CREATED_SERIES=True
WORKDIR /app
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt
COPY api ./api
RUN pip install --no-cache-dir --no-deps .
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
