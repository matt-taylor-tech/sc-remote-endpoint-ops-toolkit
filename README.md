# screenconnect plugin

Query and lightly manage ScreenConnect (ConnectWise Control) sessions from Claude/Cowork, via the RESTful API Manager extension. No MCP server or hosting required - this is a skill (SKILL.md + Python scripts) that calls the extension's HTTP endpoint directly from the sandbox each time it runs.

## Scope

- Reads: session lookup by name, serial, or ID; full session details with event history; uptime from LastBootTime
- Commands: `run <id> "<command>"` sends a command and waits for stdout (EventType 70 polling). cmd by default, `--shell powershell` supported. Destructive commands are denylisted behind `--force`.
- Actions (confirm-first): add note, send on-screen message, rename session, update custom properties
- Read-only diagnostic playbooks (`diag.py`): event logs, sound, OneDrive, network, health, memory, drivers, battery
- Chat transcript retrieval
- Blocked by client: CreateSession

Optional: if you use Freshservice for ticketing, `run`/`chat` can auto-post a private, redacted note to a ticket via `--ticket <id>`. Without a Freshservice config, this is silently skipped and everything else works normally.

## One-time setup on ScreenConnect

1. In ScreenConnect, go to **Admin > Extensions**.
2. Install (or confirm installed) the **RESTful API Manager** extension - GUID `2d558935-686a-4bd0-9991-07539f5fe749`. This is the standard marketplace extension, so this GUID should match on any ScreenConnect instance.
3. Open its settings and set **RESTfulAuthenticationSecret** to a new, strong shared secret. Treat this like a master password: it authorizes every method the extension exposes, including remote command execution.
4. If **RESTfulAllowedOrigin** is set, note the value - you'll need it in the config below.

## Configuration

You need two values: your instance URL and the `RESTfulAuthenticationSecret` from the step above. There are three ways to supply them, in the order most people will want them.

### 1. Plugin user config (easiest)

If you installed this as a plugin, the client prompts for **ScreenConnect URL** and **RESTful API secret** when you enable it. The secret is masked and kept in the OS keychain (or `~/.claude/.credentials.json`), not in this repo and not in your settings file. A `SessionStart` hook (`hooks/write-config.py`) writes those values to `~/.config/screenconnect/screenconnect-config.json` with mode 0600 at the start of each session, which is what the skill actually reads. Nothing else to do.

To change them later, edit the plugin's configuration in your client and start a new session.

### 2. Just tell Claude

If no config exists, every command exits with a "not configured yet" message that tells Claude to ask you for the URL and secret and then run:

```bash
python3 skills/sessions/scripts/sc.py setup --url https://<your-instance>.screenconnect.com --secret <secret>
```

`setup` verifies the credentials against your instance before it writes anything, then saves them to `~/.config/screenconnect/screenconnect-config.json` (mode 0600). Add `--origin <value>` if `RESTfulAllowedOrigin` is set, `--path <file>` to write elsewhere, `--no-verify` to skip the check.

### 3. By hand (env vars or a config file)

For CI, cron, or a serverless runner, set `SC_URL` and `SC_AUTH_SECRET` (plus optional `SC_EXTENSION_ID`, `SC_ORIGIN`). Or write the file yourself:

```json
{
  "url": "https://<your-instance>.screenconnect.com",
  "extension_id": "2d558935-686a-4bd0-9991-07539f5fe749",
  "auth_secret": "<the RESTfulAuthenticationSecret you set above>"
}
```

(Template at `config-templates/screenconnect-config.example.json`.) Add `"origin": "<value>"` if `RESTfulAllowedOrigin` is set on the extension.

**Config discovery order** (`scripts/sc.py`, first match wins):

1. `SC_URL` + `SC_AUTH_SECRET` env vars (+ optional `SC_EXTENSION_ID`, `SC_ORIGIN`)
2. `CLAUDE_PLUGIN_OPTION_SC_*` - plugin user config, when the host exports it
3. `SC_CONFIG` env var - path to a `screenconnect-config.json` file
4. Any mounted `*/mnt/Configs/screenconnect-config.json` (the Cowork folder-mount convention - connect a folder containing this file)
5. `~/.config/screenconnect/screenconnect-config.json` - what `setup` and the hook write

## Installing as a Cowork/Claude Code plugin

This repo is set up as its own plugin marketplace (`.claude-plugin/marketplace.json`), so it installs directly from GitHub, no zip file, no manual copying.

**In Cowork:**

1. Open **Customize** in the sidebar, then **Plugins**.
2. Select **Add marketplace** and enter `matt-taylor-tech/sc-remote-endpoint-ops-toolkit`.
3. Install **sc-remote-endpoint-ops-toolkit** from that marketplace.

**In Claude Code (CLI):**

```
/plugin marketplace add matt-taylor-tech/sc-remote-endpoint-ops-toolkit
/plugin install sc-remote-endpoint-ops-toolkit@sc-remote-endpoint-ops-toolkit
```

Either way, you still need the config step above (`screenconnect-config.json` or `SC_URL`/`SC_AUTH_SECRET`) before the skill can actually reach your ScreenConnect instance - installing the plugin only adds the skill, it doesn't prompt for or store the secret.

## Notes

- Auth is a single shared-secret header (`CTRLAuthHeader`). It authorizes every method on the extension - there is no per-user or read-vs-write scoping at the ScreenConnect API layer. Anyone who can reach this config can run commands on any session. Store the secret like any other credential (not in source control, restrict who can read the config file).
- `CreateSession` is blocked client-side by `sc.py`; everything else the extension exposes is reachable, gated by the plugin's own destructive-command denylist and confirm-first guidance in SKILL.md.
- The plugin never uses ticket/asset text as command input - commands must come from the operator in chat. Keep that discipline if you extend this.
- Optional integrations (Freshservice ticket notes, a Supabase `command_runs` audit table) degrade gracefully: if their config isn't present, those features no-op instead of erroring. You can ignore them entirely if you don't use those systems.
- Not included: a company-specific "can't print" network-triage check that existed in the source environment. It depended on that org's internal network documentation and a Freshservice printer-asset export. See `skills/sessions/SKILL.md` for the pattern if you want to build an equivalent for your own site inventory.
