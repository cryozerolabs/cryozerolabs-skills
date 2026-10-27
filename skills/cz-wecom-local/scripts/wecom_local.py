#!/usr/bin/env python3
"""Independent local WeCom reader: messages, links, attachments and raw data."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

from common import (VERSION, ReadError, config_file, instant, iso, load_config,
                    private_dir, private_json, private_jsonl, outside_source)
from database import connection, initialize_key, locate, read_rows, accepts
from messages import encoded, identity, local_message, official_message, merge, raw_ref, wire_tree
from official import Official
from attachments import cache_roots, enrich_cache, materialize


def parser():
    cli = argparse.ArgumentParser(description='企微消息只读解密、官方信息补全与通用结构交付')
    cli.add_argument('--version', action='version', version=VERSION)
    commands = cli.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'groups', 'read', 'keys', 'fetch'):
        command = commands.add_parser(name)
        command.add_argument('--config', type=Path)
        command.add_argument('--db', type=Path)
        if name in ('groups', 'read'):
            command.add_argument('--source', choices=('local', 'official', 'both'), default='both')
            command.add_argument('--since')
            command.add_argument('--until')
        if name == 'read':
            command.add_argument('--group', required=True)
            command.add_argument('--output', required=True, type=Path)
        if name == 'groups':
            command.add_argument('--name', help='按名称包含关系过滤本次会话列表')
        if name == 'keys':
            command.add_argument('--import-file', type=Path)
        if name == 'fetch':
            command.add_argument('--bundle', required=True, type=Path)
            selection = command.add_mutually_exclusive_group(required=True)
            selection.add_argument('--attachment-ref')
            selection.add_argument('--all', action='store_true')
            command.add_argument('--output', type=Path)
    return cli


def load_official(config):
    api = Official(config)
    api.check()
    return api


def summary(bundle):
    messages = bundle['messages']
    attachments = [item for message in messages for item in message['attachments']]
    return {'message_records': len(messages), 'parse_status': dict(Counter(item['parse_status'] for item in messages)),
            'links': sum(len(item['links']) for item in messages), 'attachments': len(attachments),
            'attachment_availability': dict(Counter(item['availability'] for item in attachments))}


def doctor(args, config):
    report = {'version': VERSION, 'config_present': config_file(args.config).is_file(), 'local': {}, 'official': {}}
    try:
        file = locate(config, args.db)
        report['local'] = {'database_found': True, 'key_verified': accepts(file, config),
                           'crypto_available': importlib.util.find_spec('Crypto') is not None}
    except ReadError as error:
        report['local'] = {'database_found': False, 'error': error.data()}
    try:
        load_official(config)
        report['official'] = {'authorized': True}
    except ReadError as error:
        report['official'] = {'authorized': False, 'error': error.data()}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['local'].get('key_verified') or report['official'].get('authorized') else 2


def groups(args, config):
    rows, coverage = [], {}
    if args.source in ('local', 'both'):
        try:
            with connection(config, args.db) as (db, source):
                for row in db.execute('SELECT name,conversationtype,COUNT(*) AS matches FROM CONVERSATION GROUP BY name,conversationtype ORDER BY name'):
                    if not args.name or args.name in (row['name'] or ''):
                        rows.append({'name': row['name'], 'source': 'local', 'matches': row['matches'], 'conversation_type': row['conversationtype']})
            coverage['local'] = {'status': 'read'}
        except ReadError as error:
            coverage['local'] = {'status': 'unavailable', 'error': error.data()}
    if args.source in ('official', 'both'):
        try:
            api = load_official(config)
            items = api.groups(instant(args.since), instant(args.until))
            rows.extend({'name': item.get('chat_name'), 'source': 'official', 'message_count': item.get('msg_count')}
                        for item in items if not args.name or args.name in (item.get('chat_name') or ''))
            coverage['official'] = {'status': 'read'}
        except ReadError as error:
            coverage['official'] = {'status': 'unavailable', 'error': error.data()}
    print(json.dumps({'conversations': rows, 'coverage': coverage}, ensure_ascii=False))
    return 0 if any(item['status'] == 'read' for item in coverage.values()) else 2


def read(args, config):
    begin, end = instant(args.since), instant(args.until)
    if begin is not None and end is not None and begin >= end:
        raise ReadError('time_range', '--until 需要晚于 --since；结束时间不包含在查询范围内。')
    output = private_dir(args.output)
    if (output / 'messages.json').exists():
        raise ReadError('output_exists', '该目录已有消息交付；请为新快照选择新的输出目录。')
    raw = private_dir(output / 'raw')
    local, official, coverage, roots = [], [], {}, []
    if args.source in ('local', 'both'):
        try:
            with connection(config, args.db) as (db, database):
                conversation, rows = read_rows(db, args.group, begin, end)
                reference, wire = raw / 'local.jsonl', raw / 'wire.jsonl'
                private_jsonl(reference, ({key: encoded(value) for key, value in row.items()} for row in rows))
                private_jsonl(wire, ({key: wire_tree(row.get(key)) for key in ('content', 'extras', 'extracontent')} for row in rows))
                account = identity('database', str(database))
                local = [local_message(row, args.group, account, raw_ref(reference, index), raw_ref(wire, index))
                         for index, row in enumerate(rows, 1)]
                roots = cache_roots(config, database)
                coverage['local'] = {'status': 'read', 'count': len(rows), 'scope': 'local_cached_records',
                    'first_message_at': iso(rows[0]['send_time']) if rows else None,
                    'last_message_at': iso(rows[-1]['send_time']) if rows else None,
                    'database_integrity_checked': True}
        except ReadError as error:
            coverage['local'] = {'status': 'unavailable', 'error': error.data()}
    if args.source in ('official', 'both'):
        api = None
        try:
            api = load_official(config)
            items, scope = api.read(args.group, begin, end)
            reference = raw / 'official.json'
            private_json(reference, {'messages': items})
            official = [official_message(item, args.group, {'path': str(reference), 'json_pointer': f'/messages/{index}'}, zone=api.zone)
                        for index, item in enumerate(items)]
            coverage['official'] = scope
        except ReadError as error:
            coverage['official'] = {'status': 'unavailable', 'error': error.data()}
            if api and api.partial_messages:
                reference = raw / 'official.json'
                private_json(reference, {'messages': api.partial_messages})
                official = [official_message(item, args.group, {'path': str(reference), 'json_pointer': f'/messages/{index}'}, zone=api.zone)
                            for index, item in enumerate(api.partial_messages)]
                coverage['official'].update(status='partial', count=len(official))
    records = merge(local, official) if local and official else local or official
    enrich_cache(records, roots or cache_roots(config))
    bundle = {'schema_version': '1.0', 'skill_version': VERSION, 'conversation': args.group,
              'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'query': {'since': iso(begin) if begin is not None else None, 'until': iso(end) if end is not None else None},
              'coverage': coverage, 'messages': records}
    bundle['summary'] = summary(bundle)
    file = output / 'messages.json'
    private_json(file, bundle)
    private_json(output / 'links.json', {'schema_version': '1.0', 'items': [
        {**link, 'message_ref': message['message_ref'], 'conversation': message['conversation'], 'sent_at': message['sent_at']}
        for message in records for link in message['links']]})
    private_json(output / 'attachments.json', {'schema_version': '1.0', 'items': [
        {**item, 'message_ref': message['message_ref']} for message in records for item in message['attachments']]})
    print(json.dumps({**bundle['summary'], 'bundle': str(file), 'coverage': coverage}, ensure_ascii=False))
    success = any(item['status'] in ('read', 'partial') for item in coverage.values())
    incomplete = any(item['status'] in ('unavailable', 'partial') for item in coverage.values())
    return 1 if success and incomplete else (0 if success else 2)


def fetch(args, config):
    path = outside_source(args.bundle)
    bundle = json.loads(path.read_text())
    if bundle.get('schema_version') != '1.0':
        raise ReadError('bundle_schema', '请使用该技能生成的 1.0 版本消息交付文件。')
    destination = private_dir(args.output or path.parent / 'attachments')
    items = [item for message in bundle['messages'] for item in message['attachments']
             if args.all or item['attachment_ref'] == args.attachment_ref]
    if not items:
        raise ReadError('attachment_missing', '消息交付中没有匹配的附件。')
    try:
        database = locate(config, args.db)
    except ReadError:
        database = None
    roots = cache_roots(config, database) + [(path.parent / 'attachments').resolve()]
    api, failed, blocked_cli = Official(config), 0, None
    for item in items:
        try:
            if blocked_cli and item.get('availability') != 'file_ready':
                raise blocked_cli
            materialize(item, destination, api, roots)
            item.pop('error', None)
        except ReadError as error:
            item['error'] = error.data()
            failed += 1
            if error.code in ('cli_850005', 'cli_853006', 'cli_unauthorized', 'cli_missing'):
                blocked_cli = error
        bundle['summary'] = summary(bundle)
        private_json(path, bundle)
        private_json(path.parent / 'attachments.json', {'schema_version': '1.0', 'items': [
            {**attachment, 'message_ref': message['message_ref']} for message in bundle['messages'] for attachment in message['attachments']]})
    print(json.dumps({'requested': len(items), 'completed': len(items) - failed, 'failed': failed, 'bundle': str(path)}, ensure_ascii=False))
    return 1 if failed else 0


def main():
    os.umask(0o077)
    try:
        args = parser().parse_args()
        config = load_config(args.config)
        if args.command == 'doctor': return doctor(args, config)
        if args.command == 'groups': return groups(args, config)
        if args.command == 'read': return read(args, config)
        if args.command == 'fetch': return fetch(args, config)
        initialize_key(config, args.import_file, args.db)
        print(json.dumps({'key_verified': True}, ensure_ascii=False))
        return 0
    except ReadError as error:
        print(json.dumps({'error': error.data()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'error': {'code': 'local_data', 'message': '请检查本机配置、输入数据和文件路径。', 'type': type(error).__name__}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
