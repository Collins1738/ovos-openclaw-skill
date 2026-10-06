# TODO

## Barge-in

**Status:** Planned, not currently in development.

Barge-in lets Collins interrupt Dravon while TTS is still speaking. OVOS should
stop the current audio promptly, capture the interruption, and route it through
the same OpenClaw conversation so the exchange continues naturally.

When we work on it, account for speaker echo and false triggers, preserve
conversation and follow-up state, and make stop/cancel behavior deterministic.
