# AITRADER iPhone Native Shell

This folder contains a Capacitor-based iPhone wrapper for the existing AITRADER Python web dashboard.

## Important architecture note

The trading engine remains in the Python server.  
This iPhone app is a native shell that opens your running dashboard server.

That means:

- the iPhone app can be installed as a native iOS app
- but it still needs the AITRADER server to be reachable
- best for internal use, private deployment, and TestFlight-style distribution

## Files

- `capacitor.config.ts`: Capacitor app config
- `www/`: bundled local shell UI
- `scripts/build-web.mjs`: regenerates the local shell UI
- `scripts/ios-doctor.sh`: checks Xcode, signing, and native project readiness
- `IOS_RELEASE.md`: full iPhone / TestFlight release workflow
- `assets/`: editable icon and splash SVG sources
- `APP_STORE_METADATA.md`: App Store Connect text draft
- `PRIVACY_TEXT.md`: privacy wording draft
- `TESTFLIGHT_CHECKLIST.md`: TestFlight upload checklist

## Commands

From `mobile_app/`:

```bash
npm run build
npm run ios:doctor
npm run ios:add
npm run cap:sync
npm run cap:open:ios
```

## Build prerequisites

To generate and archive a real iPhone app, you need:

- full Apple Xcode installed
- Xcode command line tools pointing to full Xcode
- an Apple developer signing setup

At the moment this machine still points to:

```text
/Library/Developer/CommandLineTools
```

So the native wrapper project is ready, but final iPhone archive/TestFlight build still needs full Xcode activation.

## Suggested server URL

Use your Mac's LAN address, for example:

```text
http://192.168.0.10:8080
```

The iPhone and Mac should be on the same network unless you expose the server over HTTPS.

## Native app status

Completed:

- Capacitor iOS project created
- custom AITRADER app icon added
- custom splash screen added
- local shell UI added
- alternate icon concepts added in `assets/icon-options/`

Next:

1. install/activate full Xcode
2. open `ios/App.xcworkspace`
3. set signing team and bundle id
4. build to device or archive for TestFlight
