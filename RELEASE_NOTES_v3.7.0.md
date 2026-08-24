# Portfolio Control v3.7.0

## Demo Mode, Multilingual UI & Personalization

### New

- **Demo Mode** with a fully synthetic multi-asset portfolio for screenshots, demos and first-run exploration.
- Demo state is isolated from the real portfolio state in the UI layer.
- Demo edits are kept in session storage only; they are not written to the real SQLite database.
- Database beacon saves, automatic quote writes and Supabase sync actions are blocked while Demo Mode is active.
- A visible **DEMO MODE** banner makes screenshots clearly identifiable as synthetic data.

### Languages

- Simplified Chinese (`zh-CN`)
- Traditional Chinese / Hong Kong (`zh-HK`)
- English (`en`)

Language preference is stored locally on each device and is not included in portfolio cloud sync.
User-authored investment notes, thesis text and decision-journal content are not automatically rewritten when the interface language changes.

### Themes

Six built-in themes:

1. Ocean Blue
2. Emerald
3. Graphite
4. Midnight
5. Burgundy
6. Warm Sand

Theme preference is also device-local and independent from portfolio data.

### Public Demo Dataset

`demo_portfolio.json` contains synthetic holdings, watchlist items, history, macro data, calendar entries and financial-news examples. It contains no real portfolio or credential data.

### Packaging

- Version bumped to `3.7.0` in the Python backend, macOS bundle metadata, manifest and Windows version-info resource.
- macOS PyInstaller spec now includes `demo_portfolio.json`.

### Notes

- Demo market prices and macro/news/calendar data are intentionally synthetic and locked for presentation consistency.
- External third-party content may remain in its original language; Portfolio Control's existing translation workflow remains available for supported news content.

## Multi-currency base

- Added HKD, USD, CNY, EUR, GBP, SGD, JPY, AUD, CAD and CHF portfolio display currencies.
- Base currency is a local UI preference and does not alter native asset quote currencies.
- Portfolio totals, P&L, history, risk suggestions, simulator and value-based rebalancing inputs follow the selected base currency.
- Existing HKD accounting remains backward compatible internally.
- Physical-gold purchase and liquidation values can now record their own currencies while legacy HKD fields remain readable.
- Demo workspace and demo news content now follow the selected interface language.

## Internationalization polish

- Completed an English UI audit for dynamically generated status, risk, macro, sync, quote and system messages.
- Fixed mixed-language output such as `已同步`, `需处理`, `Data日`, `未Settings`, and Chinese risk-alert suffixes appearing in English mode.
- Dynamic translation now applies longer phrases before shorter fragments to prevent partial translations.
- Demo Mode cloud status is now always localized and cannot be overwritten by asynchronous real cloud status updates.
- Macro change labels use `1D / 7D / 30D` in English.
- System dialogs (`alert` / `confirm`) follow the selected interface language.
- User-entered notes, asset names and third-party source content continue to remain unchanged.

## RC3 i18n hardening

- Completed English localization for asset-entry and asset-class target dialogs.
- Completed English localization for P&L explanatory text and generated rebalancing content.
- Dynamic dialogs now re-apply localization after content is generated.
- Protected user asset names while translating system-generated rebalance messages.
- Fixed mixed Chinese/English placeholders and descriptions in English mode.
