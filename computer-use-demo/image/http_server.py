import os
import socket
from http.server import HTTPServer, SimpleHTTPRequestHandler


INDEX_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Computer Use Demo</title>
    <style>
      body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica, Arial, 'Apple Color Emoji', 'Segoe UI Emoji'; }
      header { padding: 10px 16px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: center; }
      main { display: grid; grid-template-columns: 400px 1fr; height: calc(100vh - 52px); }
      .left { border-right: 1px solid #eee; padding: 12px; overflow: auto; }
      .right { height: 100%; }
      .row { margin-bottom: 8px; }
      label { display: block; font-size: 12px; color: #333; margin-bottom: 4px; }
      input, select { width: 100%; padding: 8px; }
      textarea { width: 100%; min-height: 80px; padding: 8px; }
      button { padding: 8px 12px; cursor: pointer; }
      #log { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace; background: #fafafa; padding: 8px; height: 200px; overflow: auto; }
      #vnc { width: 100%; height: 100%; border: 0; }
    </style>
  </head>
  <body>
    <header>
      <div><strong>Computer Use Demo</strong> — FastAPI</div>
      <div>
        <a id="openVnc" href="#" target="_blank">Open VNC in new tab</a>
      </div>
    </header>
    <main>
      <section class="left">
        <div class="row">
          <label>Session</label>
          <div style="display:flex; gap:8px">
            <button id="create">Create</button>
            <button id="delete" disabled>Delete</button>
          </div>
          <div id="sessionId" style="margin-top:6px; font-size:12px; color:#666"></div>
        </div>
        <div class="row">
          <label>Message</label>
          <textarea id="msg" placeholder="Type a message for Claude to control the computer..."></textarea>
          <div style="display:flex; gap:8px; margin-top:6px">
            <button id="send" disabled>Send</button>
          </div>
        </div>
        <div class="row">
          <label>Log</label>
          <div id="log"></div>
        </div>
      </section>
      <section class="right">
        <iframe id="vnc"></iframe>
      </section>
    </main>

    <script>
      const log = (m) => { const el = document.getElementById('log'); el.textContent += `\n${m}`; el.scrollTop = el.scrollHeight; };
      let sessionId = null;
      let ws = null;
      const API_BASE = `${location.protocol}//${location.hostname}:9000`;
      const VNC_BASE = `${location.protocol}//${location.hostname}:6080`;
      document.getElementById('vnc').src = `${VNC_BASE}/vnc.html?resize=scale&autoconnect=1&view_only=1`;
      document.getElementById('openVnc').href = `${VNC_BASE}/vnc.html`;

      const setSession = (id) => {
        sessionId = id;
        document.getElementById('sessionId').textContent = id ? `ID: ${id}` : '';
        document.getElementById('delete').disabled = !id;
        document.getElementById('send').disabled = !id;
      };

      document.getElementById('create').onclick = async () => {
        try {
          const res = await fetch(`${API_BASE}/sessions`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({}) });
          if (!res.ok) {
            const body = await res.text();
            log(`create error: HTTP ${res.status} ${res.statusText} ${body}`);
            return;
          }
          const data = await res.json();
          if (!data || !data.id) {
            log('create error: invalid response');
            return;
          }
          setSession(data.id);
          log('Session created');
          if (ws) { try { ws.close(); } catch (e) {} }
          const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
          ws = new WebSocket(`${wsProto}://${location.hostname}:9000/ws/${data.id}`);
          ws.onopen = () => {
            log('ws open');
          };
          ws.onmessage = (ev) => {
            try {
              const msg = JSON.parse(ev.data);
              if (msg.type === 'history') {
                log(`[history] ${msg.messages.length} messages`);
              } else if (msg.type === 'assistant_block') {
                log(`[assistant] ${JSON.stringify(msg.block)}`);
              } else if (msg.type === 'tool_result') {
                log(`[tool] ${msg.error ? 'ERROR: ' + msg.error : (msg.output || '(image)')}`);
              } else if (msg.type === 'api_exchange') {
                log(`[api] status=${msg.status} error=${msg.error}`);
              } else if (msg.type === 'done') {
                log(`[done]`);
              } else if (msg.type === 'error') {
                log(`[error] ${msg.message}`);
              }
            } catch (e) { log('ws parse error: ' + e.message) }
          };
          ws.onclose = () => log('ws closed');
          ws.onerror = (e) => log('ws error');
        } catch (e) { log('create error: ' + e.message) }
      };

      document.getElementById('delete').onclick = async () => {
        if (!sessionId) return;
        try {
          await fetch(`${API_BASE}/sessions/${sessionId}`, { method: 'DELETE' });
          setSession(null);
          if (ws) { try { ws.close(); } catch (e) {} ws = null; }
          log('Session deleted');
        } catch (e) { log('delete error: ' + e.message) }
      };

      document.getElementById('send').onclick = async () => {
        if (!sessionId) return;
        const text = document.getElementById('msg').value.trim();
        if (!text) return;
        document.getElementById('msg').value = '';
        log('you: ' + text);
        try {
          await fetch(`${API_BASE}/sessions/${sessionId}/messages`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text }) });
        } catch (e) { log('send error: ' + e.message) }
      };
    </script>
  </body>
  </html>
"""


class HTTPServerV6(HTTPServer):
    address_family = socket.AF_INET6


class RootHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
        else:
            # For everything else, serve files from static_content
            self.path = "/static_content" + (self.path if self.path.startswith('/') else '/' + self.path)
            return super().do_GET()


def run_server():
    os.chdir(os.path.dirname(__file__))
    server_address = ("::", 8080)
    httpd = HTTPServerV6(server_address, RootHandler)
    print("Starting HTTP server on port 8080...")  # noqa: T201
    httpd.serve_forever()


if __name__ == "__main__":
    run_server()
