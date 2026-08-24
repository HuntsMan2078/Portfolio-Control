# Security Policy

Portfolio Control handles financial portfolio information and therefore treats data privacy and credential security as important design considerations.

## Supported Versions

Security fixes are currently provided for the latest released version of Portfolio Control.

Users are encouraged to update to the newest available release.

## Reporting a Vulnerability

If you discover a security vulnerability in Portfolio Control, please do not disclose it publicly in a GitHub Issue.

Use GitHub's private vulnerability reporting or Security Advisory feature when available.

When reporting a vulnerability, please include:

* A clear description of the issue
* Steps to reproduce the issue
* The affected Portfolio Control version
* The affected operating system
* The potential security impact
* Any suggested mitigation, if available

Please avoid including real financial data or credentials unless absolutely necessary.

## Local-First Security Model

Portfolio Control is designed as a local-first application.

Core portfolio information is stored locally using SQLite.

The application should remain usable even when cloud synchronization is unavailable.

Depending on the configured features, local data may include:

* Portfolio holdings
* Asset allocation targets
* Cost basis
* Profit and loss
* Risk-control settings
* Rebalancing settings
* Calendar events
* Investment notes
* Watchlists
* Decision logs
* Portfolio history

## Cloud Synchronization

Cloud synchronization is optional.

When enabled, Portfolio Control can use Supabase as a synchronization layer between devices.

The local SQLite database remains available on each device.

Cloud synchronization should not require users to upload their raw local SQLite database file.

Sensitive credentials should remain local.

## Credentials and Secrets

The following information must never be committed to the source repository:

* Longbridge OAuth credentials
* Longbridge application secrets
* Marketaux API tokens
* Supabase secret keys
* Supabase service-role keys
* Supabase account passwords
* Portfolio synchronization passwords
* Personal access tokens
* Private cryptographic keys
* `.env` secrets
* `secrets.json`

Portfolio Control uses local secret storage for supported credentials.

## AI JSON Exports

Portfolio Control can export portfolio information as JSON for analysis by AI systems.

AI exports may contain sensitive financial information, including:

* Holdings
* Portfolio value
* Cost basis
* Profit and loss
* Risk settings
* Watchlists
* Investment notes
* Decision history
* Portfolio allocation

Users should review exported files before sharing them with third parties.

Credentials and API secrets should not be included in AI exports.

## Git Repository Safety

The repository is configured to exclude common sensitive files such as:

```text
portfolio.db
*.db
*.db-wal
*.db-shm
secrets.json
.env
.env.*
backups/
exports/
build/
dist/
release/
release-macos/
```

Contributors should verify staged files before committing:

```bash
git status
git diff --cached --name-only
```

Credential scanning tools such as Gitleaks are also recommended before public releases.

Example:

```bash
gitleaks git .
```

## Never Share These Publicly

Do not publish the following in GitHub Issues, Discussions, Pull Requests, screenshots, logs, or documentation:

* Real portfolio databases
* Full AI portfolio exports containing personal data
* Longbridge OAuth tokens
* Supabase secret/service-role keys
* Marketaux API tokens
* Portfolio synchronization passwords
* Authentication tokens
* Private financial records

Always redact sensitive values before sharing logs or screenshots.

## Market Data

Portfolio Control may use third-party or official market-data sources.

Market data can be:

* Delayed
* Temporarily unavailable
* Incorrect
* Incomplete
* Subject to provider limitations

Portfolio Control should preserve the last valid value or clearly indicate unavailable data rather than silently replacing valid portfolio data with invalid values.

Market data must not be treated as guaranteed real-time information unless the underlying provider explicitly supports it.

## Rebalancing and Risk Recommendations

Portfolio Control can calculate:

* Allocation differences
* Rebalancing suggestions
* Take-profit suggestions
* Stop-loss suggestions
* Approximate sell quantities
* Approximate buy quantities

These calculations are analytical tools only.

Portfolio Control does not guarantee that suggested transactions are suitable, executable, or financially optimal.

Users remain responsible for all investment decisions.

## Trade Execution

Portfolio Control is currently designed as an analysis and portfolio-management tool.

It should not automatically execute investment trades without an explicit, separately designed security architecture and user authorization process.

## Backup Security

Automatic backups may contain sensitive portfolio data.

Backup directories should remain private.

Users should be careful when synchronizing backup folders through third-party cloud-storage services.

Directly synchronizing a live SQLite database through generic file-sync tools may result in conflicts or corruption and is not recommended.

## Third-Party Services

Portfolio Control may integrate with services including:

* Longbridge
* Binance
* Supabase
* Marketaux
* Federal Reserve data sources
* HKMA data sources
* US Treasury data sources
* Other public financial-data providers

The security, availability, and privacy practices of these third-party services are outside the direct control of Portfolio Control.

Users should review the terms and privacy policies of services they choose to enable.

## Dependency Security

Contributors should avoid unnecessary dependencies.

When adding dependencies:

* Prefer actively maintained projects.
* Pin versions where appropriate.
* Review the dependency's security history.
* Avoid packages that unnecessarily collect user information.
* Avoid packages that require excessive permissions.

## Security Updates

If a vulnerability is confirmed, maintainers may:

* Patch the current release
* Publish a security advisory
* Rotate affected credentials
* Recommend users upgrade immediately
* Disable affected integrations until a fix is available

## Disclaimer

Portfolio Control is provided under the MIT License without warranty.

It is a portfolio-management and analysis application and does not constitute financial, investment, tax, or legal advice.

Users are responsible for protecting their devices, accounts, credentials, backups, and financial information.

