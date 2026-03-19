# Openclaw + Earth Rover

Control your Earth Rover with natural language through any messaging platform using [Openclaw](https://openclaw.ai).

## What You Get

- Drive the rover with natural language ("move forward", "turn left")
- Take photos and receive them directly in chat
- Speak through the rover's speaker via TTS
- Monitor battery, GPS, and telemetry
- Run missions and track checkpoints

## Prerequisites

1. Earth Rover SDK running on `http://localhost:8000` (see main [README](../../README.md))
2. [Openclaw](https://openclaw.ai) installed and connected to a messaging channel

## Integration

### 1. Point Openclaw to the SDK workspace

In your `~/.openclaw/openclaw.json`, set the agent's workspace to this SDK directory:

```json5
{
  agents: {
    defaults: {
      workspace: "/path/to/earth-rovers-sdk",
    },
  },
}
```

Alternatively, copy the files from this folder into your existing workspace:

```bash
cp examples/openclaw/*.md ~/.openclaw/workspace/
```

### 2. Add the workspace files

Copy these files into your workspace directory:

| File | Purpose |
|------|---------|
| `AGENTS.md` | Operating rules, API reference, curl examples. **This is the main instruction file.** |
| `SOUL.md` | Personality, tone, response style. Controls how the agent talks. |
| `IDENTITY.md` | Name, emoji, avatar. The agent's public identity. |
| `USER.md` | Info about you (the human). The agent fills this in over time. |
| `TOOLS.md` | Environment-specific notes (device names, preferences). |
| `HEARTBEAT.md` | Periodic tasks the agent runs automatically. |

### 3. Start the SDK server

```bash
pip install -r requirements.txt
hypercorn main:app --reload
```

### 4. Chat with your rover

Message your agent through your connected channel. It knows the full rover API and will use `curl` to control it.

## How Images Work

When the agent takes a screenshot, it saves the file and outputs `MEDIA:front.png`. Openclaw detects the `MEDIA:` prefix and automatically attaches the image to the reply. The file must be saved inside the workspace directory (not `/tmp/`).

## How TTS Works

The agent calls `POST /speak` with text. The SDK converts it to speech using edge-tts or Gemini, then streams the audio to the rover's physical speaker through the Agora RTC channel.

Configure the TTS provider in the SDK's `.env`:

```bash
TTS_PROVIDER="edge"          # "edge" (free, default) or "gemini"
TTS_API_KEY=""                # Required for gemini only
TTS_VOICE="en-US-GuyNeural"  # Voice name
```

## Customization

### Personality

Edit `SOUL.md`. The example gives a dry, minimal rover that speaks in first person. Make it whatever you want.

### Name

Edit `IDENTITY.md`. Give it a name, an emoji, a vibe.

### Dance routine

Add a `## Dance Routine` section to `AGENTS.md` with a bash script of movement commands. The agent will execute it when asked to dance.

## Architecture

```
Chat Message → Openclaw Gateway → LLM Agent → curl to localhost:8000 → Earth Rover
```

## Troubleshooting

**Agent can't connect to server**: Make sure the SDK is running before chatting.

**Images not sending**: If you get `LocalMediaAccessError`, the image path is outside the workspace. Make sure images are saved inside the workspace directory.

**TTS not working**: Verify with `curl -s -X POST http://localhost:8000/speak -H "Content-Type: application/json" -d '{"text": "test"}'`.

## Resources

- [Openclaw Documentation](https://docs.openclaw.ai)
- [Earth Rovers Shop](https://shop.frodobots.com)
- [Frodobots Discord](https://discord.com/invite/AUegJCJwyb)
