#!/usr/bin/env python3
"""把战队整包 ward_value 缓存拆成单场 match_ward 缓存。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "web" / "backend"))

from app import migrate_match_ward_cache_from_team_reports  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Split team ward_value caches into per-match caches.")
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be migrated.")
    args = parser.parse_args()
    result = migrate_match_ward_cache_from_team_reports(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
