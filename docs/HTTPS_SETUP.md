# AITRADER HTTPS Setup

## Current local HTTPS
- Local HTTPS is enabled on `8443`.
- HTTP on `8080` should redirect to HTTPS.
- Current LAN URL: `https://192.168.219.193:8443`

## Local certificate note
- The current certificate is self-signed.
- On desktop browsers you may need to accept the warning once.
- On iPhone Safari you may need to tap `Show Details` and continue to the site once before adding it to the home screen.

## Recommended production setup
Use a real domain and terminate TLS at a reverse proxy.

Examples in this repo:
- `deploy/https/nginx-aitrader.conf`
- `deploy/https/Caddyfile`

## Recommended architecture
1. Public domain: `aitrader.example.com`
2. Reverse proxy on `443`
3. Proxy pass to local AITRADER app on `127.0.0.1:8443` or `127.0.0.1:8080`
4. Replace `MOBILE_SERVER_URL` with the public HTTPS URL

## Environment variables
```env
WEB_HTTPS_ENABLED=true
WEB_HTTPS_PORT=8443
WEB_SSL_CERTFILE=data/certs/web-local.crt
WEB_SSL_KEYFILE=data/certs/web-local.key
MOBILE_SERVER_URL=https://192.168.219.193:8443
```
