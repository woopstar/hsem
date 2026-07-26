#!/usr/bin/env python3
"""Validate all JSON/JSONC files in the repo."""
import json
import re
import sys
from pathlib import Path

ROOTS = [Path(".devcontainer")]
errors = 0

for root in ROOTS:
    for json_file in sorted(root.rglob("*.json")):
        with open(json_file) as f:
            content = f.read()
        # Strip JSONC comments
        content = re.sub(r"//.*$", "", content, flags=re.MULTILINE)
        content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
        try:
            json.loads(content)
            print(f"OK  {json_file}")
        except json.JSONDecodeError as e:
            print(f"ERR {json_file}: {e}")
            errors += 1

sys.exit(errors)