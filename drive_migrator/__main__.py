"""Command line entry point. All cloud writes require an explicit action."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sys


@contextlib.contextmanager
def lock(path):
    """OS releases this lock on crashes; no stale PID or lock-file removal races."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def bind_state(c):
    expected = {key: c.CONFIG[key] for key in (
        'source_email', 'destination_email', 'source_root_id', 'destination_root_id')}
    path = c.BASE/'identity.json'
    if path.exists():
        if json.loads(path.read_text()) != expected:
            raise c.SafetyStop('This state directory belongs to another migration')
    else:
        c.atomic_private(path, expected)


def initialize(c, config_path):
    """Explicitly create one native Docs controller; never create/recreate Drive roots."""
    import urllib.request
    from .planning import identities
    source, destination = c.API('source'), c.API('destination')
    identities(source, destination)
    if c.DOC != 'CONTROLLER_ID':
        raise c.SafetyStop('Controller is already configured; reuse it')
    pending = c.BASE/'controller-initialization.json'
    state = {'schema_version': 1, 'status': 'READY', 'runner_mode': 'local_oauth',
             'source_email': c.SRC_EMAIL, 'destination_email': c.DST_EMAIL,
             'source_root_id': c.SRC_ROOT, 'destination_root_id': c.DST_ROOT,
             'lease': None, 'mappings': {c.SRC_ROOT: c.DST_ROOT},
             'blocked_source_ids': sorted(c.KNOWN_BLOCKS)}
    if pending.exists():
        receipt = json.loads(pending.read_text())
        doc_id = receipt.get('document_id')
        if not doc_id:
            raise c.SafetyStop('Uncertain controller creation: inspect Drive before manually resolving the receipt')
    else:
        c.atomic_private(pending, {'state': 'intent'})
        req = urllib.request.Request('https://docs.googleapis.com/v1/documents',
            data=json.dumps({'title': 'Drive Account Migrator Controller'}).encode(), method='POST',
            headers={'Authorization': 'Bearer '+destination.token['access_token'], 'Content-Type': 'application/json'})
        # A timeout must not cause a second controller to be created automatically.
        with urllib.request.urlopen(req, timeout=60) as response:
            doc_id = json.load(response)['documentId']
        c.atomic_private(pending, {'document_id': doc_id, 'state': 'created'})
    c.DOC = doc_id
    doc = destination.request('GET', 'docs/documents/'+doc_id, {'includeTabsContent': 'true'})
    try:
        existing, _, _ = c.extract_document(doc)
    except (ValueError, c.SafetyStop):
        # Only a completely blank single document tab may be initialized.
        tabs = doc.get('tabs', [])
        if len(tabs) != 1 or tabs[0].get('childTabs'):
            raise c.SafetyStop('Expected a blank, single-tab controller')
        tab = tabs[0]
        body = tab.get('documentTab', {}).get('body', {})
        for element in body.get('content', []):
            if 'sectionBreak' in element:
                continue
            paragraph = element.get('paragraph')
            if paragraph is None:
                raise c.SafetyStop('Nonblank controller: will not overwrite it')
            for run in paragraph.get('elements', []):
                if run.get('textRun', {}).get('content', '').strip() or 'textRun' not in run:
                    raise c.SafetyStop('Nonblank controller: will not overwrite it')
        destination.request('POST', 'docs/documents/'+doc_id+':batchUpdate', data={
            'writeControl': {'requiredRevisionId': doc['revisionId']},
            'requests': [{'insertText': {'location': {'index': 1, 'tabId': tab['tabProperties']['tabId']},
                                         'text': json.dumps(state, indent=2)}}]})
        existing, _, _ = c.extract_document(destination.request('GET', 'docs/documents/'+doc_id, {'includeTabsContent': 'true'}))
    if existing != state:
        raise c.SafetyStop('Controller initialization readback differs')
    config = json.loads(config_path.read_text())
    config['controller_id'] = doc_id
    c.atomic_private(config_path, config)
    c.atomic_private(pending, {'document_id': doc_id, 'state': 'verified'})
    print(json.dumps({'controller_id': doc_id, 'config_updated': str(config_path)}))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Copy first; review before optional source cleanup.')
    parser.add_argument('--config', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('gui', help='Open the local graphical setup and control panel')
    auth = sub.add_parser('connect')
    auth.add_argument('--role', choices=['source', 'destination', 'source-cleanup'], required=True)
    auth.add_argument('--client', type=Path, required=True)
    sub.add_parser('plan', help='Read-only recursive inventory and diff')
    init = sub.add_parser('init-controller')
    init.add_argument('--apply', action='store_true', required=True)
    copy = sub.add_parser('copy')
    copy.add_argument('--apply', action='store_true', required=True)
    cleanup_plan = sub.add_parser('cleanup-plan')
    cleanup_plan.add_argument('--phase', choices=['trash', 'purge'], required=True)
    for name in ('trash', 'purge'):
        command = sub.add_parser(name)
        command.add_argument('--plan-sha256', required=True)
        command.add_argument('--acknowledge', required=True,
                             help='TRASH_VERIFIED_ORIGINALS or PERMANENTLY_DELETE_VERIFIED_ORIGINALS')
    for command in (copy, sub.choices['trash'], sub.choices['purge']):
        command.add_argument('--minutes', type=int, default=30)
        command.add_argument('--max-items', type=int, default=100)
    sub.add_parser('status')
    args = parser.parse_args(argv)
    if args.command == 'gui':
        from .gui import main as gui_main
        return gui_main(args.config.expanduser().resolve() if args.config else None)
    if not args.config:
        parser.error('--config is required except for gui')
    config_path = args.config.expanduser().resolve()
    os.environ['DRIVE_MIGRATOR_CONFIG'] = str(config_path)
    try:
        os.umask(0o077)
        from . import core as c
        c.BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hasattr(args, 'minutes') and not (1 <= args.minutes <= 45 and 1 <= args.max_items <= 10000):
            raise ValueError('Use 1–45 minutes and 1–10000 items per bounded run')
        with lock(c.BASE/'worker.lock'):
            bind_state(c)
            if args.command == 'connect':
                from .oauth import connect
                expected = c.DST_EMAIL if args.role == 'destination' else c.SRC_EMAIL
                connect(args.client.expanduser(), args.role, c.BASE/'auth', expected)
            elif args.command == 'init-controller':
                initialize(c, config_path)
            elif args.command == 'plan':
                from .planning import plan
                plan()
            elif args.command == 'copy':
                return c.Worker(args.minutes, args.max_items).run()
            elif args.command == 'cleanup-plan':
                from .cleanup import make_plan
                make_plan(args.phase)
            elif args.command in ('trash', 'purge'):
                required = 'TRASH_VERIFIED_ORIGINALS' if args.command == 'trash' else 'PERMANENTLY_DELETE_VERIFIED_ORIGINALS'
                if args.acknowledge != required:
                    raise c.SafetyStop('Destructive action acknowledgement does not match')
                from .cleanup import CleanupRun
                return CleanupRun(args.command, args.plan_sha256, args.minutes, args.max_items).run()
            elif args.command == 'status':
                source = c.API('source')
                quota = source.request('GET', 'drive/about', {'fields': 'user,storageQuota'})
                if quota['user']['emailAddress'].lower() != c.SRC_EMAIL:
                    raise c.SafetyStop('Authenticated account mismatch')
                print(json.dumps(quota, indent=2))
                for name in ('live-status.json', 'cleanup-status.json'):
                    path = c.BASE/name
                    if path.exists():
                        print(path.read_text())
        return 0
    except KeyboardInterrupt:
        print('Interrupted. Preserve the state directory; reconcile before retrying.', file=sys.stderr)
        return 130
    except Exception as error:
        # HTTP exceptions can contain sensitive URLs: do not print raw request objects.
        core = sys.modules.get('drive_migrator.core')
        safe_types = (ValueError, FileNotFoundError)
        if core is not None:
            safe_types += (core.SafetyStop, core.APIError)
        text = str(error) if isinstance(error, safe_types) else type(error).__name__
        print('Stopped: '+text, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
