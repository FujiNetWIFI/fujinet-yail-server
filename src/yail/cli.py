"""Command-line entry point for the YAIL server."""
import argparse
import logging
import os
import signal
import socket
import sys

from dotenv import load_dotenv

from yail import __version__
from yail.camera import init_camera, shutdown_camera
from yail.config import DEFAULT_EXTENSIONS, DEFAULT_PORT, ImageGenConfig, ServerConfig
from yail.files import collect_files
from yail.server import YailServer
from yail.stats import install_log_buffer
from yail.webui import DEFAULT_UI_PORT, WebUIContext, start_webui

logger = logging.getLogger("yail")

LOG_LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]


def log_network_info() -> None:
    """Log the addresses clients can likely reach this server on."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        logger.info(f"Recommended IP for client connections: {local_ip}")
    except OSError as e:
        logger.warning(f"Could not determine outbound IP: {e}")

    try:
        import netifaces
        for interface in netifaces.interfaces():
            addresses = netifaces.ifaddresses(interface)
            for address in addresses.get(netifaces.AF_INET, []):
                ip = address.get("addr", "")
                if ip and not ip.startswith("127."):
                    logger.info(f"  Interface {interface}: {ip}")
    except ImportError:
        logger.debug("netifaces not installed; skipping interface listing")
    except Exception as e:
        logger.warning(f"Error listing interfaces: {e}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yail-server", description="YAIL Image Server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--paths", nargs="*", default=[],
                        help="Directories and/or image files to stream for the 'files' command")
    parser.add_argument("--extensions", nargs="*", default=list(DEFAULT_EXTENSIONS),
                        help="File extensions to include")
    parser.add_argument("--camera", nargs="?", const="", default=None,
                        help="Enable webcam streaming (optionally pass a device name)")
    parser.add_argument("--host", default="0.0.0.0", help="Address to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"Port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--loglevel", choices=LOG_LEVELS + [l.lower() for l in LOG_LEVELS],
                        default=None, help="Logging level")
    parser.add_argument("--env-file", default=None,
                        help="Path to an env file (default: ./.env, then legacy ./server/env)")
    parser.add_argument("--openrouter-api-key", default=None, help="OpenRouter API key")
    parser.add_argument("--gen-model", default=None,
                        help="Image generation model, an OpenRouter id "
                             "(e.g. google/gemini-2.5-flash-image)")
    parser.add_argument("--ui-host", default="127.0.0.1",
                        help="Address for the admin web UI (default 127.0.0.1; "
                             "it can change API keys, so expose with care)")
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT,
                        help=f"Port for the admin web UI (default {DEFAULT_UI_PORT})")
    parser.add_argument("--no-ui", action="store_true",
                        help="Disable the admin web UI")
    return parser


def load_environment(env_file: str | None) -> str:
    """Load env vars and return the env file path in effect.

    The env file overrides the process environment (matching legacy
    precedence); CLI args override both. The returned path is where the
    web UI persists configuration changes (it may not exist yet)."""
    if env_file:
        if os.path.exists(env_file):
            logger.info(f"Loading environment variables from {env_file}")
            load_dotenv(env_file, override=True)
        else:
            logger.warning(f"Env file not found: {env_file}")
        return env_file

    for candidate in (".env", os.path.join("server", "env")):
        if os.path.exists(candidate):
            logger.info(f"Loading environment variables from {candidate}")
            load_dotenv(candidate, override=True)
            return candidate
    logger.info("No env file found. Using process environment variables.")
    return ".env"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    args = build_parser().parse_args(argv)

    if args.loglevel:
        logging.getLogger().setLevel(args.loglevel.upper())

    install_log_buffer()
    env_path = load_environment(args.env_file)

    logger.info("Environment:")
    logger.info(f"  OPENROUTER_API_KEY: "
                f"{'set' if os.environ.get('OPENROUTER_API_KEY') else 'not set'}")
    logger.info(f"  GEN_MODEL: {os.environ.get('GEN_MODEL', 'not set (server default)')}")

    gen_config = ImageGenConfig()
    if args.openrouter_api_key:
        gen_config.set_api_key(args.openrouter_api_key)
    if args.gen_model:
        gen_config.set_model(args.gen_model)
    logger.info(f"Image generation: {gen_config}")

    # Local file serving is opt-in: --paths on the CLI is an explicit choice;
    # otherwise FILES_PATH + FILES_ENABLED=true from the env file (managed by
    # the web UI) enable it. With neither, the 'files' command is disabled.
    env_files_path = os.environ.get("FILES_PATH", "").strip()
    env_files_enabled = os.environ.get("FILES_ENABLED", "").strip().lower() in ("1", "true", "yes")
    if args.paths:
        paths, files_enabled = args.paths, True
    elif env_files_path:
        paths, files_enabled = [env_files_path], env_files_enabled
    else:
        paths, files_enabled = [], False

    def env_bool(name: str, default: bool) -> bool:
        value = os.environ.get(name, "").strip().lower()
        return default if not value else value in ("1", "true", "yes")

    def env_num(name: str, default, cast):
        try:
            return cast(os.environ.get(name, ""))
        except (TypeError, ValueError):
            return default

    search_backends = [b.strip().lower()
                       for b in os.environ.get("SEARCH_BACKENDS", "auto").split(",")
                       if b.strip()] or ["auto"]

    server_config = ServerConfig(
        host=args.host,
        port=args.port if args.port is not None else int(os.environ.get("YAIL_PORT", DEFAULT_PORT)),
        paths=paths,
        extensions=args.extensions,
        camera=args.camera if args.camera else None,
        enable_camera=args.camera is not None,
        files_enabled=files_enabled,
        streaming_enabled=env_bool("STREAM_ENABLED", True),
        stream_max_retries=env_num("STREAM_MAX_RETRIES", 10, int),
        stream_retry_wait=env_num("STREAM_RETRY_WAIT", 1.0, float),
        download_timeout=env_num("STREAM_DOWNLOAD_TIMEOUT", 5.0, float),
        search_backends=search_backends,
        search_max_results=env_num("SEARCH_MAX_RESULTS", 1000, int),
    )

    filenames = collect_files(server_config.paths, server_config.extensions) if paths else []
    if filenames:
        logger.info(f"Local file serving {'ENABLED' if files_enabled else 'disabled'}: "
                    f"{len(filenames)} files from {', '.join(paths)}")
    else:
        logger.info("Local file serving disabled (no folder configured)")

    if server_config.enable_camera:
        if not init_camera(server_config.camera):
            logger.warning("Camera requested but could not be initialized")

    server = YailServer(server_config, gen_config, filenames)
    try:
        server.bind()
    except OSError as e:
        logger.error(f"Error binding to {server_config.host}:{server_config.port}: {e}")
        logger.error("Port may already be in use. Try killing any existing YAIL processes.")
        return 1

    webui_server = None
    if not args.no_ui:
        context = WebUIContext(gen_config, server_config, filenames, env_path=env_path)
        try:
            webui_server = start_webui(args.ui_host, args.ui_port, context)
        except OSError as e:
            logger.warning(f"Could not start admin web UI on "
                           f"{args.ui_host}:{args.ui_port}: {e}")

    def signal_handler(sig, frame):
        logger.info("Shutting down YAIL server...")
        shutdown_camera()
        if webui_server is not None:
            webui_server.shutdown()
        server.shutdown()
        logger.info("YAIL server shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log_network_info()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
