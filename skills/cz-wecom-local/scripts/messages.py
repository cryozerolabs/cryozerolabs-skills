"""Loss-preserving protobuf parsing and source-aware message normalization."""
import base64
from collections import defaultdict
import hashlib
import html
import re
from urllib.parse import parse_qs, urlsplit

from common import ReadError, instant, iso, TZ


def protobuf(data):
    def integer(offset):
        number = shift = 0
        while offset < len(data) and shift < 70:
            byte = data[offset]; offset += 1
            number |= (byte & 127) << shift
            if byte < 128:
                return number, offset
            shift += 7
        raise ValueError('invalid varint')
    result, offset = [], 0
    while offset < len(data):
        tag, offset = integer(offset)
        field, wire = tag >> 3, tag & 7
        if not 0 < field < 2 ** 29:
            raise ValueError('invalid field')
        if wire == 0:
            value, offset = integer(offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = integer(offset)
            else:
                size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise ValueError('truncated field')
            value, offset = data[offset:offset + size], offset + size
        else:
            raise ValueError('unknown wire type')
        result.append((field, wire, value))
    return result


def fields(value):
    try:
        return protobuf(value)
    except (ValueError, TypeError):
        return []


def text(value):
    if not isinstance(value, bytes):
        return None
    try:
        decoded = value.decode('utf-8')
    except UnicodeDecodeError:
        return None
    return decoded if decoded and not any(ord(c) < 32 and c not in '\t\r\n' for c in decoded) else None


def first(values, tag):
    return next((value for number, wire, value in values if number == tag), None)


def encoded(value):
    return {'encoding': 'base64', 'data': base64.b64encode(value).decode('ascii'), 'length': len(value)} if isinstance(value, bytes) else value


def wire_tree(data, depth=0):
    if not isinstance(data, bytes):
        return data
    if depth >= 10:
        return encoded(data)
    try:
        values = protobuf(data)
    except ValueError:
        literal = text(data)
        return {'encoding': 'utf-8', 'text': literal} if literal is not None else encoded(data)
    return {'fields': [{'field': tag, 'wire_type': wire,
                        'value': (text(value) if text(value) is not None else wire_tree(value, depth + 1))
                        if isinstance(value, bytes) else value} for tag, wire, value in values]}


def identity(prefix, *values):
    return prefix + ':' + hashlib.sha256('\0'.join(map(str, values)).encode()).hexdigest()[:24]


def raw_ref(path, line):
    return {'path': str(path), 'jsonl_line': line}


def links_in(value):
    links = []
    for found in re.findall(r'https?://[^\s<>"\x00-\x20]+', html.unescape(value)):
        found = found.rstrip('.,;，。；')
        while found.endswith(')') and found.count(')') > found.count('('):
            found = found[:-1]
        if valid_link(found):
            links.append({'url': found, 'source': 'text'})
    return links


def valid_link(value):
    try:
        return bool(value and urlsplit(value).scheme in ('http', 'https') and urlsplit(value).hostname)
    except (ValueError, TypeError):
        return False


def local_message(row, group, account, reference, wire_reference):
    payload = fields(row.get('content') or b'')
    kind = row['content_type']
    record = {'message_ref': identity('local', account, row['conv_id'], row['fw_id'], row['LID']),
              'conversation': group, 'sent_at': iso(row['send_time']),
              'sender': {'local_ref': str(row['sender_id'])},
              'kind': 'unknown', 'local_content_type': kind,
              'parts': [], 'links': [], 'attachments': [], 'parse_status': 'raw_only',
              'sources': ['local'], 'raw_refs': [reference], 'wire_fields_ref': wire_reference}
    if kind in (0, 2):
        record['kind'] = 'text'
        for index, (tag, wire, segment) in enumerate(payload):
            if tag != 1 or not isinstance(segment, bytes):
                continue
            section = fields(segment)
            content = first(section, 2)
            values = fields(content) if isinstance(content, bytes) else []
            fragments = [text(value) for number, _, value in values if number == 1 and text(value) is not None]
            if not values and text(content) is not None:
                fragments = [text(content)]
            if fragments:
                record['parts'].extend({'type': 'text', 'text': fragment, 'segment': index,
                                        'format_kind': first(section, 1)} for fragment in fragments)
            else:
                record['parts'].append({'type': 'unknown', 'segment': index, 'raw_ref': reference})
        record['text'] = ''.join(part.get('text', '') for part in record['parts'])
        record['links'] = links_in(record['text'])
        if record['text']:
            record['parse_status'] = 'partial' if any(part['type'] == 'unknown' for part in record['parts']) else 'parsed'
    elif kind == 13:
        record['kind'] = 'link_card'
        link = text(first(payload, 1))
        if valid_link(link):
            record['links'] = [{'url': link, 'title': text(first(payload, 3)),
                                'author': text(first(payload, 4)), 'source': 'share_card'}]
            record['parse_status'] = 'partial'
        record['title'] = text(first(payload, 3))
    elif kind == 78:
        record['kind'] = 'miniapp'
        app = fields(first(payload, 107) or b'')
        page = text(first(app, 3)) or ''
        record['miniapp'] = {'title': text(first(app, 7)), 'app_name': text(first(app, 10)), 'page_path': page}
        record['title'] = record['miniapp']['title']
        try:
            values = parse_qs(urlsplit(page).query).get('shareUrl', [])
        except ValueError:
            values = []
        for link in values:
            if valid_link(link):
                record['links'].append({'url': link, 'source': 'miniapp_page_parameter'})
        record['parse_status'] = 'partial'
    elif kind in (14, 15, 22, 23):
        record['kind'] = {14: 'image', 15: 'file', 22: 'video', 23: 'video'}[kind]
        name_tag, size_tag = (2, 4) if kind in (14, 15) else ((107, 3) if kind == 22 else (1, 3))
        name = text(first(payload, name_tag))
        # Sender-device paths are labels only, never trusted local file locations.
        if name and ('/' in name or '\\' in name):
            name = name.replace('\\', '/').rsplit('/', 1)[-1]
        attachment = {'name': name, 'kind': record['kind'], 'size_bytes': first(payload, size_tag),
                      'availability': 'metadata_only', 'content_status': 'not_read', 'raw_descriptor_ref': reference}
        checksum = text(first(payload, 10)) if kind == 15 else None
        if checksum and re.fullmatch('[a-fA-F0-9]{32}', checksum):
            attachment['expected_md5'] = checksum.lower()
        attachment['attachment_ref'] = identity('attachment', record['message_ref'], 0)
        record['attachments'] = [attachment]
        record['parse_status'] = 'partial'
    elif kind == 154:
        record['kind'] = 'message_bundle'
        record['title'] = text(first(payload, 2))
    return record


def official_message(message, group, reference, zone=TZ):
    kind = message.get('msg_type', 'unknown')
    try:
        stamp = instant(message.get('send_time'), zone)
    except ReadError:
        stamp = None
    record = {'message_ref': identity('official', group, message.get('userid'), message.get('send_time'), str(reference)),
              'conversation': group, 'sent_at': iso(stamp) if stamp is not None else None,
              'sender': {'official_ref': message.get('userid'), 'name': message.get('user_name')},
              'kind': kind, 'parts': [], 'links': [], 'attachments': [], 'sources': ['official'],
              'parse_status': 'raw_only', 'raw_refs': [reference]}
    if kind == 'text':
        value = message.get('text', {}).get('content', '')
        record.update(text=value, parts=[{'type': 'text', 'text': value}], links=links_in(value), parse_status='partial')
    media = message.get(kind, {})
    if kind in ('file', 'image', 'voice', 'video') and isinstance(media, dict):
        record['attachments'] = [{'attachment_ref': identity('attachment', record['message_ref'], 0),
            'name': media.get('file_name'), 'kind': kind, 'availability': 'download_reference_available',
            'content_status': 'not_read', 'retrieval': {'provider': 'wecom_cli',
                'media_id': media.get('media_id')}, 'raw_descriptor_ref': reference}]
    if kind == 'mixed':
        for part in message.get('mixed', {}).get('items', []):
            if part.get('msg_type') == 'text':
                value = part.get('text', {}).get('content', '')
                record['parts'].append({'type': 'text', 'text': value})
                record['links'].extend(links_in(value))
            elif part.get('msg_type') == 'image':
                record['attachments'].append({'attachment_ref': identity('attachment', record['message_ref'], len(record['attachments'])),
                    'name': None, 'kind': 'image', 'availability': 'download_reference_available',
                    'content_status': 'not_read', 'retrieval': {'provider': 'wecom_cli', 'media_id': part.get('image', {}).get('media_id')}})
        record['parse_status'] = 'partial'
    return record


def merge(local, official):
    """Only one-to-one, evidence-based joins; retain unresolved source variants."""
    by_time = defaultdict(list)
    for index, item in enumerate(official):
        by_time[item['sent_at']].append(index)
    sender_candidates = defaultdict(set)
    for item in local:
        if not item.get('text'):
            continue
        candidates = [official[index] for index in by_time[item['sent_at']]
                      if official[index].get('text') == item['text']]
        if len(candidates) == 1 and candidates[0]['sender'].get('official_ref'):
            sender_candidates[item['sender']['local_ref']].add(candidates[0]['sender']['official_ref'])
    sender_map = {key: next(iter(values)) for key, values in sender_candidates.items() if len(values) == 1}
    proposals = {}
    for index, item in enumerate(local):
        candidates = []
        sender = sender_map.get(item['sender']['local_ref'])
        for remote_index in by_time[item['sent_at']]:
            other = official[remote_index]
            if sender and sender != other['sender'].get('official_ref'):
                continue
            same = bool(item.get('text')) and item.get('text') == other.get('text')
            if item['kind'] in ('link_card', 'miniapp'):
                title = item.get('title') or ''
                same = bool(title and title in other.get('text', ''))
            if item['kind'] == 'file' and other['kind'] == 'file':
                same = bool(item['attachments'][0].get('name') and item['attachments'][0]['name'] == other['attachments'][0].get('name'))
            if item['kind'] in ('image', 'voice', 'video') and item['kind'] == other['kind']:
                # Media-only joins require an independently established sender mapping.
                same = bool(sender)
            if same:
                candidates.append(remote_index)
        if len(candidates) == 1:
            proposals[index] = candidates[0]
        elif candidates:
            item['merge_status'] = 'ambiguous'
    reverse = defaultdict(list)
    for index, remote_index in proposals.items():
        reverse[remote_index].append(index)
    used = set()
    for index, remote_index in proposals.items():
        item, other = local[index], official[remote_index]
        if len(reverse[remote_index]) != 1:
            item['merge_status'] = 'ambiguous'; continue
        used.add(remote_index)
        item['sources'].append('official')
        item['raw_refs'].extend(other['raw_refs'])
        item['sender'].update(other['sender'])
        item['merge_status'] = 'matched'
        # Keep the source's rendering as a variant instead of overwriting local content.
        item['official_variant'] = {'kind': other['kind'], 'text': other.get('text')}
        for attachment, remote in zip(item['attachments'], other['attachments']):
            if remote.get('retrieval'):
                attachment['retrieval'] = remote['retrieval']
                attachment['availability'] = 'download_reference_available'
    return sorted([*local, *(item for index, item in enumerate(official) if index not in used)], key=lambda item: (item['sent_at'] or '', item['message_ref']))
