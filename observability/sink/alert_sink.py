"""A webhook receiver for Alertmanager: records every notification and lets a test read them back.

    POST /alert    Alertmanager's webhook payload
    GET  /alerts   everything received, oldest first
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

received = []
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        with lock:
            for alert in body.get("alerts", []):
                received.append({"status": alert.get("status"), "alertname": alert["labels"].get("alertname"),
                                 "severity": alert["labels"].get("severity"), "slo": alert["labels"].get("slo"),
                                 "summary": alert.get("annotations", {}).get("summary"), "startsAt": alert.get("startsAt"),
                                 "endsAt": alert.get("endsAt")})
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        with lock:
            payload = json.dumps(received).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9099), Handler).serve_forever()
