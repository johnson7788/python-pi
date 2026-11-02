"""Build the distillation dataset: (question, answer, rubric) -> boolean verdicts from DeepSeek.

The student learns the *format and judgement* of the teacher, so its training targets are exactly
the teacher's per-criterion verdicts — no new labels are invented here. Records are split by
question (never by answer), using the locked split file, because all answers to one question share
a rubric and letting them straddle the split would leak that rubric into the test set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from coharness.healthbench import Criterion, boolean_prompt, truncate_answer


def _write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)


def criteria_for(points: list[dict]) -> list[Criterion]:
    out = []
    for index, point in enumerate(points, start=1):
        kind = point.get("kind", "core")
        out.append(Criterion(criterion_id=index, text=point.get("label", ""),
                             points=2.0 if kind == "core" else (1.0 if kind == "support" else -4.0),
                             axis=kind))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rubrics", type=Path, required=True)
    parser.add_argument("--splits", type=Path, required=True)
    parser.add_argument("--graded", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    cases = {c["id"]: c for c in json.loads(args.rubrics.read_text())["rubrics"]}
    assignments = {int(k): v for k, v in
                   json.loads(args.splits.read_text())["assignments"].items()}
    graded = json.loads(args.graded.read_text())["rows"]
    answers = {json.loads(line)["run_id"]: json.loads(line)["answer"]
               for line in args.answers.read_text().splitlines() if line.strip()}

    buckets: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    counts: dict[str, int] = {}
    skipped = {"no_answer_text": 0, "case_not_in_split": 0}
    for row in graded:
        case_id = row["case_id"]
        if case_id not in cases or case_id not in assignments:
            skipped["case_not_in_split"] += 1
            continue
        case = cases[case_id]
        answer = answers.get(row["run_id"])
        if not answer:
            skipped["no_answer_text"] += 1
            continue
        criteria = criteria_for(case["points"])
        prompt = boolean_prompt(case["question"], truncate_answer(answer), criteria)
        target = "[" + ", ".join("true" if bit else "false" for bit in row["criteria_met"]) + "]"
        record = {"messages": [{"role": "user", "content": prompt},
                               {"role": "assistant", "content": target}],
                  "case_id": case_id, "run_id": row["run_id"],
                  "n_criteria": len(criteria)}
        buckets[assignments[case_id]].append(json.dumps(record, ensure_ascii=False))
        counts[assignments[case_id]] = counts.get(assignments[case_id], 0) + 1

    manifest = {"counts": counts, "skipped": skipped,
                "questions": {split: sorted({json.loads(line)["case_id"] for line in payload})
                              for split, payload in buckets.items()},
                "targets": "DeepSeek per-criterion verdicts, unmodified",
                "note": "split by question via the locked split file"}
    for split, payload in buckets.items():
        _write(args.out_dir / f"{split}.jsonl", "\n".join(payload) + ("\n" if payload else ""))
    _write(args.out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
