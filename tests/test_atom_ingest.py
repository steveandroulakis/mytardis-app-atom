from django.test import TestCase
from tardis.tardis_portal.models import Dataset, Schema, User
from tardis.apps.atomimport.atom_ingest import AtomPersister, AtomWalker, AtomImportSchemas
import feedparser
from nose import SkipTest
from nose.tools import ok_, eq_
from flexmock import flexmock, flexmock_teardown
from SimpleHTTPServer import SimpleHTTPRequestHandler
import BaseHTTPServer, base64, os, inspect, SocketServer, threading, urllib2

class TestWebServer:
    '''
    Utility class for running a test web server with a given handler.
    '''

    class QuietSimpleHTTPRequestHandler(SimpleHTTPRequestHandler):
        '''
        Simple subclass that only prints output to STDOUT, not STDERR
        '''
        def log_message(self, msg, *args):
            print msg % args

        def _isAuthorized(self):
            if self.headers.getheader('Authorization') == None:
                return False
            t, creds = self.headers.getheader('Authorization').split(" ")
            if t != "Basic":
                return False
            if base64.b64decode(creds) != "username:password":
                return False
            return True

        def do_GET(self):
            if not self._isAuthorized():
                self.send_response(401, 'Unauthorized')
                self.send_header('WWW-Authenticate', 'Basic realm="Test"')
                self.end_headers()
                return
            SimpleHTTPRequestHandler.do_GET(self)

    class ThreadedTCPServer(SocketServer.ThreadingMixIn, \
                            BaseHTTPServer.HTTPServer):
        pass

    def __init__(self):
        self.handler = self.QuietSimpleHTTPRequestHandler

    def start(self):
        server = self.ThreadedTCPServer(('127.0.0.1', self.getPort()),
                                        self.handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        self.server = server
        return server.socket.getsockname()

    def getUrl(self):
        return 'http://%s:%d/' % self.server.socket.getsockname()

    @classmethod
    def getPort(cls):
        return 4272

    def stop(self):
        self.server.shutdown()
        self.server.socket.close()



class AbstractAtomServerTestCase(TestCase):

    @classmethod
    def setUpClass(cls):
        cls.priorcwd = os.getcwd()
        os.chdir(os.path.dirname(__file__)+'/atom_test')
        cls.server = TestWebServer()
        cls.server.start()
        pass

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.priorcwd)
        cls.server.stop()



class SchemaTestCase(TestCase):

    def testHasSchemas(self):
        assert inspect.ismethod(AtomImportSchemas.get_schemas)
        assert len(AtomImportSchemas.get_schemas()) > 0
        for schema in AtomImportSchemas.get_schemas():
            assert isinstance(schema, Schema)
            assert schema.namespace.find(AtomImportSchemas.BASE_NAMESPACE) > -1
            assert schema.name != None
            assert schema.type != None
            assert schema.subtype != None
            assert schema.id != None



class PersisterTestCase(AbstractAtomServerTestCase):

    def testPersisterRecognisesNewEntries(self):
        feed, entry = self._getTestEntry()
        p = AtomPersister()
        ok_(p.is_new(feed, entry), "Entry should not already be in DB")
        ok_(p.is_new(feed, entry), "New entries change only when processed")
        dataset = p.process(feed, entry)
        ok_(dataset != None)
        eq_(p.is_new(feed, entry), False, "(processed => !new) != True")


    def testPersisterCreatesDatafiles(self):
        feed, entry = self._getTestEntry()
        p = AtomPersister()
        ok_(p.is_new(feed, entry), "Entry should not already be in DB")
        dataset = p.process(feed, entry)
        datafiles = dataset.dataset_file_set.all()
        eq_(len(datafiles), 2)
        image = dataset.dataset_file_set.get(filename='abcd0001.tif')
        eq_(image.mimetype, 'application/octet-stream')
        ok_(image.url.startswith('tardis://'), "Not local: %s" % image.url)
        image = dataset.dataset_file_set.get(filename='metadata.txt')
        eq_(image.mimetype, 'text/plain')


    def testPersisterUsesTitleElements(self):
        feed, entry = self._getTestEntry()
        p = AtomPersister()
        dataset = p.process(feed, entry)
        eq_(dataset.description, entry.title)


    def testPersisterUsesAuthorNameAsUsername(self):
        # Create user to associate with dataset
        user = User(username="tatkins")
        user.save()
        feed, entry = self._getTestEntry()
        p = AtomPersister()
        dataset = p.process(feed, entry)
        eq_(dataset.experiment.created_by, user)


    def testPersisterPrefersAuthorEmailToMatchUser(self):
        # Create user to associate with dataset
        user = User(username="tatkins")
        user.save()
        # Create user to associate with dataset
        user2 = User(username="tommy", email='tatkins@example.test')
        user2.save()
        feed, entry = self._getTestEntry()
        p = AtomPersister()
        dataset = p.process(feed, entry)
        eq_(dataset.experiment.created_by, user2)


    def testPersisterHandlesMultipleDatafiles(self):
        doc = feedparser.parse('datasets.atom')
        p = AtomPersister()
        for feed, entry in map(lambda x: (doc.feed, x), doc.entries):
            ok_(p.is_new(feed, entry))
            p.process(feed, entry)
            eq_(p.is_new(feed, entry), False, "(processed => !new) != True")


    def _getTestEntry(self):
        doc = feedparser.parse('datasets.atom')
        entry = doc.entries.pop()
        # Check we're looking at the right entry
        assert entry.id.endswith('BEEFCAFE0001')
        return (doc.feed, entry)



