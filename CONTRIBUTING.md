# Contributing to Portfolio Control

Thank you for your interest in Portfolio Control.

Contributions, bug reports, documentation improvements, feature ideas, and pull requests are welcome.

## Before You Start

Please:

1. Check existing Issues before opening a new one.
2. Avoid including personal investment data or API credentials.
3. Keep changes focused and reasonably small.
4. Explain the purpose of your change clearly.
5. Test your changes locally before submitting a Pull Request.

## Development

Portfolio Control currently uses:

* Python
* HTML / CSS / JavaScript
* SQLite
* pywebview
* Supabase for optional cloud synchronization
* Longbridge CLI for supported stock market data
* Binance public market data for cryptocurrency prices
* Public / official sources for selected financial news, calendars, and macro indicators

The application follows a local-first design. Core portfolio data should remain usable even when cloud synchronization is unavailable.

## Getting Started

Clone your fork:

```bash
git clone https://github.com/YOUR_USERNAME/Portfolio-Control.git
cd Portfolio-Control
```

Create a development branch:

```bash
git checkout -b feature/my-feature
```

For macOS development, make sure Python 3 is available.

For Windows development, use a supported Python 3 environment.

Refer to the project README and platform-specific build documentation for additional setup instructions.

## Suggested Workflow

Make your changes locally.

Before committing, review the changed files:

```bash
git status
git diff
```

Then stage and commit:

```bash
git add .
git commit -m "Add my feature"
```

Push your branch:

```bash
git push -u origin feature/my-feature
```

Then open a Pull Request against the `main` branch.

## Pull Requests

A good Pull Request should include:

* What changed
* Why the change is useful
* How it was tested
* Any known limitations
* Screenshots for UI changes when appropriate
* Relevant Issue numbers when applicable

Please avoid combining unrelated features or fixes into a single Pull Request.

## Code and UI Guidelines

When contributing to Portfolio Control:

* Preserve the local-first architecture.
* Do not require cloud connectivity for core portfolio functionality.
* Avoid introducing unnecessary external dependencies.
* Preserve compatibility with both Windows and macOS when possible.
* Keep portfolio calculations deterministic and explainable.
* Do not silently change portfolio, risk-control, or rebalancing rules.
* Clearly label estimated values and market-data limitations.
* Do not automatically execute financial trades.
* Keep the interface modern, readable, and suitable for high-DPI displays.

## Portfolio and Risk Logic

Changes involving portfolio calculations require extra care.

Please test relevant cases such as:

* Individual asset target allocations
* Asset-class target allocations
* Stocks, cryptocurrency, gold, and cash
* Internal rebalancing without using cash
* Cash-funded rebalancing
* Minimum holding quantities
* One-share positions that should not be automatically sold
* Take-profit and stop-loss rules
* Physical gold and tokenized gold
* Stablecoin valuation
* Currency conversion
* Missing or delayed market data

Any calculation that produces a trade suggestion should remain a recommendation only.

## Cloud Sync

Portfolio Control supports optional Supabase synchronization.

Cloud-sync changes should preserve these principles:

* Local SQLite remains the primary usable data store.
* The application must continue to work offline.
* Authentication credentials must not be written into portfolio exports.
* Longbridge OAuth credentials must remain local.
* API tokens must remain local unless explicitly designed otherwise.
* Synchronization conflicts must not silently destroy user data.
* Sensitive portfolio data should remain protected during synchronization.

## Security

Never commit or upload:

* `portfolio.db`
* `secrets.json`
* `.env`
* `.env.*`
* API tokens
* OAuth credentials
* Supabase secret keys
* Supabase service-role keys
* Portfolio synchronization passwords
* Personal portfolio exports
* Automatic backup files
* Personal financial information

Before submitting a Pull Request, you can inspect tracked files with:

```bash
git status
git diff --cached
```

For security-sensitive changes, please also read `SECURITY.md`.

## Third-Party Services

Portfolio Control can interact with third-party services such as Longbridge, Binance, Supabase, Marketaux, and public financial-data sources.

Contributors should:

* Follow the relevant service terms and documentation.
* Avoid committing third-party credentials.
* Avoid redistributing proprietary binaries without verifying redistribution rights.
* Prefer official and documented APIs.
* Provide graceful fallbacks when optional services are unavailable.

## Documentation

Documentation improvements are welcome.

Useful contributions include:

* Installation guides
* macOS instructions
* Windows instructions
* Cloud-sync documentation
* Troubleshooting
* Screenshots
* Translation
* Architecture documentation
* Market-data source documentation

## Areas Where Contributions Are Welcome

Some useful areas include:

* Additional market-data sources
* Portfolio analytics
* Performance attribution
* Transaction ledger support
* Automatic cost-basis calculation
* Dividend tracking
* Risk-management models
* Rebalancing improvements
* Financial-calendar improvements
* Additional macroeconomic indicators
* Additional brokers
* macOS and Windows packaging
* Translation and localization
* UI / UX improvements
* Accessibility
* Documentation
* Automated tests

## Bug Reports

When reporting a bug, please include:

* Portfolio Control version
* Operating system and version
* Whether the issue occurs on Windows, macOS, or both
* Steps to reproduce
* Expected behavior
* Actual behavior
* Relevant error messages

Please remove all personal portfolio data and credentials from screenshots and logs before posting them.

## Feature Requests

Feature requests are welcome.

Please describe:

* The problem you are trying to solve
* How the feature would improve Portfolio Control
* A possible workflow or UI
* Whether the feature affects portfolio calculations, privacy, or cloud synchronization

## License

Portfolio Control is licensed under the MIT License.

By contributing to Portfolio Control, you agree that your contributions will be licensed under the same MIT License.

