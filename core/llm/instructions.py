from __future__ import annotations

INSTRUCTIONS: dict[str, str] = {
    "C1": (
        "Classify and place source chunks into source-backed context blocks with an inclusion "
        "default: content is placed unless there is a specific exclusion reason such as "
        "extraction garbage, boilerplate, or navigation debris. 'not typical wiki prose' and "
        "'tabular/row-level data' are NOT valid exclusion reasons. Every irrelevant verdict "
        "requires a concrete per-chunk reason. The domain hint is context for organizing "
        "placements, not a relevance gate. Reuse a key from any supplied page inventory when "
        "it names the same entity and attribute, even when placing the block on another page. "
        "Keep different entities and attributes separate."
    ),
    "C2": (
        "Convert the supplied structured flow definition into Mermaid while preserving every "
        "identifiable node, edge, and branch condition; return only the structured JSON fields "
        "required by the flow validator."
    ),
    "C3": (
        "Compare the candidate block with the existing block and decide whether it is distinct, "
        "a duplicate, or a conflict; use the supplied fields only and return a valid conflict "
        "type and advisory recommendation when a conflict exists. Different source paths and "
        "different pages do not make facts distinct. When different keys describe the same "
        "entity and attribute, set resolved_key to the existing block's exact key and explain "
        "the match using both excerpts in rationale. Values may disagree; that is a conflict. "
        "Similar words, units, or numbers alone do not establish a match. Different entities "
        "or attributes are distinct; independently applicable scopes (such as different "
        "products or regions) are distinct. If identity is uncertain, return resolved_key=null. "
        "Use null also when keys already match. A distinct verdict must have resolved_key=null. "
        "Never invent a third key. Timestamp recency is advisory and never resolves a conflict."
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
