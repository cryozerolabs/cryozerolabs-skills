"""Verify local cache objects and fetch ordinary attachments via wecom-cli."""
import hashlib
from pathlib import Path
import shutil
import tempfile

from common import ReadError, digest_file, private_dir, safe_name


def cache_roots(config, database=None):
    roots = [Path(value).expanduser().resolve() for value in config.get('cache_roots', [])]
    if database:
        profile = Path(database).parent.parent
        roots.extend(profile / 'Caches' / kind for kind in ('Files', 'Images', 'Videos', 'Voices'))
    return list(dict.fromkeys(roots))


def inspect(path, attachment):
    path = Path(path).resolve()
    if not path.is_file():
        raise ReadError('attachment_missing', '附件文件当前不在本机。')
    size = path.stat().st_size
    expected = attachment.get('size_bytes')
    if isinstance(expected, int) and expected >= 0 and size != expected:
        raise ReadError('attachment_size', '附件文件大小与消息记录不一致。')
    md5 = attachment.get('expected_md5')
    if md5 and digest_file(path, 'md5') != md5:
        raise ReadError('attachment_checksum', '附件校验值与消息记录不一致。')
    name = (attachment.get('name') or '').lower()
    with path.open('rb') as handle:
        header = handle.read(16)
    if name.endswith('.pdf') and not header.startswith(b'%PDF-'):
        raise ReadError('attachment_format', '附件响应未通过 PDF 文件头检查。')
    if name.endswith(('.docx', '.xlsx', '.pptx')) and not header.startswith(b'PK'):
        raise ReadError('attachment_format', '附件响应未通过 Office 文件头检查。')
    return {'path': str(path), 'size_bytes': size, 'sha256': digest_file(path),
            'integrity': {'expected_size_verified': isinstance(expected, int), 'expected_md5_verified': bool(md5)}}


def enrich_cache(messages, roots):
    wanted = {}
    for message in messages:
        for item in message['attachments']:
            if item.get('expected_md5') and isinstance(item.get('size_bytes'), int):
                wanted.setdefault(item['size_bytes'], []).append(item)
    remaining = sum(len(items) for items in wanted.values())
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob('*'):
            if remaining == 0:
                return
            # Never follow a cache symlink into unrelated files.
            try:
                resolved = path.resolve()
                if root.resolve() not in resolved.parents or not path.is_file() or path.is_symlink():
                    continue
                candidates = [item for item in wanted.get(path.stat().st_size, []) if item.get('availability') != 'file_ready']
                if not candidates:
                    continue
                checksum = digest_file(path, 'md5')
                for item in candidates:
                    if checksum == item['expected_md5']:
                        try:
                            verified = inspect(path, item)
                        except ReadError:
                            continue
                        item.update(file=verified, availability='file_ready', file_source='local_cache')
                        remaining -= 1
            except OSError:
                continue


def materialize(item, destination, official, trusted_roots):
    destination = private_dir(destination)
    previous = item.get('file', {})
    existing = Path(previous['path']).expanduser().resolve() if previous.get('path') else None
    if existing and existing.is_file():
        allowed = any(existing == root or root in existing.parents for root in trusted_roots)
        if allowed:
            verified = inspect(existing, item)
            if previous.get('sha256') and verified['sha256'] != previous['sha256']:
                raise ReadError('attachment_changed', '已记录的附件文件内容发生变化。')
            name = item.get('name') or existing.name
            target = destination / (item['attachment_ref'].split(':')[-1] + '--' + safe_name(name))
            if target.resolve() != existing:
                shutil.copy2(existing, target)
                target.chmod(0o600)
            item.update(file=inspect(target, item), availability='file_ready', content_status=item.get('content_status', 'not_read'))
            return
    retrieval = item.get('retrieval', {})
    if retrieval.get('provider') != 'wecom_cli' or not retrieval.get('media_id'):
        raise ReadError('attachment_reference_missing', '该附件尚无已验证的本机文件或唯一匹配的官方下载引用。')
    if not official.checked:
        official.check()
    with tempfile.TemporaryDirectory(prefix='.fetch-', dir=destination) as temporary:
        staging = Path(temporary).resolve()
        response = official.call(['message', 'files', 'get'], {'media_id': retrieval['media_id']}, output=staging)
        media = response.get('media_item', {})
        if media.get('file_path'):
            file = Path(media['file_path']).expanduser().resolve()
            if staging not in file.parents:
                raise ReadError('attachment_output', '官方下载结果路径不在本次私有下载目录中。')
        elif isinstance(media.get('content'), str):
            file = staging / 'inline-content'
            file.write_text(media['content'], encoding='utf-8'); file.chmod(0o600)
        else:
            raise ReadError('attachment_response', '官方下载没有返回实际文件内容。')
        inspect(file, item)
        name = item.get('name') or media.get('file_name') or 'attachment'
        target = destination / (item['attachment_ref'].split(':')[-1] + '--' + safe_name(name))
        shutil.copy2(file, target); target.chmod(0o600)
        item.update(name=name, file=inspect(target, item), availability='file_ready', file_source='official_download', content_status='not_read')
