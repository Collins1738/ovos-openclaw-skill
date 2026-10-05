# OVOS OpenClaw Skill

An installable OVOS skill that sends voice requests to a local OpenClaw
Gateway. It targets `ovos-workshop` 9.8.x and `ovos-core` 3.7.x.

## Architecture

```text
wake word -> local STT -> high-priority OpenClaw fallback -> OVOS skill
          -> token lookup -> HTTP POST /v1/chat/completions
          -> local OpenClaw Gateway -> short spoken response
```

The OpenAI-compatible request uses model `openclaw/default`, a concise
voice-oriented system message, a bounded completion cap, and a stable `user`
value (`conversation`) so the Gateway can maintain continuity. The default
Gateway address is loopback-only: `http://127.0.0.1:18789`.

Direct routing is enabled by default. The skill registers a priority-1 OVOS
fallback and consumes each real post-wake utterance, including failures, so no
second skill answers alongside OpenClaw. Stop/cancel remains local when the OVOS
stop pipeline stays first. Explicit phrases such as `ask Dravon ...` continue
to work as a compatibility path.

To make direct routing take precedence over native skills, preserve the current
`intents.pipeline` list in `mycroft.conf` but move
`ovos-fallback-pipeline-plugin-high` immediately after
`ovos-stop-pipeline-plugin-high`. Leave every other entry in its existing order.

## Security boundary

The OpenClaw Gateway token grants **full operator access** to the Gateway. Treat
it like a root credential. This project never stores it in the repository or
ordinary OVOS JSON settings. Lookup order is:

1. `OPENCLAW_GATEWAY_TOKEN`
2. macOS Keychain service `ovos-openclaw-skill`, account `gateway-token`

The skill logs only coarse request failures, never credentials or response
payloads. It does not speak transport error details and rejects a model response
containing the configured token.
Keep the Gateway bound to loopback unless you have separately secured the
network path.

## Install and configure

```bash
python3.11 -m pip install .

# Recommended persistent token storage on macOS. The command prompts securely.
security add-generic-password -U \
  -s ovos-openclaw-skill -a gateway-token -w
```

For a process-scoped alternative, export `OPENCLAW_GATEWAY_TOKEN` in the OVOS
service environment. Do not put the token in `ovos.conf`, skill settings, shell
history, or a checked-in dotenv file.

Non-secret skill settings are optional:

| Setting | Default | Meaning |
| --- | --- | --- |
| `direct_route` | `true` | Consume every post-wake utterance through OpenClaw |
| `gateway_url` | `http://127.0.0.1:18789` | Gateway base URL |
| `model` | `openclaw/default` | OpenAI-compatible model name |
| `conversation` | `ovos-openclaw-skill` | Stable OpenAI `user` ID |
| `timeout` | `60` | HTTP timeout in seconds, bounded 1–120 |
| `max_tokens` | `180` | Completion cap, bounded 16–512 |

## Develop and test

```bash
~/.venvs/ovos/bin/python -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m build
```

The plugin is published under the current `opm.skill` entry-point group.

