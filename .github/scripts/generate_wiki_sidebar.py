#!/usr/bin/env python3
"""Generate _Sidebar.md for the GitHub wiki from docs/index.md table of contents."""

import re
import sys
from pathlib import Path


def extract_table_rows(index_path: str) -> list[tuple[str, str]]:
    """Extract (doc_name, description) pairs from the first markdown table in the index."""
    text = Path(index_path).read_text(encoding="utf-8")

    rows: list[tuple[str, str]] = []
    in_table = False

    for line in text.splitlines():
        line = line.strip()

        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r"-{2,}", c) for c in cells if c):
                in_table = True
                continue
            if in_table and len(cells) >= 2:
                doc = cells[0].strip()
                desc = cells[1].strip() if len(cells) > 1 else ""
                rows.append((doc, desc))
        elif in_table and not line:
            break

    return rows


def wiki_path_for_file(filename: str) -> str:
    """Map a docs filename (without .md) to its wiki page name."""
    # home.md becomes Home.md on the wiki (the landing page)
    if filename == "home":
        return "Home"
    return filename


def link_to_wiki_path(md_text: str) -> str | None:
    """Extract the wiki page path from a markdown link or code backtick."""
    # [Text](file.md)
    m = re.search(r"\[.*?\]\(([^)]+)\)", md_text)
    if m:
        return wiki_path_for_file(m.group(1).removesuffix(".md"))
    # `file.md`
    m = re.search(r"`([^`]+\.md)`", md_text)
    if m:
        return wiki_path_for_file(m.group(1).removesuffix(".md"))
    return None


def discover_adrs(docs_dir: str) -> list[tuple[str, str]]:
    """Discover ADR .md files in the adr/ subdirectory, sorted by number."""
    adr_dir = Path(docs_dir) / "adr"
    if not adr_dir.is_dir():
        return []

    adrs: list[tuple[str, str]] = []
    for f in sorted(adr_dir.glob("ADR-*.md")):
        # Guard against case-insensitive filesystems matching unrelated files
        if not f.name.startswith("ADR-"):
            continue
        name = f.stem  # e.g. "ADR-001-planner-extraction"
        path = f"adr/{name}"
        adrs.append((name, path))

    return adrs


def generate_sidebar(rows: list[tuple[str, str]], adrs: list[tuple[str, str]]) -> str:
    """Generate _Sidebar.md content."""
    lines: list[str] = [
        "# HSEM Documentation",
        "",
        "## Quick Reference",
        "",
    ]

    for doc, desc in rows:
        wiki_path = link_to_wiki_path(doc)
        if not wiki_path:
            continue
        display = re.search(r"\[([^\]]+)\]", doc)
        name = display.group(1) if display else wiki_path
        lines.append(f"- [{name}]({wiki_path}) — {desc}")

    if adrs:
        lines.append("")
        lines.append("## Architecture Decision Records")
        lines.append("")
        lines.append("- [ADR Index](adr/index)")
        for name, path in adrs:
            lines.append(f"- [{name}]({path})")

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: generate_wiki_sidebar.py <path-to-docs/index.md>",
            file=sys.stderr,
        )
        sys.exit(1)

    index_path = sys.argv[1]
    docs_dir = str(Path(index_path).parent)

    rows = extract_table_rows(index_path)
    adrs = discover_adrs(docs_dir)
    sidebar = generate_sidebar(rows, adrs)
    print(sidebar)


if __name__ == "__main__":
    main()
