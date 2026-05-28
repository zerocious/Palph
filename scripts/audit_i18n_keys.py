"""One-off audit: t() keys used in code vs locales/*.json."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["bot.py", "plan_handlers.py", "locale_bot.py", "services.py"]
# Require dotted keys (e.g. timer.started) to avoid false positives from t.get("field").
KEY_RE = re.compile(
    r"""t\(\s*[\"']([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)[\"']""",
    re.IGNORECASE,
)


def flatten(d, prefix=""):
    out = set()
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= flatten(v, key)
        else:
            out.add(key)
    return out


def main():
    used = set()
    for fn in FILES:
        p = ROOT / fn
        if p.exists():
            used |= set(KEY_RE.findall(p.read_text(encoding="utf-8")))

    ru = json.loads((ROOT / "locales" / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
    ru_keys = flatten(ru)
    en_keys = flatten(en)

    missing_ru = sorted(used - ru_keys)
    missing_en = sorted(used - en_keys)

    print(f"Used keys: {len(used)}")
    print(f"Missing in ru.json: {len(missing_ru)}")
    for k in missing_ru:
        print(f"  {k}")
    print(f"Missing in en.json: {len(missing_en)}")
    for k in missing_en:
        if k not in missing_ru:
            print(f"  en-only: {k}")


if __name__ == "__main__":
    main()
