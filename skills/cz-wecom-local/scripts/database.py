"""Read-only WeCom SQLite snapshots, wxSQLite3 page decoding and key providers.

The on-disk format uses AES-CBC and MD5-derived page keys. This implementation
is specific to that file format, not to any downstream message workflow.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile

from common import ReadError, outside_source, private_dir, private_json

PAGE = 4096
HEADER = b'SQLite format 3\0'


def aes_module():
    try:
        from Crypto.Cipher import AES
        return AES
    except ImportError:
        raise ReadError('crypto_missing', '请为当前 Python 环境安装 requirements.txt 中的 pycryptodome。') from None


def page_iv(number):
    state = number + 1
    words = []
    for _ in range(4):
        quotient, remainder = divmod(state, 52774)
        state = 40692 * remainder - 3791 * quotient
        if state < 0:
            state += 2147483399
        words.append(state & 0xffffffff)
    return hashlib.md5(struct.pack('<4I', *words)).digest()


def decode_page(data, number, key):
    aes = aes_module()
    if len(data) != PAGE:
        raise ReadError('database_truncated', '数据库存在不完整的数据页。')
    derived = hashlib.md5(key + struct.pack('<I', number) + b'sAlT').digest()
    if number != 1:
        return aes.new(derived, aes.MODE_CBC, page_iv(number)).decrypt(data)
    if data.startswith(HEADER):
        return data
    if data[16:18] != b'\x10\x00' or data[21:24] != b'\x40\x20\x20':
        raise ReadError('database_format', '消息库页格式与当前 wxSQLite3 解码器不匹配。')
    # The first encrypted page leaves its SQLite header fragment visible.
    ciphertext = data[8:16] + data[24:]
    decoded = HEADER + aes.new(derived, aes.MODE_CBC, page_iv(1)).decrypt(ciphertext)
    if decoded[16:24] != data[16:24]:
        raise ReadError('key_invalid', '密钥未通过消息库首个数据页验证。')
    return decoded


def key_for(file, key_file):
    try:
        data = json.loads(Path(key_file).expanduser().read_text())
    except (OSError, ValueError):
        raise ReadError('key_missing', '请导入已验证的本机密钥文件，或配置本机 key_provider。') from None
    item = data.get(file.name, {})
    if not isinstance(item, dict):
        raise ReadError('key_invalid', '密钥文件条目格式不正确。')
    try:
        key = bytes.fromhex(item.get('enc_key', ''))
    except (ValueError, TypeError):
        raise ReadError('key_invalid', '密钥文件条目格式不正确。') from None
    if len(key) != 16:
        raise ReadError('key_invalid', '消息库密钥需要是已验证的 16 字节原始密钥。')
    return key


def accepts(file, config):
    try:
        with file.open('rb') as handle:
            head = handle.read(PAGE)
        if head.startswith(HEADER):
            return True
        key = key_for(file, config['key_file'])
        decode_page(head, 1, key)
        return True
    except (ReadError, OSError):
        return False


def locate(config, explicit=None):
    chosen = explicit or config.get('database')
    if chosen:
        file = Path(chosen).expanduser().resolve()
        file = file / 'Info.db' if file.is_dir() else file
        if not file.is_file():
            raise ReadError('database_missing', '指定的本机消息库不存在。')
        return file
    candidates = set()
    for root in config['profile_roots']:
        candidates.update(path.resolve() for path in Path(root).expanduser().glob('*/Messages1/Info.db'))
    verified = [path for path in candidates if accepts(path, config)]
    if len(verified) == 1:
        return verified[0]
    if len(verified) > 1:
        raise ReadError('database_ambiguous', '有多个可读取的账号消息库，请在配置或 --db 中指定目标。')
    if len(candidates) == 1:
        return next(iter(candidates))
    raise ReadError('database_not_selected', '请配置目标消息库或导入可用于匹配账号的密钥文件。')


def wal_pages(wal):
    if not wal:
        return [], None
    if len(wal) < 32:
        raise ReadError('wal_incomplete', 'WAL 文件头尚不完整，请稍后重试。')
    magic, version, size = struct.unpack('>III', wal[:12])
    if magic not in (0x377f0682, 0x377f0683) or size != PAGE:
        raise ReadError('wal_format', 'WAL 页格式与当前读取器不匹配。')
    order = '<' if magic == 0x377f0682 else '>'
    def checksum(raw, seed):
        a, b = seed
        words = struct.unpack(order + 'I' * (len(raw) // 4), raw)
        for index in range(0, len(words), 2):
            a = (a + words[index] + b) & 0xffffffff
            b = (b + words[index + 1] + a) & 0xffffffff
        return a, b
    previous = checksum(wal[:24], (0, 0))
    if previous != struct.unpack('>II', wal[24:32]):
        raise ReadError('wal_checksum', 'WAL 文件头校验失败。')
    frames, committed, final_size = [], 0, None
    for offset in range(32, len(wal) - PAGE - 23, PAGE + 24):
        frame = wal[offset:offset + PAGE + 24]
        number, size = struct.unpack('>II', frame[:8])
        if not 0 < number <= 10000000 or frame[8:16] != wal[16:24]:
            break
        current = checksum(frame[:8] + frame[24:], previous)
        if current != struct.unpack('>II', frame[16:24]):
            break
        previous = current
        frames.append((number, frame[24:]))
        if size:
            committed, final_size = len(frames), size
    return frames[:committed], final_size


def snapshot(file, destination):
    wal_file = file.with_name(file.name + '-wal')
    for _ in range(3):
        before = file.stat()
        wal_before = wal_file.read_bytes() if wal_file.exists() else b''
        body = file.read_bytes()
        after = file.stat()
        wal_after = wal_file.read_bytes() if wal_file.exists() else b''
        final = file.stat()
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) == (final.st_size, final.st_mtime_ns) and wal_before == wal_after:
            break
    else:
        raise ReadError('snapshot_busy', '消息库在复制期间持续变化，请重试本次只读快照。')
    if len(body) % PAGE:
        raise ReadError('database_truncated', '数据库长度不是完整数据页。')
    frames, size = wal_pages(wal_after)
    with destination.open('wb') as handle:
        os.chmod(destination, 0o600)
        handle.write(body)
        for number, data in frames:
            handle.seek((number - 1) * PAGE)
            handle.write(data)
        if size is not None:
            handle.truncate(size * PAGE)


@contextmanager
def connection(config, explicit=None):
    source = locate(config, explicit)
    scratch = private_dir(Path(config['data_dir']) / 'tmp')
    with tempfile.TemporaryDirectory(prefix='read-', dir=scratch) as folder:
        encrypted, plain = Path(folder) / source.name, Path(folder) / 'plain.db'
        snapshot(source, encrypted)
        with encrypted.open('rb') as handle:
            is_plain = handle.read(16) == HEADER
        if is_plain:
            shutil.copy2(encrypted, plain)
        else:
            key = key_for(source, config['key_file'])
            with encrypted.open('rb') as incoming, plain.open('wb') as outgoing:
                os.chmod(plain, 0o600)
                for number, page in enumerate(iter(lambda: incoming.read(PAGE), b''), 1):
                    outgoing.write(decode_page(page, number, key))
        db = sqlite3.connect(plain.as_uri() + '?mode=ro&immutable=1', uri=True)
        db.row_factory = sqlite3.Row
        try:
            for name in ('MESSAGE', 'CONVERSATION'):
                if [row[0] for row in db.execute(f'PRAGMA integrity_check({name})')] != ['ok']:
                    raise ReadError('database_integrity', '消息库副本未通过完整性检查。')
            yield db, source
        except sqlite3.Error:
            raise ReadError('database_schema', '消息库副本的结构与当前读取器不匹配。') from None
        finally:
            db.close()


def read_rows(db, group, since=None, until=None):
    groups = db.execute('SELECT * FROM CONVERSATION WHERE name=?', (group,)).fetchall()
    if len(groups) != 1:
        raise ReadError('group_ambiguous' if groups else 'group_missing', '会话名称需要在目标消息库中唯一匹配。')
    conversation = dict(groups[0])
    query = 'SELECT * FROM MESSAGE WHERE conv_id=? AND message_type=? AND fw_id=?'
    parameters = [conversation['RID'], conversation['conversationtype'], conversation['fw_id']]
    if since is not None:
        query += ' AND send_time>=?'; parameters.append(since)
    if until is not None:
        query += ' AND send_time<?'; parameters.append(until)
    rows = [dict(row) for row in db.execute(query + ' ORDER BY send_time,LID', parameters)]
    return conversation, rows


def initialize_key(config, imported=None, explicit=None):
    database = locate(config, explicit)
    target = outside_source(config['key_file'])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if imported:
        source = Path(imported).expanduser().resolve()
        key = key_for(database, source)
        with database.open('rb') as handle:
            decode_page(handle.read(PAGE), 1, key)
        private_json(target, {database.name: {'enc_key': key.hex()}})
        return
    provider = config.get('key_provider', {})
    argv = provider.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
        raise ReadError('key_provider_missing', '请配置已有的本机取钥工具 key_provider.argv，或通过 --import-file 导入密钥。')
    # Providers write into a private staging file; a failed refresh preserves the prior key.
    with tempfile.TemporaryDirectory(prefix='key-', dir=target.parent) as directory:
        candidate = Path(directory) / 'keys.json'
        command = [value.replace('{db}', str(database)).replace('{output}', str(candidate)) for value in argv]
        try:
            result = subprocess.run(command, cwd=provider.get('cwd'), capture_output=True, timeout=provider.get('timeout', 180))
        except (OSError, subprocess.TimeoutExpired):
            raise ReadError('key_provider_failed', '本机取钥工具未完成；现有密钥保持不变。') from None
        if result.returncode or not candidate.is_file():
            raise ReadError('key_provider_failed', '本机取钥工具未提供可验证的密钥；现有密钥保持不变。')
        key = key_for(database, candidate)
        with database.open('rb') as handle:
            decode_page(handle.read(PAGE), 1, key)
        private_json(target, {database.name: {'enc_key': key.hex()}})
