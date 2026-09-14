# Working on this plugin

## One-time setup

```bash
git config core.hooksPath .githooks
```

That enables a `pre-push` hook which refuses a push that changes plugin files
without bumping the version. Do this in every fresh clone. Git deliberately
does not share hooks through the repo itself, so a clone without this command
gets no protection.

## Before you push

Bump the version whenever you change anything under `.claude-plugin/`,
`skills/`, `hooks/`, or `tools/`:

```bash
./tools/bump-version.sh patch     # 0.1.0 -> 0.1.1  (fixes, doc tweaks)
./tools/bump-version.sh minor     # 0.1.0 -> 0.2.0  (new checks, new flags)
./tools/bump-version.sh major     # 0.1.0 -> 1.0.0  (breaking changes)
./tools/bump-version.sh 1.4.2     # set it explicitly
```

This writes the same version to `plugin.json` and to the marketplace entry.
They have to match: Cowork and Claude Code decide whether an installed copy is
out of date by comparing the marketplace entry's version, so a change shipped
without a bump may never reach anyone who already installed it.

To push anyway, for example a README-only fix you judge not worth a version:

```bash
git push --no-verify
```

## Testing without a live instance

`sc.py` talks to one HTTP endpoint, so a small local stub is enough to exercise
`run`, `chat`, and session resolution without touching a real ScreenConnect
instance. Point it at the stub with `SC_CONFIG`:

```bash
python3 skills/screenconnect/scripts/sc.py setup \
  --url http://127.0.0.1:8932 --secret test --path /tmp/cfg.json --no-verify
SC_CONFIG=/tmp/cfg.json python3 skills/screenconnect/scripts/sc.py run DESKTOP-ABC123 "ipconfig /all"
```

## Scope

ScreenConnect only. The plugin talks to one system and needs one credential.
Earlier versions carried optional ticketing and database integrations; those
were removed deliberately. If you need that kind of chaining, do it in a
separate plugin rather than widening this one's credential surface.
