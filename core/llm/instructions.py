from __future__ import annotations

INSTRUCTIONS: dict[str, str] = {
    "C1": (
        "Classify and place source chunks into source-backed context blocks. Relevance is judged "
        "against the workspace domains described in workspace_domain_hint. The structured/tabular "
        "reference data (tables, directories, row-level data) is in scope and should be placed "
        "(e.g., as fact/term blocks on reference pages), NOT marked irrelevant. Marking a chunk "
        "irrelevant requires a specific per-chunk reason. When uncertain, prefer relevant."
    ),
    "C2": (
        "Convert the supplied structured flow definition into Mermaid while preserving every "
        "identifiable node, edge, and branch condition; return only the structured JSON fields "
        "required by the flow validator."
    ),
    "C3": (
        "Compare the candidate block with the existing block and decide whether it is distinct, "
        "a duplicate, or a conflict; use the supplied fields only and return a valid conflict "
        "type and advisory recommendation when a conflict exists."
    ),
    "C4": (
        "Draft a merged candidate block body from the two conflicting blocks without discarding "
        "evidence; the caller will keep both excerpts and route the draft through review."
    ),
    "C5": (
        "Write a concise derived summary of the current page blocks for bounded agent reads, "
        "staying within the provided word limit and avoiding new source claims."
    ),
    "C6": (
        "Classify ordered transcript segments for relevance, identify later segments that "
        "supersede earlier statements on the same key, and extract a source date only when it "
        "is explicit in the transcript text."
    ),
}
