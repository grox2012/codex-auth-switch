# codex-auth-switch

Safely switch the shared Codex CLI back to ChatGPT OAuth, launch isolated
per-account OAuth sessions, switch to a local TokenFactory gateway, and recover
a broken provider configuration. The project is standalone and has no runtime
Python dependencies.

## Recovery first

If Codex reports `Model provider 'tokenfactory' not found`, run:

```powershell
codex-auth-switch --reset
codex
```

`--reset` uses only Python's standard library. It does not load the account
configuration and does not invoke `codex`, `op`, TokenFactory, or any network
service. It atomically removes the top-level `model_provider` override and the
`[model_providers.tokenfactory]` tables, preserves every unrelated setting, and
writes the pre-repair file to `config.toml.codex-auth-switch.bak`.

Codex then falls back to its built-in `openai` provider and its normal ChatGPT
OAuth flow. Reset intentionally does not delete or rewrite Codex credentials.

## Install

Python 3.11 or newer is required.

```powershell
uv tool install git+https://github.com/grox2012/codex-auth-switch.git
codex-auth-switch --version
```

There are no runtime Python packages to install. Account switching uses the
existing `codex` and 1Password `op` CLIs; recovery mode does not need them.

## Configure accounts

```powershell
codex-auth-switch init `
  --source-id codex-personal `
  --label "Personal Codex" `
  --onepassword-ref op://tokenfactory/codex-personal/access-token
codex-auth-switch doctor
```

The new config lives at `%APPDATA%\CodexAuthSwitch\config.json` on Windows,
`~/Library/Application Support/CodexAuthSwitch/config.json` on macOS, and
`$XDG_CONFIG_HOME/codex-auth-switch/config.json` on Linux. Existing TokenFactory
account configs are detected as a backward-compatible fallback.

## Use

```powershell
# Probe configured accounts
codex-auth-switch status

# Reset the shared CLI to built-in OpenAI and clear its saved login
codex-auth-switch --single codex-personal

# Then use plain Codex and complete ChatGPT OAuth in the browser
codex

# Or launch an OAuth account in its isolated CODEX_HOME
codex-auth-switch launch codex-personal

# Forward Codex arguments after --
codex-auth-switch launch codex-personal -- -C C:\source\project

# Reset shared Codex for the first configured account
codex-auth-switch --single

# Legacy only: save the source's 1Password access token in shared Codex
codex-auth-switch --switch codex-personal

# Define the provider and switch to the local gateway
codex-auth-switch --tokenfactory

# Config-only emergency recovery
codex-auth-switch --reset
```

`--single SOURCE_ID` is the shared-login workflow. It removes the TokenFactory
provider override and the switcher's managed model-catalog override from
`~/.codex/config.toml`, then runs `codex logout` against the shared Codex home.
The next plain `codex` run therefore starts the normal **Sign in with ChatGPT**
flow. Select the browser account that corresponds to `SOURCE_ID`; the source ID
is a local label and cannot choose a browser account on your behalf.

`launch SOURCE_ID` is the isolated-login workflow. It uses a separate
`CODEX_HOME` for each source, resets any TokenFactory provider settings in that
home, removes inherited API-key/access-token/workload-identity environment
variables, and accepts the cached login only when `codex login status` reports
ChatGPT. Otherwise it clears the isolated login and starts interactive ChatGPT
OAuth before launching Codex. The launcher also uses file credential storage
and `--no-daemon`, so it does not attach to an app server belonging to another
login.

`--switch SOURCE_ID` remains available only for compatibility. It reads the
source's access token from 1Password and saves that token with `codex login
--with-access-token`. It does **not** create a ChatGPT OAuth session and should
not be used when you need OAuth-only account actions such as reset credit.

Codex normally shares cached login details between local surfaces. Because
`--single` intentionally logs out the shared home, an already-running terminal,
desktop, or IDE session that uses those credentials can lose authorization.
Use `launch SOURCE_ID` when other Codex sessions must remain alive.

`--tokenfactory` never writes a dangling provider reference. It creates a
complete `[model_providers.tokenfactory]` definition when one is missing, then
selects it. Before changing `config.toml`, it checks the gateway's `/healthz`
endpoint; an unreachable gateway leaves the current Codex provider unchanged.
Use `--skip-tokenfactory-health-check` only when intentionally preparing an
offline gateway.

If `.codex/models_cache.json` contains a valid Codex model catalog, the switcher
also references it through `model_catalog_json`. This keeps current Codex builds
quiet when an older TokenFactory release returns only the OpenAI-style
`/v1/models` shape. A user-supplied catalog setting is preserved; the managed
fallback is removed when switching back to OpenAI or running `--reset`.

Override the endpoint with `--tokenfactory-url` or `TOKENFACTORY_BASE_URL`.
An explicit URL updates an existing provider definition instead of silently
keeping a stale address. Without an override, the switcher preserves an
existing TokenFactory URL and otherwise defaults to `http://127.0.0.1:8080/v1`.

For a gateway exposed to a Windows machine through Tailscale Serve:

```powershell
$url = 'https://gateway.example.ts.net:8443/v1'
[Environment]::SetEnvironmentVariable('TOKENFACTORY_BASE_URL', $url, 'User')
codex-auth-switch --tokenfactory --tokenfactory-url $url
```

## Security

- Access tokens used by probes or legacy `--switch` are read from 1Password and
  are never placed in argv; probe errors redact the active token.
- Per-account OAuth credentials and probe state are isolated under the platform
  user state directory.
- Account launches use a private app server instead of the shared daemon.
- `--reset` changes only `config.toml` and its local backup.
