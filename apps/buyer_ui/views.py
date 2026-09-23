"""Supplier proposal review, edit, approval and backend-generated CSV."""
from decimal import Decimal, InvalidOperation
from html import escape
from uuid import uuid4

import streamlit as st

from .client import ApiError
from .formatting import format_date, format_decimal, format_money, format_uom, show_api_error, show_issues
from .presentation import section_heading, status_badge
from .workflow import approve_reviewed, export_reviewed

URGENCY = {"Все": "Все товары", "critical": "Критично", "soon": "Скоро", "routine": "Планово"}


def _flash(kind, message):
    st.session_state["order_flash"] = (kind, message)


def _draft_dirty(draft):
    try:
        return Decimal(draft["qty"]) != Decimal(draft.get("original_qty", draft["qty"]))
    except InvalidOperation:
        return True


def _context(proposal):
    st.caption(f"Данные на {format_date(proposal.get('as_of'))} · Версия {proposal['version']}")
    if proposal.get("mode") == "synthetic_demo":
        missing = any(w.get("code") == "FORECAST_PROVIDER_NOT_CONNECTED"
                      for line in proposal.get("lines", []) for w in line.get("warnings", []))
        text = "Демо: синтетические данные"
        if missing:
            text += ", временный прогноз (модель спроса ещё не подключена)"
        st.caption(text + ". CSV не является заказом поставщику.")
    else:
        st.caption("Предпросмотр реальных данных. Учитывайте ограничения источников.")


def _summary(proposal):
    st.subheader("Итог заказа")
    approved = proposal.get("status") == "approved"
    status_badge("Утверждён" if approved else "Ожидает вашей проверки", approved)
    st.metric("Сумма к закупке", format_money(proposal.get("total_cost"), proposal.get("currency")))
    if approved:
        st.html('<p class="buyer-next">Следующий шаг — скачать CSV</p>'
                '<p class="buyer-next-detail">Выгрузите утверждённую версию заказа.</p>')
    else:
        st.html('<p class="buyer-next">Решение — за вами</p>'
                '<p class="buyer-next-detail">Проверьте количество и объяснения, затем утвердите заказ целиком.</p>')


def _summary_details(proposal):
    lines = proposal.get("lines", [])
    critical = sum(line.get("urgency") == "critical" for line in lines)
    rows = (("Поставщик", proposal.get("supplier_id", "—")),
            ("Склад", proposal.get("warehouse_id", "—")),
            ("Позиций в заказе", len(lines)),
            ("Критичных позиций", critical))
    st.html('<ul class="buyer-summary-list">' + ''.join(
        f'<li><span>{escape(label)}</span><strong>{escape(str(value))}</strong></li>'
        for label, value in rows) + '</ul>')


def _line_rows(lines, currency):
    return [{
        "Товар": line["name"], "Артикул": line["sku_id"],
        "Заказать": f"{format_decimal(line.get('selected_purchase_qty'))} {format_uom(line['purchase_uom'])}",
        "Срочность": URGENCY.get(line.get("urgency"), "Не задана"),
        "Сумма": format_money(line.get("line_cost"), currency),
    } for line in lines]


def _ledger(line):
    unit = format_uom(line["purchase_uom"])
    st.write(f"**{line['name']}**")
    st.caption(f"Артикул {line['sku_id']}. Рекомендовано {format_decimal(line.get('recommended_purchase_qty'))} {unit}; "
               f"в заказе {format_decimal(line.get('selected_purchase_qty'))} {unit}".rstrip(".") + ".")
    if line.get("projected_stockout_date"):
        st.warning(f"Ожидаемый дефицит с {format_date(line['projected_stockout_date'])}.")
    with st.expander("Почему столько рекомендовано"):
        ledger = [{"Что учтено": item["label"],
                   "Количество": format_decimal(item.get("delta_base_qty"), signed=True, places=3),
                   "Ед.": format_uom(line["base_uom"])}
                  for item in line.get("explanation", [])]
        if ledger:
            st.dataframe(ledger, hide_index=True, width="stretch")
            st.caption("Расчёт сервера. Для чтения значения округлены до трёх знаков; точные значения — ниже.")
        else:
            st.warning("Сервер не вернул объяснение количества.")
        rows = [{"Показатель": label, "Значение": format_decimal(line.get(field), places=3), "Ед.": format_uom(uom)}
                for field, label, uom in (
                    ("safety_stock", "Страховой запас", line["base_uom"]),
                    ("rop", "Порог пополнения", line["base_uom"]),
                    ("raw_need", "Потребность до округления", line["base_uom"]),
                    ("moq_purchase", "Минимальная партия", line["purchase_uom"]),
                    ("pack_multiple_purchase", "Кратность упаковки", line["purchase_uom"]),
                    ("conversion", "Базовых единиц в упаковке", line["base_uom"]))]
        st.dataframe(rows, hide_index=True, width="stretch")
        show_issues(line.get("warnings"))
        with st.expander("Точные значения и источники"):
            st.json(line)


