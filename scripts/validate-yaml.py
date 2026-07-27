#!/usr/bin/env python3
"""Validate all YAML files in the repo."""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyYAML"])
    import yaml

errors = 0
for yaml_file in sorted(Path(".github").glob("**/*.yml")):
    try:
        yaml.safe_load(yaml_file.read_text())
        print(f"OK  {yaml_file}")
    except Exception as e:
        print(f"ERR {yaml_file}: {e}")
        errors += 1
for yaml_file in sorted(Path(".github").glob("**/*.yaml")):
    try:
        yaml.safe_load(yaml_file.read_text())
        print(f"OK  {yaml_file}")
    except Exception as e:
        print(f"ERR {yaml_file}: {e}")
        errors += 1

sys.exit(errors)
