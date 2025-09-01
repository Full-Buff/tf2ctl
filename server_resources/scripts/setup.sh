#!/bin/bash
# TF2 Tournament Server Setup Script
# This script configures a complete TF2 server using Melkor's container
# Run after basic system setup (Docker, user creation) is complete

set -e
LOG_FILE="/var/log/tf2-setup.log"
exec > >(tee -a $LOG_FILE) 2>&1

echo "=== TF2 Server Setup Started at $(date) ==="

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root"
   exit 1
fi

# Verify Docker is available
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed"
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh ./get-docker.sh
fi

# Verify tf2server user exists
if ! id "tf2server" &>/dev/null; then
    echo "ERROR: tf2server user does not exist"
    # make user with home directory
    useradd -m tf2server
fi

echo "Prerequisites verified - Docker and tf2server user are ready"

# =============================================================================
# CONFIGURATION VARIABLES (replaced by TF2 Manager)
# =============================================================================
# Default values (will be replaced by string substitution)
SERVER_HOSTNAME="SERVER_HOSTNAME_REPLACE"
RCON_PASSWORD="RCON_PASSWORD_REPLACE"
SERVER_PASSWORD="SERVER_PASSWORD_REPLACE"
STV_PASSWORD="STV_PASSWORD_REPLACE"
START_MAP="START_MAP_REPLACE"
DEMOS_TF_APIKEY="DEMOS_TF_APIKEY_REPLACE"
LOGS_TF_APIKEY="LOGS_TF_APIKEY_REPLACE"

echo "=== Server Configuration ==="
echo "SERVER_HOSTNAME: ${SERVER_HOSTNAME}"
echo "START_MAP: ${START_MAP}"
echo "SERVER_PASSWORD: $([ -n "$SERVER_PASSWORD" ] && echo '[SET]' || echo '[EMPTY]')"
echo "STV_PASSWORD: ${STV_PASSWORD}"
echo "RCON_PASSWORD: [REDACTED]"
echo "DEMOS_TF_APIKEY: $([ -n "$DEMOS_TF_APIKEY" ] && echo '[SET]' || echo '[EMPTY]')"
echo "LOGS_TF_APIKEY: $([ -n "$LOGS_TF_APIKEY" ] && echo '[SET]' || echo '[EMPTY]')"
echo "=============================="

# =============================================================================
# DIRECTORY STRUCTURE SETUP
# =============================================================================
echo "Setting up TF2 server directory structure..."
mkdir -p /home/tf2server/tf2-server/{cfg,maps,addons,logs}
mkdir -p /home/tf2server/tf2-server/addons/{sourcemod/plugins,sourcemod/configs,sourcemod/data,metamod}

# Set proper ownership
chown -R tf2server:tf2server /home/tf2server/tf2-server
chmod -R 755 /home/tf2server/tf2-server

echo "Directory structure created"

# =============================================================================
# FIREWALL CONFIGURATION
# =============================================================================
echo "Configuring firewall for TF2..."
ufw --force enable
ufw allow ssh
ufw allow 27015/udp  # TF2 game port (UDP primary)
ufw allow 27015/tcp  # TF2 game port (TCP for queries)
ufw allow 27020/udp  # SourceTV port
ufw reload

echo "Firewall configured"

# =============================================================================
# CUSTOM REMOTE FILE DOWNLOADS
# =============================================================================
# This section is for users who want to download additional files from remote sources
# Examples: custom maps, plugins, configs from GitHub, fastDL, etc.
#
# EXAMPLE - Uncomment and modify as needed:
#
# echo "Downloading custom maps..."
# cd /home/tf2server/tf2-server/maps
# wget -q https://example.com/path/to/custom_map.bsp
#
# echo "Downloading custom plugins..."
# cd /home/tf2server/tf2-server/addons/sourcemod/plugins
# wget -q https://github.com/user/plugin/releases/latest/download/plugin.smx
#
# echo "Downloading custom configs..."
# cd /home/tf2server/tf2-server/cfg
# wget -q https://raw.githubusercontent.com/user/configs/main/server.cfg

echo "Custom downloads section (modify script to add custom downloads)"

# =============================================================================
# CONTAINER SETUP AND DEPLOYMENT
# =============================================================================
echo "Setting up TF2 container..."

