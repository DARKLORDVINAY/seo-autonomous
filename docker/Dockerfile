FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.lock.txt /app/requirements.lock.txt
RUN pip install --no-cache-dir --requirement requirements.lock.txt \
    && groupadd --gid 10001 seo \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin seo

COPY --chown=10001:10001 backend /app/backend
COPY --chown=10001:10001 seo_mcp /app/seo_mcp
COPY --chown=10001:10001 dashboard /app/dashboard
COPY --chown=10001:10001 scripts /app/scripts
COPY --chown=10001:10001 test_lab /app/test_lab
COPY --chown=10001:10001 docker /app/docker
COPY --chown=10001:10001 alembic.ini pyproject.toml /app/

USER 10001:10001
EXPOSE 8000 8001
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["api"]
