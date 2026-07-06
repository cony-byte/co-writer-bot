#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""story-v1-scripts repo의 reference/를 이 repo의 data/reference/로 동기화.

우선순위:
  1) 로컬 형제 체크아웃 (../story-v1-scripts/reference) → 그대로 복사
  2) 없으면 GitHub에서 clone (gh auth 필요) 후 복사

사용법: python3 scripts/sync_reference.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEST = BASE / "data" / "reference"
SIBLING = BASE.parent / "story-v1-scripts" / "reference"
REPO = "https://github.com/cony-byte/story-v1-scripts.git"


def copy_reference(src: Path) -> None:
    if not (src / "reference_db.json").exists():
        sys.exit(f"레퍼런스가 아님 (reference_db.json 없음): {src}")
    if DEST.exists():
        shutil.rmtree(DEST)
    shutil.copytree(src, DEST)
    n = len(list(DEST.rglob("*")))
    print(f"동기화 완료: {src} → {DEST} ({n}개 파일)")


def main() -> None:
    if SIBLING.is_dir():
        copy_reference(SIBLING)
        return
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "--depth", "1", REPO, tmp], check=True)
        copy_reference(Path(tmp) / "reference")


if __name__ == "__main__":
    main()
