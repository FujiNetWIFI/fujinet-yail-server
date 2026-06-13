"""Threaded TCP server speaking the YAIL client protocol.

One thread per client; all per-connection state lives in ClientSession.
The byte-level responses are frozen for compatibility with deployed
client binaries (see PROTOCOL.md).
"""
import logging
import random
import re
import socket
import threading
import time
from io import BytesIO

import requests
from PIL import Image

from yail.camera import camera_available, capture_camera_image, init_camera
from yail.config import ImageGenConfig, ServerConfig
from yail.gen import generate_image
from yail.imaging import convert_image_to_yail
from yail.protocol import GRAPHICS_8, build_error_packet
from yail.search import search_images
from yail.stats import STATS

logger = logging.getLogger(__name__)

CLIENT_TIMEOUT = 300      # seconds
DOWNLOAD_TIMEOUT = 5      # seconds (default; ServerConfig.download_timeout overrides)
IMAGE_URL_EXTS = [".jpg", ".jpeg", ".gif", ".png"]
HTTP_METHODS = (b"GET", b"POST", b"PUT", b"DELETE", b"HEAD")
HTTP_FORBIDDEN = (b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                  b"Content-Length: 11\r\n\r\nNot Allowed")


def strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def take_phrase(tokens: list[str]) -> tuple[str, list[str]]:
    """Take a prompt/phrase from the front of a token list.

    A leading double quote groups tokens until the closing quote; without
    quotes all remaining tokens are consumed (legacy behavior, where the
    phrase always ends the request buffer).
    """
    if tokens and tokens[0].startswith('"'):
        for i, token in enumerate(tokens):
            if token.endswith('"') and (i > 0 or len(token) > 1):
                return strip_quotes(" ".join(tokens[:i + 1])), tokens[i + 1:]
    return strip_quotes(" ".join(tokens)), []


def fetch_image(url: str | None = None, filepath: str | None = None,
                timeout: float = DOWNLOAD_TIMEOUT) -> Image.Image:
    """Load a PIL image from a URL or a local path."""
    if url is not None:
        logger.info(f"Loading {url}")
        response = requests.get(url, stream=True, timeout=timeout)
        image_data = b""
        for chunk in response.iter_content(4096):
            image_data += chunk
        return Image.open(BytesIO(image_data))
    return Image.open(filepath)


