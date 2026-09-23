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


def _http_client(base_url):
    configured_url = os.getenv("BUYER_API_URL", "http://127.0.0.1:8000")
    token = os.getenv("BUYER_API_TOKEN")
    if token and base_url.strip().rstrip("/") != configured_url.strip().rstrip("/"):
        raise ValueError("Адрес API с учётными данными задаётся конфигурацией сервера.")
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
    st.set_page_config(page_title="Электрокомплект · Закупки", layout="wide", initial_sidebar_state="collapsed")
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
            st.caption("Демонстрационная учётная запись. Отправки поставщикам нет.")
    with settings.popover("Настройки", use_container_width=True):
        st.write("**Подключение**")
        default_mode = os.getenv("BUYER_UI_MODE", "mock")
        if default_mode not in ("mock", "http"):
            st.error("BUYER_UI_MODE должен быть mock или http.")
            st.stop()
        mode = st.selectbox("Режим интерфейса", ("mock", "http"), index=0 if default_mode == "mock" else 1,
                            format_func=lambda value: "Демо: имитация API" if value == "mock" else "HTTP API",
                            key="connection_mode")
        base_url = st.text_input("Адрес API", value=os.getenv("BUYER_API_URL", "http://127.0.0.1:8000"),
                                 key="connection_url", disabled=mode == "mock" or bool(os.getenv("BUYER_API_TOKEN")))
        default_quality = os.getenv("BUYER_MOCK_QUALITY", "ready")
        if default_quality not in ("ready", "degraded", "blocked"):
            st.error("BUYER_MOCK_QUALITY должен быть ready, degraded или blocked.")
            st.stop()
        quality = st.selectbox("Набор демо-данных", ("ready", "degraded", "blocked"),
                               index=("ready", "degraded", "blocked").index(default_quality),
                               format_func=lambda v: {"ready": "Готовые", "degraded": "С ограничениями", "blocked": "Расчёт заблокирован"}[v],
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
        if st.button("Проверить подключение", key="health_check"):
            try:
                health = client.health()
                if health.get("status") == "ok":
                    st.success("Подключение работает")
                else:
                    st.warning("Сервер не подтвердил готовность.")
            except ApiError as error:
                show_api_error(error)
        st.caption("Полномочия задаёт сервер. Отправки поставщикам нет.")
        st.caption("Снимок: " + (st.session_state.get("snapshot_id") or "не выбран"))
        st.caption("Расчёт: " + (st.session_state.get("run_id") or "не выбран"))
        if mode == "mock" and st.button("Сбросить локальное демо", key="reset_demo"):
            _clear_context(mode)
            st.rerun()
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
