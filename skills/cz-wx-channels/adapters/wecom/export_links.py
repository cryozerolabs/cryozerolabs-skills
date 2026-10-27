#!/usr/bin/env python3
"""Convert WeCom message exports or a configured exporter into link jobs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from channels_core import VideoError, atomic_json, load_config, normalize_inputs, now, output_directory, read_input


def main():
    parser = argparse.ArgumentParser(description="企微聊天记录/本地提链器 → 视频号标准链接列表")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="TXT、JSON、JSONL，或 - 表示标准输入")
    source.add_argument("--group", help="调用本机配置的企微提链器")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        destination = args.output.expanduser().resolve()
        output_directory(destination.parent)
        if args.input:
            payload = read_input(args.input)
        else:
            config = load_config(args.config).get("wecom_exporter", {})
            argv, result_file = config.get("argv"), config.get("result_file")
            if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv) or not result_file:
                raise VideoError("exporter_config", "请在本机配置中设置 wecom_exporter.argv 和 result_file。")
            command = [arg.replace("{group}", args.group) for arg in argv]
            exported = Path(result_file).expanduser()
            before = (exported.stat().st_mtime_ns, exported.stat().st_size) if exported.exists() else None
            result = subprocess.run(command, cwd=config.get("cwd"), capture_output=True, text=True,
                                    timeout=float(config.get("timeout", 180)))
            if result.returncode:
                raise VideoError("exporter_failed", f"企微提链器返回退出码 {result.returncode}，可直接检查该提链器的运行状态。", True)
            if not exported.exists():
                raise VideoError("export_missing", "请检查提链器的结果文件路径。", True)
            after = exported.stat().st_mtime_ns, exported.stat().st_size
            if before == after:
                raise VideoError("export_stale", "提链器的结果文件未更新，可重试导出。", True)
            payload = read_input(exported)
        jobs, count = normalize_inputs(payload)
        items = []
        for job in jobs:
            if job["input_error"]:
                continue
            context = dict(job["contexts"][0]) if job["contexts"] else {}
            if args.group:
                context["group"] = args.group
            items.append({"url": job["source_url"], "title": job["title"], "author": job["author"],
                          "shared_at": job["shared_at"], "context": context})
        report = {"schema_version": "1.0", "extracted_at": now(), "input_link_count": count,
                  "unique_link_count": len(items), "items": items}
        atomic_json(destination, report)
        print(json.dumps({"links": len(items), "output": str(destination)}, ensure_ascii=False))
        return 0
    except VideoError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": {"code": "adapter_error", "message": type(exc).__name__}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
