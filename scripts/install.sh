#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_DIR="${QUANTPILOT_DEFAULT_DIR:-/opt/quantpilot}"
CONFIG_FILE="${QUANTPILOT_CONFIG_FILE:-/etc/quantpilot/quan.conf}"
QUAN_BIN="${QUANTPILOT_QUAN_BIN:-/usr/local/bin/quan}"
TTY_PATH="${QUANTPILOT_TTY:-/dev/tty}"
OS_RELEASE_FILE="${QUANTPILOT_OS_RELEASE:-/etc/os-release}"
REPO_SLUG="Charles-0509/QuantPilot"
IMAGE="ghcr.io/charles-0509/quantpilot:latest"
INSTALL_REF="${QUANTPILOT_INSTALL_REF:-v1.4.0}"
MARKER_FILE=".quantpilot-install"

die() {
    echo "Error: $*" >&2
    exit 1
}

if [[ $EUID -ne 0 && "${QUANTPILOT_ALLOW_NON_ROOT:-0}" != "1" ]]; then
    die "Run this installer as root, for example: curl ... | sudo bash"
fi

if [[ ! -r "$TTY_PATH" ]]; then
    die "An interactive terminal is required. Run the installer directly from a terminal."
fi
exec 3<"$TTY_PATH"

prompt() {
    local message="$1" default="${2:-}" answer
    if [[ -n "$default" ]]; then
        printf '%s [%s]: ' "$message" "$default" >&2
    else
        printf '%s: ' "$message" >&2
    fi
    IFS= read -r answer <&3 || die "Unable to read from the terminal"
    printf '%s\n' "${answer:-$default}"
}

read_choice() {
    local message="$1" valid="$2" answer
    while true; do
        answer="$(prompt "$message")"
        if [[ " $valid " == *" $answer "* ]]; then
            printf '%s\n' "$answer"
            return
        fi
        echo "Please enter one of: $valid" >&2
    done
}

check_platform() {
    [[ -r "$OS_RELEASE_FILE" ]] || die "Cannot read $OS_RELEASE_FILE"
    # shellcheck source=/dev/null
    source "$OS_RELEASE_FILE"
    case "${ID:-}" in
        debian|ubuntu) ;;
        *) die "Only Debian and Ubuntu are supported by this installer." ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64|aarch64|arm64) ;;
        *) die "Only AMD64 and ARM64 systems are supported." ;;
    esac
}

ensure_docker() {
    if [[ "${QUANTPILOT_SKIP_DOCKER_INSTALL:-0}" == "1" ]]; then
        return
    fi
    if command -v docker >/dev/null 2>&1 \
        && docker info >/dev/null 2>&1 \
        && docker compose version >/dev/null 2>&1; then
        return
    fi

    echo "Installing Docker Engine and Docker Compose..."
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    local architecture codename
    architecture="$(dpkg --print-architecture)"
    codename="${VERSION_CODENAME:-}"
    [[ -n "$codename" ]] || die "VERSION_CODENAME is missing from $OS_RELEASE_FILE"
    cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${architecture} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${codename} stable
EOF
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    docker info >/dev/null
    docker compose version >/dev/null
}