class WalkerTestCase(AbstractAtomServerTestCase):

    def tearDown(self):
        flexmock_teardown()


    def testWalkerCredentials(self):
        '''
        Test that the walker manages credentials.
        '''
        address = 'http://localhost:%d/datasets.atom' % \
                                (TestWebServer.getPort())
        try:
            urllib2.urlopen(address)
            ok_(False, 'Should have thrown error')
        except urllib2.HTTPError:
            pass
        opener = urllib2.build_opener((AtomWalker.get_credential_handler()))
        try:
            f = opener.open(address)
            eq_(f.getcode(), 200, 'Should have been: "200 OK"')
        except urllib2.HTTPError:
            ok_(False, 'Should not have thrown error')
        pass


    def testWalkerFollowsAtomLinks(self):
        '''
        Test that the walker follows links.
        '''
        # We build a persister which says all entries are new.
        persister = flexmock(AtomPersister())
        persister.should_receive('is_new').with_args(object, object)\
            .and_return(True).times(4)
        persister.should_receive('process').with_args(object, object)\
            .times(4)
        walker = AtomWalker('http://localhost:%d/datasets.atom' %
                            (TestWebServer.getPort()),
                            persister)
        ok_(inspect.ismethod(walker.ingest),"Walker must have an ingest method")
        walker.ingest()


    def testWalkerProcessesEntriesInCorrectOrder(self):
        '''
        Test that the walker processes the entries in the revese order that it
        finds them.
        '''
        checked_entries = []
        processed_entries = []
        # We build a persister which says all entries are new.
        persister = flexmock(AtomPersister())
        # Grab the checked entry and return true
        persister.should_receive('is_new').with_args(object, object)\
            .replace_with(lambda feed, entry: checked_entries.append(entry) or True)
        # Grab the processed entry
        persister.should_receive('process').with_args(object, object)\
            .replace_with(lambda feed, entry: processed_entries.append(entry))
        parser = AtomWalker('http://localhost:%d/datasets.atom' %
                            (TestWebServer.getPort()),
                            persister)
        parser.ingest()
        # We should have checked four entries, chronologically decendent
        eq_(len(checked_entries), 4)
        checked_backwards = reduce(self._check_chronological_asc_order,\
                                     reversed(checked_entries), None)
        ok_(checked_backwards, "Entries aren't parsed in reverse chronological order")
        # We should have processed four entries, chronologically ascendent
        eq_(len(processed_entries), 4)
        processed_forwards = reduce(self._check_chronological_asc_order,\
                                     processed_entries, None)
        ok_(processed_forwards, "Entries aren't parsed in chronological order")


    def testWalkerOnlyIngestsNewEntries(self):
        '''
        Test that the walker will stop when it gets to an entry that isn't new.
        '''
        # We build a persister which says there are three entries
        # that aren't in the repository.
        persister = flexmock(AtomPersister())
        persister.should_receive('is_new').with_args(object, object)\
            .and_return(True, True, True, False).one_by_one.times(4)
        persister.should_receive('process').with_args(object, object).times(3)
        parser = AtomWalker('http://localhost:%d/datasets.atom' %
                            (TestWebServer.getPort()),
                            persister)
        parser.ingest()
        # We build a persister which says there are two entries
        # that aren't in the repository.
        persister = flexmock(AtomPersister())
        persister.should_receive('is_new').with_args(object, object)\
            .and_return(True, True, False, False).one_by_one.times(4)
        persister.should_receive('process').with_args(object, object).times(2)
        parser = AtomWalker('http://localhost:%d/datasets.atom' %
                            (TestWebServer.getPort()),
                            persister)
        parser.ingest()
        # We build a persister which says there is one entry
        # that isn't in the repository.
        persister = flexmock(AtomPersister())
        persister.should_receive('is_new').with_args(object, object)\
            .and_return(True, False, False, False).one_by_one.times(2)
        persister.should_receive('process').with_args(object, object).times(1)
        parser = AtomWalker('http://localhost:%d/datasets.atom' %
                            (TestWebServer.getPort()),
                            persister)
        parser.ingest()

    @staticmethod
    def _check_chronological_asc_order(entry_a, entry_b):
        '''
        This function checks that the Atom entries sets are in chronological
        order.
        '''
        if entry_a == False:
            return False
        if entry_a == None or entry_a.updated_parsed < entry_b.updated_parsed:
            return entry_b
        return False


