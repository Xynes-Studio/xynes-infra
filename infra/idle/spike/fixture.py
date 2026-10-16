"""Disposable HTTP workload. Never use this synthetic application in a release."""
import json
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

started = time.monotonic()
allocation = bytearray(48 * 1024 * 1024)
lock = threading.Lock()
journal = pathlib.Path('/tmp/fixture-journal.json')


class Fixture(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass  # Headers, query strings and request bodies are never logged.

    def do_GET(self):
        self.handle_work()

    def do_POST(self):
        self.handle_work()

    def handle_work(self):
        if self.path == '/ready':
            ready = time.monotonic() - started >= float(os.getenv('READY_DELAY', '.25'))
            self.reply(200 if ready else 503, {'ready': ready})
            return
        if self.path == '/journal':
            with lock:
                self.reply(200, json.loads(journal.read_text()) if journal.exists() else [])
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        record = {'method': self.command, 'path': self.path,
                  'authorization': self.headers.get('Authorization'),
                  'body': body.decode(), 'id': self.headers.get('X-Fixture-ID')}
        with lock:
            records = json.loads(journal.read_text()) if journal.exists() else []
            records.append(record)
            journal.write_text(json.dumps(records))
        if self.path.startswith('/long'):
            time.sleep(36)
        if self.path.startswith('/stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', '25')
            self.end_headers()
            self.wfile.write(b'data: start\n\n')
            self.wfile.flush()
            time.sleep(36)
            self.wfile.write(b'data: finish')
            return
        self.reply(200, record)

    def reply(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Fixture).serve_forever()
