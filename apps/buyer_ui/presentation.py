"""Small, escaped presentation primitives; all quantities come from the API."""

from html import escape

import streamlit as st


def brand():
    st.html('''<div class="buyer-brand">
      <span class="buyer-brand-mark" aria-hidden="true">EK</span>
      <div><strong>Электрокомплект</strong><span>Планирование закупок</span></div>
    </div>''')


def page_intro(title, description):
    st.html(f'''<section class="buyer-intro">
      <div class="buyer-intro-copy"><h1>{escape(title)}</h1><p>{escape(description)}</p></div>
    </section>''')


def section_heading(number, title, description=""):
    detail = f'<p>{escape(description)}</p>' if description else ""
    marker = f'<span class="buyer-section-number">{escape(str(number))}</span>' if number is not None else ""
    st.html(f'''<div class="buyer-section-heading">
      {marker}
      <div><h2>{escape(title)}</h2>{detail}</div>
    </div>''')


def status_badge(label, approved=False):
    state = "approved" if approved else "draft"
    st.html(f'<span class="buyer-status {state}">{escape(label)}</span>')
