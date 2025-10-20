# stub_server.py
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Parse query parameters
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)

        # get data
        payload = query_params.get('payload', [''])[0]
        status = query_params.get('status', [''])[0]

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