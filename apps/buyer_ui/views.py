"""Supplier proposal review, edit, approval and backend-generated CSV."""
from uuid import uuid4

import streamlit as st

from .client import ApiError
from .formatting import format_decimal, format_money, show_api_error, show_issues
from .workflow import approve_reviewed, export_reviewed


def _flash(kind, message):
    st.session_state["order_flash"] = (kind, message)


def _context(proposal):
    if proposal.get("mode") == "synthetic_demo":
        st.warning("ДЕМОНСТРАЦИЯ · Синтетические данные. CSV не является заказом поставщику.")
    else:
        st.info("Реальные данные · предварительный просмотр; учитывайте ограничения источников.")
    cols = st.columns(4)
    cols[0].metric("Поставщик", proposal.get("supplier_id", "Не указан"))
    cols[1].metric("Склад", proposal.get("warehouse_id", "Не указан"))
    cols[2].metric("Версия", str(proposal["version"]))
    cols[3].metric("Стоимость", format_money(proposal.get("total_cost"), proposal.get("currency")))
    st.caption(f"По состоянию на {proposal.get('as_of', 'не сообщено')} · "
               f"{'Утверждено' if proposal.get('status') == 'approved' else 'Черновик'}")
    with st.expander("Контекст расчёта"):
        st.json({key: proposal.get(key) for key in (
            "proposal_id", "run_id", "snapshot_id", "content_hash", "mode", "as_of")})


def _line_rows(lines, currency):
    urgency = {"critical": "Критично", "soon": "Скоро", "routine": "Планово"}
    return [{
        "Строка": line["line_id"], "SKU": line["sku_id"], "Наименование": line["name"],
        "Заказать": format_decimal(line.get("selected_purchase_qty")),
        "Ед. закупки": line["purchase_uom"],
        "В базовой ед.": format_decimal(line.get("selected_base_qty")), "Базовая ед.": line["base_uom"],
        "Срочность": urgency.get(line.get("urgency"), line.get("urgency", "Не задана")),
        "Стоимость": format_money(line.get("line_cost"), currency),
        "Ограничения": "; ".join(item.get("message", item.get("code", "")) for item in line.get("warnings", [])),
    } for line in lines]


def _ledger(line):
    st.subheader(f"{line['sku_id']} · {line['name']}")
    cols = st.columns(3)
    cols[0].metric("Рекомендовано", f"{format_decimal(line.get('recommended_purchase_qty'))} {line['purchase_uom']}")
    cols[1].metric("Выбрано покупателем", f"{format_decimal(line.get('selected_purchase_qty'))} {line['purchase_uom']}")
    cols[2].metric("В базовых единицах", f"{format_decimal(line.get('selected_base_qty'))} {line['base_uom']}")
    st.caption("Нулевой заказ означает: пополнение не требуется. Метры, штуки и упаковки не суммируются.")
    rows = [{"Показатель": label, "Значение": format_decimal(line.get(field)), "Ед.": uom}
            for field, label, uom in (
                ("safety_stock", "Страховой запас", line["base_uom"]),
                ("rop", "Точка заказа (ROP)", line["base_uom"]),
                ("raw_need", "Потребность до округления", line["base_uom"]),
                ("moq_purchase", "Минимальный заказ (MOQ)", line["purchase_uom"]),
                ("pack_multiple_purchase", "Кратность", line["purchase_uom"]),
                ("conversion", "Базовых единиц в закупочной", line["base_uom"]))]
    st.dataframe(rows, hide_index=True, width="stretch")
    st.write("**Объяснение каждой единицы**")
    ledger = [{"Компонент": item["label"], "Изменение": format_decimal(item.get("delta_base_qty"), signed=True),
               "Ед.": line["base_uom"], "Код": item["code"], "Пояснение": item.get("note") or "",
               "Источники": ", ".join(item.get("source_refs") or [])}
              for item in line.get("explanation", [])]
    if ledger:
        st.dataframe(ledger, hide_index=True, width="stretch")
        st.caption("Порядок и итог получены от API. Восстановленный спрос уже входит в прогноз; "
                   "неаддитивные пояснения не прибавляются повторно.")
    else:
        st.warning("API не вернул объяснение количества.")
    if line.get("projected_stockout_date"):
        st.write("Прогнозируемая дата дефицита:", line["projected_stockout_date"])
    show_issues(line.get("warnings"))


