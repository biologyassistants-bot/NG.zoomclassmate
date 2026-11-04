#!/usr/bin/env python3
"""Conservative one-time repair for NG-ClassMate's partially recovered roster.

Default mode is DRY RUN: it never replaces roster.json. It reads the original
backup plus V13's recovered copy, repairs only records missing from the known-
good copy, validates identities/counts, and writes a candidate JSON file.

Run from Render Shell after adding this file to the deployed backend:
    python repair_roster_76.py

Only after reviewing the generated report and confirming all checks pass:
    python repair_roster_76.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

EXPECTED_TOTAL = 2842
EXPECTED_RECOVERED = 2766
EXPECTED_MISSING = EXPECTED_TOTAL - EXPECTED_RECOVERED


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


def parse_record(chunk: str, json_repair_loads):
    """Parse one roster object, progressively using safer parsers."""
    chunk = chunk.strip()
    # The object chunk is bounded by the next top-level object. Remove only
    # array/object separators outside the record at its end.
    while chunk.endswith(","):
        chunk = chunk[:-1].rstrip()
    if chunk.endswith("]"):
        chunk = chunk[:-1].rstrip()
        if chunk.endswith(","):
            chunk = chunk[:-1].rstrip()

    attempts = []
    for label, parser in (
        ("stdlib-strict", lambda s: json.loads(s)),
        ("stdlib-permissive-controls", lambda s: json.loads(s, strict=False)),
    ):
        try:
            obj = parser(chunk)
            if isinstance(obj, dict):
                return obj, label, None
            attempts.append(f"{label}: parsed non-object {type(obj).__name__}")
        except Exception as exc:
            attempts.append(f"{label}: {type(exc).__name__}: {exc}")

    # Last resort: json-repair can handle structural faults such as missing
    # separators/quotes. This is used only for a single record, never the whole
    # roster, so the already-recovered records remain untouched.
    try:
        obj = json_repair_loads(chunk)
        if isinstance(obj, dict):
            return obj, "json-repair", None
        attempts.append(f"json-repair: parsed non-object {type(obj).__name__}")
    except Exception as exc:
        attempts.append(f"json-repair: {type(exc).__name__}: {exc}")
    return None, None, " | ".join(attempts)


def normalized_id(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def load_json_list(path: Path, label: str) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    with path.open("r", encoding="utf-8-sig") as fh:
        value = json.load(fh)
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise ValueError(f"{label} must be a JSON array of objects")
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=os.getenv("DATA_DIR") or "/var/data")
    ap.add_argument("--apply", action="store_true", help="Replace roster.json only after all checks pass")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    original = data_dir / "roster.json.pre-control-char-repair.bak"
    known_good_path = data_dir / "roster.json.recovered.json"
    candidate = data_dir / "roster.json.repaired-candidate.json"
    report = data_dir / "roster.json.repaired-candidate-report.txt"
    live_roster = data_dir / "roster.json"

    # Import only at runtime so the message is actionable in Render Shell.
    try:
        from json_repair import loads as json_repair_loads
    except ImportError:
        print("Missing dependency. Run: python -m pip install 'json-repair==0.*'")
        return 2

    try:
        known_good = load_json_list(known_good_path, "V13 recovered roster")
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    known_by_id: dict[str, dict] = {}
    for i, student in enumerate(known_good):
        sid = normalized_id(student.get("id"))
        if not sid:
            print(f"ERROR: recovered record {i} has no id")
            return 2
        if sid in known_by_id:
            print(f"ERROR: duplicate id in known-good recovered file at record {i}")
            return 2
        known_by_id[sid] = student

    try:
        raw = original.read_text(encoding="utf-8-sig")
    except Exception as exc:
        print(f"ERROR: could not read original backup {original}: {exc}")
        return 2

    starts = [m.start() for m in re.finditer(r"(?m)^  \{", raw)]
    if len(starts) != EXPECTED_TOTAL:
        print(f"ERROR: expected {EXPECTED_TOTAL} top-level records, detected {len(starts)}. No files changed.")
        return 2

    ordered: list[dict] = []
    seen_ids: set[str] = set()
    parser_counts = {"known-good-preserved": 0, "stdlib-strict": 0,
                     "stdlib-permissive-controls": 0, "json-repair": 0}
    failures: list[str] = []
    repaired_new: list[dict] = []

    for index, begin in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(raw)
        chunk = raw[begin:end]
        obj, method, error = parse_record(chunk, json_repair_loads)
        if not isinstance(obj, dict):
            failures.append(f"record {index}: {error or 'could not parse object'}")
            continue

        sid = normalized_id(obj.get("id"))
        if not sid:
            failures.append(f"record {index}: parsed object has no usable id")
            continue
        if sid in seen_ids:
            failures.append(f"record {index}: duplicate id encountered")
            continue
        seen_ids.add(sid)

        if sid in known_by_id:
            # Use the previously recovered object verbatim; do not allow a
            # repair parser to mutate any of the known-good 2,766 records.
            ordered.append(known_by_id[sid])
            parser_counts["known-good-preserved"] += 1
            continue

        email = obj.get("email")
        if not isinstance(email, str) or not email.strip():
            failures.append(f"record {index}: recovered object is missing a usable email")
            continue
        if not isinstance(obj.get("courses", []), (list, str, type(None))):
            failures.append(f"record {index}: courses field has an unexpected type")
            continue
        if "password_hash" in obj and not isinstance(obj.get("password_hash"), str):
            failures.append(f"record {index}: password_hash field has an unexpected type")
            continue

        ordered.append(obj)
        repaired_new.append(obj)
        parser_counts[method] = parser_counts.get(method, 0) + 1

    # Ensure every known-good student id is present in the source record stream.
    missing_known = sorted(set(known_by_id) - seen_ids)
    if missing_known:
        failures.append(f"{len(missing_known)} known-good IDs were not found in original backup")

    # Final structural checks.
    if len(known_good) != EXPECTED_RECOVERED:
        failures.append(f"recovered file has {len(known_good)} records; expected {EXPECTED_RECOVERED}")
    if len(repaired_new) != EXPECTED_MISSING:
        failures.append(f"repaired {len(repaired_new)} missing records; expected {EXPECTED_MISSING}")
    if len(ordered) != EXPECTED_TOTAL:
        failures.append(f"candidate contains {len(ordered)} records; expected {EXPECTED_TOTAL}")
    if len(seen_ids) != EXPECTED_TOTAL:
        failures.append(f"source has {len(seen_ids)} unique IDs; expected {EXPECTED_TOTAL}")

    # Check the known-good IDs occur once and the final JSON is serializable.
    try:
        encoded = json.dumps(ordered, ensure_ascii=False, indent=2)
        json.loads(encoded)
    except Exception as exc:
        failures.append(f"candidate serialization/strict JSON validation failed: {type(exc).__name__}: {exc}")

    summary = [
        "Roster repair candidate report",
        f"Original backup: {original}",
        f"V13 recovered copy: {known_good_path}",
        f"Top-level records detected: {len(starts)}",
        f"Known-good records preserved verbatim: {parser_counts['known-good-preserved']}",
        f"Previously missing records repaired: {len(repaired_new)}",
        f"Candidate records: {len(ordered)}",
        "Parser counts: " + ", ".join(f"{k}={v}" for k, v in parser_counts.items()),
        f"Validation failures: {len(failures)}",
    ]
    if failures:
        summary.append("\nFAILURES (no candidate applied):")
        summary.extend(f"- {item}" for item in failures[:200])
        report.write_text("\n".join(summary) + "\n", encoding="utf-8")
        print("\n".join(summary))
        print(f"\nNo roster file was overwritten. Details: {report}")
        return 3

    atomic_write_json(candidate, ordered)
    summary.append(f"\nCandidate written: {candidate}")
    summary.append("The live roster.json has NOT been changed.")

    if args.apply:
        # Re-validate from disk, then keep one more immediate backup of the
        # current live file before replacing it.
        verified = load_json_list(candidate, "Repaired candidate")
        if len(verified) != EXPECTED_TOTAL or len({normalized_id(x.get('id')) for x in verified}) != EXPECTED_TOTAL:
            raise RuntimeError("Candidate changed or failed final validation; live roster was not replaced")
        if live_roster.exists():
            safety_copy = data_dir / "roster.json.pre-final-repair.bak"
            if not safety_copy.exists():
                shutil.copy2(live_roster, safety_copy)
        atomic_write_json(live_roster, verified)
        summary.append(f"\nAPPLIED: live roster replaced. Additional backup: {data_dir / 'roster.json.pre-final-repair.bak'}")

    report.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    if not args.apply:
        print("\nReview the report first. Only then rerun with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
