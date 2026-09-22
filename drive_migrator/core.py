"""One-writer, non-destructive Drive migration using explicitly granted local OAuth.

No model calls. No source writes, deletion, replacement, moves, or renames.
SQLite WAL intent/result journal; complete listings; revision-guarded Docs lease.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .oauth import atomic_private, json_request
from .settings import CONFIG

BASE = pathlib.Path(CONFIG['state_dir']).expanduser()
SRC_EMAIL = CONFIG['source_email']
DST_EMAIL = CONFIG['destination_email']
SRC_ROOT = CONFIG['source_root_id']
DST_ROOT = CONFIG['destination_root_id']
DOC = CONFIG['controller_id']
SHARE_SOURCE = CONFIG['share_source']
FOLDER = 'application/vnd.google-apps.folder'
NATIVE = 'application/vnd.google-apps.'
KNOWN_BLOCKS = set(CONFIG['blocked_source_ids'])
FIELDS = 'id,name,mimeType,parents,size,md5Checksum,sha1Checksum,sha256Checksum,owners(emailAddress),trashed,modifiedTime,version,appProperties,capabilities(canCopy),shortcutDetails'
STOP = {'PAUSED_BY_USER', 'LOCAL_UPLOAD_IN_PROGRESS', 'COMPLETE', 'COMPLETE_WITH_EXCEPTIONS'}


class SafetyStop(Exception):
    pass


class APIError(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        super().__init__('Google API %s: %s' % (status, reason))


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def stamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SafetyStop('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def extract_document(doc):
    bodies = []
    def body_text(body):
        parts = []
        for item in body.get('content', []):
            if 'paragraph' in item:
                for element in item['paragraph'].get('elements', []):
                    if 'textRun' in element:
                        parts.append(element['textRun']['content'])
                    elif any(k not in {'startIndex', 'endIndex'} for k in element):
                        raise SafetyStop('Non-text controller element')
            elif 'table' in item:
                raise SafetyStop('Controller table requires explicit parser support')
        return ''.join(parts)
    def tabs(items):
        for tab in items:
            body = tab.get('body') or tab.get('documentTab', {}).get('body')
            if body:
                bodies.append((tab.get('tabId') or tab.get('tabProperties', {}).get('tabId'), body_text(body)))
            tabs(tab.get('childTabs', []))
    if doc.get('tabs'):
        tabs(doc['tabs'])
    elif doc.get('body'):
        bodies.append((None, body_text(doc['body'])))
    text = ''.join(v for _, v in bodies)
    if not doc.get('revisionId') or not text.strip():
        raise SafetyStop('Missing controller revision/text')
    state = strict_json(text)
    return state, text, bodies


def check_controller(state, run_id=None):
    if state.get('status') in STOP or state.get('runner_mode') == 'apps_script':
        raise SafetyStop('Controller stop state or Apps Script runner')
    for key, expected in [('source_email', SRC_EMAIL), ('destination_email', DST_EMAIL),
                          ('source_root_id', SRC_ROOT), ('destination_root_id', DST_ROOT)]:
        if state.get(key) != expected:
            raise SafetyStop('Controller identity mismatch: ' + key)
    lease = state.get('lease')
    if lease:
        if not lease.get('expires_at'):
            raise SafetyStop('Unrecognized lease')
        live = stamp(lease['expires_at']) > time.time()
        if live and lease.get('run_id') != run_id:
            raise SafetyStop('Another live lease')
    if run_id and (not lease or lease.get('run_id') != run_id or not live):
        raise SafetyStop('Lease lost/expired')


def recover(state):
    maps, blocked, records = collections.defaultdict(set), set(KNOWN_BLOCKS), []
    def add(source, target):
        if isinstance(source, str) and isinstance(target, str) and re.fullmatch(r'[\w-]{20,}', source) and re.fullmatch(r'[\w-]{20,}', target):
            maps[source].add(target)
    pairs = [('source_id', 'destination_id'), ('source_folder', 'destination_folder'),
             ('source_folder_id', 'destination_folder_id'), ('source_root_id', 'destination_root_id'),
             ('next_source_folder_id', 'next_destination_folder_id'),
             ('completed_source_folder', 'completed_destination_folder')]
    def walk(value, context=''):
        if isinstance(value, list):
            for item in value:
                walk(item, context)
        elif isinstance(value, dict):
            for a, b in pairs:
                add(value.get(a), value.get(b))
            if any(word in context for word in ('map', 'pairs', 'new_files', 'new_folders')):
                for source, target in value.items():
                    add(source, target)
                    if isinstance(target, dict):
                        add(source, target.get('destination_id'))
            if any(word in context for word in ('issue', 'exception')):
                records.append(value)
                if value.get('source_id') and re.search(r'BLOCK|UNSUPPORTED|UNRESOLVED|NO_RETRY|RESTRICTION|RESTRICTED|CANNOT_COPY|PERMISSION_DENIED', json.dumps(value), re.I):
                    blocked.add(value['source_id'])
            for key, item in value.items():
                if key in {'blocked_source_ids', 'excluded_source_ids', 'unsupported_source_ids'} and isinstance(item, list):
                    blocked.update(v for v in item if isinstance(v, str))
                if key == 'blocked_source_folder' and isinstance(item, str):
                    blocked.add(item)
                walk(item, key)
    walk(state)
    conflicts = {s: sorted(v) for s, v in maps.items() if len(v) > 1}
    return {s: next(iter(v)) for s, v in maps.items() if len(v) == 1}, blocked, records, conflicts


class API:
    def __init__(self, role):
        self.role = role
        self.path = BASE / 'auth' / (role + '-token.json')
        if not self.path.exists():
            raise SafetyStop(role.capitalize() + ' account is not connected. Use Connect accounts first.')
        self.token = json.loads(self.path.read_text())

    def request(self, method, resource, params=None, data=None):
        # Explicit mutation allowlist: no deletes, updates of file bytes, or source writes.
        if method != 'GET':
            allowed = (resource == 'drive/files' or
                       re.fullmatch(r'drive/files/[\w-]+/(copy|permissions)', resource) or
                       resource == 'docs/documents/' + DOC + ':batchUpdate')
            if self.role != 'destination' or method != 'POST' or not allowed:
                raise SafetyStop('Forbidden mutation')
        if time.time() + 60 > self.token['expires_at']:
            fresh = json_request(self.token['token_uri'], {
                'client_id': self.token['client_id'], 'client_secret': self.token['client_secret'],
                'refresh_token': self.token['refresh_token'], 'grant_type': 'refresh_token'})
            self.token.update(fresh)
            self.token['expires_at'] = time.time() + fresh['expires_in']
            atomic_private(self.path, self.token)
        service, path = resource.split('/', 1)
        host = {'drive': 'https://www.googleapis.com/drive/v3/', 'docs': 'https://docs.googleapis.com/v1/'}[service]
        url = host + path + (('?' + urllib.parse.urlencode(params)) if params else '')
        payload = json.dumps(data).encode() if data is not None else None
        for attempt in range(3 if method == 'GET' else 1):
            request = urllib.request.Request(url, data=payload, method=method, headers={
                'Authorization': 'Bearer ' + self.token['access_token'], 'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    detail = json.loads(error.read())['error']
                    reason = detail.get('errors', [{}])[0].get('reason') or detail.get('status', 'unknown')
                except Exception:
                    reason = 'unreadable_error'
                if method == 'GET' and error.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise APIError(error.code, reason) from None
        raise SafetyStop('API retry limit')

    def get(self, file_id):
        return self.request('GET', 'drive/files/' + file_id, {'fields': FIELDS, 'supportsAllDrives': 'true'})

    def children(self, parent):
        return all_pages(lambda token: self.request('GET', 'drive/files', {
            'q': "'%s' in parents and trashed = false" % parent,
            'fields': 'nextPageToken,incompleteSearch,files(' + FIELDS + ')',
            'pageSize': 1000, 'spaces': 'drive', 'corpora': 'user',
            'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true',
            **({'pageToken': token} if token else {})}), 'files')

    def permissions(self, file_id):
        return all_pages(lambda token: self.request('GET', 'drive/files/' + file_id + '/permissions', {
            'fields': 'nextPageToken,permissions(id,type,role,emailAddress,deleted)', 'pageSize': 100,
            'supportsAllDrives': 'true', **({'pageToken': token} if token else {})}), 'permissions')


def all_pages(fetch, key):
    result, tokens, ids, token = [], set(), set(), None
    while True:
        page = fetch(token)
        if page.get('incompleteSearch') or key not in page or not isinstance(page[key], list):
            raise SafetyStop('Incomplete or malformed listing')
        for item in page[key]:
            if not item.get('id') or item['id'] in ids:
                raise SafetyStop('Duplicate/missing ID in paginated listing')
            ids.add(item['id'])
            result.append(item)
        token = page.get('nextPageToken')
        if not token:
            return result
        if token in tokens:
            raise SafetyStop('Repeated page token')
        tokens.add(token)


def parity(source, target):
    if source['name'] != target['name'] or source['mimeType'] != target['mimeType']:
        return False
    if not source['mimeType'].startswith(NATIVE):
        if source.get('size') is None or str(source['size']) != str(target.get('size')):
            return False
        for key in ('md5Checksum', 'sha1Checksum', 'sha256Checksum'):
            if source.get(key) and source[key] != target.get(key):
                return False
    return True


def diff(source, target, mappings, blocked):
    """Return deterministic matches/missing/conflicts, never guess duplicate identity."""
    dst = {v['id']: v for v in target}
    used, matches, missing, conflicts = set(), [], [], []
    pending = []
    for item in sorted(source, key=lambda v: (v['name'], v['id'])):
        if item['id'] in blocked:
            conflicts.append((item['id'], 'BLOCKED_SOURCE_DO_NOT_RETRY'))
        elif item['id'] in mappings:
            mapped = mappings[item['id']]
            if mapped not in dst or mapped in used or not parity(item, dst[mapped]):
                conflicts.append((item['id'], 'MAPPED_DESTINATION_MISSING_MOVED_OR_CHANGED'))
            else:
                used.add(mapped)
                matches.append((item, dst[mapped]))
        else:
            pending.append(item)
    groups = collections.defaultdict(list)
    for item in pending:
        groups[(item['name'], item['mimeType'])].append(item)
    for key, items in groups.items():
        candidates = [v for v in target if (v['name'], v['mimeType']) == key and v['id'] not in used]
        if not candidates:
            # No existing name/type candidate: every distinct source item is genuinely missing.
            missing.extend(items)
        elif (len(items) == len(candidates) == 1
              and not items[0]['mimeType'].startswith(NATIVE)
              and items[0].get('md5Checksum')
              and parity(items[0], candidates[0])):
            used.add(candidates[0]['id'])
            matches.append((items[0], candidates[0]))
        else:
            conflicts.extend((v['id'], 'AMBIGUOUS_DUPLICATE_OR_USER_EDIT') for v in items)
    for item in target:
        if item['id'] not in used:
            conflicts.append((item['id'], 'DESTINATION_EXTRA_UNCHANGED'))
    return matches, missing, conflicts


class Worker:
    def __init__(self, minutes=45, max_copies=1000):
        self.source, self.destination = API('source'), API('destination')
        self.run_id = 'migration_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
        self.deadline = time.time() + minutes * 60
        self.max_copies = max_copies
        self.run_dir = BASE / 'runs' / self.run_id
        self.run_dir.mkdir(parents=True, mode=0o700)
        self.db = sqlite3.connect(BASE / 'migration.sqlite')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS ops(source TEXT PRIMARY KEY,parent TEXT,name TEXT,mime TEXT,
          intent TEXT,destination TEXT,state TEXT,run TEXT,verification TEXT);
        CREATE TABLE IF NOT EXISTS maps(source TEXT PRIMARY KEY,destination TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS folders(source TEXT PRIMARY KEY,destination TEXT,path TEXT,state TEXT);
        CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY,run TEXT,item TEXT,reason TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT,run TEXT,kind TEXT,detail TEXT);
        ''')
        self.db.commit()
        os.chmod(BASE / 'migration.sqlite', 0o600)
        self.stats = {'new_files': 0, 'new_folders': 0, 'verified_new_files': 0,
                      'verified_new_folders': 0, 'existing_metadata_verified': 0,
                      'folder_pairs_reconciled': 0, 'binary_integrity_verified': 0,
                      'native_integrity_pending': 0, 'exceptions_this_run': 0}
        self.seq, self.since_checkpoint, self.lease_owned = 0, 0, False
        self.blocked, self.maps = set(KNOWN_BLOCKS), {}
        self.current = None
        self.checkpointed_maps = {}
        self.checkpointed_ops = {}
        self.checkpointed_issue_id = 0

    def event(self, kind, value):
        self.db.execute('INSERT INTO events(at,run,kind,detail) VALUES(?,?,?,?)',
                        (utc(), self.run_id, kind, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def issue(self, item, reason, detail=None):
        self.db.execute('INSERT INTO issues(run,item,reason,detail) VALUES(?,?,?,?)',
                        (self.run_id, item, reason, json.dumps(detail, ensure_ascii=False)))
        self.db.commit()
        self.stats['exceptions_this_run'] += 1

    def read_controller(self):
        doc = self.destination.request('GET', 'docs/documents/' + DOC, {'includeTabsContent': 'true'})
        state, text, bodies = extract_document(doc)
        return doc, state, text, bodies

    def controller_write(self, doc, text, bodies, new_lease, record=None):
        # Retain all existing text and nested history; mutate only top-level lease and append record.
        nonempty = [(tid, s) for tid, s in bodies if s.strip()]
        if len(nonempty) != 1 or nonempty[0][1] != text:
            raise SafetyStop('Multi-tab JSON requires a tab-aware writer; no write attempted')
        tab_id = nonempty[0][0]
        # Locate the top-level lease, not an older nested run's lease.
        decoder = json.JSONDecoder()
        pos = text.index('{') + 1
        lease_span = None
        while True:
            while pos < len(text) and (text[pos].isspace() or text[pos] == ','):
                pos += 1
            if text[pos] == '}':
                break
            field_start = pos
            key, length = decoder.raw_decode(text[pos:])
            pos += length
            while text[pos].isspace():
                pos += 1
            if text[pos] != ':':
                raise SafetyStop('Malformed controller field')
            pos += 1
            while text[pos].isspace():
                pos += 1
            _, length = decoder.raw_decode(text[pos:])
            if key == 'lease':
                lease_span = (field_start, pos, pos + length)
            pos += length
        if lease_span is None:
            raise SafetyStop('Controller has no top-level lease')
        start, value_start, end = lease_span
        prefix = text[start:value_start]
        old_field = text[start:end]
        if text.count(old_field) != 1:
            raise SafetyStop('Ambiguous lease text; reformat the top-level lease before continuing')
        new_field = prefix + json.dumps(new_lease, separators=(',', ':'))
        requests = []
        new_text = text.replace(old_field, new_field, 1)
        if record:
            key = self.run_id + '_checkpoint_' + str(self.seq)
            if key in strict_json(text):
                raise SafetyStop('Checkpoint key collision')
            insertion = ',\n' + json.dumps(key) + ':' + json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
            offset = text.rfind('}')
            index = len(text[:offset].encode('utf-16-le')) // 2 + 1
            location = {'index': index}
            if tab_id:
                location['tabId'] = tab_id
            requests.append({'insertText': {'location': location, 'text': insertion}})
            end = new_text.rfind('}')
            new_text = new_text[:end] + insertion + new_text[end:]
        strict_json(new_text)
        if len(new_text.encode('utf-16-le')) // 2 > 950000:
            raise SafetyStop('Controller near size limit; archive history before continuing')
        request = {'containsText': {'text': old_field, 'matchCase': True}, 'replaceText': new_field}
        if tab_id:
            request['tabsCriteria'] = {'tabIds': [tab_id]}
        requests.append({'replaceAllText': request})
        self.destination.request('POST', 'docs/documents/' + DOC + ':batchUpdate', data={
            'writeControl': {'requiredRevisionId': doc['revisionId']}, 'requests': requests})
        readback, state, actual, _ = self.read_controller()
        if actual != new_text:
            raise SafetyStop('Controller write readback differs; stop')
        self.event('controller_checkpoint', {'revision': readback['revisionId'], 'sequence': self.seq})

    def acquire(self):
        for api, expected in [(self.source, SRC_EMAIL), (self.destination, DST_EMAIL)]:
            identity = api.request('GET', 'drive/about', {'fields': 'user'})['user']
            if identity.get('emailAddress', '').lower() != expected:
                raise SafetyStop('Authenticated identity mismatch')
        for api, root, owner in [(self.source, SRC_ROOT, SRC_EMAIL), (self.destination, DST_ROOT, DST_EMAIL)]:
            meta = api.get(root)
            if meta['mimeType'] != FOLDER or meta.get('trashed') or not any(v.get('emailAddress', '').lower() == owner for v in meta.get('owners', [])):
                raise SafetyStop('Root identity/ownership mismatch')
        doc, state, text, bodies = self.read_controller()
        check_controller(state)
        atomic_private(self.run_dir / 'controller-before.json', doc)
        self.maps, self.blocked, records, conflicts = recover(state)
        self.checkpointed_maps = dict(self.maps)
        atomic_private(self.run_dir / 'imported-exceptions.json', records)
        for source, targets in conflicts.items():
            self.blocked.add(source)
            self.issue(source, 'CONTROLLER_MAPPING_CONFLICT', targets)
        for source, target in self.db.execute('SELECT source,destination FROM maps'):
            if source in self.maps and self.maps[source] != target:
                self.blocked.add(source)
                self.issue(source, 'LOCAL_CONTROLLER_MAPPING_CONFLICT', [self.maps[source], target])
            else:
                self.maps[source] = target
        for item, reason in self.db.execute('SELECT item,reason FROM issues'):
            if reason.startswith('API_RESTRICTION') or reason.startswith('UNSUPPORTED') or reason.startswith('UNCERTAIN'):
                self.blocked.add(item)
        self.lease = {'run_id': self.run_id, 'holder': 'drive_account_migrator', 'acquired_at': utc(),
                      'expires_at': dt.datetime.fromtimestamp(min(self.deadline + 300, time.time() + 3300), dt.timezone.utc).isoformat().replace('+00:00', 'Z')}
        record = {'status': 'RUNNING', 'runner': 'local_oauth', 'source_originals_unchanged': not hasattr(self, 'phase'),
                  'recovered_mappings': len(self.maps), 'blocked_source_ids': sorted(self.blocked),
                  'share_source': SHARE_SOURCE}
        # Mark possible ownership before the request; finalization checks the LIVE owner.
        self.lease_owned = True
        self.controller_write(doc, text, bodies, self.lease, record)
        # Every run starts a fresh recursive reconciliation from the same roots.
        self.db.execute("DELETE FROM folders")
        endpoints = [(SRC_ROOT, DST_ROOT, 'root')]
        for source, target, path in endpoints:
            self.ancestry(self.source, source, SRC_ROOT)
            self.ancestry(self.destination, target, DST_ROOT)
            self.db.execute('INSERT OR IGNORE INTO folders VALUES(?,?,?,?)', (source, target, path, 'pending'))
        self.db.commit()
        if SHARE_SOURCE and getattr(self, 'allow_root_sharing', True):
            self.verify_new(self.source.get(SRC_ROOT), DST_ROOT, self.destination.get(DST_ROOT).get('parents', []), root=True)
        self.publish('RUNNING')

    def ancestry(self, api, file_id, root):
        seen = set()
        while file_id != root:
            if file_id in seen or file_id in self.blocked:
                raise SafetyStop('Cycle or blocked ancestor')
            seen.add(file_id)
            parents = api.get(file_id).get('parents', [])
            if len(parents) != 1:
                raise SafetyStop('Item not beneath expected migration root')
            file_id = parents[0]

    def recover_pending(self):
        rows = self.db.execute('SELECT source,parent,intent,destination,state FROM ops WHERE state IN (?,?,?)',
                               ('intent','created','uncertain')).fetchall()
        for source_id, parent, intent, known_id, state in rows:
            self.guard(remote=True)
            if source_id in self.blocked:
                self.issue(source_id, 'BLOCKED_UNFINISHED_OPERATION_NO_RETRY')
                continue
            self.ancestry(self.destination, parent, DST_ROOT)
            live = self.destination.children(parent)
            candidates = [v for v in live if v.get('appProperties', {}).get('migrationIntent') == intent]
            if known_id:
                candidates = [v for v in live if v['id'] == known_id]
            if len(candidates) != 1:
                self.blocked.add(source_id)
                self.issue(source_id, 'UNCERTAIN_COPY_OUTCOME_NO_RETRY', {'matches':len(candidates)})
                continue
            source = self.source.get(source_id)
            checks = self.verify_new(source,candidates[0]['id'],parent)
            self.mapping(source_id,candidates[0]['id'])
            self.db.execute('UPDATE ops SET destination=?,state=?,verification=? WHERE source=?',
                            (candidates[0]['id'],'verified',json.dumps(checks),source_id))
            self.db.commit()
            self.event('recovered_verified', {'source_id':source_id,'destination_id':candidates[0]['id']})

    def guard(self, remote=False):
        if (BASE / 'STOP').exists():
            raise SafetyStop('Local stop requested; checkpointing')
        if not self.lease_owned or time.time() >= self.deadline or time.time() >= stamp(self.lease['expires_at']) - 60:
            raise SafetyStop('Bounded run finished or lease unavailable')
        if remote:
            _, state, controller_text, _ = self.read_controller()
            if len(controller_text.encode('utf-16-le')) // 2 > 900000:
                raise SafetyStop('Controller near size limit; stop before further copies')
            check_controller(state, self.run_id)
            _, blocked, _, conflicts = recover(state)
            self.blocked.update(blocked)
            self.blocked.update(conflicts)
            if time.time() >= self.deadline or time.time() >= stamp(self.lease['expires_at']) - 60:
                raise SafetyStop('Bounded run ended during controller read')

    def mapping(self, source, destination):
        if source in self.maps and self.maps[source] != destination:
            raise SafetyStop('Refusing to change existing mapping')
        self.maps[source] = destination
        self.db.execute('INSERT OR REPLACE INTO maps VALUES(?,?)', (source, destination))
        self.db.commit()

    def checkpoint(self, status='RUNNING', release=False):
        doc, state, text, bodies = self.read_controller()
        # Never release a foreign lease. Stop states don't prevent recording/releasing ours.
        if not state.get('lease') or state['lease'].get('run_id') != self.run_id:
            self.lease_owned = False
            raise SafetyStop('Lease ownership lost: no checkpoint overwrite')
        self.seq += 1
        mappings = {a:b for a,b in self.db.execute('SELECT source,destination FROM maps')
                    if self.checkpointed_maps.get(a) != b}
        ops = [dict(zip(['source_id', 'destination_id', 'state', 'verification'], row)) for row in self.db.execute(
            'SELECT source,destination,state,verification FROM ops')
            if self.checkpointed_ops.get(row[0]) != tuple(row[1:])]
        issue_rows = self.db.execute('SELECT id,item,reason,detail FROM issues WHERE run=? AND id>?',
                                    (self.run_id,self.checkpointed_issue_id)).fetchall()
        record = {'status': status, 'at': utc(), **self.stats, 'mappings': mappings,
                  'operations': ops, 'current_folder': self.current, 'full_inventory_completed': False,
                  'pending_folder_pairs': [dict(zip(['source_id','destination_id','path'], r)) for r in self.db.execute(
                      "SELECT source,destination,path FROM folders WHERE state='pending' ORDER BY rowid LIMIT 1")],
                  'exceptions': [{'source_id': a, 'status': b, 'detail': json.loads(c)} for _,a,b,c in issue_rows],
                  'source_originals_unchanged': True, 'source_storage_freed': False}
        self.controller_write(doc, text, bodies, None if release else self.lease, record)
        self.checkpointed_maps.update(mappings)
        for operation in ops:
            self.checkpointed_ops[operation['source_id']] = (operation['destination_id'],operation['state'],operation['verification'])
        if issue_rows:
            self.checkpointed_issue_id = issue_rows[-1][0]
        self.since_checkpoint = 0
        if release:
            self.lease_owned = False
        self.publish(status)

    def verify_new(self, source, destination_id, parent, root=False):
        target = self.destination.get(destination_id)
        if target.get('trashed') or target.get('parents') != (parent if root else [parent]) or not parity(source, target):
            raise SafetyStop('New item name/type/parent/integrity verification failed')
        if not any(v.get('emailAddress', '').lower() == DST_EMAIL for v in target.get('owners', [])):
            raise SafetyStop('New item destination ownership verification failed')
        permissions = self.destination.permissions(destination_id) if SHARE_SOURCE else []
        if SHARE_SOURCE and not any(v.get('emailAddress', '').lower() == SRC_EMAIL and v.get('role') == 'writer' and not v.get('deleted') for v in permissions):
            # Restricted to the newly created destination; no source or existing permissions edited.
            try:
                self.destination.request('POST', 'drive/files/' + destination_id + '/permissions',
                    {'sendNotificationEmail': 'false', 'fields': 'id', 'supportsAllDrives': 'true'},
                    {'type': 'user', 'role': 'writer', 'emailAddress': SRC_EMAIL})
            except APIError as error:
                self.blocked.add(source['id'])
                self.issue(source['id'], 'API_RESTRICTION_PERMISSION_NO_RETRY', str(error))
                raise
            permissions = self.destination.permissions(destination_id)
            if not any(v.get('emailAddress', '').lower() == SRC_EMAIL and v.get('role') == 'writer' for v in permissions):
                raise SafetyStop('Source Editor verification failed')
        checks = {'name': True, 'mime': True, 'parent': parent, 'destination_owner': DST_EMAIL,
                  'source_editor': SRC_EMAIL if SHARE_SOURCE else None, 'integrity_fields': [k for k in ('size', 'md5Checksum', 'sha1Checksum', 'sha256Checksum') if source.get(k) is not None and not source['mimeType'].startswith(NATIVE)],
                  'native_content_integrity_checked': False}
        return checks

    def create(self, source, parent):
        self.guard(remote=True)
        source_id = source['id']
        if source_id in self.blocked:
            raise SafetyStop('Attempted prohibited source')
        fresh = self.source.get(source_id)
        if not parity(source, fresh) or source.get('version') != fresh.get('version'):
            raise SafetyStop('Source changed during reconciliation')
        if source['mimeType'] != FOLDER and not source.get('capabilities', {}).get('canCopy', False):
            self.blocked.add(source_id)
            self.issue(source_id, 'API_RESTRICTION_CANNOT_COPY')
            return
        if source['mimeType'].startswith(NATIVE) and source['mimeType'] not in {FOLDER, NATIVE+'document', NATIVE+'spreadsheet', NATIVE+'presentation', NATIVE+'drawing', NATIVE+'form'}:
            self.blocked.add(source_id)
            self.issue(source_id, 'UNSUPPORTED_NATIVE_TYPE', source['mimeType'])
            return
        old = self.db.execute('SELECT destination,state,intent FROM ops WHERE source=?', (source_id,)).fetchone()
        if old:
            # An intent without a response must never trigger another blind copy.
            live = self.destination.children(parent)
            candidates = [v for v in live if v.get('appProperties', {}).get('migrationIntent') == old[2]]
            if old[0]:
                candidates = [v for v in live if v['id'] == old[0]]
            if len(candidates) != 1:
                self.issue(source_id, 'UNCERTAIN_COPY_OUTCOME_NO_RETRY', {'intent': old[2], 'matches': len(candidates)})
                return
            destination_id = candidates[0]['id']
        else:
            intent = uuid.uuid4().hex
            self.db.execute('INSERT INTO ops VALUES(?,?,?,?,?,?,?, ?,?)',
                (source_id, parent, source['name'], source['mimeType'], intent, None, 'intent', self.run_id, None))
            self.db.commit()
            body = {'name': source['name'], 'parents': [parent],
                    'appProperties': {'migrationSourceId': source_id, 'migrationIntent': intent}}
            resource = 'drive/files/' + source_id + '/copy'
            if source['mimeType'] == FOLDER:
                resource = 'drive/files'
                body['mimeType'] = FOLDER
            try:
                result = self.destination.request('POST', resource, {'fields': 'id', 'supportsAllDrives': 'true'}, body)
            except APIError as error:
                self.db.execute('UPDATE ops SET state=? WHERE source=?', ('restricted' if error.status in {400,401,403,404} else 'uncertain', source_id))
                self.db.commit()
                self.issue(source_id, 'API_RESTRICTION_NO_RETRY' if error.status in {400,401,403,404} else 'UNCERTAIN_COPY_OUTCOME_NO_RETRY', str(error))
                self.blocked.add(source_id)
                raise SafetyStop('Copy failed; outcome journaled, no retry') from None
            destination_id = result.get('id')
            if not destination_id:
                raise SafetyStop('Copy response lacked ID; reconcile before retry')
            self.db.execute('UPDATE ops SET destination=?,state=? WHERE source=?', (destination_id, 'created', source_id))
            self.db.commit()
            self.stats['new_folders' if source['mimeType'] == FOLDER else 'new_files'] += 1
        # Store ID mapping immediately, even if later verification fails.
        self.mapping(source_id, destination_id)
        checks = self.verify_new(source, destination_id, parent)
        self.db.execute('UPDATE ops SET state=?,verification=? WHERE source=?', ('verified', json.dumps(checks), source_id))
        self.db.commit()
        self.stats['verified_new_folders' if source['mimeType'] == FOLDER else 'verified_new_files'] += 1
        if not source['mimeType'].startswith(NATIVE):
            self.stats['binary_integrity_verified'] += 1
        elif source['mimeType'] != FOLDER:
            self.stats['native_integrity_pending'] += 1
        self.since_checkpoint += 1
        if self.since_checkpoint >= 10:
            self.checkpoint()
        return destination_id

    def folder(self, source_id, destination_id, path):
        self.current = {'source_id': source_id, 'destination_id': destination_id, 'path': path}
        self.guard(remote=True)
        s, d = self.source.get(source_id), self.destination.get(destination_id)
        if (s.get('trashed') or d.get('trashed') or s['mimeType'] != FOLDER or not parity(s, d)
                or not any(o.get('emailAddress', '').lower() == DST_EMAIL for o in d.get('owners', []))):
            raise SafetyStop('Folder pair identity mismatch')
        source, target = self.source.children(source_id), self.destination.children(destination_id)
        if any(v.get('parents') != [source_id] for v in source) or any(v.get('parents') != [destination_id] for v in target):
            raise SafetyStop('Listing parent mismatch')
        atomic_private(self.run_dir / (source_id + '-before.json'), {'source': source, 'destination': target})
        matches, missing, conflicts = diff(source, target, self.maps, self.blocked)
        for item, reason in conflicts:
            self.issue(item, reason, {'parent_source': source_id, 'parent_destination': destination_id})
        for a, b in matches:
            if not any(o.get('emailAddress', '').lower() == DST_EMAIL for o in b.get('owners', [])):
                self.issue(a['id'], 'DESTINATION_OWNER_MISMATCH')
                continue
            self.mapping(a['id'], b['id'])
            self.stats['existing_metadata_verified'] += 1
            if a['mimeType'] == FOLDER:
                self.db.execute('INSERT OR IGNORE INTO folders VALUES(?,?,?,?)', (a['id'], b['id'], path + '/' + a['name'], 'pending'))
        self.db.commit()
        for item in missing:
            if self.stats['new_files'] + self.stats['new_folders'] >= self.max_copies:
                raise SafetyStop('Bounded copy count reached')
            destination = self.create(item, destination_id)
            if destination and item['mimeType'] == FOLDER:
                self.db.execute('INSERT OR IGNORE INTO folders VALUES(?,?,?,?)', (item['id'], destination, path + '/' + item['name'], 'pending'))
                self.db.commit()
        # Without mutations, the complete before listings already are this folder's live audit.
        after_source, after_target = (self.source.children(source_id), self.destination.children(destination_id)) if missing else (source,target)
        stable = lambda values: sorted((v['id'], v['name'], v['mimeType'], v.get('version')) for v in values)
        if stable(source) != stable(after_source):
            raise SafetyStop('Source folder changed during run')
        _, still_missing, end_conflicts = diff(after_source, after_target, self.maps, self.blocked)
        atomic_private(self.run_dir / (source_id + '-after.json'), {'source': after_source, 'destination': after_target})
        state = 'reconciled_with_exceptions' if conflicts or end_conflicts or still_missing else 'reconciled'
        self.db.execute('UPDATE folders SET state=? WHERE source=?', (state, source_id))
        self.db.commit()
        self.stats['folder_pairs_reconciled'] += 1
        self.publish('RUNNING')

    def publish(self, status, error=None):
        pending = [dict(zip(['source_id','destination_id','path'], r)) for r in self.db.execute('SELECT source,destination,path FROM folders WHERE state=? LIMIT 5', ('pending',))]
        summary = {'run_id': self.run_id, 'status': status, 'at': utc(), **self.stats,
                   'current': self.current, 'resume': pending, 'error': error,
                   'controller_id': DOC, 'source_root_id': SRC_ROOT, 'destination_root_id': DST_ROOT,
                   'full_inventory_completed': False, 'source_originals_unchanged': True,
                   'lease_owned': self.lease_owned, 'lease_expires_at': getattr(self, 'lease', {}).get('expires_at')}
        atomic_private(BASE / 'live-status.json', summary)
        print(json.dumps({k: summary[k] for k in ['status','new_files','new_folders','verified_new_files','verified_new_folders','folder_pairs_reconciled','exceptions_this_run','error']}, ensure_ascii=False), flush=True)

    def run(self):
        status, error = 'CHECKPOINTED', None
        try:
            self.acquire()
            self.recover_pending()
            while True:
                row = self.db.execute('SELECT source,destination,path FROM folders WHERE state=? ORDER BY rowid LIMIT 1', ('pending',)).fetchone()
                if not row:
                    status = 'RECURSIVE_TRAVERSAL_FINISHED_CERTIFICATION_PENDING'
                    break
                if row[0] in self.blocked:
                    self.db.execute('UPDATE folders SET state=? WHERE source=?', ('blocked',row[0]))
                    self.db.commit()
                    continue
                self.folder(*row)
        except (SafetyStop, APIError, OSError, ValueError, KeyError, KeyboardInterrupt) as exception:
            status, error = 'STOPPED', str(exception)
            self.event('stop', {'error': error})
        finally:
            if self.lease_owned:
                try:
                    self.checkpoint(status, release=True)
                except Exception as exception:
                    error = (error or '') + '; lease/checkpoint finalization failed: ' + type(exception).__name__
                    status = 'STOPPED_LEASE_EXPIRY_REQUIRED'
            self.publish(status, error)
            self.db.close()
        return 1 if error else 0
