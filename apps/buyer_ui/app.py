"""Run from the repository: python -m streamlit run apps/buyer_ui/app.py."""
import os
from pathlib import Path
import sys

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.buyer_ui.client import ApiError, HttpClient, MockClient  # noqa: E402
from apps.buyer_ui.formatting import show_api_error  # noqa: E402
from apps.buyer_ui.presentation import brand, page_intro  # noqa: E402
from apps.buyer_ui.secondary_views import render_data, render_scenarios  # noqa: E402
from apps.buyer_ui.views import render_orders  # noqa: E402
from apps.buyer_ui.workspace import render_saved_work, restore_workspace  # noqa: E402


def _connection_locked():
    return os.getenv("BUYER_LOCK_CONNECTION", "false").strip().lower() == "true"


def _http_client(base_url):
    configured_url = os.getenv("BUYER_API_URL", "http://127.0.0.1:8000")
    token = os.getenv("BUYER_API_TOKEN")
    if (token or _connection_locked()) and base_url.strip().rstrip("/") != configured_url.strip().rstrip("/"):
        raise ValueError("Адрес API задаётся конфигурацией сервера.")
    return HttpClient(base_url, timeout=10, token=token)


def _clear_context(mode):
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


def _remember_drafts():
    # Streamlit removes widgets on navigation. Keep unsaved input scoped to its line.
    for identity, draft in st.session_state.get("order_drafts", {}).items():
        for prefix, field in (("qty", "qty"), ("reason", "reason")):
            if f"{prefix}:{identity}" in st.session_state:
                draft[field] = st.session_state[f"{prefix}:{identity}"]


def main():
    st.set_page_config(page_title="Электрокомплект · Закупки", page_icon="🛒", layout="wide", initial_sidebar_state="collapsed")
    developer = os.getenv("BUYER_DEVELOPER_MODE", "").lower() in ("1", "true", "yes")
    st.set_option("client.toolbarMode", "developer" if developer else "minimal")
    st.set_option("client.showErrorDetails", "full" if developer else "none")
    if not st.get_option("client.disableDataExport"):
        st.set_option("client.disableDataExport", True)
        st.rerun()
    st.html("<style>" + Path(__file__).with_name("styles.css").read_text(encoding="utf-8") + "</style>")
    _remember_drafts()
    with st.container(key="buyer_header"):
        title, help_area, settings = st.columns([6, 2, 2], vertical_alignment="center")
        with title:
            brand()
        with help_area.popover("Как работать", icon=":material/help_outline:", use_container_width=True):
            st.markdown("### От рекомендации к заказу")
            st.markdown("**1. Данные.** Выберите подключённый источник и рассчитайте заказ. Если заказ уже есть, начинайте с проверки.")
            st.markdown("**2. План закупки.** Выберите поставщика, проверьте товары и объяснения. При необходимости измените количество с причиной.")
            st.markdown("**3. CSV.** Утвердите проверенную версию и скачайте файл. После правки потребуется новое утверждение.")
            st.caption("В разделе «Сравнение вариантов» можно проверить другие условия поставки. Ваш заказ при этом не изменится.")
            st.caption("Утверждение сохраняет решение закупщика. Файл можно передать поставщику после проверки; приложение ничего не отправляет.")
    locked = _connection_locked()
    default_mode = "http" if locked else os.getenv("BUYER_UI_MODE", "http")
    if default_mode not in ("mock", "http"):
        st.error("Не удалось настроить приложение. Обратитесь к ответственному за систему.")
        st.stop()
    mode = default_mode
    base_url = os.getenv("BUYER_API_URL", "http://127.0.0.1:8000")
    quality = os.getenv("BUYER_MOCK_QUALITY", "ready")
    with settings.popover("Настройки", use_container_width=True):
        configured_url = os.getenv("BUYER_API_URL", "http://127.0.0.1:8000")
        if quality not in ("ready", "degraded", "blocked"):
            st.error("Не удалось настроить демонстрационные данные. Обратитесь к ответственному за систему.")
            st.stop()
        if developer:
            mode = st.selectbox("Режим интерфейса", ("mock", "http"), index=0 if default_mode == "mock" else 1,
                                format_func=lambda value: "Демо: имитация API" if value == "mock" else "HTTP API",
                                key="connection_mode", disabled=locked)
            base_url = st.text_input("Адрес API", value=configured_url, key="connection_url",
                                     disabled=locked or mode == "mock" or bool(os.getenv("BUYER_API_TOKEN")))
            quality = st.selectbox("Набор демо-данных", ("ready", "degraded", "blocked"),
                                   index=("ready", "degraded", "blocked").index(quality), key="mock_quality",
                                   disabled=mode != "mock")
        if locked:
            # Hosted sessions must retain the server destination even if widget state changes.
            mode, base_url = "http", configured_url
        st.caption("Учётная запись закупщика для локальной работы. Решения и утверждения сохраняются на сервере. Отправки поставщикам нет."
                   if mode == "http" else "Изолированный демонстрационный режим. Данные не сохраняются на сервере.")

        connection = (mode, base_url, quality)
        if st.session_state.get("connection") != connection:
            try:
                _clear_context(mode)
                st.session_state["client"] = MockClient(quality=quality) if mode == "mock" else _http_client(base_url)
            except (ValueError, OSError):
                st.error("Не удалось настроить подключение. Обратитесь к ответственному за систему.")
                st.stop()
            st.session_state["connection"] = connection
        st.session_state["client_mode"] = mode
        client = st.session_state["client"]
        if st.button("Проверить подключение", key="health_check"):
            try:
                if client.health().get("status") == "ok":
                    st.success("Подключение работает")
                    st.session_state.pop("buyer_workspace", None)
                else:
                    st.warning("Система пока не готова к работе.")
            except ApiError as error:
                show_api_error(error)
        if developer and mode == "mock" and st.button("Сбросить локальное демо", key="reset_demo"):
            _clear_context(mode)
            st.rerun()
    if not developer:
        restore_workspace(client)
        render_saved_work(client)
    if mode == "mock":
        st.caption("Демо: имитация API. Синтетические данные и заранее подготовленные результаты.")
    pending_page = st.session_state.pop("pending_page", None)
    if pending_page:
        st.session_state["workspace_page"] = pending_page
    with st.container(key="workspace_navigation"):
        labels = {"Заказы": "План закупки", "Что, если…": "Сравнение вариантов", "Данные": "Данные"}
        page = st.radio("Раздел", ["Заказы", "Что, если…", "Данные"], horizontal=True,
                        key="workspace_page", label_visibility="collapsed", format_func=labels.get)
    titles = {
        "Заказы": ("План закупки", "Выберите поставщика, проверьте количество товаров и скачайте утверждённый заказ."),
        "Что, если…": ("Сравнение вариантов", "Проверьте, как изменится закупка при задержке поставки или другом уровне запаса."),
        "Данные": ("Данные для расчёта", "Проверьте продажи и остатки, затем рассчитайте закупку."),
    }
    page_intro(*titles[page])
    # Render only the current task; background screens must not make requests or reset forms.
    if page == "Заказы":
        render_orders(client)
    elif page == "Что, если…":
        render_scenarios(client)
    else:
        render_data(client)
    st.html('<footer class="buyer-footer"><span>Электрокомплект · Планирование закупок</span></footer>')


if __name__ == "__main__":
    main()
