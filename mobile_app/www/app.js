import { App } from '@capacitor/app';

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
  if (/^https?:\/\//i.test(value)) return value.replace(/\/$/, '');
  return 'http://' + value.replace(/\/$/, '');
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
