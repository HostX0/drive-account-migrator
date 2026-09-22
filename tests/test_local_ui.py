import contextlib
import http.server
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from drive_migrator.gui import LocalApp, handler, checked_config
from drive_migrator.settings import DEFAULT


class LocalUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = LocalApp(Path(self.temp.name)/'config.json')
        self.config = {**DEFAULT, 'state_dir': str(Path(self.temp.name)/'state')}
        self.app.save(self.config)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app,'test-session'))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:'+str(self.server.server_address[1])

    def request(self, path, token='test-session', origin=None, body=None, host=None):
        headers={'X-Session-Token':token}
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(self.base+path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        return urllib.request.urlopen(req,timeout=3)

    def test_no_credentials_in_state_response(self):
        self.app.upload_client({'installed':{'auth_uri':'https://accounts.google.com/o/oauth2/auth',
            'token_uri':'https://oauth2.googleapis.com/token','client_id':'SYNTHETIC_ID','client_secret':'SYNTHETIC_SECRET'}})
        with self.request('/api/state') as response:
            data=response.read().decode()
        self.assertNotIn('SYNTHETIC_SECRET',data)
        self.assertNotIn('SYNTHETIC_ID',data)
        self.assertTrue(json.loads(data)['has_client'])

    def test_token_origin_and_host_required(self):
        for kwargs in ({'token':'wrong'}, {'origin':'https://hostile.example'}, {'host':'attacker.example'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/state',**kwargs)
            self.assertEqual(error.exception.code,403)

    def test_script_and_framing_policy(self):
        with self.request('/') as response:
            self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
            self.assertEqual(response.headers['X-Frame-Options'],'DENY')
            self.assertIn(b'Choose JSON file',response.read())

    def test_arbitrary_actions_and_report_paths_rejected(self):
        with patch('subprocess.Popen') as process:
            for value in ({'action':'emptyTrash'},{'action':'shell'},{'action':'connect','role':'arbitrary'}):
                with self.assertRaises(ValueError):self.app.launch(value)
            process.assert_not_called()
        with self.assertRaises(ValueError):self.app.report('../../auth/client')

    def test_cleanup_requires_reviewed_hash_and_exact_ack(self):
        path=Path(self.config['state_dir'])/'purge-plan.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'items':[],'excluded':[]}))
        with patch('subprocess.Popen') as process:
            with self.assertRaises(ValueError):self.app.launch({'action':'purge','acknowledge':'yes','plan_sha256':'bad'})
            process.assert_not_called()

    def test_config_validation_and_binding(self):
        for update in ({'share_source':'false'}, {'source_email':DEFAULT['destination_email']},
                       {'source_root_id':DEFAULT['destination_root_id']}):
            with self.assertRaises(ValueError):checked_config({**self.config,**update})
        state=Path(self.config['state_dir']);state.mkdir(parents=True,exist_ok=True)
        (state/'identity.json').write_text(json.dumps({'source_email':'another@example.test'}))
        with self.assertRaises(ValueError):self.app.save(self.config)

    def test_invalid_oauth_endpoint_not_saved(self):
        with self.assertRaises(ValueError):self.app.upload_client({'installed':{'auth_uri':'https://attacker.example/'}})
        self.assertFalse((Path(self.config['state_dir'])/'auth/client.json').exists())


if __name__=='__main__':unittest.main()
