"""Fixed, reviewed binary-only cleanup plans; no recursive or whole-Trash deletion."""
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from . import core as c
from .oauth import atomic_private
from .planning import identities, controller


def owned(meta, email):
    return any(o.get('emailAddress', '').lower() == email for o in meta.get('owners', []))


def validate(source, destination, proof, permissions, phase):
    if phase not in ('trash', 'purge'):
        raise c.SafetyStop('Invalid cleanup phase')
    if source['id'] == destination['id'] or source['id'] in c.KNOWN_BLOCKS:
        raise c.SafetyStop('Excluded or identical IDs')
    if (source['mimeType'].startswith(c.NATIVE) or destination.get('trashed')
            or bool(source.get('trashed')) != (phase == 'purge')):
        raise c.SafetyStop('Native file or unexpected trash state')
    if not owned(source, c.SRC_EMAIL) or not owned(destination, c.DST_EMAIL):
        raise c.SafetyStop('Unexpected ownership')
    if not source.get('md5Checksum') or not c.parity(source, destination):
        raise c.SafetyStop('Binary checksum/size/name/type mismatch')
    for actual, expected in ((source, proof['source']), (destination, proof['destination'])):
        if (actual['id'] != expected['id'] or not c.parity(actual, expected)
                or not actual.get('version') or actual['version'] != expected.get('version')
                or actual.get('parents') != expected.get('parents')):
            raise c.SafetyStop('Item changed since reviewed plan')
    if c.SHARE_SOURCE and not any(
        p.get('emailAddress', '').lower() == c.SRC_EMAIL and p.get('role') == 'writer'
        and not p.get('deleted') for p in permissions
    ):
        raise c.SafetyStop('Requested source Editor access missing')


def database():
    db = sqlite3.connect(c.BASE/'cleanup.sqlite')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS actions(source TEXT, phase TEXT, destination TEXT, '
               'state TEXT, detail TEXT, PRIMARY KEY(source, phase))')
    if 'journaled' not in {r[1] for r in db.execute('PRAGMA table_info(actions)')}:
        db.execute('ALTER TABLE actions ADD COLUMN journaled INTEGER NOT NULL DEFAULT 0')
    db.commit()
    return db


def record(db, sid, phase, did, state, detail):
    db.execute('INSERT OR REPLACE INTO actions(source,phase,destination,state,detail) VALUES(?,?,?,?,?)',
               (sid, phase, did, state, json.dumps(detail)))
    db.commit()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ancestry(api, fid, root, blocked):
    seen = set()
    while fid != root:
        if fid in seen or fid in blocked:
            raise c.SafetyStop('Blocked ancestor or cycle')
        seen.add(fid)
        parents = api.get(fid).get('parents', [])
        if len(parents) != 1:
            raise c.SafetyStop('Item is outside the configured migration root')
        fid = parents[0]


def make_plan(phase):
    source, destination = c.API('source'), c.API('destination')
    identities(source, destination)
    maps, blocked, _ = controller(destination)
    db = database()
    items, excluded = [], []
    for sid, did in sorted(maps.items()):
        if (c.BASE/'STOP').exists():
            raise c.SafetyStop('Local stop requested')
        if sid == c.SRC_ROOT or sid in blocked:
            continue
        try:
            previous = db.execute('SELECT state,detail FROM actions WHERE source=? AND phase=?',
                                  (sid, 'trash')).fetchone()
            if phase == 'purge' and (not previous or previous[0] != 'verified'):
                continue
            if db.execute('SELECT 1 FROM actions WHERE source=? AND phase=?', (sid, phase)).fetchone():
                continue  # Never re-plan already attempted actions.
            s, d = source.get(sid), destination.get(did)
            ancestry(source, sid, c.SRC_ROOT, blocked)
            ancestry(destination, did, c.DST_ROOT, set())
            if phase == 'purge':
                old = json.loads(previous[1])
                if s.get('version') != old['source_after']['version'] or d != old['destination']:
                    raise c.SafetyStop('Changed since verified trash operation')
            proof = {'source': s, 'destination': d}
            permissions = destination.permissions(did) if c.SHARE_SOURCE else []
            validate(s, d, proof, permissions, phase)
            items.append(proof)
        except (c.SafetyStop, c.APIError) as error:
            excluded.append({'source_id': sid, 'reason': str(error)})
    db.close()
    path = c.BASE/(phase+'-plan.json')
    atomic_private(path, {'phase': phase, 'created_at': c.utc(), 'source_email': c.SRC_EMAIL,
                         'destination_email': c.DST_EMAIL, 'source_root_id': c.SRC_ROOT,
                         'destination_root_id': c.DST_ROOT, 'share_source': c.SHARE_SOURCE,
                         'items': items, 'excluded': excluded})
    print(json.dumps({'plan': str(path), 'sha256': digest(path), 'eligible': len(items),
                      'excluded': len(excluded), 'bytes': sum(int(p['source']['size']) for p in items)}))


