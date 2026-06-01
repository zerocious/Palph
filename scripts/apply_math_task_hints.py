"""Apply pedagogical hints from top3_tasks_etalon_1.md to math task JSON files."""
from __future__ import annotations

import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATH_ROOT = ROOT / "study_materials" / "math"
TASKS_DIR = MATH_ROOT / "tasks"
DEFAULT_MD = MATH_ROOT / "source" / "top3_tasks_etalon_1.md"

# Tasks referenced in top3_tasks_etalon_1.md (etalon block + legacy Bernoulli refs).
HINT_TASK_NUMBERS = set(range(42, 73)) | {16, 17, 18, 20, 25}

MEDAL_RE = re.compile(r"^\*\*(🥇|🥈|🥉)\s+(.+?)\*\*(?:\s+—.*)?$")
HINT_RE = re.compile(r"^>\s*💡\s*\*\*Подсказка:\*\*\s*(.*)$", re.IGNORECASE)


def latex_to_plain(text: str) -> str:
    """Convert LaTeX fragments in hint text to Telegram-friendly plain text."""

    def _convert_fragment(s: str) -> str:
        s = s.replace("{,}", ",")
        s = s.replace("{:}", ":")
        s = s.replace("{=}", "=")
        s = s.replace("{+}", "+")
        s = s.replace("{-}", "-")
        s = s.replace("{\\cdot}", "·")
        s = re.sub(r"\\sqrt\{([^{}]+)\}", r"√(\1)", s)
        s = re.sub(r"\\dfrac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", s)
        s = re.sub(r"\\tfrac\{([^{}]+)\}\{([^{}]+)\}", r"\1/\2", s)
        s = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"\1/\2", s)
        s = s.replace(r"\cdot", "·")
        s = s.replace(r"\Phi", "Φ")
        s = s.replace(r"\alpha", "α")
        s = s.replace(r"\chi", "χ")
        s = s.replace(r"\Rightarrow", "⇒")
        s = s.replace(r"\le", "≤")
        s = s.replace(r"\ge", "≥")
        s = s.replace(r"\bar{x}", "x̄")
        s = s.replace(r"\bar{y}", "ȳ")
        s = s.replace(r"\bar{d}", "d̄")
        s = re.sub(r"\\text\{([^{}]+)\}", r"\1", s)
        s = re.sub(r"\\[a-zA-Z]+\*?", "", s)
        s = s.replace("{", "").replace("}", "")
        return s

    def _replace_math(m: re.Match[str]) -> str:
        return _convert_fragment(m.group(1))

    out = re.sub(r"\$([^$]+)\$", _replace_math, text)
    return _convert_fragment(out).strip()


