# TODO

## Barge-in

**Status:** Planned, not currently in development.

Barge-in lets Collins interrupt Dravon while TTS is still speaking. OVOS should
stop the current audio promptly, capture the interruption, and route it through
the same OpenClaw conversation so the exchange continues naturally.

When we work on it, account for speaker echo and false triggers, preserve
conversation and follow-up state, and make stop/cancel behavior deterministic.

## Railway speech relay

**Status:** Future migration after the Tailscale Funnel route is proven.

Move public speech ingress to a Railway service with a durable, expiring queue.
The Mac should maintain an outbound connection or poll for pending requests,
acknowledge successful playback, and discard announcements that expire while it
is offline. Keep the local Mac route for trusted local integrations.
