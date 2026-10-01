"""Offline backtest harness for the HSEM planner (issue #1037).

``run_planner`` is a pure function over a :class:`PlannerInput`, and
``hsem.export_diagnostics`` already serialises that input.  Together those two
facts make it possible to replay a **real** production planning cycle offline
and check it against the invariants in ``docs/planner-spec.md`` — rather than
only against synthetic fixtures, which by construction cannot contain the
input combinations that actually reach users.

Stage 1 (this package) replays recorded inputs and scores them for
*self-consistency*.  Stage 2 — savings versus a no-action baseline and regret
versus a perfect-foresight oracle — additionally needs realized PV, house load,
prices and SoC, which no dump carries today.  See ``docs/backtest-harness.md``.
"""