load_existing_config() {
    local container_dir container_port
    EXISTING_DIR=""
    EXISTING_PORT=""
    if [[ -r "$CONFIG_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE"
        EXISTING_DIR="${QUANTPILOT_DIR:-}"
        EXISTING_PORT="${QUANTPILOT_PORT:-}"
    elif [[ -f "$DEFAULT_DIR/docker-compose.yml" ]]; then
        EXISTING_DIR="$DEFAULT_DIR"
    elif docker inspect quantpilot >/dev/null 2>&1; then
        container_dir="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' quantpilot 2>/dev/null || true)"
        if [[ -n "$container_dir" && "$container_dir" != "<no value>" ]]; then
            EXISTING_DIR="$container_dir"
        else
            EXISTING_DIR="$DEFAULT_DIR"
        fi
        container_port="$(docker port quantpilot 10000/tcp 2>/dev/null | head -n 1 | awk -F: '{print $NF}')"
        [[ "$container_port" =~ ^[0-9]+$ ]] && EXISTING_PORT="$container_port"
    fi
    if [[ -n "$EXISTING_DIR" && -z "$EXISTING_PORT" && -r "$EXISTING_DIR/docker-compose.yml" ]]; then
        EXISTING_PORT="$(sed -nE 's/.*0\.0\.0\.0:([0-9]+):10000.*/\1/p' "$EXISTING_DIR/docker-compose.yml" | head -n 1)"
    fi
}

is_port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1 && ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)$port$"; then
        return 0
    fi
    (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
}

choose_port() {
    local default="$1" port
    while true; do
        port="$(prompt "Host port" "$default")"
        if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
            echo "Port must be an integer from 1 to 65535." >&2
            continue
        fi
        if is_port_in_use "$port"; then
            echo "Port $port is already in use. Choose another port." >&2
            continue
        fi
        printf '%s\n' "$port"
        return
    done
}

compose_for() {
    local directory="$1"
    docker compose --project-directory "$directory" -f "$directory/docker-compose.yml" "${@:2}"
}

stop_existing() {
    local directory="$1"
    if [[ -f "$directory/docker-compose.yml" ]]; then
        compose_for "$directory" down --remove-orphans || true
    elif docker inspect quantpilot >/dev/null 2>&1; then
        docker rm -f quantpilot || true
    fi
}

uninstall_quantpilot() {
    local directory="${EXISTING_DIR:-$DEFAULT_DIR}" data_choice confirmation
    echo "QuantPilot deployment detected at: $directory"
    data_choice="$(read_choice "1) Keep data  2) Delete data" "1 2")"
    if [[ "$data_choice" == "2" ]]; then
        confirmation="$(prompt "Type DELETE to permanently remove accounts, settings and trading records")"
        [[ "$confirmation" == "DELETE" ]] || die "Deletion cancelled because confirmation did not match."
    fi

    stop_existing "$directory"
    rm -f "$directory/docker-compose.yml" "$directory/.env" "$directory/$MARKER_FILE"
    if [[ "$data_choice" == "2" ]]; then
        rm -rf "$directory/data"
    else
        echo "Persistent data retained at: $directory/data"
    fi
    rmdir "$directory" 2>/dev/null || true
    rm -f "$CONFIG_FILE" "$QUAN_BIN"
    rmdir "$(dirname "$CONFIG_FILE")" 2>/dev/null || true
    while IFS= read -r image_ref; do
        [[ -n "$image_ref" ]] && docker image rm "$image_ref" >/dev/null 2>&1 || true
    done < <(docker images --filter reference='ghcr.io/charles-0509/quantpilot:*' --format '{{.Repository}}:{{.Tag}}' | sort -u)
    echo "QuantPilot was removed. Docker and unrelated containers were not changed."
}

write_compose() {
    local directory="$1" port="$2"
    cat > "$directory/docker-compose.yml" <<EOF
name: quantpilot

services:
  quantpilot:
    image: ${IMAGE}
    container_name: quantpilot
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    ports:
      - "0.0.0.0:${port}:10000"
    restart: unless-stopped
EOF
}

write_default_env() {
    local directory="$1"
    if [[ -f "$directory/.env" ]]; then
        chmod 0600 "$directory/.env"
        return
    fi
    cat > "$directory/.env" <<'EOF'
ALPACA_DATA_FEED=iex
# Alpaca 网络可靠性参数均为可选项；下面是程序默认值。
# ALPACA_CONNECT_TIMEOUT_SECONDS=5
# ALPACA_TRADING_READ_TIMEOUT_SECONDS=6
# ALPACA_DATA_READ_TIMEOUT_SECONDS=45
# ALPACA_RETRY_ATTEMPTS=3
# ALPACA_RETRY_BASE_SECONDS=0.5
# ALPACA_RETRY_MAX_SECONDS=4
# ALPACA_CIRCUIT_FAILURE_THRESHOLD=3
# ALPACA_CIRCUIT_RECOVERY_SECONDS=30
# ALPACA_READ_CACHE_SECONDS=5
# ALPACA_ASSET_CACHE_SECONDS=300
# ALPACA_RECENT_BARS_CACHE_SECONDS=10
# ALPACA_DAILY_BARS_CACHE_SECONDS=900
# ALPACA_STREAM_RETRY_BASE_SECONDS=5
# ALPACA_STREAM_RETRY_MAX_SECONDS=300
INVESTOR_DB_PATH=data/investor.db
QUANTPILOT_HOST=0.0.0.0
QUANTPILOT_PORT=10000
QUANTPILOT_COOKIE_SECURE=false
QUANTPILOT_SESSION_HOURS=12
QUOTE_INTERVAL_SECONDS=5
DEFAULT_SYMBOLS=SPY,QQQ,AAPL,MSFT,NVDA
LOG_LEVEL=INFO
EOF
    chmod 0600 "$directory/.env"
}

