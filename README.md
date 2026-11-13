# Weekly Discord Red-Folder News

A scheduled GitHub Actions job posts one compact economic-calendar card to Discord every Monday at **06:00 Europe/Berlin**. The owner's computer and Codex do not need to run after the cloud deployment has been activated and tested.

## Current deployment status

The cloud implementation and workflow are prepared locally. Repository publication, GitHub Secrets setup and the first actual cloud run still require account authorization. Do not treat local tests as proof that cloud hosting is active.

## Calendar rules

Eight currencies: USD, EUR, GBP, JPY, CHF, CAD, AUD, NZD. The default profile includes CPI, actual central-bank rate decisions and official US Nonfarm Payrolls only. National EUR releases, unemployment rates, ADP, speeches, statements and minutes are excluded. Related CPI variants are merged without mixing numbers from different source rows.

Investing.com and Forex Factory are fetched and compared. Dates are converted to Europe/Berlin, including daylight-saving changes. Different CPI measurement variants are recorded rather than compared as the same measure. Unresolved date or event-coverage differences stop publication. A missing source is never presented as “No news”.

**Known source limitation:** Investing.com currently blocks the direct local request with HTTP 403. The cloud runner must test access separately. If either source remains unavailable, the scheduled job reports failure and does not publish an allegedly checked calendar. Hosting alone does not solve this source-access issue.

## One-message design

One Discord message contains one embed with five vertically arranged day sections. English date headings, compact aligned columns and a muted red accent are used. Columns remain Time, Currency, Event, Forecast, Previous, Impact. Days with no matching events show “No news”.

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

The workflow can be triggered manually with check, trial or send. check reads public sources only. trial uses separate prepared preview data and state. send is limited to Monday after 06:00 Berlin time and still requires the two-source check.

The former long-running local bot can still be started with bot.py. Slash commands require that live connection. The scheduled cloud job does not keep the bot continuously online, and those commands are not continuously served while the PC is off.

## Delivery guarantees and recovery

A repository-wide workflow concurrency group serializes cloud runs. An encrypted durable reservation is saved before any Discord send, then replaced with the confirmed message state. Re-runs skip confirmed weeks. If a crash occurs after a reservation and the outcome is unclear, another message is not sent automatically; the recorded reservation requires review. Preview and production use separate slots.

Do not erase or regenerate STATE_KEY while encrypted state exists, and do not clear a delivery reservation blindly. Resolve the actual Discord message first to avoid duplicates. Existing local posting must be stopped or disabled when cloud posting is activated.

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
