from __future__ import annotations

INSTRUCTIONS: dict[str, str] = {
    "C1": (
        "Classify and place source chunks into source-backed context blocks with an inclusion "
        "default: content is placed unless there is a specific exclusion reason such as "
        "extraction garbage, boilerplate, or navigation debris. 'not typical wiki prose' and "
        "'tabular/row-level data' are NOT valid exclusion reasons. Every irrelevant verdict "
        "requires a concrete per-chunk reason. The domain hint is context for organizing "
        "placements, not a relevance gate."
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
    "C7": (
        "Generate concise workspace domain hint markdown from the sampled sources and current "
        "registry titles/descriptions. Return JSON with a non-empty domain_hint_markdown string "
        "that helps future placement organize content without excluding content by domain."
    ),
}
