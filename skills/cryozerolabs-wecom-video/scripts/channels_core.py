"""Link normalization, local downloader transport, and verified MP4 delivery."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

VERSION = "0.1.0"
DEFAULT_BACKEND = "http://127.0.0.1:2022"
URL_PATTERN = re.compile(r"https://[^\s<>\"']+")
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = next((path for path in (PACKAGE_ROOT, *PACKAGE_ROOT.parents) if (path / ".git").exists()), PACKAGE_ROOT)


class VideoError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def as_dict(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config_path():
    configured = os.environ.get("CRYOZEROLABS_WECOM_VIDEO_CONFIG")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/cryozerolabs-wecom-video/config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "cryozerolabs-wecom-video/config.json"


def load_config(path=None):
    source = Path(path).expanduser() if path else config_path()
    if not source.exists():
        if path:
            raise VideoError("config_missing", "请检查配置文件路径。")
        return {}
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VideoError("config_invalid", "配置文件需要是 JSON 对象。") from None
    if not isinstance(value, dict):
        raise VideoError("config_invalid", "配置文件需要是 JSON 对象。")
    return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def private_text(path, value):
    path = Path(path)
    with path.open("w", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(value)


def classify(url):
    url = html.unescape(url.strip())
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            raise ValueError
    except ValueError:
        raise VideoError("invalid_link", "请输入 HTTPS 视频号分享链接。") from None
    query = parse_qs(parsed.query)
    if parsed.hostname == "weixin.qq.com" and re.fullmatch(r"/sph/[A-Za-z0-9_-]+/?", parsed.path):
        identity = "sph:" + parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return url, identity, {"url": url}
    if parsed.hostname == "channels.weixin.qq.com" and parsed.path == "/finder-preview/pages/feed" and query.get("eid"):
        eid = query["eid"][0]
        return url, "eid:" + eid, {"eid": eid}
    raise VideoError("invalid_link", "请输入 sph 分享链接或带 eid 的企微视频号链接。")


def normalize_inputs(value):
    """Accept links, exported item lists, and nested message JSON with context."""
    found = []

    def add(text, metadata):
        cleaned = html.unescape(text.replace("\\/", "/"))
        for match in URL_PATTERN.finditer(cleaned):
            url = match.group(0).rstrip(".,;:)]}，。；：）】")
            parsed = urlsplit(url)
            if parsed.hostname not in ("weixin.qq.com", "channels.weixin.qq.com"):
                continue
            if not (parsed.path.startswith("/sph/") or parsed.path == "/finder-preview/pages/feed"):
                continue
            found.append({**metadata, "url": url})

    def visit(node, inherited):
        if isinstance(node, str):
            add(node, inherited)
        elif isinstance(node, list):
            for item in node:
                visit(item, inherited)
        elif isinstance(node, dict):
            metadata = dict(inherited)
            for key in ("title", "author", "shared_at", "context"):
                if key in node:
                    metadata[key] = node[key]
            if not isinstance(metadata.get("context", {}), dict):
                metadata.pop("context", None)
            for key in ("group", "send_time", "user_name"):
                if key in node and isinstance(node[key], (str, int, float)):
                    metadata["context"] = {**metadata.get("context", {}), key: node[key]}
            for key, item in node.items():
                if key not in ("context", "title", "author", "shared_at"):
                    visit(item, metadata)

    visit(value, {})
    jobs = {}
    for row in found:
        try:
            url, identity, query = classify(row["url"])
            error = None
        except VideoError as exc:
            url, identity, query, error = row["url"], row["url"], {}, exc.as_dict()
        key = hashlib.sha256(identity.encode()).hexdigest()[:20]
        context = row.get("context", {})
        if key in jobs:
            jobs[key]["occurrences"] += 1
            if context and context not in jobs[key]["contexts"]:
                jobs[key]["contexts"].append(context)
            continue
        jobs[key] = {"id": key, "source_url": url, "title": row.get("title", ""),
                     "author": row.get("author", ""), "shared_at": row.get("shared_at"),
                     "contexts": [context] if context else [], "occurrences": 1,
                     "query": query, "input_error": error}
    return list(jobs.values()), len(found)


def read_input(path):
    source = sys.stdin.read() if str(path) == "-" else Path(path).expanduser().read_text(encoding="utf-8-sig")
    try:
        return json.loads(source)
    except ValueError:
        if str(path).endswith((".jsonl", ".ndjson")):
            return [json.loads(line) for line in source.splitlines() if line.strip()]
        return source


def unwrap(payload):
    for _ in range(7):
        if not isinstance(payload, dict):
            break
        codes = (payload.get("code", 0), payload.get("errCode", 0))
        error = payload.get("errMsg")
        if any(str(code) not in ("0", "200") for code in codes) or (isinstance(error, dict) and error.get("type")):
            message = error.get("title", "详情接口返回错误。") if isinstance(error, dict) else str(error or payload.get("msg") or "详情接口返回错误。")
            if "initialize the client socket" in message:
                raise VideoError("client_not_connected", "请连接一次视频号首页，然后继续整批任务。", True)
            message = re.sub(r"https?://\S+", "[URL]", message)[:240]
            raise VideoError("profile_error", message)
        if "data" not in payload:
            return payload
        payload = payload["data"]
    return payload


class Backend:
    def __init__(self, base=DEFAULT_BACKEND, timeout=30):
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise VideoError("backend_config", "backend_url 请使用本机下载器的 HTTP 地址。")
        self.base, self.timeout = base.rstrip("/"), timeout
        self.opener = build_opener(ProxyHandler({}))

    def open(self, path, headers=None):
        request = Request(self.base + path, headers={"User-Agent": "cryozerolabs-wecom-video/" + VERSION, **(headers or {})})
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            raise VideoError("backend_http", f"本地下载器返回 HTTP {exc.code}。", exc.code in (408, 429) or exc.code >= 500) from None
        except (URLError, TimeoutError, OSError):
            raise VideoError("backend_unavailable", "请启动已配置的本地视频号下载器后继续。", True) from None

    def get(self, path):
        with self.open(path, {"Accept": "application/json"}) as response:
            try:
                return unwrap(json.loads(response.read(8 * 1024 * 1024)))
            except ValueError:
                raise VideoError("backend_json", "本地下载器需要返回 JSON。") from None
            except (OSError, HTTPException):
                raise VideoError("backend_response", "详情响应中断，可重试。", True) from None

    def status(self):
        payload = self.get("/api/channels/status")
        return {"reachable": True, "connected": isinstance(payload, dict) and payload.get("available") is True}

    def resolve(self, job):
        payload = self.get("/api/channels/feed/profile?" + urlencode(job["query"]))
        obj = payload.get("object", payload) if isinstance(payload, dict) else {}
        desc = obj.get("objectDesc", {})
        for media in desc.get("media", []):
            raw_url = media.get("url", "")
            if media.get("mediaType") not in (None, 4) or not raw_url:
                continue
            source_url = raw_url + str(media.get("urlToken") or "")
            parsed = urlsplit(source_url)
            if parsed.scheme != "https" or parsed.username or parsed.password:
                continue
            key = str(media.get("decodeKey") or "")
            path = "/play?" + urlencode({"url": source_url, "key": key})
            try:
                with self.open(path, {"Range": "bytes=0-4095"}) as response:
                    header = response.read(4096)
            except (OSError, HTTPException):
                raise VideoError("media_response", "媒体验证响应中断，可重试。", True) from None
            if len(header) < 12 or header[4:8] != b"ftyp":
                raise VideoError("invalid_mp4", "媒体响应未通过 MP4 文件头验证。", True)
            valid_for = obj.get("urlValidDuration")
            expires_at = None
            if isinstance(valid_for, (int, float)) and valid_for > 0:
                expires_at = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + valid_for, timezone.utc).isoformat(timespec="seconds")
            return {"title": desc.get("description") or job["title"],
                    "author": obj.get("contact", {}).get("nickname") or obj.get("nickname") or job["author"],
                    "resolved_at": now(), "media": {
                        "source_url": source_url, "playback_url": self.base + path,
                        "requires_decryption": bool(key and key != "0"),
                        "decryption": "wx_channels_download/play", "expires_at": expires_at,
                        "source_width": media.get("width"), "source_height": media.get("height"),
                        "source_file_size": media.get("fileSize"), "mp4_header_verified": True}}
        raise VideoError("media_missing", "该详情响应没有视频媒体。")


def safe_filename(title, identity):
    name = unicodedata.normalize("NFC", str(title or "video").splitlines()[0])
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", name).strip(" .")[:64]
    return f"{name or 'video'}--{identity}.mp4"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_mp4(path, ffprobe):
    path = Path(path)
    with path.open("rb") as handle:
        header = handle.read(12)
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise VideoError("invalid_mp4", "文件未通过 MP4 文件头验证。", True)
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                                 "format=format_name,duration,size:stream=index,codec_type,codec_name,width,height,duration",
                                 "-of", "json", str(path)], capture_output=True, text=True, timeout=45)
        info = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise VideoError("probe_failed", "视频流校验失败，可重试下载。", True) from None
    streams = info.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video or not video.get("width") or not video.get("height"):
        raise VideoError("probe_failed", "文件需要包含可识别的视频流。", True)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    return {"size_bytes": path.stat().st_size, "sha256": sha256_file(path),
            "duration_seconds": float(info.get("format", {}).get("duration") or 0),
            "width": video["width"], "height": video["height"],
            "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name"),
            "streams": streams, "verified": True}


def download(backend, entry, destination, ffprobe):
    target = destination / "videos" / safe_filename(entry["title"], entry["id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".mp4.part")
    path = entry["media"]["playback_url"][len(backend.base):]
    try:
        with backend.open(path) as response, partial.open("wb") as handle:
            os.chmod(partial, 0o600)
            expected = response.headers.get("Content-Length")
            for block in iter(lambda: response.read(256 * 1024), b""):
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        if expected and partial.stat().st_size != int(expected):
            raise VideoError("incomplete_download", "下载字节数与响应长度不一致，可重试。", True)
        metadata = inspect_mp4(partial, ffprobe)
        partial.replace(target)
        return {"path": str(target.resolve()), **metadata}
    except VideoError:
        raise
    except (OSError, HTTPException, ValueError) as exc:
        raise VideoError("download_failed", f"下载中断（{type(exc).__name__}），可重试。", True) from None
    finally:
        partial.unlink(missing_ok=True)


def resolve_ffprobe(config, override=None):
    value = override or config.get("ffprobe") or "ffprobe"
    executable = shutil.which(value)
    if not executable:
        raise VideoError("ffprobe_missing", "请安装 FFmpeg，或在配置中指定 ffprobe 路径。")
    return executable


def output_directory(path):
    destination = Path(path).expanduser().resolve()
    if destination == SOURCE_ROOT or SOURCE_ROOT in destination.parents:
        raise VideoError("output_location", "请把输出目录设置到 skill 源码目录之外。")
    destination.mkdir(parents=True, exist_ok=True)
    return destination
