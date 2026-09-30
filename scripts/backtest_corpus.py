"""Replay every cycle of a backtest corpus and check the planner spec.

The pytest suite replays the first few cycles of each corpus file to stay fast;
this replays all of them and summarises what it found.  See
``docs/backtest-runbook.md``.

Usage::

    python3 scripts/backtest_corpus.py                       # $HSEM_BACKTEST_CORPUS
    python3 scripts/backtest_corpus.py ~/hsem-actuals/corpus --limit 50
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from custom_components.hsem.planner.engine_core import run_planner  # noqa: E402
from tests.backtest.invariants import check_invariants  # noqa: E402
from tests.backtest.replay import iter_dumps, planner_input_from_dict  # noqa: E402


def _corpus_files(targets: list[str]) -> list[Path]:
    """Expand files and directories into corpus files, sorted by name."""
    files: list[Path] = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            files.extend(sorted([*path.glob("*.json"), *path.glob("*.jsonl")]))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"[error] no such corpus file or directory: {path}")
    return files


def main(argv: list[str] | None = None) -> int:
    """Replay a corpus and report fidelity, winners and invariant violations.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when no cycle violates an invariant, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "corpus",
        nargs="*",
        help="Corpus files or directories (default: $HSEM_BACKTEST_CORPUS)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Stop after N cycles (default: all)"
    )
    args = parser.parse_args(argv)

    targets = args.corpus or [os.environ.get("HSEM_BACKTEST_CORPUS", "")]
    if not targets[0]:
        raise SystemExit("[error] pass a corpus path or set HSEM_BACKTEST_CORPUS")

    cycles = 0
    versions: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    winners: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    first_seen: dict[str, str] = {}
    started = time.perf_counter()

    for path in _corpus_files(targets):
        for payload in iter_dumps(path):
            if args.limit and cycles >= args.limit:
                break
            planner_input, report = planner_input_from_dict(payload)
            cycles += 1
            versions[report.source_version] += 1
            for name in (*report.missing, *report.dropped):
                missing[name] += 1
            output = run_planner(planner_input)
            winners[output.winner_name] += 1
            for violation in check_invariants(planner_input, output):
                violations[violation.invariant] += 1
                first_seen.setdefault(
                    violation.invariant, f"{planner_input.now_iso}: {violation.detail}"
                )

    elapsed = time.perf_counter() - started
    per_cycle = elapsed / cycles * 1000 if cycles else 0.0
    print(f"cycles: {cycles}  in {elapsed:.0f}s ({per_cycle:.0f} ms/cycle)")
    print(f"versions: {dict(versions)}")
    print(f"winners: {dict(winners)}")
    if missing:
        print("fidelity: fields the dumps and the current PlannerInput disagree on")
        for name, count in missing.most_common():
            print(f"  {name}: {count} cycle(s)")
    else:
        print("fidelity: every cycle round-trips losslessly")
    if violations:
        print(f"invariants: {sum(violations.values())} violation(s)")
        for name, count in violations.most_common():
            print(f"  {name}: {count}  first at {first_seen[name][:140]}")
    else:
        print("invariants: none violated")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
