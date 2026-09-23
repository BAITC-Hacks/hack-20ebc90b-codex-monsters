FROM ghcr.io/astral-sh/uv:0.11.14 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    EKT_DATA_DIR=/app/var \
    BUYER_LOCK_CONNECTION=true \
    PORT=8501
WORKDIR /app
RUN groupadd --gid 10001 ekt && useradd --uid 10001 --gid ekt --create-home ekt \
    && mkdir /app/var && chown ekt:ekt /app/var
COPY --from=build /app/.venv /app/.venv
COPY apps/buyer_ui ./apps/buyer_ui
COPY assets/demo/ui-fixtures ./assets/demo/ui-fixtures
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY scripts/run_app.py scripts/check_health.py scripts/smoke_api.py ./scripts/
USER ekt
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "scripts/check_health.py"]
CMD ["python", "scripts/run_app.py", "--host", "0.0.0.0"]
