# AITRADER TestFlight Checklist

## Before Xcode Archive
- Full Xcode installed
- `xcode-select` points to full Xcode
- Apple Developer Team selected in Xcode
- Bundle Identifier confirmed
- Version set to `1.0.0`
- Build number set to `1`
- App icon confirmed
- Splash screen confirmed
- Remote dashboard URL strategy confirmed
- HTTPS endpoint preferred for external testing

## Xcode Checks
- Signing & Capabilities shows a valid Team
- No red signing errors
- Deployment target set appropriately
- App launches on a real iPhone
- Web shell can save and load server URL
- Dashboard opens correctly from the app shell

## TestFlight Submission Checks
- App Name set in App Store Connect
- Subtitle set
- Description pasted
- Privacy details reviewed
- Screenshots prepared
- Review notes added
- Export compliance answered if prompted

## Functional Smoke Test
- Open app
- Enter dashboard server URL
- Save URL
- Reload app
- Confirm URL persists
- Confirm dashboard loads
- Confirm key tabs render correctly
- Confirm no immediate blank screen or navigation block

## After Upload
- Wait for processing in App Store Connect
- Add internal testers first
- Verify install from TestFlight
- Validate server connectivity outside local development environment
