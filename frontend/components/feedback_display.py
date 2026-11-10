"""Reusable display for validated Groq feedback returned by the backend."""

from __future__ import annotations

import streamlit as st


def render_feedback(feedback: dict) -> None:
    st.subheader("AI feedback")
    st.write(feedback.get("overall_feedback", ""))
    for heading, key in (("Strengths", "strengths"), ("Missing skills", "missing_skills_feedback"), ("Resume improvements", "resume_improvements"), ("Suggested future projects", "project_recommendations")):
        st.markdown(f"#### {heading}")
        for item in feedback.get(key, []):
            st.write(item)
    plan = feedback.get("action_plan", {})
    if plan:
        st.markdown("#### Action plan")
        for priority in ("high_priority", "medium_priority", "long_term"):
            st.write(f"**{priority.replace('_', ' ').title()}**")
            for item in plan.get(priority, []): st.write(f"- {item}")
