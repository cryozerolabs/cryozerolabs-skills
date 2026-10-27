"""Read-only wecom-cli adapter; no alternate transport or credential extraction."""
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import time
from zoneinfo import ZoneInfo

from common import ReadError, instant


class Official:
    def __init__(self, config):
        self.config = config
        command = config.get('wecom_cli', 'wecom-cli')
        self.command = command if isinstance(command, list) else [command]
        self.last = 0.0
        self.checked = False
        self.partial_messages = []
        self.zone = ZoneInfo(config.get('timezone', 'Asia/Shanghai'))

    def check(self):
        try:
            version = subprocess.run([*self.command, '--version'], capture_output=True, text=True, timeout=20)
            match = re.search(r'(\d+)\.(\d+)\.(\d+)', version.stdout)
            if version.returncode or not match or tuple(map(int, match.groups())) < (1, 2, 1):
                raise ReadError('cli_version', 'wecom-cli 需要为 1.2.1 或更新版本。')
            status = subprocess.run([*self.command, 'auth', 'show', '--status'], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise ReadError('cli_missing', '请准备可运行且已授权的 wecom-cli。') from None
        if status.returncode or status.stdout.strip() != 'authorized':
            raise ReadError('cli_unauthorized', 'wecom-cli 尚未完成当前用户授权。')
        self.checked = True

    def call(self, route, parameters, output=None):
        interval = max(0, float(self.config.get('cli_interval_seconds', 8)))
        remaining = interval - (time.monotonic() - self.last)
        if remaining > 0:
            time.sleep(remaining)
        command = [*self.command, *route, '--json', json.dumps(parameters, ensure_ascii=False)]
        if output:
            command.extend(['--output-dir', str(output)])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.config.get('cli_timeout', 120))
            self.last = time.monotonic()
            data = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ReadError('cli_response', '官方 CLI 响应未完成；请检查运行状态后再继续。') from None
        if not isinstance(data, dict):
            raise ReadError('cli_response', '官方 CLI 未返回可识别的对象。')
        if result.returncode or data.get('error') or data.get('errcode', 0):
            error = data.get('error') if isinstance(data.get('error'), dict) else {}
            code = data.get('errcode') or error.get('code') or 'failed'
            raise ReadError('cli_' + str(code), '官方 CLI 请求未成功；未改用其他网络接口重试。')
        return data

    def pages(self, route, parameters, field):
        items, cursors = [], set()
        for _ in range(int(self.config.get('cli_max_pages', 100))):
            response = self.call(route, parameters)
            items.extend(response.get(field, []))
            if field == 'messages':
                self.partial_messages = items[:]
            if not response.get('has_more'):
                return items
            cursor = response.get('next_cursor')
            if not cursor or cursor in cursors:
                raise ReadError('cli_cursor', '官方接口分页游标未推进；已停止读取以保留现有结果。')
            cursors.add(cursor)
            parameters = {**parameters, 'cursor': cursor}
        raise ReadError('cli_page_limit', '达到配置的分页上限；本次读取没有宣称完整。')

    def window(self, since, until):
        current = int(time.time())
        begin = max(since if since is not None else current - 7 * 86400 + 60, current - 7 * 86400 + 60)
        end = min(until if until is not None else current, current)
        if begin >= end:
            return None
        fmt = lambda value: datetime.fromtimestamp(value, self.zone).strftime('%Y-%m-%d %H:%M:%S')
        return {'begin_time': fmt(begin), 'end_time': fmt(end)}

    def groups(self, since=None, until=None):
        window = self.window(since, until)
        return self.unique_groups(self.pages(['chat', 'groups', 'list'], window, 'chats')) if window else []

    @staticmethod
    def unique_groups(items):
        groups = {}
        for item in items:
            reference = item.get('chat_id')
            if not reference:
                continue
            if reference not in groups:
                groups[reference] = {**item, 'names': [item.get('chat_name')]}
                continue
            current = groups[reference]
            if item.get('chat_name') not in current['names']:
                current['names'].append(item.get('chat_name'))
            if item.get('last_msg_time', '') > current.get('last_msg_time', ''):
                current.update(last_msg_time=item['last_msg_time'], chat_name=item.get('chat_name'))
            # Returned entries can describe separate message segments, not distinct groups.
            current.pop('msg_count', None)
        return list(groups.values())

    def read(self, group, since, until):
        window = self.window(since, until)
        if window is None:
            return [], {'status': 'outside_official_time_window', 'requested_since': since, 'requested_until': until}
        groups = self.unique_groups(self.pages(['chat', 'groups', 'list'], window, 'chats'))
        matches = [item for item in groups if group in item['names']]
        if len(matches) != 1:
            raise ReadError('cli_group_ambiguous' if matches else 'cli_group_missing', '官方接口中会话名称未唯一匹配。')
        messages = self.pages(['chat', 'messages', 'list'], {**window, 'chat_id': matches[0]['chat_id']}, 'messages')
        return messages, {'status': 'read', 'window': window, 'count': len(messages),
                          'time_window_clipped': since is not None and instant(window['begin_time'], self.zone) > since}