def _notices(proposal):
    excluded = proposal.get("excluded_lines") or []
    warnings = proposal.get("warnings") or []
    # Keep decision-changing conditions visible; put their detailed evidence one level below.
    blocking = [w for w in warnings if w.get("severity") == "blocking"]
    show_issues(blocking)
    other = [w for w in warnings if w not in blocking]
    if excluded or other:
        title = f"Не включены в заказ: {len(excluded)} — проверить причины" if excluded else "Ограничения расчёта"
        with st.expander(title):
            for item in excluded:
                st.write(f"**{item['sku_id']}**")
                show_issues(item.get("reasons"))
            show_issues(other)


def _edit(client, proposal, line):
    identity = f"{proposal['proposal_id']}:{line['line_id']}"
    drafts = st.session_state.setdefault("order_drafts", {})
    draft = drafts.setdefault(identity, {"version": proposal["version"], "qty": line["selected_purchase_qty"], "reason": ""})
    draft.setdefault("original_qty", line["selected_purchase_qty"])
    st.write("**Изменить количество**")
    st.caption(f"Минимум: {format_decimal(line.get('moq_purchase'))} {format_uom(line['purchase_uom']).rstrip('.')}. "
               f"Шаг упаковки: {format_decimal(line.get('pack_multiple_purchase'))}. Ноль — не заказывать.")
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
    if _draft_dirty(draft) and st.button("Отменить правку", key=f"discard:{identity}"):
        draft.update(version=proposal["version"], qty=line["selected_purchase_qty"],
                     original_qty=line["selected_purchase_qty"], reason="")
        draft.pop("uncertain", None)
        st.session_state[f"qty:{identity}"] = draft["qty"]
        st.session_state[f"reason:{identity}"] = ""
        st.rerun()
    qty = st.text_input("Новое количество", value=draft["qty"], key=f"qty:{identity}")
    reason = st.text_area("Причина изменения (обязательно)", value=draft["reason"], key=f"reason:{identity}",
                          placeholder="Например: уточнена потребность по проекту", height=100)
    draft.update(qty=qty, reason=reason)
    submitted = st.button("Сохранить количество", key=f"save:{identity}", disabled=stale or uncertain)
    if submitted:
        draft.update(qty=qty, reason=reason)
        if not reason.strip():
            st.error("Укажите причину изменения.")
            return
        if not qty.strip():
            st.error("Укажите количество десятичной строкой, например 120 или 2.5.")
            return
        try:
            with st.spinner("Сохраняем количество…"):
                updated = client.edit_proposal(proposal["proposal_id"], {
                    "expected_version": draft["version"],
                    "edits": [{"line_id": line["line_id"], "purchase_qty": qty}], "reason": reason,
                })
            # Only the server response determines status and quantities.
            draft["version"] = updated["version"]
            draft["original_qty"] = qty
            st.session_state.pop("order_download", None)
            _flash("success", f"Количество сохранено. Версия {updated['version']} ожидает утверждения.")
            st.rerun()
        except ApiError as error:
            if error.ambiguous:
                draft["uncertain"] = True
            show_api_error(error)


def _prepare_download(client, reviewed):
    identity = (reviewed["proposal_id"], reviewed["version"], reviewed["content_hash"])
    keys = st.session_state.setdefault("export_keys", {})
    request_key = keys.setdefault(identity, str(uuid4()))
    with st.spinner("Готовим утверждённый CSV…"):
        result = export_reviewed(client, reviewed, request_key)
    st.session_state["order_download"] = {"identity": identity, "data": result.data, "filename": result.filename}


