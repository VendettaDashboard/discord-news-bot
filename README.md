# Weekly Discord Red-Folder News

A scheduled GitHub Actions job posts one compact economic-calendar card to Discord every Monday at **06:00 Europe/Berlin**. The owner's computer and Codex do not need to run after the cloud deployment has been activated and tested.

## Current deployment status

The public cloud repository is deployed. All 57 current tests passed on the standard GitHub Ubuntu runner. An actual cloud trial updated the existing Discord weekly card, and the Discord message was read back and verified: one embed, five day sections, four events. Encrypted remote state was verified with no unresolved reservation.

Investing.com returned HTTP 403 from the GitHub runner, while Forex Factory succeeded. The owner explicitly authorized automatic Forex Factory delivery during this access denial. ALLOW_FOREX_FACTORY_FALLBACK=true is enabled in the cloud workflow. The real updated-policy diagnostic passed: the current weekly Forex Factory feed was parsed, filtered and rendered into five day sections using the authorized fallback. A new cloud trial also updated the same existing next-week Discord card, verified by reading back its content and encrypted state. The next-week trial uses separately prepared data; the source diagnostic validates the current feed and does not claim future data has already been fetched.

Deployment evidence: [57 Linux tests](https://github.com/VendettaDashboard/discord-news-bot/actions/runs/43431323990), [successful live fallback diagnostic](https://github.com/VendettaDashboard/discord-news-bot/actions/runs/43431389215), [actual Discord cloud trial](https://github.com/VendettaDashboard/discord-news-bot/actions/runs/43431393963). The first scheduled production run is planned for Monday, 16 November 2026 at 06:00 Europe/Berlin and is not claimed as already executed.

## Calendar rules

Eight currencies: USD, EUR, GBP, JPY, CHF, CAD, AUD, NZD. The default profile includes CPI, actual central-bank rate decisions and official US Nonfarm Payrolls only. National EUR releases, unemployment rates, ADP, speeches, statements and minutes are excluded. Related CPI variants are merged without mixing numbers from different source rows.

Investing.com and Forex Factory are attempted on every unsent weekly run. When both are reachable, they are compared and corrected Investing timestamps have priority. Dates are converted to Europe/Berlin, including daylight-saving changes. Different CPI measurement variants are recorded rather than compared as the same measure. Unresolved date or event-coverage differences stop publication.

The owner-authorized exception applies only when Investing returns HTTP 403 and Forex Factory provides a valid, current full-week feed. The job then uses Forex Factory and records forex_factory_fallback rather than claiming a successful two-source comparison. Other Investing failures, malformed calendars, stale feeds and missing Forex Factory data still block. Empty news days are rendered only after a valid calendar was obtained. Standalone installations default to the strict policy unless ALLOW_FOREX_FACTORY_FALLBACK=true is explicitly set.

**Known source limitation:** Investing.com currently blocks direct requests with HTTP 403 on both the local PC and GitHub. The approved Forex Factory fallback handles this specific denial. It cannot guarantee delivery if Forex Factory is unavailable or has not yet updated its weekly feed. An inaccessible calendar is never reported as a successful two-source check.

## One-message design

One Discord message contains one embed with five vertically arranged day sections and a muted red accent. Each event uses a short bold line for Time, Currency and Event, followed by separate Forecast, Previous and Impact lines. Blank lines separate events. Native Discord text wraps naturally on narrow phone screens; no fixed-width tables, horizontal padding or code blocks are used. All six data items remain in the same order. English date headings and “No news” for empty days are retained.

## GitHub setup

The weekly workflow uses a standard Ubuntu runner. Its schedule is Monday 06:00, with bounded retry opportunities at 06:20 and 06:40; an already confirmed week is skipped. GitHub schedules can be delayed and are not an exact-time service. The timezone field handles summer and winter time.

For a public repository, standard GitHub-hosted runner execution is free. This workflow does not upload Actions artifacts, allocate caches, or use larger runners. Private repositories consume the account's included allowance; a zero-spend limit must be confirmed before enabling paid-account execution. Public scheduled workflows can be disabled by GitHub after 60 days without repository activity.

Required Actions secrets:

- DISCORD_TOKEN: the bot token.
- CHANNEL_ID and GUILD_ID: destination IDs.
- STATE_KEY: a Fernet encryption key generated once and retained.
- PREVIEW_PAYLOAD: optional, recently verified calendar for the separate next-week trial.
- INITIAL_PREVIEW_STATE: optional, existing trial state so the cloud test updates the same card.

Never publish .env, raw delivery state, logs, local account files or credentials. The bot token and destination IDs are supplied through GitHub Secrets. The only remote delivery-state file contains authenticated ciphertext; its encryption key remains a secret. The GITHUB_TOKEN provided by each Actions run writes that state and is not stored in the code.

The workflow requires repository Contents write permission for the encrypted state. It does not need admin privileges on Discord, only View Channel, Send Messages and Embed Links in its destination.

## Commands and tests

```bash
python -m pip install -r requirements.txt
python -B -m unittest discover -s tests -v
python -B cloud_run.py --mode check
```

The workflow can be triggered manually with check, trial or send. check reads public sources only. trial uses separate prepared preview data and state. send is limited to Monday after 06:00 Berlin time and requires either the two-source check or the explicitly enabled HTTP-403 fallback.

The former long-running local bot can still be started with bot.py. Slash commands require that live connection. The scheduled cloud job does not keep the bot continuously online, and those commands are not continuously served while the PC is off.

## Delivery guarantees and recovery

A repository-wide workflow concurrency group serializes cloud runs. An encrypted durable reservation is saved before any Discord send, then replaced with the confirmed message state. Re-runs skip confirmed weeks. If a crash occurs after a reservation and the outcome is unclear, another message is not sent automatically; the recorded reservation requires review. Preview and production use separate slots.

Do not erase or regenerate STATE_KEY while encrypted state exists, and do not clear a delivery reservation blindly. Resolve the actual Discord message first to avoid duplicates. Local posting has been stopped for the cloud handoff. LOCAL_SCHEDULE_ENABLED=false in the owner's ignored local .env prevents automatic local posting after an accidental restart. Its default is true for standalone local installations. The cloud publisher does not use this local scheduling switch.

## Relevant files

- cloud_run.py: bounded single-run entry point.
- cloud_state.py: encrypted GitHub state with conflict checking.
- calendar_sources.py: direct sources and cross-check.
- calendar_core.py: filters, timezone validation, layout and portable lock.
- bot.py: shared delivery and optional local live bot.
- .github/workflows/weekly-news.yml: scheduled cloud execution.
- .github/workflows/tests.yml: separate Linux test job without real secrets.
- tests/: calendar, delivery, cloud-state and cloud-run tests.

Official references:
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
- https://docs.github.com/en/rest/actions/secrets
