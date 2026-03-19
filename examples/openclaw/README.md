# Openclaw + Earth Rover

Control your Earth Rover through Telegram (or any other messaging platform) using [Openclaw](https://openclaw.ai) as an AI agent gateway.

## What You Get

- Drive the rover with natural language ("move forward", "turn left")
- Take photos and receive them directly in Telegram
- Speak through the rover's speaker via TTS
- Monitor battery, GPS, and telemetry
- Run missions and track checkpoints

## Prerequisites

1. Earth Rover SDK running on `http://localhost:8000` (see main [README](../../README.md))
2. Node.js 22+ installed
3. A Telegram bot token (from [@BotFather](https://t.me/BotFather))

## Setup

### 1. Install Openclaw

```bash
npm install -g openclaw@latest
```

### 2. Onboard

```bash
openclaw onboard
```

This walks you through initial setup: choosing an LLM provider, setting API keys, and creating your first workspace.

### 3. Configure Telegram

Add your Telegram bot token to the Openclaw config (`~/.openclaw/openclaw.json`):

```json5
{
  channels: {
    telegram: {
      enabled: true,
      botToken: "YOUR_BOT_TOKEN_FROM_BOTFATHER",
      dmPolicy: "pairing",
    },
  },
}
```

### 4. Set Up the Workspace

By default, Openclaw stores agent files in `~/.openclaw/workspace`. You can point it to a custom directory — for example, this SDK folder:

```json5
// In ~/.openclaw/openclaw.json
{
  agents: {
    defaults: {
      workspace: "/path/to/earth-rovers-sdk",
    },
  },
}
```

Or, if you prefer the default workspace, copy the example files into it:

```bash
cp examples/openclaw/AGENTS.md ~/.openclaw/workspace/AGENTS.md
cp examples/openclaw/SOUL.md ~/.openclaw/workspace/SOUL.md
cp examples/openclaw/IDENTITY.md ~/.openclaw/workspace/IDENTITY.md
cp examples/openclaw/USER.md ~/.openclaw/workspace/USER.md
cp examples/openclaw/TOOLS.md ~/.openclaw/workspace/TOOLS.md
cp examples/openclaw/HEARTBEAT.md ~/.openclaw/workspace/HEARTBEAT.md
```

### 5. Start Openclaw

```bash
openclaw daemon start
```

### 6. Approve Pairing

The first time someone messages the bot on Telegram, Openclaw requires approval:

```bash
openclaw pairing
```

### 7. Start Chatting

Open Telegram, message your bot, and start controlling your rover!

## Workspace Files

These Markdown files define how your agent behaves. They're loaded into the LLM's system prompt at every session start.

| File | Purpose |
|------|---------|
| `AGENTS.md` | Operating rules, API reference, curl examples. **This is the main instruction file.** |
| `SOUL.md` | Personality, tone, response style. Controls how the agent talks. |
| `IDENTITY.md` | Name, emoji, avatar. The agent's public identity. |
| `USER.md` | Info about you (the human). The agent fills this in over time. |
| `TOOLS.md` | Environment-specific notes (device names, preferences). |
| `HEARTBEAT.md` | Periodic tasks the agent runs automatically. |

## Customization

### Change the personality

Edit `SOUL.md`. The example gives a dry, minimal rover that speaks in first person. Make it whatever you want — a pirate, a scientist, a surfer dude.

### Change the name

Edit `IDENTITY.md`. Give it a name, an emoji, a vibe.

### Add a dance routine

Add a `## Dance Routine` section to `AGENTS.md` with a bash script of movement commands. The agent will execute it when asked to dance.

### Switch TTS provider

Set environment variables in the SDK's `.env`:

```bash
TTS_PROVIDER="edge"          # "edge" (free, default) or "gemini"
TTS_API_KEY=""                # Required for gemini only
TTS_VOICE="en-US-GuyNeural"  # Voice name
```

## How Images Work

When the agent takes a screenshot, it saves the file and outputs `MEDIA:front.png`. Openclaw's gateway detects the `MEDIA:` prefix and automatically attaches the image to the Telegram reply. The file must be saved inside the workspace directory (not `/tmp/`).

## How TTS Works

The agent calls `POST /speak` with text. The SDK converts it to speech using edge-tts or Gemini, then streams the audio to the rover's physical speaker through the Agora RTC channel.

## Architecture

```
Telegram → Openclaw Gateway → LLM Agent → curl to localhost:8000 → Earth Rover
```

1. User sends message in Telegram
2. Openclaw routes it to the LLM agent
3. Agent reads AGENTS.md for available commands
4. Agent uses `exec` tool to run `curl` commands
5. SDK server relays commands to the rover via Agora RTC/RTM
6. Rover moves, takes photos, or speaks

## Troubleshooting

**Agent can't connect to server**: Make sure the SDK is running (`hypercorn main:app --reload`) before chatting.

**Images not sending**: The file must be saved inside the workspace. If you get `LocalMediaAccessError`, check that the workspace path is correct and the image is saved there (not in `/tmp/`).

**TTS not working**: Check that `pip install edge-tts` was run (or `google-genai` for Gemini). Verify with `curl -s -X POST http://localhost:8000/speak -H "Content-Type: application/json" -d '{"text": "test"}'`.

**Bot not responding on Telegram**: Run `openclaw pairing` to approve the user, or check `openclaw daemon status`.

## Resources

- [Openclaw Documentation](https://docs.openclaw.ai)
- [Openclaw Telegram Setup](https://docs.openclaw.ai/channels/telegram)
- [Openclaw Agent Workspace](https://docs.openclaw.ai/concepts/agent-workspace)
- [Earth Rovers Shop](https://shop.frodobots.com)
- [Frodobots Discord](https://discord.com/invite/AUegJCJwyb)
