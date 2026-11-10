"""Reusable deterministic ATS result presentation."""

from __future__ import annotations

import streamlit as st


def score_band(score: float) -> str:
    return "Strong" if score >= 75 else "Developing" if score >= 50 else "Needs work"


def render_analysis(analysis: dict) -> None:
    score = float(analysis["overall_ats_score"])
    st.metric("ATS score", f"{score:.1f} / 100", score_band(score))
    st.progress(int(max(0, min(100, score))))
    st.caption(analysis.get("disclaimer", "This score is an indicator, not a hiring probability."))
    st.subheader("Score breakdown")
    for name, item in analysis.get("components", {}).items():
        label = name.replace("_", " ").title()
        st.write(f"**{label}** — {item.get('score', 0):.1f}/100 · weight {item.get('weight', 0)}%")
        st.progress(int(max(0, min(100, item.get("score", 0)))))
        st.caption(item.get("explanation", ""))
    left, right = st.columns(2)
    left.subheader("Matched skills"); left.write(analysis.get("matched_skills", []) or "None identified")
    right.subheader("Missing skills"); right.write(analysis.get("missing_skills", []) or "None identified")
    left.subheader("Matched keywords"); left.write(analysis.get("matched_keywords", []) or "None identified")
    right.subheader("Missing keywords"); right.write(analysis.get("missing_keywords", []) or "None identified")
    st.subheader("Relevant evidence")
    st.json(analysis.get("relevant_resume_evidence", []))
    st.subheader("Areas needing improvement")
    st.json(analysis.get("areas_needing_improvement", []))
    # st.caption(f"Methodology: {analysis.get('methodology_version', 'deterministic-ats-v1')}")
