#!/usr/bin/env python3
"""Resolve or download one or many Channels share links."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from channels_core import (Backend, DEFAULT_BACKEND, VERSION, VideoError, atomic_json,
                           download, inspect_mp4, load_config, normalize_inputs, now,
                           output_directory, private_text, read_input, resolve_ffprobe)


def parser():
    result = argparse.ArgumentParser(description="视频号分享链接 → 解密 MP4 与元数据")
    result.add_argument("--version", action="version", version=VERSION)
    commands = result.add_subparsers(dest="action", required=True)
    for name in ("resolve", "download", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path)
        command.add_argument("--backend-url")
        command.add_argument("--timeout", type=float, default=30)
        command.add_argument("--ffprobe")
        if name != "doctor":
            command.add_argument("--url", action="append", default=[])
            command.add_argument("--input", action="append", default=[])
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--retries", type=int, default=1)
            command.add_argument("--refresh", action="store_true", help="重新下载已交付的视频")
    return result


def run(args):
    os.umask(0o077)
    config = load_config(args.config)
    backend = Backend(args.backend_url or config.get("backend_url") or DEFAULT_BACKEND, args.timeout)
    if args.action == "doctor":
        result = {"version": VERSION, "backend_url": backend.base}
        try:
            result.update(backend.status())
        except VideoError as exc:
            result.update({"reachable": False, "connected": False, "error": exc.as_dict()})
        try:
            result["ffprobe"] = resolve_ffprobe(config, args.ffprobe)
        except VideoError as exc:
            result["ffprobe"] = None
            result["file_verification"] = exc.as_dict()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("connected") else 2
    if args.retries < 0 or args.retries > 5:
        raise VideoError("arguments", "--retries 请设置为 0 到 5。")
    inputs = list(args.url)
    for source in args.input:
        inputs.append(read_input(source))
    jobs, input_count = normalize_inputs(inputs)
    if not jobs:
        raise VideoError("input_empty", "请通过 --url 或 --input 提供视频号分享链接。")
    destination = output_directory(args.output)
    ffprobe = resolve_ffprobe(config, args.ffprobe) if args.action == "download" else None
    manifest_path = destination / "manifest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    previous_items = {item["id"]: item for item in previous.get("items", [])}
    manifest = {"schema_version": "1.0", "skill_version": VERSION, "action": args.action,
                "started_at": now(), "backend_url": backend.base, "input_count": input_count,
                "unique_count": len(jobs), "duplicates_removed": input_count - len(jobs), "items": []}
    environment_error = None

    def save():
        manifest["updated_at"] = now()
        manifest["summary"] = {"completed": sum(item["status"] in ("resolved", "downloaded", "reused") for item in manifest["items"]),
                               "failed": sum(item["status"] == "failed" for item in manifest["items"]),
                               "processed": len(manifest["items"]), "total": len(jobs)}
        atomic_json(manifest_path, manifest)
        links = [item["media"]["playback_url"] for item in manifest["items"]
                 if item.get("media") and item["status"] != "failed"]
        private_text(destination / "mp4-links.txt", "\n".join(links) + ("\n" if links else ""))

    for number, job in enumerate(jobs, 1):
        entry = {key: value for key, value in job.items() if key not in ("query", "input_error")}
        entry.update({"status": "pending", "attempts": 0})
        old = previous_items.get(job["id"], {})
        try:
            if job["input_error"]:
                raise VideoError(**job["input_error"])
            if args.action == "download" and not args.refresh and old.get("file", {}).get("path"):
                existing = Path(old["file"]["path"]).resolve()
                if destination in existing.parents and existing.is_file():
                    try:
                        verified = inspect_mp4(existing, ffprobe)
                        if verified["sha256"] == old["file"].get("sha256"):
                            entry.update({key: value for key, value in old.items() if key not in ("contexts", "occurrences")})
                            entry.update({"status": "reused", "attempts": 0,
                                          "file": {"path": str(existing), **verified}})
                    except VideoError:
                        pass
            if entry["status"] != "reused":
                if environment_error:
                    raise environment_error
                for attempt in range(args.retries + 1):
                    entry["attempts"] = attempt + 1
                    try:
                        entry.update(backend.resolve(job))
                        if args.action == "download":
                            entry["file"] = download(backend, entry, destination, ffprobe)
                            entry["status"] = "downloaded"
                        else:
                            entry["status"] = "resolved"
                        break
                    except VideoError as exc:
                        if not exc.retryable or attempt == args.retries:
                            raise
                        time.sleep(min(attempt + 1, 3))
        except VideoError as exc:
            entry["status"], entry["error"] = "failed", exc.as_dict()
            if exc.code in ("backend_unavailable", "client_not_connected"):
                environment_error = exc
        manifest["items"].append(entry)
        save()
        print(json.dumps({"progress": f"{number}/{len(jobs)}", "id": job["id"],
                          "status": entry["status"], "error_code": entry.get("error", {}).get("code")}, ensure_ascii=False), file=sys.stderr, flush=True)
    manifest["finished_at"] = now()
    save()
    print(json.dumps({**manifest["summary"], "manifest": str(manifest_path),
                      "files": [item["file"]["path"] for item in manifest["items"] if item.get("file") and item["status"] != "failed"]}, ensure_ascii=False))
    return 1 if manifest["summary"]["failed"] else 0


def main():
    try:
        return run(parser().parse_args())
    except VideoError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": "local_io", "message": f"请检查输入和输出路径（{type(exc).__name__}）。"}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
