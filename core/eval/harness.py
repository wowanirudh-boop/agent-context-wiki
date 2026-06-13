from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class EvalQuestionResult:
    question: str
    expected_keys: list[str]
    expected_substrings: list[str]
    retrieved_keys: list[str]
    matched_substrings: list[str]
    passed: bool


@dataclass(frozen=True, slots=True)
class EvalResult:
    workspace: str
    total: int
    passed: int
    score: float
    baseline_score: float | None
    regressed: bool
    questions: list[EvalQuestionResult]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def evaluate_workspace(
    workspace: str | Path,
    *,
    update_baseline: bool = False,
) -> EvalResult:
    ws = Path(workspace)
    questions = _load_questions(ws)
    handler = _read_handler(ws)
    results = [await _answer_and_score(handler, question) for question in questions]
    passed = sum(result.passed for result in results)
    score = passed / len(results) if results else 1.0
    baseline_path = ws / "eval" / "baseline.json"
    baseline_score = _read_baseline_score(baseline_path)
    if update_baseline or baseline_score is None:
        baseline_score = score
        _write_baseline(baseline_path, score)
    regressed = score < baseline_score
    return EvalResult(
        workspace=str(ws),
        total=len(results),
        passed=passed,
        score=score,
        baseline_score=baseline_score,
        regressed=regressed,
        questions=results,
    )


async def _answer_and_score(handler: Any, question: dict[str, Any]) -> EvalQuestionResult:
    prompt = str(question.get("question", ""))
    expected_keys = [str(value) for value in question.get("expected_keys", [])]
    expected_substrings = [str(value) for value in question.get("expected_substrings", [])]
    pages = await _candidate_pages(handler, prompt)
    retrieved_keys: set[str] = set()
    markdown_parts: list[str] = []
    for page in pages:
        try:
            detail = await handler.wiki_page(page, statuses=["*"])
        except (FileNotFoundError, KeyError):
            continue
        retrieved_keys.update(str(block["key"]) for block in detail.get("blocks", []))
        markdown_parts.append(str(detail.get("markdown", "")))
    markdown = "\n".join(markdown_parts).casefold()
    matched_substrings = [value for value in expected_substrings if value.casefold() in markdown]
    passed = set(expected_keys).issubset(retrieved_keys) and len(matched_substrings) == len(expected_substrings)
    return EvalQuestionResult(
        question=prompt,
        expected_keys=expected_keys,
        expected_substrings=expected_substrings,
        retrieved_keys=sorted(retrieved_keys),
        matched_substrings=matched_substrings,
        passed=passed,
    )


async def _candidate_pages(handler: Any, question: str) -> list[str]:
    ordered: list[str] = []
    for tier in ("summary", "full"):
        try:
            search = await handler.wiki_search(question, tier=tier, limit=5)
        except (FileNotFoundError, KeyError, ValueError):
            search = {"results": []}
        for item in search.get("results", []):
            page = str(item.get("page", ""))
            if page and page not in ordered:
                ordered.append(page)
    if ordered:
        return ordered
    index = await handler.wiki_index()
    for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", index.get("markdown", "")):
        page = link if link.startswith("wiki/") else f"wiki/{link}"
        if page not in ordered:
            ordered.append(page)
    return ordered


def _load_questions(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "eval" / "questions.yaml"
    if not path.exists():
        return []
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError("eval/questions.yaml must contain a list")
    return [item for item in raw if isinstance(item, dict)]


def _read_baseline_score(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    try:
        return float(data["score"])
    except (KeyError, TypeError, ValueError):
        return None


def _write_baseline(path: Path, score: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"score": score}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_handler(workspace: Path):
    try:
        from tools.wiki_read import WikiReadHandler
    except ImportError:
        mcp_dir = Path(__file__).resolve().parents[2] / "mcp"
        if str(mcp_dir) not in sys.path:
            sys.path.insert(0, str(mcp_dir))
        from tools.wiki_read import WikiReadHandler
    return WikiReadHandler(workspace)