class ClientSession:
    """State and command handling for a single client connection."""

    def __init__(self, client_socket: socket.socket, thread_id: int,
                 server_config: ServerConfig, gen_config: ImageGenConfig,
                 filenames: list[str]):
        self.socket = client_socket
        self.thread_id = thread_id
        self.server_config = server_config
        self.gen_config = gen_config
        self.filenames = filenames

        self.gfx_mode = GRAPHICS_8
        self.client_mode: str | None = None
        self.last_prompt: str | None = None
        self.last_model: str | None = None
        self.urls: list[str] = []
        self.done = False

    # ----- low-level send helpers -------------------------------------

    def send_text(self, message: str, is_error: bool = False) -> None:
        """Send a text (OK:) or binary error-packet response to the client."""
        try:
            if is_error:
                self.socket.sendall(build_error_packet(message.encode("utf-8"), self.gfx_mode))
                logger.warning(f"{self.thread_id} Sent error to client: {message}")
            else:
                self.socket.sendall(f"OK: {message}\r\n".encode("utf-8"))
                logger.info(f"{self.thread_id} Sent response to client: {message}")
        except Exception as e:
            logger.error(f"{self.thread_id} Failed to send response to client: {e}")

    def stream_image(self, url: str | None = None, filepath: str | None = None) -> bool:
        """Convert one image source to YAI bytes and send it. True on success."""
        start = time.monotonic()
        target = url or filepath or "?"
        try:
            image = fetch_image(url=url, filepath=filepath,
                                timeout=self.server_config.download_timeout)
            self.socket.sendall(convert_image_to_yail(image, self.gfx_mode))
            STATS.image_event(self.thread_id, self.client_mode or "?", target,
                              self.gfx_mode, True, time.monotonic() - start)
            return True
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            logger.error(f"{self.thread_id} Failed to stream {url or filepath}: {e}")
            STATS.image_event(self.thread_id, self.client_mode or "?", target,
                              self.gfx_mode, False, time.monotonic() - start)
            return False

    def stream_random_from_urls(self) -> None:
        if not self.urls:
            self.send_text("No images found", is_error=True)
            return
        for _ in range(self.server_config.stream_max_retries):
            url = random.choice(self.urls)
            if self.stream_image(url=url):
                return
            logger.warning(f"{self.thread_id} Problem with {url}, trying another...")
            time.sleep(self.server_config.stream_retry_wait)
        self.send_text("Could not load any image", is_error=True)

    def stream_random_from_files(self) -> None:
        if not self.server_config.files_enabled:
            self.send_text("File serving is disabled on this server", is_error=True)
            return
        if not self.filenames:
            self.send_text("No image files available", is_error=True)
            return
        for _ in range(self.server_config.stream_max_retries):
            filename = random.choice(self.filenames)
            if self.stream_image(filepath=filename):
                return
            logger.warning(f"{self.thread_id} Problem with {filename}, trying another...")
            time.sleep(self.server_config.stream_retry_wait)
        self.send_text("Could not load any image", is_error=True)

    def stream_generated(self, prompt: str, model: str | None = None) -> None:
        logger.info(f"{self.thread_id} Generating image with prompt: '{prompt}'")
        STATS.incr("generations")
        url_or_path, error = generate_image(prompt, self.gen_config, model=model)
        if not url_or_path:
            STATS.incr("generation_failures")
            self.send_text(f"Generation failed: {error or 'unknown error'}",
                           is_error=True)
            return
        if url_or_path.startswith("http"):
            ok = self.stream_image(url=url_or_path)
        else:
            ok = self.stream_image(filepath=url_or_path)
        if not ok:
            self.send_text("Failed to stream generated image", is_error=True)

    def stream_camera_frame(self) -> None:
        if not camera_available():
            # Lazy init: the legacy server probed the default camera at
            # startup; here the first 'video' request triggers the probe.
            init_camera(self.server_config.camera)
        if not camera_available():
            self.send_text("No camera available on server", is_error=True)
            return
        frame = capture_camera_image()
        if frame is None:
            self.send_text("Failed to capture camera image", is_error=True)
            return
        self.socket.sendall(convert_image_to_yail(frame, self.gfx_mode))

    # ----- command handlers --------------------------------------------

    def handle_video(self, tokens: list[str]) -> list[str]:
        self.client_mode = "video"
        self.stream_camera_frame()
        return tokens[1:]

    def handle_search(self, tokens: list[str]) -> list[str]:
        self.client_mode = "search"
        prompt, rest = take_phrase(tokens[1:])
        logger.info(f"{self.thread_id} Received search '{prompt}'")
        STATS.incr("searches")
        self.urls = search_images(prompt,
                                  max_images=self.server_config.search_max_results,
                                  backends=self.server_config.search_backends)
        self.stream_random_from_urls()
        return rest

    def handle_generate(self, tokens: list[str]) -> list[str]:
        # Current clients send 'gen "<prompt>"' and the server picks the
        # model; legacy clients send 'gen <model> "<prompt>"'.
        self.client_mode = "generate"
        if len(tokens) < 2:
            self.send_text("Usage: gen [model] <prompt>", is_error=True)
            return []
        model: str | None = None
        if tokens[1].startswith('"') or not self.gen_config.is_valid_model(tokens[1]):
            # No model token (or a bare prompt word): server picks the model.
            prompt, rest = take_phrase(tokens[1:])
        else:
            model = tokens[1]
            prompt, rest = take_phrase(tokens[2:])
        logger.info(f"{self.thread_id} Received {tokens[0]} "
                    f"model={model or '(server default)'} prompt='{prompt}'")
        self.last_prompt = prompt
        self.last_model = model
        self.stream_generated(prompt, model=model)
        return rest

    def handle_generate_gemini(self, tokens: list[str]) -> list[str]:
        self.client_mode = "generate"
        prompt, rest = take_phrase(tokens[1:])
        logger.info(f"{self.thread_id} Received gen-gemini prompt='{prompt}'")
        self.last_prompt = prompt
        self.last_model = self.gen_config.DEFAULT_GEMINI_MODEL
        self.stream_generated(prompt, model=self.last_model)
        return rest

    def handle_showurl(self, tokens: list[str]) -> list[str]:
        self.client_mode = "showurl"
        url = strip_quotes(" ".join(tokens[1:]))
        logger.info(f"{self.thread_id} Received showurl {url}")
        if not url.startswith("http"):
            self.send_text("Invalid URL", is_error=True)
        elif not self.stream_image(url=url):
            self.send_text("Failed to load URL", is_error=True)
        return []

    def handle_files(self, tokens: list[str]) -> list[str]:
        self.client_mode = "files"
        self.stream_random_from_files()
        return tokens[1:]

    def handle_next(self, tokens: list[str]) -> list[str]:
        if not self.server_config.streaming_enabled:
            self.send_text("Streaming is disabled on this server", is_error=True)
            return tokens[1:]
        if self.client_mode == "search":
            self.stream_random_from_urls()
        elif self.client_mode == "video":
            self.stream_camera_frame()
        elif self.client_mode == "generate":
            logger.info(f"{self.thread_id} Regenerating image with prompt: '{self.last_prompt}'")
            self.stream_generated(self.last_prompt, model=self.last_model)
        elif self.client_mode == "files":
            self.stream_random_from_files()
        else:
            self.send_text("No previous command to repeat", is_error=True)
        return tokens[1:]

    def handle_gfx(self, tokens: list[str]) -> list[str]:
        try:
            self.gfx_mode = int(tokens[1])
            logger.info(f"{self.thread_id} Graphics mode set to {self.gfx_mode}")
        except (IndexError, ValueError):
            self.send_text("Usage: gfx <mode>", is_error=True)
            return []
        return tokens[2:]

    def handle_openai_config(self, tokens: list[str]) -> list[str]:
        tokens = tokens[1:]
        if not tokens:
            self.send_text(f"Current OpenAI config: {self.gen_config}")
            return []

        param = tokens[0].lower()
        tokens = tokens[1:]
        if not tokens:
            self.send_text(f"Current OpenAI config: {self.gen_config}")
            return []

        value = tokens[0]
        tokens = tokens[1:]

        if param == "model":
            if self.gen_config.set_model(value):
                self.send_text(f"OpenAI model set to {value}")
            else:
                self.send_text("Invalid model. Use a dall-e, gpt or gemini model name", is_error=True)
        elif param == "size":
            if self.gen_config.set_size(value):
                self.send_text(f"Image size set to {value}")
            else:
                self.send_text("Invalid size for the configured model", is_error=True)
        elif param == "quality":
            if self.gen_config.set_quality(value):
                self.send_text(f"Image quality set to {value}")
            else:
                self.send_text("Invalid quality for the configured model", is_error=True)
        elif param == "style":
            if self.gen_config.set_style(value):
                self.send_text(f"Image style set to {value}")
            else:
                self.send_text("Invalid style. Use 'vivid' or 'natural'", is_error=True)
        elif param == "system_prompt":
            self.gen_config.set_system_prompt(value)
            self.send_text(f"System prompt set to {value}")
        else:
            self.send_text(f"Unknown parameter '{param}'. Use 'model', 'size', 'quality', "
                           f"'style', or 'system_prompt'", is_error=True)
        return tokens

    def handle_quit(self, tokens: list[str]) -> list[str]:
        self.done = True
        return tokens[1:]

    # ----- main loop ----------------------------------------------------

    def run(self) -> None:
        self.socket.settimeout(CLIENT_TIMEOUT)
        tokens: list[str] = []

        while not self.done:
            if not tokens:
                request = self.socket.recv(1024)
                if not request:
                    break
                logger.info(f"{self.thread_id} Client request {request!r}")

                if request.startswith(HTTP_METHODS):
                    logger.warning(f"{self.thread_id} HTTP request detected - sending 403")
                    self.socket.sendall(HTTP_FORBIDDEN)
                    break

                try:
                    r_string = request.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning(f"{self.thread_id} Undecodable request, ignoring")
                    continue
                tokens = r_string.rstrip(" \r\n").split(" ")
                STATS.session_update(self.thread_id,
                                     last_command=r_string.rstrip(" \r\n")[:120])

            logger.debug(f"{self.thread_id} Tokens {tokens}")
            command = tokens[0]

            if command == "video":
                tokens = self.handle_video(tokens)
            elif command == "search":
                tokens = self.handle_search(tokens)
            elif command == "gen-gemini":
                tokens = self.handle_generate_gemini(tokens)
            elif command.startswith("gen"):  # gen, generate
                tokens = self.handle_generate(tokens)
            elif command == "showurl":
                tokens = self.handle_showurl(tokens)
            elif command == "files":
                tokens = self.handle_files(tokens)
            elif command == "next":
                tokens = self.handle_next(tokens)
            elif command == "gfx":
                tokens = self.handle_gfx(tokens)
            elif command == "openai-config":
                tokens = self.handle_openai_config(tokens)
            elif command == "quit":
                tokens = self.handle_quit(tokens)
            else:
                tokens = []
                logger.info(f"{self.thread_id} Unrecognized command: {command!r}")
                self.send_text("ACK!")

            STATS.session_update(self.thread_id, mode=self.client_mode,
                                 gfx_mode=self.gfx_mode)


