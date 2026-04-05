import fs from 'node:fs/promises';
import path from 'node:path';

const root = path.resolve(process.cwd(), 'www');

await fs.mkdir(root, { recursive: true });

const files = {
  'index.html': `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <meta name="theme-color" content="#0b1524" />
    <title>AITRADER iPhone</title>
    <link rel="stylesheet" href="./styles.css" />
  </head>
  <body>
    <div class="app-shell">
      <header class="hero">
        <div class="eyebrow">AITRADER</div>
        <h1>Native iPhone Shell</h1>
        <p class="sub">
          Connect your iPhone app to the running AITRADER web server. Because the trading engine is Python-based,
          this iPhone app works as a native wrapper around your live dashboard.
        </p>
      </header>

      <main class="layout">
        <section class="card config-card">
          <div class="section-title">Server Connection</div>
          <label class="field">
            <span>AITRADER Server URL</span>
            <input id="serverUrl" type="url" placeholder="http://192.168.0.10:8080" />
          </label>
          <div class="button-row">
            <button id="connectBtn" class="primary">Open Dashboard</button>
            <button id="saveBtn" class="secondary">Save URL</button>
            <button id="resetBtn" class="ghost">Reset</button>
          </div>
          <div class="hint">
            Use your Mac's LAN IP, for example <code>http://192.168.x.x:8080</code>. The iPhone and Mac should be on
            the same network unless you expose the server through HTTPS.
          </div>
          <div class="summary-grid">
            <div class="summary-item">
              <div class="summary-label">Mode</div>
              <div class="summary-value">Native shell + remote Python server</div>
            </div>
            <div class="summary-item">
              <div class="summary-label">Deep Link</div>
              <div class="summary-value">aitrader://connect?server=...</div>
            </div>
            <div class="summary-item">
              <div class="summary-label">Best For</div>
              <div class="summary-value">Internal install, TestFlight prep, operator dashboard</div>
            </div>
          </div>
          <div id="status" class="status">Set the server URL and open the dashboard.</div>
        </section>

        <section class="card preview-card">
          <div class="section-title">Live Preview</div>
          <div class="iframe-wrap">
            <iframe id="dashboardFrame" title="AITRADER Dashboard" referrerpolicy="no-referrer"></iframe>
          </div>
        </section>
      </main>
    </div>

    <script type="module" src="./app.js"></script>
  </body>
</html>
`,
  'styles.css': `:root {
  color-scheme: dark;
  --bg: #07111d;
  --panel: #0e1728;
  --panel-2: #13243b;
  --line: #294565;
  --ink: #eef5ff;
  --muted: #9eb3cf;
  --green: #43d39c;
  --amber: #f0b35e;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "SF Pro Display", "Segoe UI", system-ui, sans-serif;
  background: radial-gradient(circle at top, #143154 0%, #08111d 42%, #050b14 100%);
  color: var(--ink);
}
.app-shell {
  min-height: 100vh;
  padding: max(18px, env(safe-area-inset-top)) 18px max(18px, env(safe-area-inset-bottom));
  display: grid;
  gap: 14px;
}
.hero, .card {
  border: 1px solid var(--line);
  border-radius: 22px;
  background: linear-gradient(180deg, rgba(14, 23, 40, 0.96), rgba(8, 17, 29, 0.94));
  box-shadow: 0 18px 42px rgba(0, 0, 0, 0.24);
}
.hero {
  padding: 18px 18px 16px;
}
.eyebrow {
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.18em;
  color: #9ec8ff;
  text-transform: uppercase;
}
h1 {
  margin: 8px 0 10px;
  font-size: 30px;
  line-height: 1.02;
}
.sub {
  margin: 0;
  color: var(--muted);
  line-height: 1.55;
  font-size: 14px;
}
.layout {
  display: grid;
  grid-template-columns: minmax(300px, 420px) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}
.card {
  padding: 16px;
}
.section-title {
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #9ec8ff;
  margin-bottom: 12px;
  font-weight: 800;
}
.field {
  display: grid;
  gap: 6px;
}
.field span {
  color: var(--muted);
  font-size: 12px;
}
.field input {
  width: 100%;
  border-radius: 14px;
  border: 1px solid #34557a;
  background: #0b1626;
  color: var(--ink);
  padding: 12px 14px;
  font-size: 15px;
}
.button-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
button {
  border: 0;
  border-radius: 999px;
  padding: 11px 16px;
  font-weight: 800;
  font-size: 13px;
}
button.primary {
  background: linear-gradient(145deg, #2f8fff, #1f5dbe);
  color: white;
}
button.secondary {
  background: linear-gradient(145deg, #1e8b67, #146247);
  color: #e8fff5;
}
button.ghost {
  background: #142338;
  color: #d8e7ff;
  border: 1px solid #34557a;
}
.hint {
  margin-top: 12px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.summary-grid {
  margin-top: 12px;
  display: grid;
  gap: 8px;
}
.summary-item {
  border: 1px solid #233a57;
  border-radius: 16px;
  padding: 10px 12px;
  background: #0a1422;
}
.summary-label {
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.summary-value {
  margin-top: 4px;
  font-weight: 700;
  line-height: 1.45;
}
.status {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 14px;
  background: #0a1422;
  color: #dfe9f8;
  border: 1px solid #233a57;
  min-height: 48px;
}
.status.ok { border-color: rgba(67, 211, 156, 0.45); color: #d7fff1; }
.status.warn { border-color: rgba(240, 179, 94, 0.45); color: #ffe7c3; }
.iframe-wrap {
  border-radius: 18px;
  overflow: hidden;
  border: 1px solid #233a57;
  min-height: calc(100vh - 210px);
  background: #08111d;
}
iframe {
  width: 100%;
  min-height: calc(100vh - 210px);
  border: 0;
  background: #08111d;
}
code {
  background: #142338;
  padding: 2px 6px;
  border-radius: 8px;
}
@media (max-width: 980px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .iframe-wrap, iframe {
    min-height: 60vh;
  }
}
`,
  'app.js': `import { App } from '@capacitor/app';

const STORAGE_KEY = 'aitrader_server_url_v1';

const serverUrlInput = document.getElementById('serverUrl');
const connectBtn = document.getElementById('connectBtn');
const saveBtn = document.getElementById('saveBtn');
const resetBtn = document.getElementById('resetBtn');
const frame = document.getElementById('dashboardFrame');
const statusEl = document.getElementById('status');

const setStatus = (message, kind = 'info') => {
  statusEl.textContent = message;
  statusEl.className = 'status' + (kind === 'ok' ? ' ok' : kind === 'warn' ? ' warn' : '');
};

const normalizeUrl = (raw) => {
  const value = (raw || '').trim();
  if (!value) return '';
  if (/^https?:\\/\\//i.test(value)) return value.replace(/\\/$/, '');
  return 'http://' + value.replace(/\\/$/, '');
};

const parseIncomingServer = (urlLike) => {
  try {
    const parsed = new URL(urlLike);
    return normalizeUrl(parsed.searchParams.get('server') || '');
  } catch (_err) {
    return '';
  }
};

const applyServerUrl = (raw, message) => {
  const normalized = normalizeUrl(raw);
  if (!normalized) return '';
  localStorage.setItem(STORAGE_KEY, normalized);
  serverUrlInput.value = normalized;
  frame.src = normalized;
  setStatus(message || 'Dashboard connected. You can use the app like a native shell now.', 'ok');
  return normalized;
};

const loadSavedUrl = () => {
  const saved = localStorage.getItem(STORAGE_KEY) || '';
  if (saved) {
    applyServerUrl(saved, 'Saved server loaded. Tap "Open Dashboard" if you update the address.');
  } else {
    setStatus('Set the server URL and open the dashboard.');
  }
};

const saveUrl = () => {
  const normalized = normalizeUrl(serverUrlInput.value);
  if (!normalized) {
    setStatus('Server URL is empty. Enter your Mac server address first.', 'warn');
    return '';
  }
  localStorage.setItem(STORAGE_KEY, normalized);
  serverUrlInput.value = normalized;
  setStatus('Server URL saved for the next launch.', 'ok');
  return normalized;
};

connectBtn.addEventListener('click', () => {
  const normalized = saveUrl();
  if (!normalized) return;
  frame.src = normalized;
  setStatus('Opening AITRADER dashboard in the native shell...', 'ok');
});

saveBtn.addEventListener('click', () => {
  saveUrl();
});

resetBtn.addEventListener('click', () => {
  localStorage.removeItem(STORAGE_KEY);
  serverUrlInput.value = '';
  frame.removeAttribute('src');
  setStatus('Saved URL removed. Enter a new server URL to continue.', 'warn');
});

frame.addEventListener('load', () => {
  const current = frame.getAttribute('src') || '';
  if (current) setStatus('Dashboard connected. You can use the app like a native shell now.', 'ok');
});

App.addListener('appUrlOpen', ({ url }) => {
  const incoming = parseIncomingServer(url);
  if (!incoming) return;
  applyServerUrl(incoming, 'Server address received from the install page. Dashboard opened automatically.');
});

const initialIncoming = parseIncomingServer(globalThis.location?.href || '');
if (initialIncoming) {
  applyServerUrl(initialIncoming, 'Server address received on app launch. Dashboard opened automatically.');
} else {
  loadSavedUrl();
}
`,
};

await Promise.all(
  Object.entries(files).map(async ([name, content]) => {
    await fs.writeFile(path.join(root, name), content, 'utf8');
  }),
);

console.log('mobile_app/www refreshed');
