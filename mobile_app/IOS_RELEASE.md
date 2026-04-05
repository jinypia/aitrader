# AITRADER iPhone Release Guide

This guide takes the Capacitor wrapper from local project to a real iPhone install or TestFlight build.

## 1. Install full Xcode

The current machine still points to Apple Command Line Tools only.  
To build a native iPhone app, install full Xcode from the App Store.

After installation:

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -runFirstLaunch
```

Then verify:

```bash
cd /Users/superarchi/aitrader/mobile_app
npm run ios:doctor
```

## 2. Open the iOS project

```bash
cd /Users/superarchi/aitrader/mobile_app
npm run cap:open:ios
```

This opens the native workspace in Xcode.

## 3. Configure signing

Inside Xcode:

1. Open the `App` target.
2. Set `Bundle Identifier`.
   Suggested default:
   - `com.superarchi.aitrader`
3. Choose your Apple Developer `Team`.
4. Let Xcode manage signing automatically unless you have a custom signing policy.

## 4. Build to a real iPhone

1. Connect the iPhone by cable or use the same Apple ID for wireless deploy.
2. Select your device from the Xcode scheme/device selector.
3. Press Run.

At first launch on the phone, trust the developer profile if iOS asks.

## 5. Prepare TestFlight

Recommended release checklist:

1. App icon and splash are already customized in:
   - `ios/App/App/Assets.xcassets/AppIcon.appiconset`
   - `ios/App/App/Assets.xcassets/Splash.imageset`
2. Update app metadata in App Store Connect:
   - app name
   - subtitle
   - privacy details
   - screenshots
3. Confirm the remote dashboard URL strategy:
   - local LAN URL for internal testing
   - HTTPS public URL for broader access

## 6. Archive and upload

In Xcode:

1. Choose `Any iOS Device (arm64)` or a generic iPhone device.
2. `Product` -> `Archive`
3. In Organizer:
   - `Distribute App`
   - `App Store Connect`
   - `Upload`

Then finish TestFlight setup in App Store Connect.

## 7. Runtime architecture reminder

This app is a native shell around the AITRADER Python dashboard.  
The trading engine still runs on the server side.

That means:

- the iPhone app is native
- but it still needs a reachable dashboard server URL
- for external distribution, a stable HTTPS endpoint is strongly recommended

## 8. Useful commands

```bash
cd /Users/superarchi/aitrader/mobile_app
npm run build
npm run cap:sync
npm run ios:doctor
npm run cap:open:ios
```
