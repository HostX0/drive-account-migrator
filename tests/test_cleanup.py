import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from drive_migrator import core as c
from drive_migrator.cleanup import validate, CleanupSource, database, record, CleanupRun
from drive_migrator.gui import drive_id, command


def fixtures():
    source = {'id': 's', 'name': 'photo.jpg', 'mimeType': 'image/jpeg', 'size': '4',
              'md5Checksum': 'hash', 'sha256Checksum': 'stronghash', 'parents': ['sp'],
              'version': '1', 'trashed': False, 'owners': [{'emailAddress': c.SRC_EMAIL}]}
    dest = {**source, 'id': 'd', 'parents': ['dp'], 'owners': [{'emailAddress': c.DST_EMAIL}]}
    proof = copy.deepcopy({'source': source, 'destination': dest})
    perms = [{'emailAddress': c.SRC_EMAIL, 'role': 'writer'}]
    return source, dest, proof, perms


class CleanupTests(unittest.TestCase):
    def test_changed_content_version_parent_or_owner_is_retained(self):
        for field, value in [('version','2'), ('parents',['other']), ('name','edited'),
                             ('md5Checksum','other'), ('sha256Checksum','other'),
                             ('owners',[{'emailAddress':'someone@example.test'}]), ('trashed',True)]:
            s, d, p, permissions = fixtures()
            s[field] = value
            with self.subTest(field=field), self.assertRaises(c.SafetyStop):
                validate(s,d,p,permissions,'trash')

    def test_native_and_folder_cleanup_forbidden(self):
        for mime in (c.FOLDER,c.NATIVE+'document'):
            s,d,p,permissions=fixtures()
            s['mimeType']=mime
            with self.assertRaises(c.SafetyStop):validate(s,d,p,permissions,'trash')

    def test_optional_editor_requirement(self):
        s,d,p,permissions=fixtures()
        with patch.object(c,'SHARE_SOURCE',False):validate(s,d,p,[],'trash')
        with patch.object(c,'SHARE_SOURCE',True):
            with self.assertRaises(c.SafetyStop):validate(s,d,p,[],'trash')
            validate(s,d,p,permissions,'trash')

    def test_purge_requires_trashed_source_and_live_copy(self):
        s,d,p,permissions=fixtures()
        with self.assertRaises(c.SafetyStop):validate(s,d,p,permissions,'purge')
        s['trashed']=True;p['source']['trashed']=True
        validate(s,d,p,permissions,'purge')
        d['trashed']=True
        with self.assertRaises(c.SafetyStop):validate(s,d,p,permissions,'purge')

    def test_cleanup_mutation_allowlist(self):
        api=object.__new__(CleanupSource);api.allowlist={'s'}
        api.request=Mock()
        for sid,phase in [('other','purge'),('s','emptyTrash'),(c.SRC_ROOT,'purge')]:
            with self.assertRaises(c.SafetyStop):api.mutate(sid,phase)
        api.request.assert_not_called()

    def test_mutation_timeout_not_retried(self):
        api=object.__new__(CleanupSource);api.allowlist={'s'}
        api.token={'access_token':'FAKE_TEST_TOKEN'};api.request=Mock()
        with patch('urllib.request.urlopen', side_effect=TimeoutError()) as http:
            with self.assertRaises(TimeoutError):api.mutate('s','purge')
            self.assertEqual(http.call_count,1)

    def test_durable_verified_record_survives_controller_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(c,'BASE',Path(folder)):
            db=database();record(db,'s','purge','d','verified',{'get_status':404})
            run=object.__new__(CleanupRun);run.run_id='run';run.seq=0;run.lease={'run_id':'run'}
            run.phase='purge';run.cleanup_db=db;run.cleanup_deltas=[{'source_id':'s','destination_id':'d','phase':'purge','state':'verified'}]
            run.read_controller=Mock(return_value=({}, {'lease':run.lease},'',[]))
            run.controller_write=Mock(side_effect=c.APIError(400,'revisionMismatch'))
            with self.assertRaises(c.APIError):run.checkpoint()
            self.assertEqual(db.execute('SELECT state,journaled FROM actions').fetchone(),('verified',0))
            self.assertTrue(run.cleanup_deltas)
            db.close()

    def test_gui_inputs_cannot_become_shell_commands(self):
        self.assertEqual(drive_id('https://drive.google.com/drive/folders/folder_id?x=1'),'folder_id')
        with self.assertRaises(ValueError):drive_id('$(touch injected)')
        args=command('/a path/config.json','connect','--client','file;touch bad.json')
        self.assertEqual(args[-1],'file;touch bad.json')
        self.assertIn('/a path/config.json',args)


if __name__=='__main__':unittest.main()
