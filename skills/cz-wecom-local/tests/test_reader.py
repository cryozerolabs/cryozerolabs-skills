import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from common import ReadError, outside_source
from database import PAGE, connection, decode_page, page_iv, wal_pages, initialize_key
from messages import local_message, official_message, merge, protobuf, wire_tree
from attachments import enrich_cache, inspect, materialize
from official import Official


def integer(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128); value >>= 7
    result.append(value)
    return bytes(result)


def field(tag, value):
    if isinstance(value, str): value = value.encode()
    if isinstance(value, bytes): return integer(tag << 3 | 2) + integer(len(value)) + value
    return integer(tag << 3) + integer(value)


def body(*parts):
    return b''.join(field(1, field(1, 0) + field(2, field(1, part))) for part in parts)


def message(kind=0, content=None, lid=1, sender=9, stamp=100):
    return {'LID': lid, 'RID': lid, 'sender_id': sender, 'send_time': stamp, 'content_type': kind,
            'content': body('hello') if content is None else content, 'conv_id': 12, 'fw_id': 0,
            'message_type': 2, 'extras': b'', 'extracontent': b''}


def local(row):
    return local_message(row, 'Example Group', 'account', {'path': '/private/raw.jsonl', 'jsonl_line': row['LID']}, {})


def remote(kind='text', text='hello', stamp=100, sender='member', name='sample.pdf'):
    from datetime import datetime
    from common import TZ
    data = {'msg_type': kind, 'send_time': datetime.fromtimestamp(stamp, TZ).strftime('%Y-%m-%d %H:%M:%S'),
            'userid': sender, 'user_name': 'Example Sender'}
    data[kind] = {'content': text} if kind == 'text' else {'file_name': name, 'media_id': 'fixture-media'}
    return official_message(data, 'Example Group', {'path': '/private/official.json', 'json_pointer': '/messages/0'})