def _approval_and_export(client, proposal, reviewed):
    st.divider()
    st.caption("Демонстрационная учётная запись")
    caps = proposal.get("capabilities") or {}
    dirty = any(_draft_dirty(draft) for identity, draft in st.session_state.get("order_drafts", {}).items()
                if identity.startswith(proposal["proposal_id"] + ":"))
    if dirty:
        st.info("Есть несохранённая правка количества. Сохраните или отмените её в блоке товара перед утверждением и выгрузкой.")
        return
    identity = (proposal["proposal_id"], proposal["version"], proposal["content_hash"])
    download = st.session_state.get("order_download")
    if download and (download["identity"] != identity or not caps.get("can_export", False)):
        st.session_state.pop("order_download", None)
        download = None
    if download:
        st.download_button("Скачать CSV", data=download["data"], file_name=download["filename"],
                           mime="text/csv; charset=utf-8", type="primary", key=f"download:{proposal['proposal_id']}",
                           use_container_width=True, icon=":material/download:")
        st.caption(f"Утверждённая версия {proposal['version']}. Файл подготовлен сервером.")
    elif proposal.get("status") == "approved":
        if st.button("Подготовить CSV", type="primary", disabled=not caps.get("can_export", False),
                     key=f"export:{proposal['proposal_id']}", use_container_width=True, icon=":material/file_download:"):
            try:
                _prepare_download(client, reviewed)
                st.rerun()
            except ApiError as error:
                show_api_error(error)
    else:
        st.caption(f"Будет утверждена версия {proposal['version']}; поставщику ничего не отправляется.")
        if st.button("Утвердить и подготовить CSV", type="primary", disabled=not caps.get("can_approve", False),
                     key=f"approve:{proposal['proposal_id']}", use_container_width=True, icon=":material/check:"):
            try:
                with st.spinner("Утверждаем проверенную версию…"):
                    approve_reviewed(client, reviewed)
            except ApiError as error:
                show_api_error(error)
            else:
                # Approval is persisted independently. An export failure must not re-approve.
                try:
                    _prepare_download(client, reviewed)
                    _flash("success", "Заказ утверждён. CSV готов к скачиванию.")
                except ApiError as error:
                    st.session_state["order_error"] = error
                    _flash("info", f"Версия {reviewed['version']} утверждена, но CSV не подготовлен. Проверьте актуальное состояние заказа.")
                st.rerun()
    if (proposal.get("status") == "approved" and not caps.get("can_export", False)) or (
            proposal.get("status") != "approved" and not caps.get("can_approve", False)):
        st.warning("Действие пока недоступно. Проверьте ограничения данных и права доступа.")
        for reason in caps.get("reasons") or []:
            st.caption(reason)


