# codex-auth-switch

Safely switch the local Codex CLI between saved ChatGPT access-token accounts,
the built-in OpenAI provider, and a local TokenFactory gateway. The project is
standalone and has no runtime Python dependencies.

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

# Repair provider first, then log into one account
codex-auth-switch --single codex-personal

# Probe and select the highest-priority available account
codex-auth-switch --single

# Define the provider and switch to the local gateway
codex-auth-switch --tokenfactory

# Config-only emergency recovery
codex-auth-switch --reset
```

`--single` changes `model_provider` to the built-in `openai` provider before it
invokes `codex login`. This ordering is deliberate: `codex login` loads
`config.toml`, so an undefined provider would otherwise prevent the login from
starting. If login later fails, the repaired `openai` selection remains in place
so Codex stays launchable.

`--tokenfactory` never writes a dangling provider reference. It creates a
complete `[model_providers.tokenfactory]` definition when one is missing, then
selects it. Override the default endpoint with `--tokenfactory-url` or
`TOKENFACTORY_BASE_URL`.

## Security

- Tokens are read from 1Password over stdin and are never placed in argv.
- Probe errors redact the active token.
- Per-account probe state is isolated under the platform user state directory.
- `--reset` changes only `config.toml` and its local backup.