def _edit(client, proposal, line):
    identity = f"{proposal['proposal_id']}:{line['line_id']}"
    drafts = st.session_state.setdefault("order_drafts", {})
    draft = drafts.setdefault(identity, {"version": proposal["version"], "qty": line["selected_purchase_qty"], "reason": ""})
    st.write("**Изменить количество**")
    st.caption(f"Количество в {line['purchase_uom']}. MOQ и кратность проверяет сервер.")
    uncertain = draft.get("uncertain", False)
    stale = draft["version"] != proposal["version"]
    if uncertain:
        st.warning("Результат предыдущей правки неизвестен. Сверьте количество, версию и ручную дельту "
                   "на сервере перед новой отправкой. Автоматического повтора PATCH нет.")
    if stale:
        st.warning(f"Черновик относится к версии {draft['version']}; сервер вернул {proposal['version']}. "
                   "Сохранённый ввод не применён к новой версии.")
    if stale or uncertain:
        if st.button("Сверил сервер: использовать текущую версию для правки", key=f"rebase:{identity}"):
            draft["version"] = proposal["version"]
            draft.pop("uncertain", None)
            st.rerun()
    with st.form(f"edit:{identity}"):
        qty = st.text_input("Новое количество", value=draft["qty"], key=f"qty:{identity}")
        reason = st.text_area("Причина изменения (обязательно)", value=draft["reason"], key=f"reason:{identity}")
        submitted = st.form_submit_button("Сохранить новую версию", disabled=stale or uncertain)
    if submitted:
        draft.update(qty=qty, reason=reason)
        if not reason.strip():
            st.error("Укажите причину изменения.")
            return
        if not qty.strip():
            st.error("Укажите количество десятичной строкой, например 120 или 2.5.")
            return
        try:
            updated = client.edit_proposal(proposal["proposal_id"], {
                "expected_version": draft["version"],
                "edits": [{"line_id": line["line_id"], "purchase_qty": qty}], "reason": reason,
            })
            # Only the server response determines status and quantities.
            draft["version"] = updated["version"]
            st.session_state.pop("order_download", None)
            _flash("success", f"Сервер создал версию {updated['version']}. Проверьте объяснение и утвердите её отдельно.")
            st.rerun()
        except ApiError as error:
            if error.ambiguous:
                draft["uncertain"] = True
            show_api_error(error)


def _approval_and_export(client, proposal, reviewed):
    st.divider()
    st.subheader("Утверждение и CSV")
    caps = proposal.get("capabilities") or {}
    for reason in caps.get("reasons") or []:
        st.warning(reason)
    st.write(f"Поставщик **{proposal['supplier_id']}** · версия **{proposal['version']}** · "
             f"данные на **{proposal['as_of']}**")
    st.caption("Демонстрационная учётная запись. Полномочия определяет конфигурация сервера.")
    approval_key = f"approve:{proposal['proposal_id']}"
    if st.button("Утвердить просмотренную версию", disabled=not caps.get("can_approve", False), key=approval_key):
        try:
            approve_reviewed(client, reviewed)
            # A successful POST is followed by a fresh GET on rerun, not a local approved flag.
            _flash("info", "Ответ на утверждение получен. Ниже показано перечитанное состояние API.")
            st.rerun()
        except ApiError as error:
            show_api_error(error)
    export_identity = (proposal["proposal_id"], proposal["version"], proposal["content_hash"])
    downloads = st.session_state.get("order_download")
    if downloads and (downloads["identity"] != export_identity or not caps.get("can_export", False)):
        st.session_state.pop("order_download", None)
        downloads = None
    request_keys = st.session_state.setdefault("export_keys", {})
    if st.button("Подготовить утверждённый CSV", disabled=not caps.get("can_export", False),
                 key=f"export:{proposal['proposal_id']}"):
        request_key = request_keys.setdefault(export_identity, str(uuid4()))
        try:
            result = export_reviewed(client, reviewed, request_key)
            st.session_state["order_download"] = {"identity": export_identity, "data": result.data, "filename": result.filename}
            st.rerun()
        except ApiError as error:
            show_api_error(error)
    if downloads:
        st.download_button("Скачать утверждённый CSV", data=downloads["data"], file_name=downloads["filename"],
                           mime="text/csv; charset=utf-8", key=f"download:{proposal['proposal_id']}")
        st.caption(f"Файл получен от API для версии {proposal['version']}. После изменения понадобится новое утверждение и экспорт.")
    if not caps.get("can_export", False):
        st.info("Экспорт недоступен. Требуется утверждённая текущая версия и разрешение API.")


