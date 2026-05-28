# pr-bot

A Discord bot that keeps `#pending-prs` reactions in sync with GitHub pull request state.

## What it does

- Watches `#pending-prs` for messages containing GitHub PR links
- Treats a **self-reply** (replying to your own message, e.g. a "bump") as linking
  the same PR(s) as the original, so the bumped message tracks and shows the same
  state. Replies to someone else's message inherit nothing.
- Listens for GitHub review and merge webhooks
- Keeps each message's reactions matching the true PR state:
  - ✅ every linked PR approved
  - 💬 any open PR needs changes (or has a comment from someone **other than** the
    PR author, configurable — a PR author commenting on their own PR never triggers 💬)
  - 🔀 all PRs merged (override with your `:merged:` custom emoji)
  - ❌ all PRs closed, at least one without merging
  - ❓ message has no PR links
- Mirrors merge-readiness **labels** as reactions (any open linked PR carrying the label):
  - 🟢 `ready`, 🟠 `Not Ready/Delayed`, 🧪 `Tested`, ⚖️ `monthly-balance`
  - label names are matched case-insensitively; both the name and the emoji are configurable
- 🐶 when any GitHub Actions check is **failing** on an open linked PR; removed once all pass
- DMs the message poster when their PR is reviewed or merged (configurable per-user with
  `/pr_notify`), and on a failing check (for any `/pr_notify` level except `off`)

## Quick start

```bash
cd pr-bot
make venv        # create .venv
make test        # lint + typecheck

# Run (requires env vars below)
DISCORD_TOKEN=... DISCORD_CHANNEL=... GITHUB_REPOS=... \
GITHUB_WEBHOOK_SECRET=... GITHUB_API_TOKEN=... \
python server.py
```

## Environment variables

| Variable | Default | Required | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | — | ✓ | Bot token |
| `DISCORD_CHANNEL` | — | ✓ | `#pending-prs` channel ID |
| `GITHUB_REPOS` | — | ✓ | Comma-separated `owner/name` repos to track |
| `GITHUB_WEBHOOK_SECRET` | — | ✓ | HMAC secret shared with the GitHub webhook |
| `GITHUB_API_TOKEN` | — | ✓ | Read-only GitHub PAT for on-ingest and startup state fetches |
| `DB_PATH` | `prbot.db` | | SQLite path |
| `PORT` | `8080` | | HTTP port |
| `PR_RETENTION_DAYS` | `21` | | Delete tracking rows older than this many days |
| `PR_CLEANUP_PERIOD_SECONDS` | `3600` | | Cleanup loop interval |
| `PR_DM_ENABLED` | `true` | | Global master switch for all DMs |
| `REVIEW_COMMENT_IS_CHANGES` | `true` | | Treat bare `commented` reviews as 💬 |
| `REACTION_APPROVED` | `✅` | | Override with Unicode or `name:id` |
| `REACTION_CHANGES` | `💬` | | Override with Unicode or `name:id` |
| `REACTION_MERGED` | `🔀` | | Override with your `:merged:` custom emoji as `name:id` |
| `REACTION_CLOSED` | `❌` | | Override with Unicode or `name:id` |
| `REACTION_QUESTION` | `❓` | | Override with Unicode or `name:id` |
| `LABEL_READY` | `ready` | | GitHub label name (case-insensitive) for the 🟢 reaction |
| `LABEL_NOT_READY` | `Not Ready/Delayed` | | GitHub label name for the 🟠 reaction |
| `LABEL_TESTED` | `Tested` | | GitHub label name for the 🧪 reaction |
| `LABEL_MONTHLY_BALANCE` | `monthly-balance` | | GitHub label name for the ⚖️ reaction |
| `REACTION_READY` | `🟢` | | Reaction when an open PR has the `ready` label. Override with Unicode or `name:id` |
| `REACTION_NOT_READY` | `🟠` | | Reaction for the `Not Ready/Delayed` label. Override with Unicode or `name:id` |
| `REACTION_TESTED` | `🧪` | | Reaction for the `Tested` label. Override with Unicode or `name:id` |
| `REACTION_MONTHLY_BALANCE` | `⚖️` | | Reaction for the `monthly-balance` label. Override with Unicode or `name:id` |
| `REACTION_CHECKS_FAILED` | `🐶` | | Reaction when an automated check is failing on an open PR. Override with Unicode or `name:id` |
| `VERBOSE` | `true` | | Logging verbosity. Any value other than `false` (case-insensitive) raises the bot's own log level to `DEBUG`; `false` keeps it at `INFO` (see [Logging](#logging)) |

