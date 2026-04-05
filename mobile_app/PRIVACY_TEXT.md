# AITRADER Privacy Text Draft

## Privacy Summary
AITRADER is a native shell for a user-managed trading dashboard. The app is intended for private/internal use and connects to a reachable AITRADER server.

## Data Use Draft
- The app may access network resources in order to connect to the AITRADER dashboard server.
- The app does not require consumer social login.
- The app does not provide in-app payments.
- The app is not designed to collect public user marketplace content.
- The current wrapper does not request camera, microphone, photo library, contacts, or location access.

## Suggested App Privacy Positioning
Choose only the items that are truly enabled in your production deployment.

### Likely applicable
- Diagnostics: if crash logging is later added
- Network access: yes, required to reach the dashboard server

### Likely not applicable for current wrapper
- Health data
- Contacts
- Location
- Photos
- Camera
- Microphone
- Purchases
- User-generated public content

## Review-safe Explanation
AITRADER is a private monitoring and operations app for a server-hosted trading dashboard. Any account, trade, and market data shown in the app comes from the user's connected AITRADER environment rather than direct consumer account creation inside the app.

## Plain-language Privacy Summary
- Data shown in the app comes from the connected AITRADER server.
- The wrapper app itself is mainly a native viewer and controller shell.
- Sensitive permissions should remain disabled unless a future feature explicitly requires them.
