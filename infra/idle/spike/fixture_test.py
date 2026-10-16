"""Exercise real fixture HTTP parsing without opening ports or Docker."""
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('fixture', pathlib.Path(__file__).with_name('fixture.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class Socket:
    def __init__(self, wire):
        self.input = io.BytesIO(wire)
        self.output = io.BytesIO()

    def makefile(self, _mode, *_args):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class FixtureHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        fixture.journal = pathlib.Path(self.directory.name) / 'journal.json'

    def tearDown(self):
        self.directory.cleanup()

    def dispatch(self, method='GET', path='/echo?q=retained', body=b'', extra=''):
        wire = (f'{method} {path} HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n'
                f'Content-Length: {len(body)}\r\n{extra}\r\n').encode() + body
        sock = Socket(wire)
        fixture.Fixture(sock, ('127.0.0.1', 0), object())
        header, payload = sock.output.getvalue().split(b'\r\n\r\n', 1)
        return header, json.loads(payload)

    def test_cold_post_preserves_wire_contract_and_records_one_delivery(self):
        header, value = self.dispatch('POST', '/callback?state=retained', b'{"synthetic":true}',
                                      'Authorization: Bearer fixture-only\r\nX-Fixture-ID: one\r\n')
        self.assertIn(b'200 OK', header)
        self.assertEqual(value, {'method': 'POST', 'path': '/callback?state=retained',
                                'body': '{"synthetic":true}', 'authorization': 'Bearer fixture-only', 'id': 'one'})
        self.assertEqual(len(json.loads(fixture.journal.read_text())), 1)

    def test_readiness_and_journal_queries_do_not_count_as_user_work(self):
        self.dispatch(path='/ready')
        self.assertFalse(fixture.journal.exists())
        _, journal = self.dispatch(path='/journal')
        self.assertEqual(journal, [])
        self.assertFalse(fixture.journal.exists())

    def test_journal_survives_separate_requests_and_records_get_and_post(self):
        self.dispatch('GET')
        self.dispatch('POST', body=b'body')
        _, journal = self.dispatch(path='/journal')
        self.assertEqual([item['method'] for item in journal], ['GET', 'POST'])
        self.assertEqual(journal[1]['body'], 'body')


if __name__ == '__main__':
    unittest.main()
