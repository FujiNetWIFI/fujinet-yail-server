# YAIL (Yet Another Image Loader) Image Server

## About ##
This is the image server software that streams images to the YAIL client.  The server performs the "heavy lifting" of finding the image then converting it to the requested format and finally streams it via TCP to the client.

## Command line ##
** TBD ***

### Features ###
- **Multi-API Image Generation**: Generate images using OpenAI's DALL-E 3 model or Google's Gemini model
- **Local Image Streaming**: Stream images from a local directory
- **Web Camera Support**: Stream live video from a connected webcam
- **Multiple Graphics Modes**: Support for different Atari graphics modes (8, 9, and VBXE)
- **Custom Image Processing**: Automatically resize, crop, and format images for optimal display on Atari
- **HTTP Request Handling**: Properly responds to HTTP requests with appropriate messages
- **Network Detection**: Automatically detects available network interfaces and recommends the best IP for connections

### Requirements ###
- Python 3.6+
- Required Python packages (install via pip):
  - requests
  - duckduckgo_search
  - fastcore
  - pillow
  - tqdm
  - olefile
  - numpy
  - pygame
  - openai
  - python-dotenv
  - netifaces
  - google-generativeai (for Gemini support)

### Server Commands ###
The YAIL server can process the following commands from clients:
- `generate <prompt>` or `gen <prompt>`: Generate an image using the configured image generation model
- `search <terms>`: Search for images using the provided terms (redirects to image generation)
- `camera`: Stream from a connected webcam
- `openai`: Configure image generation settings
- `gfx <mode>`: Set the graphics mode
- `quit`: Exit the client connection

### Configuration ###
The server can be configured using environment variables. Copy the `deployment/env.example` file to `server/env` and edit it to set your API keys and preferences:

```bash
# Image Generation API Configuration
OPENAI_API_KEY=your_openai_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here_if_needed

# Image Generation Model Configuration
GEN_MODEL=dall-e-3  # Options: dall-e-3, dall-e-2, gemini

# OpenAI-specific Configuration (used only with dall-e models)
OPENAI_SIZE=1024x1024
OPENAI_QUALITY=standard
OPENAI_STYLE=vivid
OPENAI_SYSTEM_PROMPT='You are an expert illustrator creating beautiful, imaginative artwork'
```

### API Keys

- For OpenAI models (dall-e-3, dall-e-2), you need an OpenAI API key from [OpenAI's platform](https://platform.openai.com/api-keys)
- For Google Gemini model, you need a Gemini API key from [Google AI Studio](https://aistudio.google.com/)

### Image Generation Models

The server supports multiple image generation models:

1. **OpenAI DALL-E Models**:
   - `dall-e-3`: High-quality image generation with detailed prompt following
   - `dall-e-2`: Faster generation with lower cost
   - Other OpenAI models as they become available

2. **Google Gemini Models**:
   - `gemini-2.5-pro-exp-03-25`: Google's advanced image generation model
   - Other Gemini models as they become available

Set your preferred model using the `GEN_MODEL` environment variable or the `--gen-model` command-line argument. The server automatically detects which API to use based on the model name prefix:
- Models starting with `dall-e-` or `gpt-` use the OpenAI API
- Models starting with `gemini` use the Google Gemini API

```bash
# Example: Using OpenAI DALL-E 3
GEN_MODEL=dall-e-3

# Example: Using Google Gemini
GEN_MODEL=gemini-2.5-pro-exp-03-25
```

### Deployment ###
The `deployment` directory contains scripts and configuration files to help deploy the YAIL server as a systemd service on Linux systems.

#### Deployment Files
- `fujinet-yail.service`: Systemd service file that properly activates the Python virtual environment
- `deploy.sh`: Installation script that sets up the service, environment, and dependencies
- `test_service.sh`: Script to test the YAIL server via curl, automatically detecting server IP and port
- `env.example`: Example environment configuration file

#### Deployment Instructions
1. Clone the repository
2. Navigate to the deployment directory
3. Run the deployment script:
   ```
   ./deploy.sh
   ```
4. The script will:
   - Create a Python virtual environment
   - Install required dependencies
   - Set up the systemd service
   - Configure environment variables

### Testing ###
The `deployment` directory also contains test scripts to verify the server's functionality:

#### Test Scripts
- `test_service.sh`: Tests basic connectivity to the YAIL server
- `test_gen_command.py`: Tests the image generation functionality
- `test_image_gen.py`: Advanced testing script with detailed binary data analysis
- `test_server_logs.py`: Monitors server logs during testing

#### Running Tests
```
# Test basic connectivity
./deployment/test_service.sh

# Test image generation
python deployment/test_gen_command.py "happy people dancing"
```

### Example Usage ###
1. Start the server with local images:
   ```
   python server/yail.py --paths /path/to/images --loglevel INFO
   ```

2. Start the server with OpenAI's DALL-E 3 for image generation:
   ```
   python server/yail.py --openai-api-key your_api_key_here --gen-model dall-e-3
   ```

3. Start the server with Google's Gemini model for image generation:
   ```
   python server/yail.py --gen-model gemini
   ```

4. Start the server as a systemd service:
   ```
   sudo systemctl start fujinet-yail
   ```
