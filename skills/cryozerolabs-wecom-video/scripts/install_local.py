#!/usr/bin/env python3
"""Install an independent copy of the skill, keeping runtime data separate."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile

from channels_core import SOURCE_ROOT

NAME = "cryozerolabs-wecom-video"
ROOT = Path(__file__).resolve().parents[1]


def main():
    default_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser = argparse.ArgumentParser(description="安装独立的本地 skill 副本")
    parser.add_argument("--destination", type=Path, default=default_home / "skills" / NAME)
    parser.add_argument("--backup-dir", type=Path, default=default_home / "skill-backups" / NAME)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    if destination == SOURCE_ROOT or SOURCE_ROOT in destination.parents or destination in SOURCE_ROOT.parents:
        parser.error("请选择发布源码目录之外的本地安装目录。")
    if destination.exists() and not args.replace:
        parser.error("本地副本已存在；更新时添加 --replace，现有副本会先备份。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wecom-video-install-", dir=destination.parent) as temporary:
        staged = Path(temporary) / NAME
        staged.mkdir()
        for name in ("SKILL.md", "LICENSE", "agents", "scripts", "references", "adapters", "examples"):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, staged / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            elif source.is_file():
                shutil.copy2(source, staged / name)
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_root = args.backup_dir.expanduser().resolve()
            if backup_root == destination or destination in backup_root.parents or backup_root == destination.parent or destination.parent in backup_root.parents:
                parser.error("请选择 skills 安装目录之外的备份目录。")
            backup_root.mkdir(parents=True, exist_ok=True)
            destination.rename(backup_root / stamp)
        staged.rename(destination)
    print(destination)


if __name__ == "__main__":
    main()
