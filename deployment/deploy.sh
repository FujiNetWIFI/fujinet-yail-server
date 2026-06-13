#!/bin/bash

# Script to deploy the FujiNet YAIL Server systemd service
# This script should be run with sudo privileges

# Exit on error
set -e

# Configuration
SERVICE_NAME="fujinet-yail"
SERVICE_FILE="${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_SOURCE="${SCRIPT_DIR}/${SERVICE_FILE}"
SERVICE_DEST="/etc/systemd/system/${SERVICE_FILE}"

# Check if script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo privileges"
  exit 1
fi

# Ask which user the service should run as and where to install.
read -r -p "User to run the YAIL service as [fujinet]: " FUJINET_USER
FUJINET_USER="${FUJINET_USER:-fujinet}"

read -r -p "Full path of the install directory [/opt/fujinet-yail-server]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-/opt/fujinet-yail-server}"
case "$INSTALL_DIR" in
  /*) ;;
  *)
    echo "ERROR: install directory must be an absolute path (got: $INSTALL_DIR)"
    exit 1
    ;;
esac

echo "Deploying as user '$FUJINET_USER' to '$INSTALL_DIR'"

# systemd does not expand variables in WorkingDirectory= or the ExecStart=
# executable path, so render the unit template with absolute paths.
RENDERED_SERVICE="$(mktemp)"
trap 'rm -f "$RENDERED_SERVICE"' EXIT
sed -e "s|@YAIL_USER@|${FUJINET_USER}|g" \
    -e "s|@YAIL_ROOT@|${INSTALL_DIR}|g" \
    "$SERVICE_SOURCE" > "$RENDERED_SERVICE"

# Function to check if service exists and if it's different from our version
check_service() {
  if [ -f "$SERVICE_DEST" ]; then
    echo "Service file already exists at $SERVICE_DEST"

    # Check if files are different
    if diff -q "$RENDERED_SERVICE" "$SERVICE_DEST" >/dev/null; then
      echo "Existing service file is identical to the new one. No changes needed."
      return 1  # No changes needed
    else
      echo "Existing service file is different from the new one. Will update."
      return 0  # Update needed
    fi
  else
    echo "Service file does not exist. Will install."
    return 0  # Install needed
  fi
}

# Function to setup the virtual environment and install dependencies
setup_environment() {
  echo "Setting up virtual environment and installing dependencies..."

  # Check if the service user exists, create if not
  if ! id -u "$FUJINET_USER" &>/dev/null; then
    echo "Creating $FUJINET_USER user..."
    useradd -m -s /bin/bash "$FUJINET_USER"
  fi

  # Create installation directory if it doesn't exist
  if [ ! -d "$INSTALL_DIR" ]; then
    echo "Creating installation directory at $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
  fi

  # Copy project files to installation directory
  echo "Copying project files to $INSTALL_DIR..."
  cp -r "$PROJECT_ROOT"/* "$INSTALL_DIR"

  # Set ownership
  echo "Setting ownership to $FUJINET_USER..."
  chown -R "$FUJINET_USER:$FUJINET_USER" "$INSTALL_DIR"

  # Setup virtual environment
  echo "Setting up Python virtual environment..."
  cd "$INSTALL_DIR"
  if [ ! -d "venv" ]; then
    echo "Creating new virtual environment..."
    sudo -u "$FUJINET_USER" python3 -m venv venv
  fi

  # Install the yail-server package with generation backends
  echo "Installing yail-server package..."
  sudo -u "$FUJINET_USER" bash -c "source venv/bin/activate && pip install --upgrade '.[gen]'"

  # Create env file if it doesn't exist
  if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "Creating .env file from example..."
    cp "$INSTALL_DIR/deployment/env.example" "$INSTALL_DIR/.env"
    chown "$FUJINET_USER:$FUJINET_USER" "$INSTALL_DIR/.env"
    echo "NOTE: You need to edit $INSTALL_DIR/.env to add your OpenRouter API key"
  fi

  echo "Environment setup complete."
}

# Function to install or update service
install_service() {
  echo "Installing/updating systemd service..."

  # Copy the rendered service file (absolute paths baked in) into systemd
  cp "$RENDERED_SERVICE" "$SERVICE_DEST"

  # Set proper permissions
  chmod 644 "$SERVICE_DEST"

  # Reload systemd to recognize the new service
  systemctl daemon-reload

  echo "Service file installed at $SERVICE_DEST"
}

# Function to restart service if it's already running
restart_service() {
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Stopping existing $SERVICE_NAME service..."
    systemctl stop "$SERVICE_NAME"
  fi

  echo "Starting $SERVICE_NAME service..."
  systemctl start "$SERVICE_NAME"

  echo "Enabling $SERVICE_NAME service to start on boot..."
  systemctl enable "$SERVICE_NAME"

  echo "Service status:"
  systemctl status "$SERVICE_NAME"
}

# Main execution
echo "Deploying FujiNet YAIL Server systemd service..."

# Setup the environment first
setup_environment

# Check if we need to update the service
if check_service; then
  install_service
  restart_service
else
  # Even if the service file is the same, make sure it's enabled and running
  if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Service is not running. Starting it..."
    systemctl start "$SERVICE_NAME"
    systemctl enable "$SERVICE_NAME"
  else
    echo "Service is already running and up to date."
  fi
fi

echo "Deployment completed successfully!"
