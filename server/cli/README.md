# exctl

A zero-dependency Python CLI for the exception tracker's network API
([`/api/*`](../../NETWORK_API.md)). Lets you query and mutate exception groups from a
terminal instead of Discord.

Nothing to install: it is a single stdlib-only script. Run it as `python3 cli/exctl.py`,
or make it directly invocable with `chmod +x` (already set in git) plus a symlink or alias:

```bash
alias exctl='python3 /path/to/server/cli/exctl.py'
```

The rest of this document writes it as `exctl` for brevity.

## Usage

```
exctl [--base-url URL] [--timeout SECONDS] <command> [args...]

  list | top | new   [--status active|muted|resolved|all] [--server X] [--search TEXT]
                     [--sort last_seen|first_seen|total_count|recent] [--window-hours N]
                     [--new-within-hours N] [--limit N] [--offset N] [--json]
                     # `top` is shorthand for `list --sort recent`
                     # `new` is shorthand for `list --sort first_seen --new-within-hours 24`
  show               <id> [--json]
  occurrences        <id> [--limit N] [--json]
  servers            [--json]
  health             [--json]
  mute               <id> [<id>...] [--json]
  unmute | reopen    <id> [<id>...] [--json]
  resolve            <id> [<id>...] [--json]
  fix                <id> [--json]
  fix-status         <job_id> [--json]
  fix-history        <id> [--limit N] [--json]
  fix-list           [--status S[,S...]|all] [--limit N] [--json]
                     # default --status pending,running: the live Chisel queue
  fix-cancel         <job_id> [--json]
```

`exctl --help` lists the commands; `exctl <command> --help` documents each flag and
its default. There is no `purge` command: purge is Discord-only (see
[NETWORK_API.md](../../NETWORK_API.md)).

`<id>` accepts either the 8-character short ID shown in group listings or the full
64-character fingerprint, in upper or lower case. `mute`, `unmute`/`reopen`, `resolve`,
`fix` and `fix-cancel` require a bearer token (see below); the read-only commands don't.

`mute`, `unmute`/`reopen` and `resolve` take several ids and make one API call per id,
so one bug that fragmented into many groups can be closed in one command. An id that
fails (say, a group that has already expired) is reported on stderr and the rest still
run; the exit status is non-zero if any failed, and with more than one id a summary
(`Resolved 27 of 29; failed: ...`) goes to stderr. An unreachable server, a timeout, a
rejected token or Ctrl-C stops the run instead and lists the ids not done. Repeated ids
are dropped. With `--json`, each successful response is printed as one JSON object per
line; failures appear only on stderr and in the exit status. `fix` stays one id at a
time, since each one is a Chisel job and a pull request.

`--json` works on every command, read or mutating, and prints the raw API response
for scripting instead of the default human-readable text. Timestamps in the default
output are rendered in **local time**; under `--json` they are the API's raw epoch
seconds.

Any error (an HTTP error status, an unreachable server, a timeout) prints to stderr
and exits non-zero, quoting the server's own error message when there is one.

## Examples

```bash
# What's noisiest right now, and where?
exctl top --limit 10

# Everything a single server has ever reported, including muted and resolved groups
exctl list --server build --status all

# Page through a large result set
exctl list --limit 50 --offset 50

# Look at one group, then at the raw messages behind it
exctl show 95daa028
exctl occurrences 95daa028 --limit 5

# Triage
exctl mute 95daa028
exctl resolve 95daa028
exctl resolve 95daa028 1f3c9e07 c54b6556   # one bug, several groups

# Ask Chisel for a fix and follow it without watching Discord
exctl fix 95daa028          # prints a job ID
exctl fix-status <job_id>
exctl fix-history 95daa028

# Is Chisel keeping up? What's queued or in flight across every group?
exctl fix-list
exctl fix-list --status failure --limit 10

# Withdraw a job that hasn't been claimed yet (a running one can't be cancelled)
exctl fix-cancel <job_id>
```

## Reaching the server

`--base-url` defaults to `http://localhost:8080`. Set `$EXCTL_BASE_URL` to point
somewhere else:

```bash
export EXCTL_BASE_URL=http://localhost:9099
exctl health
```

Proxy environment variables (`$http_proxy` / `$https_proxy`) are ignored, so a
configured proxy can't make a reachable server look unreachable.

## Getting a token

Mutating commands (`mute`, `unmute`, `reopen`, `resolve`, `fix`, `fix-cancel`) require a bearer
token that proves which Discord account you are. Run `/api-token create` in Discord
(optionally with a `lifetime_hours` argument). It replies ephemerally with a token,
shown once, and its expiry. Set it as `$EXCTL_API_TOKEN`:

```bash
export EXCTL_API_TOKEN=<token from /api-token create>
exctl mute deadbeef
```

There is deliberately no `--token` flag: a token is a secret, and a CLI argument
would land it in shell history and in any other process's view of `ps`.

Every mutation is attributed to your resolved Discord display name (`muted_by`/
`resolved_by` in the group JSON). Lost or leaked a token? Run `/api-token revoke`
in Discord to invalidate every token you've minted.

A fix requested here behaves exactly like one triggered by the 🔧 reaction in Discord:
the group's channel message gets the in-progress emoji while Chisel works, then the
outcome emoji, and you receive the result as a DM. `exctl fix-status` exists so you
don't have to rely on that DM.

<!-- TODO(docs): this file documents usage but not output formats. A docs pass should add
     a short "Output" section showing an annotated example of a `list` line and a `show`
     block (what each column means, where server counts come from, why `recent` can be 0
     for a group with a large total), since those are the questions people actually ask
     when reading the output for the first time. -->