def render_orders(client):
    if st.session_state.pop("_reset_order_filters", False):
        st.session_state["order_search"] = ""
        st.session_state["urgency_filter"] = "Все"
    flash = st.session_state.pop("order_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    error = st.session_state.pop("order_error", None)
    if error:
        show_api_error(error)
    run_id = st.session_state.get("run_id")
    if not run_id:
        with st.container(key="empty_order"):
            section_heading(1, "Подготовьте первый заказ", "Начните с данных о продажах и запасах.")
            st.write("Выберите подключённый источник в разделе «Данные» и запустите расчёт. "
                     "Здесь появится список товаров по поставщикам с количеством и объяснением.")
            if st.button("Перейти к данным", type="primary", icon=":material/arrow_forward:"):
                st.session_state["pending_page"] = "Данные"
                st.rerun()
        return
    try:
        run = client.get_planning_run(run_id)
        if run.get("status") != "succeeded":
            st.info("Расчёт ещё не готов. Проверьте его состояние в разделе «Данные».")
            if run.get("error"):
                st.error(run["error"].get("message", "Расчёт завершился ошибкой"))
            if st.button("Обновить расчёт", key="orders_refresh_run"):
                st.rerun()
            return
        picker, more = st.columns([4, 1], vertical_alignment="bottom")
        with more.popover("Ещё", use_container_width=True):
            supplier = st.text_input("ID поставщика", key="orders_supplier").strip()
            if st.button("Обновить заказ", key="orders_refresh"):
                st.rerun()
        filter_context = (run_id, supplier)
        if st.session_state.get("orders_filter_context") != filter_context:
            st.session_state["orders_filter_context"] = filter_context
            st.session_state["orders_cursor"] = None
        page = client.list_proposals(run_id=run_id, supplier_id=supplier or None,
                                     cursor=st.session_state.get("orders_cursor"), limit=50)
        items = page.get("items") or []
        if not items:
            st.info("Заказов по выбранным условиям нет. Проверьте фильтр поставщика и качество данных.")
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
        proposal_id = picker.selectbox("Поставщик и склад", ids, key="proposal_picker",
                                      format_func=lambda value: f"{by_id[value].get('supplier_id', 'Поставщик')} / {by_id[value].get('warehouse_id', 'Склад')}")
        st.session_state["proposal_id"] = proposal_id
        if st.session_state.get("orders_cursor") or page.get("next_cursor"):
            prev, nxt = st.columns(2)
            if prev.button("Первая страница", disabled=not st.session_state.get("orders_cursor"), key="orders_first"):
                st.session_state["orders_cursor"] = None
                st.rerun()
            if nxt.button("Следующие поставщики", disabled=not page.get("next_cursor"), key="orders_next"):
                st.session_state["orders_cursor"] = page["next_cursor"]
                st.rerun()
        proposal = client.get_proposal(proposal_id)
        if proposal.get("run_id") != run_id or (st.session_state.get("snapshot_id") and proposal.get("snapshot_id") != st.session_state["snapshot_id"]):
            st.error("Заказ относится к другим данным. Выберите согласованный расчёт в разделе «Данные».")
            return
        review_key = f"reviewed:{proposal_id}"
        reviewed = st.session_state.get(review_key, proposal)
        with st.container(key="order_columns"):
            working, summary = st.columns([2.35, 1], gap="medium")
            with working, st.container(key="order_workspace"):
                section_heading(2, "Товары к закупке", "Начните с позиций с риском дефицита.")
                _context(proposal)
                _notices(proposal)
                search, filters = st.columns([3, 2])
                query = search.text_input("Найти товар", placeholder="Название или артикул", key="order_search").strip().casefold()
                urgency = filters.selectbox("Срочность", tuple(URGENCY), format_func=URGENCY.get, key="urgency_filter")
                lines = [line for line in proposal.get("lines", [])
                         if (urgency == "Все" or line.get("urgency") == urgency)
                         and (not query or query in (line["name"] + " " + line["sku_id"]).casefold())]
                lines.sort(key=lambda item: (item["sku_id"], item["line_id"]))
                if lines:
                    st.dataframe(_line_rows(lines, proposal.get("currency")), hide_index=True, width="stretch",
                                 height=min(480, len(lines) * 38 + 40), row_height=38)
                    st.caption(f"Показано {len(lines)} из {len(proposal.get('lines', []))} позиций. "
                               "Для объяснения и правки выберите товар ниже.")
                    if len(lines) != len(proposal.get("lines", [])):
                        st.caption("Фильтр меняет только отображение. Утверждается весь заказ.")
                    st.divider()
                    st.subheader("Проверить или изменить товар")
                    by_line = {line["line_id"]: line for line in lines}
                    key = f"line_picker:{proposal_id}"
                    if st.session_state.get(key) not in by_line:
                        st.session_state[key] = lines[0]["line_id"]
                    line_id = st.selectbox("Товар", list(by_line), key=key,
                                          format_func=lambda value: f"{by_line[value]['name']} ({by_line[value]['sku_id']})")
                    _ledger(by_line[line_id])
                    with st.expander("Изменить количество"):
                        _edit(client, proposal, by_line[line_id])
                else:
                    st.info("Товары не найдены. Измените название или фильтр срочности.")
                    st.caption("Фильтр меняет только отображение. Утверждается весь заказ.")
                    if st.button("Сбросить поиск и фильтр", key="orders_reset_filters"):
                        st.session_state["_reset_order_filters"] = True
                        st.rerun()
            with summary, st.container(key="order_summary"):
                _summary(proposal)
                _approval_and_export(client, proposal, reviewed)
                _summary_details(proposal)
                st.caption("CSV содержит весь заказ выбранному поставщику. Отправки поставщику нет.")
                with st.expander("Технические сведения о заказе"):
                    st.json({key: proposal.get(key) for key in
                             ("proposal_id", "run_id", "snapshot_id", "version", "content_hash", "mode", "as_of", "capabilities")})
        st.session_state[review_key] = {key: proposal[key] for key in
                                      ("proposal_id", "version", "content_hash", "run_id", "snapshot_id")}
    except ApiError as error:
        show_api_error(error)
