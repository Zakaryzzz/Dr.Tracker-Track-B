"""Read-only presentation of the implemented extraction dictionary and prompt."""
import inspect
from pathlib import Path
import re

import streamlit as st

from . import extract_symptoms as keyword
from . import llm_extraction as llm

EXAMPLES = {
    "pain": ("pain", "throat hurts", "jaw sore", "swallowing uncomfortable"),
    "vomiting": ("vomited", "threw up", "puking"),
    "dizziness": ("dizzy", "dizziness", "light-headed"),
    "fatigue": ("tired", "exhausted", "worn out"),
    "dry_mouth": ("dry mouth", "mouth feels dry"),
    "mouth_sores": ("mouth sores", "canker sore", "sore inside my cheek"),
    "fever": ("fever", "feverish", "high temperature"),
    "chills": ("chills", "shivering"),
    "bleeding": ("bleeding", "bled", "bloody saliva"),
    "insomnia": ("insomnia", "trouble sleeping", "couldn't sleep"),
    "cramps": ("cramps", "cramping", "muscle spasms"),
    "mood_changes": ("mood changes", "mood swings", "irritable"),
    "taste_changes": ("taste changes", "tasted different", "metallic taste"),
}


def dictionary_rows():
    """Keep displayed examples tied to the running parser's actual patterns."""
    return [
        {"Symptom label": measure,
         "Example matches": ", ".join(
             phrase for phrase in EXAMPLES.get(measure, ())
             if re.search(pattern, phrase, re.IGNORECASE)
         )}
        for measure, pattern in keyword.PATTERNS.items()
    ]


def show_extraction_rules():
    st.title("Extraction Rules")
    st.caption("Implemented keyword dictionary and local LLM instructions")
    rule_tab, llm_tab = st.tabs(["Keyword Parser", "LLM Prompt & Rules"])
    with rule_tab:
        st.subheader("Words → symptom labels")
        st.dataframe(dictionary_rows(), hide_index=True, width="stretch")
        st.caption("Examples illustrate the implemented patterns. Context rules determine the final status.")
        st.subheader("Context and value rules")
        st.dataframe([
            {"Input wording": "haven't, no, never, without", "Handling": "Mark the local symptom absent"},
            {"Input wording": "I think, maybe, not sure", "Handling": "Mark the local symptom uncertain"},
            {"Input wording": "if, risk of, worried about", "Handling": "Skip a conditional symptom mention"},
            {"Input wording": "4/10, 4 out of 10", "Handling": "Extract an explicit 0–10 severity score"},
            {"Input wording": "once, twice, 3 times", "Handling": "Extract explicit vomiting counts"},
            {"Input wording": "today, this morning, earlier this week", "Handling": "Preserve time wording"},
        ], hide_index=True, width="stretch")
        with st.expander("Number-word dictionary"):
            st.dataframe([{"Word": word, "Count": value} for word, value in keyword.NUMBER_WORDS.items()],
                         hide_index=True)
        st.subheader("Dictionary and context code")
        source = Path(inspect.getfile(keyword)).read_text(encoding="utf-8")
        start, end = source.index("PATTERNS = {"), source.index("def parse_keywords(")
        st.code(source[start:end].strip(), language="python")
        with st.expander("Parser function"):
            st.code(inspect.getsource(keyword.parse_keywords), language="python")

    with llm_tab:
        st.subheader("LLM instructions")
        st.caption(f"Configured model {llm.DEFAULT_MODEL} · Prompt version {llm.PROMPT_VERSION}")
        st.write("The model receives the original free text and the instructions below. "
                 "Questionnaire answers and keyword predictions are excluded from its input.")
        prompt, _, _ = llm.PROMPT.partition("Schema: ")
        st.code(prompt.strip(), language="text")
        with st.expander("JSON output schema"):
            st.json(llm.FORMAT, expanded=True)
        with st.expander("Inference settings"):
            st.json(llm.OPTIONS)
        with st.expander("Evidence validation code"):
            st.code(inspect.getsource(llm.validate_response), language="python")
        with st.expander("Model request and retry instructions"):
            st.code(inspect.getsource(llm.extract_live), language="python")
