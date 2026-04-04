# AITRADER Private CA Trust Guide

## Files
- Root CA: `data/certs/private_ca/aitrader-root-ca.crt`
- Server cert: `data/certs/private_ca/aitrader-web.crt`

## iPhone trust steps
1. Send `aitrader-root-ca.crt` to your iPhone (AirDrop, email, Files, etc.)
2. Open the file on iPhone
3. Tap `Allow` if profile install is requested
4. Go to `Settings > General > VPN & Device Management` and install the profile
5. Go to `Settings > General > About > Certificate Trust Settings`
6. Enable full trust for `AITRADER Private Root CA`
7. Reopen Safari and access the dashboard again

## macOS trust steps
1. Open `data/certs/private_ca/aitrader-root-ca.crt`
2. Add it to `System` or `login` keychain
3. Set trust to `Always Trust`
4. Restart the browser

## Important note
The certificate includes these names:
- `superarchi.servecounterstrike.com`
- `localhost`
- `127.0.0.1`
- `192.168.219.193`

The domain itself currently resolves to a public IP different from this Mac, so the private certificate helps only after you trust the CA and connect to the actual server address you control.
