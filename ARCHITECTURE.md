# Why this is a skill and not an MCP server

Decision recorded 2026-09-14. Revisit it when one of the triggers below fires,
not because a single user hits a wall.

## The decision

This plugin is a skill plus two standalone Python scripts. It declares no MCP
server, has no dependencies outside the standard library, and needs nothing
hosted.

Three things follow from that, and they are the reasons:

**It runs anywhere.** The same `sc.py` works inside a Cowork session, under
Claude Code CLI, from an ordinary terminal, and from cron. Nobody has to install
a runtime, a package, or a server to use it. Someone evaluating it can read two
files and run one command.

**One credential, one machine.** The RESTfulAuthenticationSecret authorizes every
method the extension exposes, including remote command execution on any endpoint.
In this design it lives in a 0600 file on the operator's own machine and travels
nowhere else. That property is worth more than convenience.

**Nothing to operate.** No deployment, no uptime, no version skew between a
hosted component and the plugin.

## The known cost

Cowork sends outbound traffic through an egress proxy with an allowlist, and a
private ScreenConnect instance is not on it. Script-initiated calls from inside a
session fail at connect time with a 403. See "Network access" in the README for
the symptom and the three ways around it.

This is the real tradeoff, and it is not hypothetical. It is the thing an MCP
server would solve, because MCP tools are not subject to that allowlist.

## Alternatives considered

**Bundle a stdio MCP server in the plugin.** The most attractive option on paper.
`plugin.json` supports `mcpServers` alongside `skills`, and `${user_config.*}`
substitutes into an MCP server's env, so the secret would still arrive through the
install prompt and still land in the OS keychain. The user would install one thing,
exactly as now. As of this writing there is an open report, anthropics/claude-code
issue 87537, titled "Plugin-bundled stdio MCP servers silently dropped in cloud
sessions (skill loads, MCP does not)." That was read from the issue title only, not
the body, so treat it as a strong signal rather than a settled fact. If accurate, a
bundled server buys nothing in the environment that needs it most.

**Have the user stand up their own local MCP server.** This demonstrably works: a
shell MCP server running natively on the operator's machine reaches the instance
fine, because it is outside the sandbox. But it converts "install a plugin" into
"install a plugin and separately configure an MCP server," which is worse setup
friction than the problem it solves. The README documents it as a fallback for
people who already run one, which is the right place for it.

**Host a remote MCP server.** Bypasses the allowlist cleanly and asks almost
nothing of the user. Rejected on two grounds. Each operator's ScreenConnect
instance is their own, so it needs a per-tenant deployment rather than one service.
More seriously, it moves a master-password-equivalent secret off the operator's
machine and onto a host someone else runs. Centralizing other people's
remote-access credentials is a large and permanent liability in exchange for a
convenience, and it inverts the security property this design was built around.

## What would change this

Add an MCP path when either of these is true, and add it as an option beside the
scripts rather than a replacement, since `plugin.json` can declare both:

1. Plugin-bundled stdio MCP servers work reliably in Cowork cloud sessions.
   Verify against issue 87537 before building anything.
2. The egress allowlist restriction proves permanent rather than a regression.
   The reports clustered around 2026-09-09 through 09-11 and were unresolved at
   the time of writing, which looked much more like a regression than a policy.

Do not adopt the hosted option to solve a convenience problem. The credential
custody question is the deciding one, and convenience does not outweigh it.
