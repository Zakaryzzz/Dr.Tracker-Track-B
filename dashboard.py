"""Streamlit entrypoint for the Dr.Tracker patient workspace."""
import streamlit as st

from src.patient_view import render_patient_dashboard
from src.schema_page import show_schema
from review_annotations import show_review_demo


def show_dashboard():
    render_patient_dashboard()


def main():
    st.set_page_config(page_title="Dr.Tracker", page_icon="◈", layout="wide")
    page = st.navigation([
        st.Page(show_dashboard, title="Patient Dashboard", default=True),
        st.Page(show_schema, title="Relational Schema", url_path="schema"),
        st.Page(show_review_demo, title="Annotation Review", url_path="annotation-review"),
    ])
    page.run()


if __name__ == "__main__":
    main()
