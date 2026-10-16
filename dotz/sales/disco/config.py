"""Discovery v2 config lives OUTSIDE policy.yaml: editing policy.yaml voids owner approval (hash-bound)."""
from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_FILE = Path(__file__).with_name("discovery_v2.yaml")


def load(path: Path = CONFIG_FILE) -> dict:
    return yaml.safe_load(Path(path).read_text())