## Logging

Logging is two-tiered, controlled by `VERBOSE`:

- **`INFO` (always on)** — the meaningful events: bot login, new/edited/deleted
  `#pending-prs` messages, accepted GitHub webhooks, PR state transitions
  (approved / changes requested / commented / merged / closed), reaction sets that
  actually changed, DMs sent, startup-reconcile phases and counts, and cleanup
  deletions. This is what you get with `VERBOSE=false`.
- **`DEBUG` (verbose, the default)** — per-message ingest detail, parsed link sets,
  GitHub REST requests/responses, no-op reconciles, ignored webhook events,
  suppressed DMs, and slash-command invocations.

`VERBOSE` only raises the verbosity of the bot's *own* loggers; `discord.py` and
`aiohttp` are pinned to `INFO` so their per-heartbeat/per-request chatter never
floods the log even in verbose mode.

## Slash commands

| Command | Description |
|---|---|
| `/pr_notify <state>` | Set your DM level: `off`, `review comments`, `any review`, `all` |
| `/pr_status <message>` | Show tracked PRs and their state for a message |
| `/pr_resync <message>` | Force-reprocess a message (re-parse links, re-fetch state, re-reconcile) |
| `/pr_repos` | List configured repos |

## Deploy

See `deployment.yml` for the Kubernetes manifest (Ingress + Service + Deployment).

### Generating `GITHUB_WEBHOOK_SECRET`

The webhook secret is any random high-entropy string — there's no format GitHub
requires. Generate one and use the **same value** in both the GitHub webhook config
and the k8s secret below. For example:

```bash
openssl rand -hex 32
# or:  python3 -c 'import secrets; print(secrets.token_hex(32))'
```

### Create the secret before deploying:

```bash
kubectl create secret generic pr-bot \
  --from-literal=discord-token=YOUR_TOKEN \
  --from-literal=github-webhook-secret=YOUR_SECRET \
  --from-literal=github-api-token=ghp_...
```

### GitHub API token permissions (`GITHUB_API_TOKEN`)

The token is **read-only**. It is used only for the on-ingest and startup state
fetches (`GET .../pulls/{n}`, `.../pulls/{n}/reviews`, and
`.../commits/{sha}/check-runs`) — the bot never writes to GitHub. It needs read
access to every repo in `GITHUB_REPOS`.

**Fine-grained PAT** (recommended) — Repository access: the tracked repos;
Repository permissions:

| Permission | Access | Why |
|---|---|---|
| Metadata | Read | Mandatory baseline for fine-grained tokens |
| Pull requests | Read | PR merge/close/label state and the reviews list |

> **Note — the 🐶 reaction and the Checks API.** Reading check-run status
> (`GET .../commits/{sha}/check-runs`) requires a `Checks` permission that GitHub
> **does not grant to fine-grained PATs** — the scope was disabled and is not
> selectable when you create one, even though GitHub's own docs still list it.
> The bot handles this gracefully: it drives the 🐶 reaction from the
> **`check_suite` webhook payload** instead, so no extra token permission is
> needed for the common case (it falls back to last-completed-suite-wins for a PR
> with multiple check suites). The only thing a fine-grained PAT can't do is
> **refresh check status during startup reconcile** — any failures that happened
> while the bot was down are picked up on the next `check_suite` webhook rather
> than at startup. If you want true cross-suite aggregation on startup too, use a
> classic PAT (scope `repo`) or a GitHub App token, which *can* read the Checks
> API.

### Register the webhook

Register a GitHub webhook (org-level recommended) at `https://pr-bot.playmonumenta.com/github/webhook`:
- Content type: `application/json`
- Secret: value of `GITHUB_WEBHOOK_SECRET`
- SSL verification: enabled
- Events (select "Let me select individual events"):

| Event | Drives |
|---|---|
| **Pull requests** | 🔀/❌ merge & close reactions (`closed`) and 🟢🟠🧪⚖️ label reactions (`labeled`/`unlabeled`) |
| **Pull request reviews** | ✅/💬 review reactions and review DMs (`submitted`/`dismissed`) |
| **Check suites** | 🐶 failing-check reaction and check-failure DMs (`completed`) |

The bot also accepts GitHub's `ping` event so the webhook's initial delivery test
succeeds.

## Discord Developer Portal setup

Enable the **Message Content** privileged intent in the portal for your application.
Grant the bot **Add Reactions**, **Read Message History**, and (recommended) **Manage Messages** in `#pending-prs`.