def normalize_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[\s\n\r\t]+", " ", text)
    text = re.sub(r"[^\w\s,.\-+%]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_ref_keys(header: str) -> set[str]:
    """Pull searchable tokens from a medal header line."""
    keys: set[str] = set()
    header_l = header.lower()
    for m in re.finditer(r"\b(\d+(?:\.\d+)?)\b", header):
        keys.add(m.group(1))
    for m in re.finditer(r"вар\.?\s*(\d+)", header_l):
        keys.add(f"var{m.group(1)}")
    for m in re.finditer(r"вариант\s*(\d+)", header_l):
        keys.add(f"var{m.group(1)}")
    for m in re.finditer(r"зад(?:ача|\.)\s*(\d+)", header_l):
        keys.add(f"task{m.group(1)}")
    if "гмурман" in header_l or "2.pdf" in header_l:
        keys.add("gmurman")
    if "dkrterver" in header_l:
        keys.add("dkrterver")
    if "дз" in header_l and "3" in header_l:
        keys.add("dz3")
    if "идз1" in header_l:
        keys.add("idz1")
    if "идз2" in header_l:
        keys.add("idz2")
    if "идз3" in header_l:
        keys.add("idz3")
    return keys


def subtitle_keys(subtitle: str) -> set[str]:
    keys: set[str] = set()
    sub_l = subtitle.lower()
    for m in re.finditer(r"\b(\d+(?:\.\d+)?)\b", subtitle):
        keys.add(m.group(1))
    for m in re.finditer(r"вар\.?\s*(\d+)", sub_l):
        keys.add(f"var{m.group(1)}")
    for m in re.finditer(r"зад\.?\s*(\d+)", sub_l):
        keys.add(f"task{m.group(1)}")
    if "гмурман" in sub_l:
        keys.add("gmurman")
    if "dkrterver" in sub_l:
        keys.add("dkrterver")
    if "дз" in sub_l and "3" in sub_l:
        keys.add("dz3")
    if "идз1" in sub_l:
        keys.add("idz1")
    if "идз2" in sub_l:
        keys.add("idz2")
    if "идз3" in sub_l:
        keys.add("idz3")
    return keys


def parse_md_hints(md_path: Path) -> list[dict]:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        m = MEDAL_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        header = m.group(2).strip()
        i += 1
        if i < len(lines) and lines[i].strip().startswith("*"):
            i += 1
        problem_lines: list[str] = []
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if HINT_RE.match(stripped):
                break
            if MEDAL_RE.match(stripped):
                break
            if stripped.startswith("## ") or stripped.startswith("---"):
                break
            if stripped.startswith("```"):
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    problem_lines.append(lines[i].strip())
                    i += 1
                if i < len(lines):
                    i += 1
                continue
            if stripped.startswith(">") and not HINT_RE.match(stripped):
                break
            if stripped:
                problem_lines.append(stripped)
            elif problem_lines:
                if i + 1 < len(lines) and HINT_RE.match(lines[i + 1].strip()):
                    i += 1
                    break
            i += 1
        hint_lines: list[str] = []
        if i < len(lines):
            hm = HINT_RE.match(lines[i].strip())
            if hm:
                if hm.group(1).strip():
                    hint_lines.append(hm.group(1).strip())
                i += 1
                while i < len(lines):
                    stripped = lines[i].strip()
                    if not stripped.startswith(">"):
                        break
                    if stripped.startswith("> ⚠️") or "⚠️" in stripped[:10]:
                        break
                    cont = stripped[1:].strip()
                    if cont:
                        hint_lines.append(cont)
                    i += 1
        problem = "\n".join(problem_lines).strip()
        hint = latex_to_plain("\n".join(hint_lines).strip())
        if problem and hint:
            entries.append(
                {
                    "header": header,
                    "ref_keys": extract_ref_keys(header),
                    "problem": problem,
                    "problem_norm": normalize_text(problem),
                    "hint": hint,
                }
            )
    return entries


def load_task_json_files() -> list[tuple[Path, dict]]:
    items: list[tuple[Path, dict]] = []
    for path in sorted(TASKS_DIR.glob("task-*.json")):
        num = int(path.stem.split("-")[1])
        if num not in HINT_TASK_NUMBERS:
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items.append((path, data))
    return items


def clear_stale_hints(*, dry_run: bool = False) -> int:
    """Remove hint field from tasks outside the etalon hint set."""
    cleared = 0
    for path in sorted(TASKS_DIR.glob("task-*.json")):
        num = int(path.stem.split("-")[1])
        if num in HINT_TASK_NUMBERS:
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "hint" not in data:
            continue
        del data["hint"]
        if not dry_run:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
        cleared += 1
    return cleared


def score_match(entry: dict, task: dict) -> float:
    task_problem = str(task.get("problem") or "")
    task_norm = normalize_text(task_problem)
    if not task_norm or not entry["problem_norm"]:
        return 0.0
    ratio = SequenceMatcher(None, entry["problem_norm"], task_norm).ratio()
    if entry["problem_norm"] in task_norm or task_norm in entry["problem_norm"]:
        ratio = max(ratio, 0.92)
    entry_keys = entry["ref_keys"]
    sub_keys = subtitle_keys(str(task.get("subtitle") or ""))
    overlap = entry_keys & sub_keys
    if overlap:
        ratio += 0.12 * len(overlap)
    numeric_overlap = {
        k for k in overlap
        if re.fullmatch(r"\d+(?:\.\d+)?", k) and not k.startswith("0")
    }
    if numeric_overlap:
        ratio = max(ratio, 0.85)
    elif entry_keys and sub_keys and not overlap:
        ratio -= 0.2
    return ratio


def match_hints_to_tasks(
    entries: list[dict], tasks: list[tuple[Path, dict]]
) -> tuple[dict[Path, str], list[dict]]:
    candidates: list[tuple[float, int, Path, str]] = []
    for ei, entry in enumerate(entries):
        for path, task in tasks:
            score = score_match(entry, task)
            if score >= 0.42:
                candidates.append((score, ei, path, entry["hint"]))
    candidates.sort(key=lambda item: item[0], reverse=True)

    assigned: dict[Path, str] = {}
    used_entries: set[int] = set()
    used_paths: set[Path] = set()
    for score, ei, path, hint in candidates:
        if ei in used_entries or path in used_paths:
            continue
        assigned[path] = hint
        used_entries.add(ei)
        used_paths.add(path)

    unmatched = [
        {**entries[ei], "best_score": round(max(
            (score_match(entries[ei], task) for _, task in tasks),
            default=0.0,
        ), 3)}
        for ei in range(len(entries))
        if ei not in used_entries
    ]
    return assigned, unmatched


def apply_hints(md_path: Path, *, dry_run: bool = False) -> int:
    if not md_path.is_file():
        raise FileNotFoundError(md_path)
    entries = parse_md_hints(md_path)
    tasks = load_task_json_files()
    assigned, unmatched = match_hints_to_tasks(entries, tasks)
    cleared = clear_stale_hints(dry_run=dry_run)
    updated = 0
    for path, hint in assigned.items():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("hint") == hint:
            continue
        data["hint"] = hint
        if not dry_run:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
        updated += 1
    assigned_paths = set(assigned)
    for path, _ in tasks:
        if path in assigned_paths:
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "hint" not in data:
            continue
        del data["hint"]
        if not dry_run:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
        updated += 1
    print(f"Parsed {len(entries)} hints from {md_path.name}")
    print(f"Matched {len(assigned)} tasks, updated {updated} JSON files, cleared {cleared} stale hints")
    if unmatched:
        print(f"Unmatched hints ({len(unmatched)}):")
        for item in unmatched:
            print(f"  - {item['header'][:70]} (best={item['best_score']})")
    return updated


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    md_path = Path(args[0]) if args else DEFAULT_MD
    apply_hints(md_path, dry_run=dry_run)


if __name__ == "__main__":
    main()
