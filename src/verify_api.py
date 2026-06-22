"""Verify OPENAI_API_KEY works (simple API call, not Realtime)."""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI


def main():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not found in .env")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    models = client.models.list()
    count = len(models.data)

    print("API key is valid.")
    print(f"Connected to OpenAI ({count} models visible on your account).")


if __name__ == "__main__":
    main()