# Download Badlands as default
cd /home/tf2server/tf2-server/maps
wget -q https://fastdl.fullbuff.gg/tf/maps/cp_badlands.bsp

# Pre-pull the container image
echo "Pulling TF2 server container image..."
docker pull ghcr.io/melkortf/tf2-competitive:latest

echo "Starting TF2 server container..."
echo "Server: ${SERVER_HOSTNAME}"
echo "Map: ${START_MAP}"

# Stop and remove existing container if it exists
if [ "$(docker ps -aq -f name=tf2)" ]; then
    echo "Stopping existing TF2 container..."
    docker stop tf2 2>/dev/null || true
    docker rm tf2 2>/dev/null || true
fi

# Start the TF2 server container with hybrid approach (env vars + command line args)
echo "Launching TF2 server container..."
docker run -d \
    --name tf2 \
    --restart unless-stopped \
    -p 27015:27015/udp \
    -p 27015:27015/tcp \
    -p 27020:27020/udp \
    -v /home/tf2server/tf2-server/maps:/home/tf2/server/tf/maps \
    -e "RCON_PASSWORD=${RCON_PASSWORD}" \
    -e "SERVER_HOSTNAME=${SERVER_HOSTNAME}" \
    -e "SERVER_PASSWORD=${SERVER_PASSWORD}" \
    -e "STV_NAME=${SERVER_HOSTNAME} TV" \
    -e "STV_PASSWORD=${STV_PASSWORD}" \
    -e "DEMOS_TF_APIKEY=${DEMOS_TF_APIKEY}" \
    -e "LOGS_TF_APIKEY=${LOGS_TF_APIKEY}" \
    -e "ENABLE_FAKE_IP=1" \
    ghcr.io/melkortf/tf2-competitive:latest \
    +map "${START_MAP}" \
    +rcon_password "${RCON_PASSWORD}" \
    +hostname "${SERVER_HOSTNAME}" \
    +sv_password "${SERVER_PASSWORD}" \
    +tv_name "${SERVER_HOSTNAME} TV" \
    +tv_password "${STV_PASSWORD}" \
    +sm_demostf_apikey "${DEMOS_TF_APIKEY}" \
    +logstf_apikey "${LOGS_TF_APIKEY}"

# Wait for container to start
echo "Waiting for container to start..."
sleep 15

# Verify container is running
if [ "$(docker ps -q -f name=tf2)" ]; then
    echo "✅ TF2 server container started successfully!"

    # Get server IP
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "unknown")

    echo ""
    echo "=== CONNECTION INFORMATION ==="
    echo "Game Server: connect ${SERVER_IP}:27015"
    if [ -n "${SERVER_PASSWORD}" ]; then
        echo "Game Password: ${SERVER_PASSWORD}"
    fi
    echo "SourceTV: connect ${SERVER_IP}:27020"
    echo "SourceTV Password: ${STV_PASSWORD}"
    echo "RCON: rcon_address ${SERVER_IP}:27015"
    echo "RCON Password: ${RCON_PASSWORD}"
    echo "=============================="
    echo ""
else
    echo "❌ Failed to start TF2 server container"
    echo "Container logs:"
    docker logs tf2 2>/dev/null || echo "No logs available"
    exit 1
fi

# Set final permissions
echo "Setting final permissions..."
chown -R tf2server:tf2server /home/tf2server/tf2-server

# Create init-done marker for tf2ctl wait logic
mkdir -p /var/local
date -u > /var/local/tf2ctl-init-done

# TF2CTL_POSTCOPY
bash /root/tf2-copy.sh || true

# Create completion marker for the Python application
touch /tmp/tf2-setup-complete

# Final container restart
echo "Restarting TF2 server container..."
docker restart tf2

echo ""
echo "=== TF2 Server Setup Completed Successfully at $(date) ==="
echo ""
echo "Setup Summary:"
echo "✅ Directory structure created"
echo "✅ Firewall configured (ports 27015, 27020)"
echo "✅ TF2 container image pulled"
echo "✅ TF2 server container started"
echo ""
echo "The server is ready! Custom configs will be uploaded by TF2 Manager."
echo "=== Setup Complete ==="