class YailServer:
    """Accept loop dispatching one ClientSession per connection."""

    def __init__(self, server_config: ServerConfig, gen_config: ImageGenConfig,
                 filenames: list[str]):
        self.server_config = server_config
        self.gen_config = gen_config
        self.filenames = filenames
        self.server_socket: socket.socket | None = None
        self.active_threads: list[threading.Thread] = []
        self._connections = 0
        self._lock = threading.Lock()
        self._next_id = 0

    def _handle_client(self, client_socket: socket.socket, thread_id: int,
                       address: str = "?") -> None:
        with self._lock:
            self._connections += 1
            logger.info(f"Starting connection {thread_id} "
                        f"(active: {self._connections})")
        STATS.session_started(thread_id, address)

        session = ClientSession(client_socket, thread_id, self.server_config,
                                self.gen_config, self.filenames)
        try:
            session.run()
        except socket.timeout:
            logger.warning(f"Client connection {thread_id} timed out")
        except ConnectionResetError:
            logger.warning(f"Client connection {thread_id} was reset by the client")
        except BrokenPipeError:
            logger.warning(f"Client connection {thread_id} has a broken pipe")
        except OSError as e:
            logger.warning(f"Client connection {thread_id} socket error: {e}")
        except Exception as e:
            logger.error(f"Error handling client connection {thread_id}: {e}", exc_info=True)
        finally:
            try:
                client_socket.close()
            except OSError:
                pass
            STATS.session_ended(thread_id)
            with self._lock:
                self._connections -= 1
                logger.info(f"Closed connection {thread_id} (active: {self._connections})")

    def bind(self) -> None:
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.server_config.host, self.server_config.port))
        self.server_socket.listen(10)

    def serve_forever(self) -> None:
        if self.server_socket is None:
            self.bind()

        logger.info("=" * 50)
        logger.info("YAIL Server started successfully")
        logger.info(f"Listening on {self.server_config.host}:{self.server_config.port}")
        logger.info("=" * 50)

        while True:
            try:
                client_sock, address = self.server_socket.accept()
            except OSError:
                # Socket closed during shutdown.
                break

            logger.info(f"Accepted connection from {address[0]}:{address[1]}")
            self.active_threads = [t for t in self.active_threads if t.is_alive()]
            self._next_id += 1
            thread = threading.Thread(
                target=self._handle_client,
                args=(client_sock, self._next_id, f"{address[0]}:{address[1]}"),
                daemon=True,
            )
            thread.start()
            self.active_threads.append(thread)

    def shutdown(self) -> None:
        if self.server_socket is not None:
            try:
                self.server_socket.close()
                logger.info("Server socket closed")
            except OSError as e:
                logger.error(f"Error closing server socket: {e}")
        for thread in self.active_threads:
            if thread.is_alive():
                thread.join(timeout=1.0)