class CleanupSource(c.API):
    """Only the exact reviewed ID set can be trashed/deleted; read API stays unchanged."""
    def __init__(self, allowlist):
        self.role = 'source'
        self.path = c.BASE/'auth/source-cleanup-token.json'
        self.token = json.loads(self.path.read_text())
        self.allowlist = frozenset(allowlist)
        if (self.token.get('email') != c.SRC_EMAIL
                or 'https://www.googleapis.com/auth/drive' not in self.token.get('scope', '').split()):
            raise c.SafetyStop('Separate source cleanup authorization required')

    def mutate(self, sid, phase):
        if (phase not in ('trash', 'purge') or sid not in self.allowlist
                or sid in c.KNOWN_BLOCKS or sid == c.SRC_ROOT):
            raise c.SafetyStop('Forbidden cleanup mutation')
        # Refresh through read-only API; NEVER retry mutation on any failure.
        self.request('GET', 'drive/about', {'fields': 'user'})
        params = {'supportsAllDrives': 'true'}
        if phase == 'trash':
            params['fields'] = c.FIELDS
        url = 'https://www.googleapis.com/drive/v3/files/' + sid + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method='PATCH' if phase == 'trash' else 'DELETE',
            data=b'{"trashed":true}' if phase == 'trash' else None,
            headers={'Authorization': 'Bearer '+self.token['access_token'], 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                if phase == 'purge' and response.status != 204:
                    raise c.SafetyStop('Unexpected delete response')
                return None if response.status == 204 else json.load(response)
        except urllib.error.HTTPError as error:
            raise c.APIError(error.code, 'Cleanup mutation rejected; not retried') from None


class CleanupRun(c.Worker):
    def __init__(self, phase, plan_hash, minutes, max_items):
        super().__init__(minutes, max_items)
        self.phase, self.max_items = phase, max_items
        path = c.BASE/(phase+'-plan.json')
        if digest(path) != plan_hash:
            raise c.SafetyStop('Reviewed plan hash does not match')
        self.plan = json.loads(path.read_text())
        for key, value in [('phase', phase), ('source_email', c.SRC_EMAIL),
                           ('destination_email', c.DST_EMAIL), ('source_root_id', c.SRC_ROOT),
                           ('destination_root_id', c.DST_ROOT), ('share_source', c.SHARE_SOURCE)]:
            if self.plan.get(key) != value:
                raise c.SafetyStop('Reviewed plan configuration mismatch')
        ids = [p['source']['id'] for p in self.plan['items']]
        if len(ids) != len(set(ids)):
            raise c.SafetyStop('Duplicate IDs in plan')
        self.source = CleanupSource(ids)
        self.cleanup_db = database()
        self.cleanup_deltas = [dict(zip(('source_id','destination_id','phase','state'), row))
            for row in self.cleanup_db.execute('SELECT source,destination,phase,state FROM actions WHERE journaled=0')]
        self.allow_root_sharing = False

    def publish(self, status, error=None):
        counts = dict(self.cleanup_db.execute(
            'SELECT state,count(*) FROM actions WHERE phase=? GROUP BY state', (self.phase,)))
        value = {'run_id': self.run_id, 'status': status, 'phase': self.phase,
                 'counts': counts, 'at': c.utc(), 'error': error,
                 'lease_owned': self.lease_owned, 'source_originals_unchanged': False,
                 'destination_copies_unchanged': True, 'controller_id': c.DOC,
                 'source_root_id': c.SRC_ROOT, 'destination_root_id': c.DST_ROOT}
        atomic_private(c.BASE/'cleanup-status.json', value)
        print(json.dumps(value), flush=True)

    def checkpoint(self, status='RUNNING', release=False):
        doc, state, text, bodies = self.read_controller()
        if not state.get('lease') or state['lease'].get('run_id') != self.run_id:
            self.lease_owned = False
            raise c.SafetyStop('Lease lost; will not overwrite another writer')
        self.seq += 1
        record_value = {'status': status, 'at': c.utc(), 'cleanup_phase': self.phase,
                        'source_originals_unchanged': False, 'destination_copies_unchanged': True,
                        'operations': list(self.cleanup_deltas)}
        self.controller_write(doc, text, bodies, None if release else self.lease, record_value)
        self.cleanup_db.executemany('UPDATE actions SET journaled=1 WHERE source=? AND phase=?',
                                    [(r['source_id'], r['phase']) for r in self.cleanup_deltas])
        self.cleanup_db.commit()
        self.cleanup_deltas.clear()
        if release:
            self.lease_owned = False
        self.publish(status)

    def run(self):
        status, error, count = 'CLEANUP_CHECKPOINTED', None, 0
        try:
            self.acquire()
            if self.cleanup_deltas:
                self.checkpoint('CLEANUP_RECOVERED_JOURNAL')
            for proof in self.plan['items']:
                sid, did = proof['source']['id'], proof['destination']['id']
                old = self.cleanup_db.execute('SELECT state FROM actions WHERE source=? AND phase=?',
                                              (sid, self.phase)).fetchone()
                if old:
                    if old[0] == 'uncertain' or old[0] == 'intent':
                        raise c.SafetyStop('Uncertain cleanup outcome; inspect/reconcile without retry')
                    continue
                if count >= self.max_items or time.time() > self.deadline - 90:
                    break
                self.guard(remote=True)
                if sid in self.blocked or self.maps.get(sid) != did:
                    raise c.SafetyStop('New exclusion or changed controller mapping')
                ancestry(self.source, sid, c.SRC_ROOT, self.blocked)
                ancestry(self.destination, did, c.DST_ROOT, set())
                s, d = self.source.get(sid), self.destination.get(did)
                permissions = self.destination.permissions(did) if c.SHARE_SOURCE else []
                validate(s, d, proof, permissions, self.phase)
                if self.phase == 'purge':
                    row = self.cleanup_db.execute("SELECT detail FROM actions WHERE source=? AND phase='trash' AND state='verified'", (sid,)).fetchone()
                    if not row or json.loads(row[0])['source_after']['version'] != s['version']:
                        raise c.SafetyStop('No matching verified trash operation')
                # Refresh lease/stop state after ancestry reads, then recheck source.
                self.guard(remote=True)
                validate(self.source.get(sid), d, proof, permissions, self.phase)
                record(self.cleanup_db, sid, self.phase, did, 'intent', {'at': c.utc()})
                try:
                    self.source.mutate(sid, self.phase)
                    d_after = self.destination.get(did)
                    if d_after != d:
                        raise c.SafetyStop('Destination changed during cleanup')
                    if self.phase == 'trash':
                        s_after = self.source.get(sid)
                        if not s_after.get('trashed') or not c.parity(s, s_after):
                            raise c.SafetyStop('Trash verification failed')
                    else:
                        try:
                            self.source.get(sid)
                        except c.APIError as absence:
                            if absence.status != 404:
                                raise
                        else:
                            raise c.SafetyStop('Deleted source is still readable')
                        s_after = {'id': sid, 'get_status': 404}
                    record(self.cleanup_db, sid, self.phase, did, 'verified',
                           {'source_before': s, 'source_after': s_after, 'destination': d, 'at': c.utc()})
                    self.cleanup_deltas.append({'source_id': sid, 'destination_id': did,
                                                'phase': self.phase, 'state': 'verified'})
                    count += 1
                except Exception:
                    record(self.cleanup_db, sid, self.phase, did, 'uncertain', {'at': c.utc(), 'no_retry': True})
                    self.cleanup_deltas.append({'source_id': sid, 'destination_id': did,
                                                'phase': self.phase, 'state': 'uncertain'})
                    raise
                self.checkpoint('CLEANUP_RUNNING')  # At least once per ten; here every item.
            status = 'CLEANUP_BOUNDED_RUN_FINISHED'
        except (Exception, KeyboardInterrupt) as exception:
            status, error = 'CLEANUP_STOPPED', type(exception).__name__ + ': ' + str(exception)
        finally:
            if self.lease_owned:
                try:
                    self.checkpoint(status, release=True)
                except Exception as exception:
                    error = (error or '') + '; checkpoint/release failed: ' + type(exception).__name__
            self.publish(status, error)
            self.cleanup_db.close()
            self.db.close()
        return 1 if error else 0
