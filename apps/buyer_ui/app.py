"""Run from the repository: python -m streamlit run apps/buyer_ui/app.py."""
import os
from pathlib import Path
import sys

# Streamlit executes this file directly rather than as a package.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from apps.buyer_ui.client import ApiError, HttpClient, MockClient
from apps.buyer_ui.formatting import show_api_error
from apps.buyer_ui.secondary_views import render_data, render_scenarios
from apps.buyer_ui.views import render_orders


def _http_client(base_url):
    configured_url = os.getenv("BUYER_API_URL", "http://127.0.0.1:8000")
    token = os.getenv("BUYER_API_TOKEN")
    if token and base_url.strip().rstrip("/") != configured_url.strip().rstrip("/"):
        raise ValueError("Адрес API с учётными данными задаётся конфигурацией сервера.")
    return HttpClient(base_url, timeout=10, token=token)


def _clear_context(mode):
    # UI drafts/requests are scoped to the selected API. No business state is inferred here.
    keep = {"connection_mode", "connection_url", "mock_quality"}
    for key in list(st.session_state):
        if key not in keep:
            del st.session_state[key]
    mock = mode == "mock"
    st.session_state["snapshot_id"] = os.getenv("BUYER_SNAPSHOT_ID", "demo-snapshot" if mock else "")
    st.session_state["run_id"] = os.getenv("BUYER_RUN_ID", "demo-run" if mock else "")
    st.session_state["data_mode"] = "synthetic_demo"
    st.session_state["as_of"] = "2026-09-01T00:00:00+00:00"
    st.session_state["mapping_version"] = "demo-mapping-v1" if mock else "1.0"
    st.session_state["policy_version"] = "1.0"
    st.session_state["base_seed"] = int(os.getenv("BUYER_SEED", "42"))


def main():
    st.set_page_config(page_title="Закупки · CODEX MONSTERS", page_icon="📦", layout="wide")
    # A table download would omit the API's approval/version and DEMO watermark.
    # Keep the deliberate, backend-generated export as the sole CSV control.
    st.set_option("client.disableDataExport", True)
    st.title("Закупки с объяснением")
    st.caption("Предложения поставщикам · проверка покупателем · утверждённый CSV")
    with st.sidebar:
        st.header("Подключение")
        default_mode = os.getenv("BUYER_UI_MODE", "mock")
        if default_mode not in ("mock", "http"):
            st.error("BUYER_UI_MODE должен быть mock или http.")
            st.stop()
        mode = st.selectbox("Режим интерфейса", ("mock", "http"), index=0 if default_mode == "mock" else 1,
                            format_func=lambda value: "Демо: имитация API" if value == "mock" else "HTTP API",
                            key="connection_mode")
        base_url = st.text_input("Адрес API", value=os.getenv("BUYER_API_URL", "http://127.0.0.1:8000"),
                                 key="connection_url", disabled=mode == "mock" or bool(os.getenv("BUYER_API_TOKEN")))
        if os.getenv("BUYER_API_TOKEN"):
            st.caption("Адрес подключения зафиксирован конфигурацией сервера.")
        default_quality = os.getenv("BUYER_MOCK_QUALITY", "ready")
        if default_quality not in ("ready", "degraded", "blocked"):
            st.error("BUYER_MOCK_QUALITY должен быть ready, degraded или blocked.")
            st.stop()
        quality = st.selectbox("Набор демо-данных", ("ready", "degraded", "blocked"),
                               index=("ready", "degraded", "blocked").index(default_quality),
                               key="mock_quality", disabled=mode != "mock")
    connection = (mode, base_url, quality)
    if st.session_state.get("connection") != connection:
        try:
            _clear_context(mode)
            st.session_state["client"] = MockClient(quality=quality) if mode == "mock" else _http_client(base_url)
        except (ValueError, OSError) as error:
            st.error(f"Не удалось настроить подключение: {error}")
            st.stop()
        st.session_state["connection"] = connection
    st.session_state["client_mode"] = mode
    client = st.session_state["client"]
    if mode == "mock":
        st.warning("Демо: имитация API · Все данные синтетические. Результаты — заранее подготовленные "
                   "примеры интерфейса; расчёты backend этим режимом не проверяются.")
    else:
        st.info("HTTP API · Источник данных и разрешения определяются ответами сервера.")
    st.caption("Демонстрационная учётная запись · роль задаёт сервер · поставщикам ничего не отправляется")
    with st.sidebar:
        st.divider()
        if st.button("Проверить подключение", key="health_check"):
            try:
                health = client.health()
                if health.get("status") == "ok":
                    st.success("API отвечает · " + str(health.get("version", "версия не сообщена")))
                else:
                    st.warning("API не подтвердил готовность.")
            except ApiError as error:
                show_api_error(error)
        st.write("**Выбранный контекст**")
        st.caption("Снимок: " + (st.session_state.get("snapshot_id") or "не выбран"))
        st.caption("Расчёт: " + (st.session_state.get("run_id") or "не выбран"))
        if mode == "mock" and st.button("Сбросить локальное демо", key="reset_demo"):
            _clear_context(mode)
            st.rerun()
        st.caption("В HTTP-режиме ошибка соединения остаётся ошибкой; демонстрационные ответы не подставляются.")
    orders, scenarios, data = st.tabs(["Заказы", "Сценарии", "Данные/проекты"])
    with orders:
        render_orders(client)
    with scenarios:
        render_scenarios(client)
    with data:
        render_data(client)


if __name__ == "__main__":
    main()
