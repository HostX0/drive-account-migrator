import copy
import json
import unittest
import sqlite3
import time
from unittest.mock import Mock
from drive_migrator.core import (all_pages, diff, parity, recover, extract_document,
                     check_controller, SafetyStop, APIError, API, FOLDER, NATIVE,
                     SRC_ROOT, DST_ROOT, SRC_EMAIL, DST_EMAIL, Worker)


def item(id, name='x', mime='image/png', size='9'):
    return {'id': id, 'name': name, 'mimeType': mime, 'size': size}


class SafetyTests(unittest.TestCase):
    def test_pagination_follows_empty_page(self):
        pages = {None: {'files': [], 'nextPageToken': 'p'}, 'p': {'files': [item('a')]}}
        self.assertEqual(len(all_pages(pages.get, 'files')),1)

    def test_incomplete_listing_never_empty(self):
        for page in [{'files': [],'incompleteSearch':True}, {}, {'files':None}]:
            with self.assertRaises(SafetyStop):all_pages(lambda _:page,'files')

    def test_403_not_empty(self):
        def fail(_):raise APIError(403,'permissionDenied')
        with self.assertRaises(APIError):all_pages(fail,'files')

    def test_repeated_token(self):
        with self.assertRaises(SafetyStop):all_pages(lambda _:{'files':[],'nextPageToken':'p'},'files')

    def test_same_name_folders_not_merged(self):
        src=[item('a','same',FOLDER),item('b','same',FOLDER)]
        matches,missing,issues=diff(src,[item('z','same',FOLDER)],{},set())
        self.assertFalse(matches or missing)
        self.assertEqual(len([i for i in issues if 'AMBIGUOUS' in i[1]]),2)

    def test_two_missing_same_name_folders_distinct(self):
        src=[item('a','same',FOLDER),item('b','same',FOLDER)]
        self.assertEqual(len(diff(src,[],{},set())[1]),2)

    def test_duplicate_files_ambiguous(self):
        self.assertFalse(diff([item('a'),item('b')],[item('z')],{},set())[1])

    def test_empty_folder(self):
        self.assertEqual(diff([],[],{},set()),([],[],[]))

    def test_native_sizes(self):
        self.assertTrue(parity(item('a',mime=NATIVE+'document',size='7'),item('b',mime=NATIVE+'document',size='99')))

    def test_user_edit_is_conflict(self):
        self.assertFalse(diff([item('a')],[item('b',size='10')],{},set())[1])

    def test_binary_checksum_mismatch(self):
        a,b=item('a'),item('b');a['md5Checksum']='aa';b['md5Checksum']='bb'
        self.assertFalse(parity(a,b))

    def test_moved_mapped_destination_no_duplicate(self):
        self.assertFalse(diff([item('a')],[],{'a':'b'},set())[1])

    def test_exact_unicode_and_whitespace(self):
        self.assertFalse(parity(item('a','x '),item('b','x')))

    def test_blocked_not_copied(self):
        self.assertFalse(diff([item('a')],[],{}, {'a'})[1])

    def test_nested_mapping_and_exception(self):
        a,b='1'+'a'*25,'1'+'b'*25
        c={'run_x':{'batch_x':{'file_mappings':{a:b},'exceptions':[{'source_id':a,'status':'POLICY_BLOCK'}]}}}
        maps,blocks,_,conflicts=recover(c)
        self.assertEqual(maps[a],b);self.assertIn(a,blocks);self.assertFalse(conflicts)

    def test_conflicting_maps_preserved(self):
        a,b,c='1'+'a'*25,'1'+'b'*25,'1'+'c'*25
        maps,_,_,conflicts=recover({'file_map':{a:b},'run_x':{'file_mappings':{a:c}}})
        self.assertNotIn(a,maps);self.assertIn(a,conflicts)

    def test_all_tabs_and_nested_tabs(self):
        def tab(s):return {'documentTab':{'body':{'content':[{'paragraph':{'elements':[{'textRun':{'content':s}}]}}]}}}
        t=tab('{"a":');t['childTabs']=[tab('1}')]
        self.assertEqual(extract_document({'revisionId':'r','tabs':[t]})[0],{'a':1})

    def test_invalid_and_duplicate_controller_json(self):
        for s in ['{','{"a":1,"a":2}']:
            d={'revisionId':'r','body':{'content':[{'paragraph':{'elements':[{'textRun':{'content':s}}]}}]}}
            with self.assertRaises((ValueError,SafetyStop)):extract_document(d)

    def test_stop_runner_lease(self):
        c={'source_email':SRC_EMAIL,'destination_email':DST_EMAIL,'source_root_id':SRC_ROOT,'destination_root_id':DST_ROOT}
        check_controller(c)
        for key,val in [('status','PAUSED_BY_USER'),('status','LOCAL_UPLOAD_IN_PROGRESS'),('status','COMPLETE'),('runner_mode','apps_script'),('lease',{'run_id':'other','expires_at':'2099-01-01T00:00:00Z'})]:
            d=copy.deepcopy(c);d[key]=val
            with self.assertRaises(SafetyStop):check_controller(d)

    def test_mutation_allowlist(self):
        api=object.__new__(API);api.role='source'
        with self.assertRaises(SafetyStop):api.request('POST','drive/files')
        api.role='destination'
        for method,resource in [('DELETE','drive/files/abc'),('PATCH','drive/files/abc'),('POST','docs/documents/other:batchUpdate')]:
            with self.assertRaises(SafetyStop):api.request(method,resource)

    def test_required_revision_and_conflict_no_retry(self):
        worker=object.__new__(Worker);worker.run_id='test';worker.seq=1
        worker.destination=Mock();worker.destination.request.side_effect=APIError(400,'revisionMismatch')
        text='{"lease":null}'
        with self.assertRaises(APIError):
            worker.controller_write({'revisionId':'fresh'},text,[('t.0',text)],{'run_id':'test'})
        self.assertEqual(worker.destination.request.call_count,1)
        sent=worker.destination.request.call_args.kwargs['data']
        self.assertEqual(sent['writeControl'],{'requiredRevisionId':'fresh'})

    def test_crash_after_copy_reconciles_before_any_retry(self):
        worker=object.__new__(Worker)
        worker.db=sqlite3.connect(':memory:')
        worker.db.execute('CREATE TABLE ops(source,parent,intent,destination,state,verification)')
        worker.db.execute('INSERT INTO ops VALUES(?,?,?,?,?,?)',('s','p','operation',None,'intent',None))
        worker.blocked=set();worker.guard=Mock();worker.ancestry=Mock()
        worker.destination=Mock();worker.destination.children.return_value=[{'id':'d','appProperties':{'migrationIntent':'operation'}}]
        worker.source=Mock();worker.source.get.return_value=item('s')
        worker.verify_new=Mock(return_value={'verified':True});worker.mapping=Mock();worker.event=Mock();worker.issue=Mock()
        worker.recover_pending()
        self.assertEqual(worker.db.execute('SELECT destination,state FROM ops').fetchone(),('d','verified'))
        worker.destination.request.assert_not_called()
        worker.destination.children.assert_called_once_with('p')
        worker.db.close()

    def test_uncertain_missing_outcome_is_not_retried(self):
        worker=object.__new__(Worker)
        worker.db=sqlite3.connect(':memory:')
        worker.db.execute('CREATE TABLE ops(source,parent,intent,destination,state,verification)')
        worker.db.execute('INSERT INTO ops VALUES(?,?,?,?,?,?)',('s','p','operation',None,'intent',None))
        worker.blocked=set();worker.guard=Mock();worker.ancestry=Mock();worker.issue=Mock()
        worker.destination=Mock();worker.destination.children.return_value=[]
        worker.recover_pending()
        self.assertIn('s',worker.blocked);worker.destination.request.assert_not_called()
        worker.db.close()


    def test_checkpoint_only_new_deltas_and_queue(self):
        worker=object.__new__(Worker)
        worker.db=sqlite3.connect(':memory:')
        worker.db.executescript("CREATE TABLE maps(source,destination); CREATE TABLE ops(source,destination,state,verification); CREATE TABLE issues(id INTEGER PRIMARY KEY,run,item,reason,detail); CREATE TABLE folders(source,destination,path,state);")
        worker.db.execute("INSERT INTO maps VALUES('s','d')")
        worker.db.execute("INSERT INTO ops VALUES('s','d','verified','{}')")
        worker.db.execute("INSERT INTO issues VALUES(1,'run','blocked','BLOCKED','null')")
        worker.db.execute("INSERT INTO folders VALUES('f','g','ALL DATA/f','pending')")
        worker.run_id='run';worker.seq=0;worker.lease={'run_id':'run'};worker.lease_owned=True
        worker.stats={};worker.current=None;worker.checkpointed_maps={};worker.checkpointed_ops={};worker.checkpointed_issue_id=0
        worker.read_controller=Mock(return_value=({}, {'lease':worker.lease}, '', []))
        worker.controller_write=Mock();worker.publish=Mock()
        worker.checkpoint()
        first=worker.controller_write.call_args.args[-1]
        self.assertEqual(first['mappings'],{'s':'d'})
        self.assertEqual(len(first['operations']),1)
        self.assertEqual(len(first['exceptions']),1)
        self.assertEqual(first['pending_folder_pairs'][0]['source_id'],'f')
        worker.checkpoint()
        second=worker.controller_write.call_args.args[-1]
        self.assertEqual(second['mappings'],{})
        self.assertEqual(second['operations'],[])
        self.assertEqual(second['exceptions'],[])
        worker.db.close()


if __name__=='__main__':unittest.main()
