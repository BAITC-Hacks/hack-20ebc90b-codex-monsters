"""Small, escaped presentation primitives; all quantities come from the API."""

from html import escape

import streamlit as st


def brand():
    st.html('''<div class="buyer-brand">
      <span class="buyer-brand-mark" aria-hidden="true">EK</span>
      <div><strong>Электрокомплект</strong><span>Планирование закупок</span></div>
    </div>''')


def page_intro(title, description, step_index=1):
    steps = ("Подготовить данные", "Проверить заказ", "Получить CSV")
    route = "".join(
        f'<li class="{"current" if index == step_index else ""}">'
        f'<span class="buyer-step-number">{index + 1}</span><span>{label}</span></li>'
        for index, label in enumerate(steps)
    )
    st.html(f'''<section class="buyer-intro">
      <div class="buyer-intro-copy"><h1>{escape(title)}</h1><p>{escape(description)}</p></div>
      <ol class="buyer-route" aria-label="Порядок работы с заказом">{route}</ol>
    </section>''')


def section_heading(number, title, description=""):
    detail = f'<p>{escape(description)}</p>' if description else ""
    st.html(f'''<div class="buyer-section-heading">
      <span class="buyer-section-number">{escape(str(number))}</span>
      <div><h2>{escape(title)}</h2>{detail}</div>
    </div>''')


def status_badge(label, approved=False):
    state = "approved" if approved else "draft"
    st.html(f'<span class="buyer-status {state}">{escape(label)}</span>')
