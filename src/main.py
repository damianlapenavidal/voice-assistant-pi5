import os

from dotenv import load_dotenv


def main():
    print("Hello from the Raspberry Pi 5!")
    print("Voice assistant setup test is running.")
    print("If you see this message, Python and the project structure are working.")

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        print("OpenAI API key loaded successfully.")
    else:
        print("Warning: OPENAI_API_KEY not found in .env")


if __name__ == "__main__":
    main()
