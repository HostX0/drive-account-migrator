"""Offline end-to-end copy tests against a small in-memory Drive."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from drive_migrator import core as c


SROOT = 'source_root_'+'s'*24
DROOT = 'destination_root_'+'d'*24


def file(fid, name, parent, mime='application/octet-stream', owner='source@example.test'):
    return {'id': fid, 'name': name, 'parents': [parent], 'mimeType': mime,
            'size': '5', 'md5Checksum': 'checksum-'+name, 'version': '1', 'trashed': False,
            'owners': [{'emailAddress': owner}], 'capabilities': {'canCopy': True}}


class MemoryDrive:
    def __init__(self):
        self.source = {SROOT: file(SROOT, 'Data', 'source_parent', c.FOLDER)}
        self.destination = {DROOT: file(DROOT, 'Data', 'destination_parent', c.FOLDER, 'destination@example.test')}
        self.permission = {}
        self.calls = []
        self.fail_after_copy = False
        self.counter = 0
        self.controller = {'source_email': 'source@example.test', 'destination_email': 'destination@example.test',
                           'source_root_id': SROOT, 'destination_root_id': DROOT,
                           'lease': None, 'status': 'READY', 'mappings': {SROOT: DROOT}}

    def api(self, role):
        drive = self
        class Account:
            def get(self, fid):
                data = drive.source if role == 'source' else drive.destination
                if fid not in data:
                    raise c.APIError(404, 'notFound')
                return copy.deepcopy(data[fid])

            def children(self, parent):
                drive.calls.append(('list', role, parent))
                data = drive.source if role == 'source' else drive.destination
                return [copy.deepcopy(v) for v in data.values() if v['parents'] == [parent] and not v['trashed']]

            def permissions(self, fid):
                drive.calls.append(('permissions', role, fid))
                return copy.deepcopy(drive.permission.get(fid, drive.permission.get(DROOT, [])))

            def request(self, method, resource, params=None, data=None):
                drive.calls.append((method, role, resource))
                if resource == 'drive/about':
                    return {'user': {'emailAddress': role+'@example.test'}}
                if role != 'destination' or method != 'POST':
                    raise AssertionError('Unexpected mutation')
                if resource.endswith('/permissions'):
                    fid = resource.split('/')[2]
                    drive.permission[fid] = [{'id': 'permission', **data}]
                    return {'id': 'permission'}
                drive.counter += 1
                fid = 'new_destination_'+str(drive.counter)+'x'*25
                if resource == 'drive/files':
                    created = file(fid, data['name'], data['parents'][0], c.FOLDER, 'destination@example.test')
                else:
                    original = resource.split('/')[2]
                    created = copy.deepcopy(drive.source[original])
                    created.update(id=fid, owners=[{'emailAddress': 'destination@example.test'}])
                created.update(copy.deepcopy(data))
                drive.destination[fid] = created
                if drive.fail_after_copy:
                    drive.fail_after_copy = False
                    raise ConnectionError('Lost response after successful copy')
                return {'id': fid}
        return Account()


class MemoryWorker(c.Worker):
    drive = None

    def read_controller(self):
        state = copy.deepcopy(self.drive.controller)
        text = json.dumps(state)
        return {'revisionId': 'test-revision'}, state, text, [('tab', text)]

    def controller_write(self, doc, text, bodies, new_lease, record=None):
        self.drive.controller['lease'] = new_lease
        if record:
            self.drive.controller[self.run_id+'_'+str(self.seq)] = copy.deepcopy(record)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.drive = MemoryDrive()
        MemoryWorker.drive = self.drive
        self.patches = [patch.object(c, 'BASE', Path(self.directory.name)),
                        patch.object(c, 'SRC_ROOT', SROOT), patch.object(c, 'DST_ROOT', DROOT),
                        patch.object(c, 'SRC_EMAIL', 'source@example.test'),
                        patch.object(c, 'DST_EMAIL', 'destination@example.test'),
                        patch.object(c, 'SHARE_SOURCE', False), patch.object(c, 'KNOWN_BLOCKS', set()),
                        patch.object(c, 'API', self.drive.api)]
        for p in self.patches:
            p.start()
        self.addCleanup(self.directory.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def run_worker(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return MemoryWorker(minutes=2, max_copies=20).run()

    def test_two_distinct_same_name_folders_and_empty_folder(self):
        for sid in ('folder_a', 'folder_b', 'empty_folder'):
            self.drive.source[sid] = file(sid, 'Same', SROOT, c.FOLDER)
        self.drive.source['binary_a'] = file('binary_a', 'a.bin', 'folder_a')
        self.drive.source['binary_b'] = file('binary_b', 'b.bin', 'folder_b')
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(len(self.drive.destination), 6)
        folders = [v for v in self.drive.destination.values() if v['parents'] == [DROOT]]
        self.assertEqual(len(folders), 3)
        self.assertEqual(len({v['id'] for v in folders}), 3)
        self.assertFalse(any(call[0] == 'permissions' for call in self.drive.calls))
        self.assertIsNone(self.drive.controller['lease'])
        before = copy.deepcopy(self.drive.destination)
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.drive.destination, before)

    def test_lost_copy_response_recovered_without_second_copy(self):
        self.drive.source['file'] = file('file', 'a.bin', SROOT)
        self.drive.fail_after_copy = True
        self.assertEqual(self.run_worker(), 1)
        self.assertEqual(self.drive.counter, 1)
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.drive.counter, 1)
        self.assertIsNone(self.drive.controller['lease'])

    def test_share_option_grants_root_writer(self):
        self.drive.source['file'] = file('file', 'a.bin', SROOT)
        with patch.object(c, 'SHARE_SOURCE', True):
            self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.drive.permission[DROOT][0]['emailAddress'], 'source@example.test')
        self.assertEqual(self.drive.permission[DROOT][0]['role'], 'writer')

    def test_user_edit_is_never_overwritten_or_duplicated(self):
        self.drive.source['file'] = file('file', 'a.bin', SROOT)
        self.assertEqual(self.run_worker(), 0)
        new = next(v for k, v in self.drive.destination.items() if k != DROOT)
        new['md5Checksum'] = 'user-edited-content'
        before = copy.deepcopy(self.drive.destination)
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.drive.destination, before)
        self.assertEqual(self.drive.counter, 1)

    def test_no_name_only_native_or_folder_adoption(self):
        for mime in (c.FOLDER, c.NATIVE+'document'):
            a, b = file('a', 'same', SROOT, mime), file('b', 'same', DROOT, mime)
            matches, missing, conflicts = c.diff([a], [b], {}, set())
            self.assertFalse(matches or missing)
            self.assertTrue(conflicts)


if __name__ == '__main__':
    unittest.main()