install_quan() {
    local source_file="${QUANTPILOT_QUAN_SOURCE:-}" temporary
    temporary="$(mktemp)"
    if [[ -n "$source_file" ]]; then
        cp "$source_file" "$temporary"
    else
        curl -fsSL --max-time 20 \
            "https://raw.githubusercontent.com/${REPO_SLUG}/${INSTALL_REF}/scripts/quan" \
            -o "$temporary"
    fi
    bash -n "$temporary"
    mkdir -p "$(dirname "$QUAN_BIN")"
    install -m 0755 "$temporary" "$QUAN_BIN"
    rm -f "$temporary"
}

write_management_config() {
    local directory="$1" port="$2"
    mkdir -p "$(dirname "$CONFIG_FILE")"
    {
        printf 'QUANTPILOT_DIR=%q\n' "$directory"
        printf 'COMPOSE_FILE=%q\n' "$directory/docker-compose.yml"
        printf 'QUANTPILOT_PORT=%q\n' "$port"
        printf 'REPO_SLUG=%q\n' "$REPO_SLUG"
        printf 'IMAGE=%q\n' "$IMAGE"
        printf 'QUAN_BIN_PATH=%q\n' "$QUAN_BIN"
    } > "$CONFIG_FILE"
    chmod 0644 "$CONFIG_FILE"
}

wait_for_health() {
    local port="$1" response
    local url="http://127.0.0.1:${port}/api/health"
    echo "Waiting for QuantPilot..."
    for _ in {1..30}; do
        if response="$(curl -fsS --max-time 3 "$url" 2>/dev/null)"; then
            printf '%s\n' "$response"
            return 0
        fi
        sleep 2
    done
    return 1
}

server_ip() {
    hostname -I 2>/dev/null | awk '{print $1}' || true
}

install_or_repair() {
    local mode="$1" directory port ip
    if [[ "$mode" == "repair" ]]; then
        directory="${EXISTING_DIR:-$DEFAULT_DIR}"
        port="${EXISTING_PORT:-10000}"
        echo "Repairing the existing deployment at $directory on port $port."
        stop_existing "$directory"
    else
        directory="$(prompt "Installation directory" "$DEFAULT_DIR")"
        [[ "$directory" == /* ]] || die "Installation directory must be an absolute path."
        port="$(choose_port 10000)"
    fi

    mkdir -p "$directory/data"
    chmod 0750 "$directory" "$directory/data"
    write_default_env "$directory"
    write_compose "$directory" "$port"
    printf 'managed-by=quan\n' > "$directory/$MARKER_FILE"
    install_quan
    write_management_config "$directory" "$port"

    compose_for "$directory" pull
    compose_for "$directory" up -d --remove-orphans
    if ! wait_for_health "$port"; then
        compose_for "$directory" logs --tail=100 || true
        die "QuantPilot did not pass its health check within 60 seconds."
    fi

    ip="$(server_ip)"
    echo
    echo "QuantPilot installation completed."
    echo "Management command: quan help"
    if [[ -n "$ip" ]]; then
        echo "Open: http://${ip}:${port}"
    else
        echo "Open: http://SERVER_IP:${port}"
    fi
    echo "Create the initial administrator directly on the first-run page."
    echo "HTTP works for trusted networks. For public access, HTTPS through Nginx or Caddy is strongly recommended."
}

main() {
    local action="install"
    check_platform
    ensure_docker
    load_existing_config
    if [[ -n "$EXISTING_DIR" ]]; then
        echo "An existing QuantPilot deployment was found."
        action="$(read_choice "1) Reinstall/repair  2) Completely remove" "1 2")"
        if [[ "$action" == "2" ]]; then
            uninstall_quantpilot
            return
        fi
        action="repair"
    fi
    install_or_repair "$action"
}

main "$@"
