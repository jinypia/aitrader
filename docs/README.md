# Documentation Hub

This repository has many markdown files for setup guides, implementation notes, and historical status reports.
Use this page as the single navigation entry point.

## Start Here

1. Main project overview: [../README.md](../README.md)
2. Weekly automation runbook: [../SCALPING_AUTOMATION_GUIDE.md](../SCALPING_AUTOMATION_GUIDE.md)
3. CLI dashboard user guide: [CLI_DASHBOARD.md](CLI_DASHBOARD.md)

## Canonical Operational Guides

- Scalping quickstart: [SCALPING_QUICKSTART.md](SCALPING_QUICKSTART.md)
- Scalping data guide: [SCALPING_DATA_GUIDE.md](SCALPING_DATA_GUIDE.md)
- Weekday stock selection: [WEEKDAY_STOCK_SELECTION.md](WEEKDAY_STOCK_SELECTION.md)
- Stock selection details: [STOCK_SELECTION.md](STOCK_SELECTION.md)
- Slack setup and troubleshooting: [SLACK_INTEGRATION.md](SLACK_INTEGRATION.md)
- Simulation dashboard guide: [SIMULATION_DASHBOARD.md](SIMULATION_DASHBOARD.md)
- Professional scalping upgrade notes: [PROFESSIONAL_SCALPING_UPGRADE.md](PROFESSIONAL_SCALPING_UPGRADE.md)

## Platform / Infra Guides

- HTTPS setup: [HTTPS_SETUP.md](HTTPS_SETUP.md)
- Private CA trust: [PRIVATE_CA_TRUST.md](PRIVATE_CA_TRUST.md)

## Implementation / Status Reports (Historical)

These files are useful for implementation history and handoff context, but are not the primary runbooks:

- Automation status report: [history/AUTOMATION_STATUS.md](history/AUTOMATION_STATUS.md)
- Completion report: [history/COMPLETION.md](history/COMPLETION.md)
- Simulation completion report: [history/SIMULATION_COMPLETE.md](history/SIMULATION_COMPLETE.md)
- Dashboard implementation report: [history/DASHBOARD_IMPLEMENTATION.md](history/DASHBOARD_IMPLEMENTATION.md)
- Dashboard developer guide: [history/DASHBOARD_DEVELOPER_GUIDE.md](history/DASHBOARD_DEVELOPER_GUIDE.md)

## Mobile App Docs

- App Store metadata: [../mobile_app/APP_STORE_METADATA.md](../mobile_app/APP_STORE_METADATA.md)
- iOS release guide: [../mobile_app/IOS_RELEASE.md](../mobile_app/IOS_RELEASE.md)
- Mobile app README: [../mobile_app/README.md](../mobile_app/README.md)

## Agent Specs (Automation)

- Market analyst agent: [../.github/agents/market-analyst.agent.md](../.github/agents/market-analyst.agent.md)
- Strategy simulator agent: [../.github/agents/strategy-simulator.agent.md](../.github/agents/strategy-simulator.agent.md)

## Maintenance Rules

- Put stable user-facing guides under `docs/`.
- Keep one canonical guide per feature area and avoid duplicate copies.
- Keep root-level `*_STATUS.md` / `*_COMPLETE*.md` files as historical snapshots only.
- Store full historical reports in `docs/history/` and keep root files as lightweight redirects.
- Add new docs to this index when created.
