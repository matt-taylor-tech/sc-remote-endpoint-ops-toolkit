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

## Tests

The tests run `sc.py` against a local stub of the RESTful API Manager extension
(`tests/stub.py`), so they need no ScreenConnect instance, no network and no
packages:

```bash
python3 -m unittest discover -s tests -v
```

Add a test for every bug fixed and every option added. The stub plays the
instance; a responder function plays the endpoint (see `PushEndpoint` in
`tests/test_sc.py` for one that decodes the scripts `push` sends).

The stub can't tell you how a real agent behaves (interpreter quirks, exit codes,
ACLs, antivirus), so also try anything that changes what gets sent to an endpoint
against a machine you control before you push.

Keep it generic: this repo is public. No client names, hostnames, IPs, paths or
session IDs in code, tests, docs or commit messages.

## Scope

ScreenConnect only. The plugin talks to one system and needs one credential.
Earlier versions carried optional ticketing and database integrations; those
were removed deliberately. If you need that kind of chaining, do it in a
separate plugin rather than widening this one's credential surface.
