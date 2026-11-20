"""Read-only relational schema page for the Dr.Tracker dashboard."""
from collections import defaultdict
from contextlib import closing

import streamlit as st

from .analysis import open_readonly
from .database import DATABASE_PATH, SCHEMA_PATH

TABLES = {
    "patients": "One row per synthetic patient.",
    "checkins": "One row per submitted questionnaire and free-text report.",
    "symptoms": "Structured observations linked to a source check-in.",
    "weight": "Recorded weight measurements linked to a patient and check-in.",
    "treatments": "Treatment events linked to a patient, with an optional check-in.",
}


def describe_tables(db):
    for table, description in TABLES.items():
        fields = db.execute("SELECT * FROM pragma_table_info(?)", (table,)).fetchall()
        keys = defaultdict(list)
        for key in db.execute(
            "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id, seq", (table,)
        ):
            keys[key["id"]].append(key)
        yield table, description, fields, list(keys.values())


def schema_diagram(tables):
    lines = [
        "graph schema {", 'rankdir=LR; bgcolor="transparent";',
        'node [shape=box style="rounded,filled" fillcolor="#eaf5f4" color="#588b86"];',
        'edge [color="#627673"];',
    ]
    for table, _, fields, _ in tables:
        primary_key = ", ".join(field["name"] for field in fields if field["pk"])
        lines.append(f'{table} [label="{table}\\nPK {primary_key}"];')
    for table, _, _, keys in tables:
        for parts in keys:
            lines.append(f'{table} -- {parts[0]["table"]};')
    return "\n".join([*lines, "}"])


def show_schema():
    st.title("Relational Schema")
    st.caption("Dr.Tracker · SQLite data model")
    if not DATABASE_PATH.is_file():
        st.info("Initialize the demo database to inspect its live schema.")
        return
    with closing(open_readonly()) as db:
        tables = list(describe_tables(db))
        samples = {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 2")]
            for table in TABLES
        }

    st.graphviz_chart(schema_diagram(tables), width="stretch",
                      alt="Five SQLite tables connected by their foreign-key relationships.")
    st.caption("Expand a table below to see its foreign keys.")
    st.markdown(
        "The composite foreign keys in **weight** and **treatments** ensure that a linked "
        "check-in belongs to the same patient. A treatment can also be recorded without a check-in."
    )
    for table, description, fields, keys in tables:
        with st.expander(table):
            st.write(description)
            references = defaultdict(list)
            for parts in keys:
                for part in parts:
                    references[part["from"]].append(f'{part["table"]}.{part["to"]}')
            st.dataframe([
                {
                    "Column": field["name"], "Type": field["type"],
                    "Primary key": bool(field["pk"]), "Required": bool(field["notnull"]),
                    "References": ", ".join(references[field["name"]]),
                }
                for field in fields
            ], hide_index=True)
            st.markdown("**Sample data**")
            st.dataframe(samples[table], hide_index=True)
    with st.expander("View SQL definition"):
        st.code(SCHEMA_PATH.read_text(encoding="utf-8"), language="sql")