def render_orders(client):
    st.subheader("Предложения поставщикам")
    flash = st.session_state.pop("order_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("Выберите снимок и завершённый расчёт во вкладке «Данные/проекты».")
        return
    try:
        run = client.get_planning_run(run_id)
        if run.get("status") != "succeeded":
            st.info(f"Расчёт {run_id}: {run.get('status', 'неизвестно')} · {run.get('stage', '')}")
            if run.get("error"):
                st.error(run["error"].get("message", "Расчёт завершился ошибкой"))
            if st.button("Обновить расчёт", key="orders_refresh_run"):
                st.rerun()
            return
        supplier = st.text_input("Фильтр по ID поставщика", key="orders_supplier").strip()
        filter_context = (run_id, supplier)
        if st.session_state.get("orders_filter_context") != filter_context:
            st.session_state["orders_filter_context"] = filter_context
            st.session_state["orders_cursor"] = None
        page = client.list_proposals(run_id=run_id, supplier_id=supplier or None,
                                     cursor=st.session_state.get("orders_cursor"), limit=50)
        items = page.get("items") or []
        if not items:
            st.info("Предложений в этом расчёте или фильтре нет. Проверьте качество данных и исключённые строки.")
            if st.session_state.get("orders_cursor") and st.button("На первую страницу", key="orders_empty_first"):
                st.session_state["orders_cursor"] = None
                st.rerun()
            return
        by_id = {item["proposal_id"]: item for item in items}
        ids = list(by_id)
        requested = st.session_state.pop("pending_proposal_selection", None)
        if requested in ids:
            st.session_state["proposal_picker"] = requested
        if st.session_state.get("proposal_picker") not in ids:
            st.session_state["proposal_picker"] = st.session_state.get("proposal_id") if st.session_state.get("proposal_id") in ids else ids[0]
        proposal_id = st.selectbox("Предложение", ids, key="proposal_picker",
                                   format_func=lambda value: f"{by_id[value].get('supplier_id', value)} · {value}")
        st.session_state["proposal_id"] = proposal_id
        c1, c2, c3 = st.columns(3)
        if c1.button("Обновить предложение", key="orders_refresh"):
            st.rerun()
        if c2.button("Первая страница", disabled=not st.session_state.get("orders_cursor"), key="orders_first"):
            st.session_state["orders_cursor"] = None
            st.rerun()
        if c3.button("Следующие предложения", disabled=not page.get("next_cursor"), key="orders_next"):
            st.session_state["orders_cursor"] = page["next_cursor"]
            st.rerun()
        proposal = client.get_proposal(proposal_id)
        if proposal.get("run_id") != run_id or (st.session_state.get("snapshot_id") and proposal.get("snapshot_id") != st.session_state["snapshot_id"]):
            st.error("Предложение относится к другому снимку или расчёту. Выберите согласованный контекст.")
            return
        review_key = f"reviewed:{proposal_id}"
        reviewed = st.session_state.get(review_key, proposal)
        _context(proposal)
        show_issues(proposal.get("warnings"))
        excluded = proposal.get("excluded_lines") or []
        if excluded:
            with st.expander(f"Исключённые строки: {len(excluded)}", expanded=True):
                for item in excluded:
                    st.write("**SKU:**", item["sku_id"])
                    show_issues(item.get("reasons"))
        urgency = st.selectbox("Срочность", ("Все", "critical", "soon", "routine"), key="urgency_filter")
        lines = [line for line in proposal.get("lines", []) if urgency == "Все" or line.get("urgency") == urgency]
        lines.sort(key=lambda item: (item["sku_id"], item["line_id"]))
        if lines:
            st.dataframe(_line_rows(lines, proposal.get("currency")), hide_index=True, width="stretch")
            by_line = {line["line_id"]: line for line in lines}
            key = f"line_picker:{proposal_id}"
            if st.session_state.get(key) not in by_line:
                st.session_state[key] = lines[0]["line_id"]
            line_id = st.selectbox("Строка для проверки", list(by_line), key=key,
                                  format_func=lambda value: f"{by_line[value]['sku_id']} · {by_line[value]['name']}")
            _ledger(by_line[line_id])
            _edit(client, proposal, by_line[line_id])
        else:
            st.info("В этом фильтре нет строк. Исключённые позиции показаны отдельно.")
        _approval_and_export(client, proposal, reviewed)
        # Persist the exact version shown; a button event must not silently target a newer GET.
        st.session_state[review_key] = {key: proposal[key] for key in
                                      ("proposal_id", "version", "content_hash", "run_id", "snapshot_id")}
    except ApiError as error:
        show_api_error(error)
