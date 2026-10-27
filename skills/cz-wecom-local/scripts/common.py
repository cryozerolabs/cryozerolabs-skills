"""Private runtime configuration and portable message-bundle utilities."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

VERSION = '0.1.0'
PACKAGE = Path(__file__).resolve().parents[1]
SOURCE = next((p for p in (PACKAGE, *PACKAGE.parents) if (p / '.git').exists()), PACKAGE)
TZ = ZoneInfo('Asia/Shanghai')


class ReadError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message

    def data(self):
        return {'code': self.code, 'message': self.message}


def default_data():
    if os.sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/cz-wecom-local'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'cz-wecom-local'


def outside_source(path):
    path = Path(path).expanduser().resolve()
    if any(path == root or root in path.parents for root in (SOURCE, PACKAGE)):
        raise ReadError('source_isolation', '请把运行数据和输出放在发布仓库及技能安装目录之外。')
    # Installed copies do not remember the checkout path. Detect a collection
    # checkout at the destination as well, without coupling the two copies.
    for parent in (path, *path.parents):
        if (parent / '.git').exists() and (parent / 'skills/cz-wecom-local/SKILL.md').is_file():
            raise ReadError('source_isolation', '请把运行数据和输出放在发布仓库及技能安装目录之外。')
    return path


def private_dir(path):
    path = outside_source(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Do not change permissions on an existing user-selected parent directory.
    return path


def private_json(path, value):
    path = outside_source(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    temporary.replace(path)


def private_jsonl(path, values):
    path = outside_source(path)
    with path.open('w', encoding='utf-8') as handle:
        os.chmod(path, 0o600)
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False) + '\n')


def config_file(explicit=None):
    return Path(explicit or os.environ.get('CZ_WECOM_LOCAL_CONFIG') or default_data() / 'config.json').expanduser()


def load_config(explicit=None):
    file = config_file(explicit)
    if not file.exists():
        if explicit:
            raise ReadError('config_missing', '指定的本机配置不存在。')
        data = {}
    else:
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            raise ReadError('config_invalid', '本机配置应为 JSON 对象。') from None
        if not isinstance(data, dict):
            raise ReadError('config_invalid', '本机配置应为 JSON 对象。')
    runtime = outside_source(data.get('data_dir', default_data()))
    data.setdefault('data_dir', str(runtime))
    data.setdefault('key_file', str(runtime / 'state/keys.json'))
    data.setdefault('profile_roots', [str(Path.home() / 'Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles')])
    data.setdefault('wecom_cli', 'wecom-cli')
    data.setdefault('timezone', 'Asia/Shanghai')
    data.setdefault('cli_interval_seconds', 8)
    return data


def instant(value, zone=TZ):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ReadError('time_invalid', '时间请使用 YYYY-MM-DD HH:MM:SS 或带时区的 ISO 格式。') from None
    return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)).timestamp())


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec='seconds')


def digest_file(path, algorithm='sha256'):
    value = hashlib.new(algorithm)
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def safe_name(name):
    return re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', str(name or 'attachment')).strip(' .')[:160] or 'attachment'
