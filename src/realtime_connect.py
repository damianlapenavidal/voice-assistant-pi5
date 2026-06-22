"""Test OpenAI Realtime API over WebSocket (text only — no mic or speaker yet)."""

import asyncio
import json
import os
import sys

import websockets
from dotenv import load_dotenv

DEFAULT_MODEL = "gpt-realtime-mini"
REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"


async def wait_for_event(ws, expected_type, timeout=30):
    """Read messages until one matches expected_type."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        event = json.loads(raw)
        event_type = event.get("type", "")
        print(f"  <- {event_type}")

        if event_type == "error":
            print("ERROR:", json.dumps(event, indent=2))
            sys.exit(1)

        if event_type == expected_type:
            return event


async def run_test():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not found in .env")
        sys.exit(1)

    model = os.getenv("REALTIME_MODEL", DEFAULT_MODEL)
    url = REALTIME_URL.format(model=model)
    headers = {"Authorization": f"Bearer {api_key}"}

    print(f"Connecting to Realtime API (model: {model})...")

    async with websockets.connect(url, additional_headers=headers) as ws:
        print("WebSocket connected.")

        session_event = await wait_for_event(ws, "session.created")
        session_id = session_event.get("session", {}).get("id", "unknown")
        print(f"Session created: {session_id}")

        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "instructions": (
                            "You are a helpful assistant. "
                            "Reply in one short sentence."
                        ),
                        "output_modalities": ["text"],
                    },
                }
            )
        )
        await wait_for_event(ws, "session.updated")

        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "What's the weather in Kingston, Rhode Island?",
                            }
                        ],
                    },
                }
            )
        )
        await ws.send(json.dumps({"type": "response.create"}))

        response_parts = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
            event = json.loads(raw)
            event_type = event.get("type", "")
            print(f"  <- {event_type}")

            if event_type == "error":
                print("ERROR:", json.dumps(event, indent=2))
                sys.exit(1)

            if event_type == "response.output_text.delta":
                response_parts.append(event.get("delta", ""))
            elif event_type == "response.done":
                break

        print(f"\nAssistant: {''.join(response_parts)}")
        print("\nRealtime API text test passed.")


def main():
    asyncio.run(run_test())


if __name__ == "__main__":
    main()
