# HEARTBEAT.md

The rover SDK runs a background **check-in loop** that sends unprompted status
updates to the OpenClaw agent on a configurable timer.

## Starting the loop

```bash
# Use the interval from OPENCLAW_CHECKIN_INTERVAL_SECONDS (default 300 s)
curl -X POST http://localhost:8000/checkin-loop/start

# Or override the interval for this session
curl -X POST http://localhost:8000/checkin-loop/start \
  -H "Content-Type: application/json" \
  -d '{"interval_seconds": 60}'
```

## Stopping / inspecting

```bash
curl -X POST http://localhost:8000/checkin-loop/stop
curl        http://localhost:8000/checkin-loop/status
```

## What the agent receives

Each check-in POSTs to `OPENCLAW_HOOK_URL` with `source: rover_scheduled_checkin`
and a message like:

```
Task: ScheduledCheckIn
Source: rover_scheduled_checkin
Interval: 300s
Iteration: 3
Timestamp: 2026-04-06T12:00:00Z
Please provide a brief rover status update: describe what you see and note any conditions worth reporting.
```

The agent should respond by calling `/describe-scene` or `/v2/screenshot` and
reporting the result back through its configured channel.

## Configuration (.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENCLAW_CHECKIN_INTERVAL_SECONDS` | `300` | Seconds between check-ins |
| `OPENCLAW_HOOK_URL` | — | Webhook endpoint (required) |
| `OPENCLAW_HOOK_TOKEN` | — | Bearer token for hook auth (required) |
