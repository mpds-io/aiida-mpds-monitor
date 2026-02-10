import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs



class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Parse request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Try to parse as JSON first, then as form data
        data = {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            # If not JSON, try to parse as form data
            try:
                parsed = parse_qs(body.decode("utf-8"))
                # Convert lists to strings (form data comes as lists)
                data = {k: v[0] if v else "" for k, v in parsed.items()}
            except Exception:
                # If all parsing fails, try to get raw data
                data = {"raw": body.decode("utf-8", errors="replace")}

        # get data
        payload = data.get("payload", "")
        status = data.get("status", "")

        # Console output
        print("\n" + "=" * 50)
        print("Webhook accepted:")
        print(f"   Data:  {data}")
        print("=" * 50 + "\n")
        print(f"   Payload: {payload}")
        print(f"   Status:  {status}")
        print("=" * 50 + "\n")

        # 200 OK
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Turn off default logging
        return


def main():
    server_address = ("localhost", 8080)
    httpd = HTTPServer(server_address, WebhookHandler)
    print("Stub server running at http://localhost:8080")
    print("Wating for webhooks...\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
