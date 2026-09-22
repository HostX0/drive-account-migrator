"""Read-only recursive planning. Never acquire a lease or mutate Drive."""
from collections import deque
import json
from . import core as c
from .oauth import atomic_private


def identities(source, destination):
    for api, expected in ((source, c.SRC_EMAIL), (destination, c.DST_EMAIL)):
        actual = api.request('GET', 'drive/about', {'fields': 'user'})['user']['emailAddress']
        if actual.lower() != expected:
            raise c.SafetyStop('Authenticated account mismatch')
    a, b = source.get(c.SRC_ROOT), destination.get(c.DST_ROOT)
    for meta, expected in ((a, c.SRC_EMAIL), (b, c.DST_EMAIL)):
        if (meta.get('trashed') or meta.get('mimeType') != c.FOLDER
                or not any(x.get('emailAddress', '').lower() == expected for x in meta.get('owners', []))):
            raise c.SafetyStop('Root is not a live personal folder owned by the expected account')
    if not c.parity(a, b):
        raise c.SafetyStop('Use source and destination folders with exactly the same name')


def controller(destination):
    doc = destination.request('GET', 'docs/documents/' + c.DOC, {'includeTabsContent': 'true'})
    state, _, _ = c.extract_document(doc)
    c.check_controller(state)
    maps, blocked, exceptions, conflicts = c.recover(state)
    blocked.update(conflicts)
    return maps, blocked, exceptions


def plan():
    source, destination = c.API('source'), c.API('destination')
    identities(source, destination)
    maps, blocked, imported = controller(destination)
    queue = deque([(c.SRC_ROOT, c.DST_ROOT)])
    seen, missing, issues, matched = set(), [], [], []
    while queue:
        if (c.BASE/'STOP').exists():
            raise c.SafetyStop('Local stop requested')
        sid, did = queue.popleft()
        if sid in seen:
            raise c.SafetyStop('Repeated source folder; unsupported graph')
        seen.add(sid)
        if sid in blocked:
            issues.append({'id': sid, 'reason': 'BLOCKED_DO_NOT_RETRY'})
            continue
        try:
            sitems = source.children(sid)
            ditems = destination.children(did) if did else []
            if any(x.get('parents') != [sid] for x in sitems) or any(x.get('parents') != [did] for x in ditems):
                raise c.SafetyStop('Listing parent mismatch')
            matches, absent, conflicts = c.diff(sitems, ditems, maps, blocked)
        except c.APIError as error:
            issues.append({'id': sid, 'reason': 'LISTING_FAILED', 'status': error.status})
            continue
        issues.extend({'id': x, 'reason': y} for x, y in conflicts)
        for a, b in matches:
            if not any(o.get('emailAddress', '').lower() == c.DST_EMAIL for o in b.get('owners', [])):
                issues.append({'id': a['id'], 'reason': 'DESTINATION_OWNER_MISMATCH'})
                continue
            matched.append({'source_id': a['id'], 'destination_id': b['id']})
            if a['mimeType'] == c.FOLDER:
                queue.append((a['id'], b['id']))
        for a in absent:
            eligible = (a['mimeType'] == c.FOLDER or a.get('capabilities', {}).get('canCopy', False))
            native_ok = (not a['mimeType'].startswith(c.NATIVE) or a['mimeType'] in {
                c.FOLDER, c.NATIVE+'document', c.NATIVE+'spreadsheet',
                c.NATIVE+'presentation', c.NATIVE+'drawing', c.NATIVE+'form'})
            if not eligible or not native_ok:
                issues.append({'id': a['id'], 'reason': 'UNSUPPORTED_OR_CANNOT_COPY'})
                continue
            missing.append({'source_id': a['id'], 'name': a['name'], 'mimeType': a['mimeType'],
                            'intended_parent': did, 'source_parent': sid})
            if a['mimeType'] == c.FOLDER:
                queue.append((a['id'], None))
    result = {'at': c.utc(), 'read_only': True, 'missing': missing, 'matched': matched,
              'exceptions': issues, 'imported_exceptions': imported,
              'folder_pairs_visited': len(seen), 'global_completion_certified': False,
              'share_source': c.SHARE_SOURCE}
    atomic_private(c.BASE/'plan.json', result)
    print(json.dumps({'missing': len(missing), 'matched': len(matched), 'exceptions': len(issues),
                      'report': str(c.BASE/'plan.json')}))
    return result