class MessageTests(unittest.TestCase):
    def test_repeated_text_fields_are_preserved(self):
        row = message(content=body('first', 'second', '🙂'))
        result = local(row)
        self.assertEqual(result['text'], 'firstsecond🙂')
        self.assertEqual(len(result['parts']), 3)
        self.assertEqual(len(protobuf(row['content'])), 3)
        self.assertEqual(len(wire_tree(row['content'])['fields']), 3)

    def test_unknown_and_malformed_records_keep_raw_reference(self):
        result = local(message(kind=999, content=b'\xff\x00'))
        self.assertEqual(result['parse_status'], 'raw_only')
        self.assertTrue(result['raw_refs'])
        card = local(message(kind=13, content=field(1, 'http://[')))
        self.assertEqual(card['links'], [])
        self.assertTrue(card['raw_refs'])

    def test_generic_article_and_miniapp_links(self):
        card = local(message(kind=13, content=field(1, 'https://example.org/article?id=3') + field(3, 'Article')))
        self.assertEqual(card['links'][0]['url'], 'https://example.org/article?id=3')
        mini = local(message(kind=78, content=field(107, field(7, 'Example') + field(3, 'pages/share?shareUrl=https%3A%2F%2Fexample.org%2Fdoc'))))
        self.assertEqual(mini['links'][0]['url'], 'https://example.org/doc')

    def test_text_match_enriches_sender_and_keeps_both_raw_sources(self):
        records = merge([local(message())], [remote()])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['sender']['name'], 'Example Sender')
        self.assertEqual(len(records[0]['raw_refs']), 2)

    def test_duplicate_attachment_candidates_do_not_guess(self):
        payload = field(2, 'sample.pdf') + field(4, 15)
        records = merge([local(message(15, payload, lid=1)), local(message(15, payload, lid=2))], [remote('file')])
        self.assertEqual(len(records), 3)
        self.assertFalse(any(item['attachments'] and item['attachments'][0].get('retrieval') for item in records if 'local' in item['sources']))

    def test_sender_conflict_prevents_attachment_join(self):
        anchor = local(message(stamp=99))
        file = local(message(15, field(2, 'sample.pdf'), stamp=100))
        result = merge([anchor, file], [remote(stamp=99, sender='one'), remote('file', sender='two')])
        self.assertEqual(len(result), 3)
        self.assertNotIn('retrieval', file['attachments'][0])

    def test_image_join_requires_sender_evidence(self):
        result = merge([local(message(14, field(2, 'image.png')))], [remote('image')])
        self.assertEqual(len(result), 2)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.source = self.home / 'Info.db'
        db = sqlite3.connect(self.source)
        db.execute('PRAGMA page_size=4096')
        db.execute('CREATE TABLE CONVERSATION (RID INTEGER, name TEXT, conversationtype INTEGER, fw_id INTEGER)')
        db.execute('CREATE TABLE MESSAGE (LID INTEGER, RID INTEGER, sender_id INTEGER, send_time INTEGER, content_type INTEGER, content BLOB, conv_id INTEGER, fw_id INTEGER, message_type INTEGER, extras BLOB, extracontent BLOB)')
        db.execute('INSERT INTO CONVERSATION VALUES (12,?,2,0)', ('Example Group',))
        row = message()
        columns = ','.join(row)
        db.execute('INSERT INTO MESSAGE (' + columns + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
        db.commit(); db.close()
        self.plain = self.source.read_bytes()
        self.config = {'database': str(self.source), 'data_dir': str(self.home / 'runtime'),
                       'key_file': str(self.home / 'keys.json')}

    def tearDown(self): self.temp.cleanup()

    def encrypt(self):
        from Crypto.Cipher import AES
        key = bytes(range(16))
        pages = []
        for index in range(0, len(self.plain), PAGE):
            number, data = index // PAGE + 1, self.plain[index:index + PAGE]
            derived = hashlib.md5(key + struct.pack('<I', number) + b'sAlT').digest()
            cipher = AES.new(derived, AES.MODE_CBC, page_iv(number)).encrypt(data[16:] if number == 1 else data)
            pages.append(b'FIXTURE!' + cipher[:8] + data[16:24] + cipher[8:] if number == 1 else cipher)
        self.source.write_bytes(b''.join(pages))
        Path(self.config['key_file']).write_text(json.dumps({'Info.db': {'enc_key': key.hex()}}))
        return key

    def test_encrypted_database_roundtrip_is_read_only_and_temp_removed(self):
        self.encrypt()
        before = self.source.read_bytes()
        with connection(self.config) as (db, path):
            self.assertEqual(db.execute('SELECT name FROM CONVERSATION').fetchone()[0], 'Example Group')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM MESSAGE').fetchone()[0], 1)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list((self.home / 'runtime/tmp').iterdir()), [])

    def test_wrong_key_fails_without_plaintext_export(self):
        self.encrypt()
        Path(self.config['key_file']).write_text(json.dumps({'Info.db': {'enc_key': (b'X' * 16).hex()}}))
        with self.assertRaises(ReadError):
            with connection(self.config): pass
        self.assertEqual(list((self.home / 'runtime/tmp').iterdir()), [])

    def test_failed_key_provider_preserves_working_key(self):
        self.encrypt()
        path = Path(self.config['key_file'])
        before = path.read_bytes()
        self.config['key_provider'] = {'argv': [sys.executable, '-c', 'raise SystemExit(1)']}
        with self.assertRaises(ReadError): initialize_key(self.config)
        self.assertEqual(path.read_bytes(), before)

    def test_plaintext_database_requires_no_key(self):
        with connection(self.config) as (db, path):
            self.assertEqual(db.execute('SELECT COUNT(*) FROM MESSAGE').fetchone()[0], 1)

    def test_end_to_end_bundle_retains_every_row_and_blocks_source_outputs(self):
        self.encrypt()
        config = self.home / 'config.json'
        config.write_text(json.dumps(self.config))
        output = self.home / 'output'
        cli = ROOT / 'scripts/wecom_local.py'
        result = subprocess.run([sys.executable, str(cli), 'read', '--config', str(config), '--source', 'local', '--group', 'Example Group', '--output', str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = json.loads((output / 'messages.json').read_text())
        self.assertEqual(bundle['messages'][0]['text'], 'hello')
        raw = json.loads((output / 'raw/local.jsonl').read_text())
        self.assertEqual(base64.b64decode(raw['content']['data']), body('hello'))
        self.assertEqual((output / 'messages.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ReadError): outside_source(ROOT / 'runtime')
        with self.assertRaises(ReadError): outside_source(ROOT.parents[1] / 'private-export')


class WalTests(unittest.TestCase):
    def test_only_committed_frames_are_returned(self):
        def checksum(data, seed=(0, 0)):
            a, b = seed
            words = struct.unpack('<' + 'I' * (len(data) // 4), data)
            for index in range(0, len(words), 2):
                a = (a + words[index] + b) & 0xffffffff
                b = (b + words[index + 1] + a) & 0xffffffff
            return a, b
        salts = b'12345678'
        header = struct.pack('>4I', 0x377f0682, 3007000, PAGE, 0) + salts
        prior = checksum(header)
        wal = header + struct.pack('>2I', *prior)
        for number, commit in [(1, 2), (2, 0)]:
            head = struct.pack('>2I', number, commit) + salts
            page = bytes([number]) * PAGE
            prior = checksum(head[:8] + page, prior)
            wal += head + struct.pack('>2I', *prior) + page
        frames, size = wal_pages(wal)
        self.assertEqual(len(frames), 1)
        self.assertEqual(size, 2)
        with self.assertRaises(ReadError): wal_pages(bytes([0]) + wal[1:])


class AttachmentTests(unittest.TestCase):
    def test_cache_requires_actual_checksum_and_does_not_claim_read(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            file = root / 'opaque-cache-name'
            file.write_bytes(b'original document')
            item = {'name': 'document.txt', 'size_bytes': file.stat().st_size,
                    'expected_md5': hashlib.md5(file.read_bytes()).hexdigest(), 'content_status': 'not_read', 'availability': 'metadata_only'}
            enrich_cache([{'attachments': [item]}], [root])
            self.assertEqual(item['availability'], 'file_ready')
            self.assertEqual(item['content_status'], 'not_read')
            bad = {**item, 'expected_md5': '0' * 32}
            with self.assertRaises(ReadError): inspect(file, bad)

    def test_official_download_is_verified_and_protected_error_not_bypassed(self):
        class API:
            checked = True
            def call(self, route, parameters, output=None):
                file = output / 'response'
                file.write_bytes(b'%PDF-fixture')
                return {'media_item': {'file_path': str(file), 'file_name': 'sample.pdf'}}
        item = {'attachment_ref': 'attachment:sample', 'name': 'sample.pdf',
                'size_bytes': 12, 'expected_md5': hashlib.md5(b'%PDF-fixture').hexdigest(),
                'retrieval': {'provider': 'wecom_cli', 'media_id': 'fixture'}}
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            materialize(item, root, API(), [])
            self.assertEqual(item['availability'], 'file_ready')
            self.assertEqual(item['content_status'], 'not_read')
            self.assertTrue(Path(item['file']['path']).exists())
            class Denied(API):
                def call(self, *args, **kwargs): raise ReadError('cli_denied', 'Protected')
            with self.assertRaises(ReadError): materialize({**item, 'file': {}}, root, Denied(), [])

    def test_download_path_cannot_read_outside_staging(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            unrelated = root / 'unrelated.txt'; unrelated.write_text('private')
            class API:
                checked = True
                def call(self, *args, **kwargs): return {'media_item': {'file_path': str(unrelated)}}
            item = {'attachment_ref': 'attachment:test', 'retrieval': {'provider': 'wecom_cli', 'media_id': 'fixture'}}
            with self.assertRaises(ReadError): materialize(item, root / 'downloads', API(), [])


class OfficialTests(unittest.TestCase):
    def test_segmented_group_entries_share_one_conversation(self):
        rows = [{'chat_id': 'same', 'chat_name': 'Example', 'last_msg_time': '2026-01-01 12:00:00', 'msg_count': 8},
                {'chat_id': 'same', 'chat_name': 'Example', 'last_msg_time': '2026-01-01 11:00:00', 'msg_count': 4}]
        groups = Official.unique_groups(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['last_msg_time'], '2026-01-01 12:00:00')

    def test_same_name_distinct_groups_remain_ambiguous(self):
        rows = [{'chat_id': 'one', 'chat_name': 'Example'}, {'chat_id': 'two', 'chat_name': 'Example'}]
        self.assertEqual(len(Official.unique_groups(rows)), 2)

    def test_missing_official_timestamp_keeps_raw_record(self):
        item = official_message({'msg_type': 'text', 'text': {'content': 'unknown time'}}, 'Example', {'path': 'raw.json'})
        self.assertIsNone(item['sent_at'])
        self.assertTrue(item['raw_refs'])

    def test_partial_pages_survive_rate_limit(self):
        api = Official({'wecom_cli': 'unused', 'cli_interval_seconds': 0})
        replies = [{'messages': [{'msg_type': 'text'}], 'has_more': True, 'next_cursor': 'next'}, ReadError('cli_850005', 'rate limit')]
        with patch.object(api, 'call', side_effect=replies):
            with self.assertRaises(ReadError): api.pages(['chat', 'messages', 'list'], {}, 'messages')
        self.assertEqual(len(api.partial_messages), 1)

    def test_time_window_reports_only_available_official_range(self):
        api = Official({})
        with patch('official.time.time', return_value=2000000):
            self.assertIsNone(api.window(1, 100))
            self.assertIsNotNone(api.window(None, None))

    def test_installation_is_independent_and_backup_outside_discovery(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            env = dict(os.environ, CODEX_HOME=str(root))
            installer = ROOT / 'scripts/install_local.py'
            subprocess.run([sys.executable, str(installer)], env=env, check=True, capture_output=True)
            installed = root / 'skills/cz-wecom-local'
            original = (ROOT / 'SKILL.md').read_bytes()
            (installed / 'SKILL.md').write_text('local customization')
            self.assertEqual((ROOT / 'SKILL.md').read_bytes(), original)
            subprocess.run([sys.executable, str(installer), '--replace'], env=env, check=True, capture_output=True)
            self.assertEqual(len(list((root / 'skills').glob('*/SKILL.md'))), 1)
            self.assertEqual(len(list((root / 'skill-backups/cz-wecom-local').glob('*/SKILL.md'))), 1)
            self.assertFalse(installed.is_symlink())
            config = root / 'config.json'
            config.write_text(json.dumps({'data_dir': str(root / 'runtime')}))
            env['CZ_WECOM_LOCAL_CONFIG'] = str(config)
            forbidden = ROOT.parents[1] / 'private-leak'
            attempt = subprocess.run([sys.executable, str(installed / 'scripts/wecom_local.py'), 'read',
                                      '--source', 'local', '--group', 'Example', '--output', str(forbidden)],
                                     env=env, capture_output=True, text=True)
            self.assertEqual(attempt.returncode, 2)
            self.assertIn('source_isolation', attempt.stderr)
            self.assertFalse(forbidden.exists())


if __name__ == '__main__': unittest.main()
