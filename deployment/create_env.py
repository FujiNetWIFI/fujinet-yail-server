#!/usr/bin/env python

import os

DEFAULT_MODEL = "google/gemini-2.5-flash-image"
DEFAULT_SYSTEM_PROMPT = ("You are an image generation assistant. "
                         "Generate an image based on the user's description.")

# Common image-output models on OpenRouter; any vendor/model id works.
# Browse the full list: https://openrouter.ai/models?output_modalities=image
MODEL_CHOICES = [
    "google/gemini-2.5-flash-image",
    "openai/gpt-image-1",
    "black-forest-labs/flux.2-pro",
]


def create_env_file():
    """Interactively create the .env file for the YAIL server."""
    # The .env lives in the project root (one level up from deployment/)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    server_dir = os.path.normpath(os.path.join(script_dir, '..'))
    env_path = os.path.join(server_dir, '.env')

    # Check if env file already exists
    if os.path.exists(env_path):
        overwrite = input(f"env file already exists at {env_path}. Overwrite? (y/n): ")
        if overwrite.lower() != 'y':
            print("Operation cancelled.")
            return

    # One key covers every model: https://openrouter.ai/keys
    api_key = input("Enter your OpenRouter API key: ").strip()

    # Model selection
    print("\nImage generation model (any OpenRouter image-output model id works):")
    for i, model_id in enumerate(MODEL_CHOICES, 1):
        default = " (default)" if i == 1 else ""
        print(f"{i}. {model_id}{default}")
    print(f"{len(MODEL_CHOICES) + 1}. enter a custom model id")
    model = DEFAULT_MODEL
    model_choice = input(f"Select model (1-{len(MODEL_CHOICES) + 1}, default is 1): ").strip()
    if model_choice.isdigit():
        choice = int(model_choice)
        if 2 <= choice <= len(MODEL_CHOICES):
            model = MODEL_CHOICES[choice - 1]
        elif choice == len(MODEL_CHOICES) + 1:
            model = input("Enter the OpenRouter model id (vendor/model): ").strip() or DEFAULT_MODEL

    # System prompt
    system_prompt = DEFAULT_SYSTEM_PROMPT
    custom_prompt = input("\nWould you like to customize the system prompt for image generation? (y/n): ")
    if custom_prompt.lower() == 'y':
        print("\nDefault system prompt: " + system_prompt)
        system_prompt = input("Enter your custom system prompt: ").strip() or DEFAULT_SYSTEM_PROMPT

    # Create env file content
    env_content = f"""# OpenRouter API Configuration (one key covers every model)
OPENROUTER_API_KEY={api_key}
GEN_MODEL={model}
OPENAI_SYSTEM_PROMPT="{system_prompt}"
"""

    # Write to env file
    with open(env_path, 'w') as f:
        f.write(env_content)

    print(f"\nenv file created successfully at {env_path}")
    print("This file contains your API key and should not be committed to version control.")
    print("It has been added to .gitignore for your protection.")
    print("\nTo start the server with these settings, run:")
    print(f"cd {server_dir}")
    print("source venv/bin/activate")
    print("yail-server --loglevel INFO")
    print("\nSettings can also be changed later in the admin web UI "
          "(http://127.0.0.1:5557/ by default).")


if __name__ == "__main__":
    create_env_file()
