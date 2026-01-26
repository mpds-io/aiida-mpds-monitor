# stub_server.py
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Parse request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        data = json.loads(body.decode('utf-8'))

        # get data
        payload = data.get('payload', '')
        status = data.get('status', '')

        # Console output
        print("\n" + "="*50)
        print("Webhook accepted:")
        print(f"   Payload: {payload}")
        print(f"   Status:  {status}")
        print("="*50 + "\n")

        # 200 OK
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Turn off default logging
        return

def main():
    server_address = ('localhost', 8080)
    httpd = HTTPServer(server_address, WebhookHandler)
    print("Stub server running at http://localhost:8080")
    print("Wating for webhooks...\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")