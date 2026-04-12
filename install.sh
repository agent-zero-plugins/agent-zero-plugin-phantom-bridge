#!/usr/bin/env bash
# Phantom Bridge — one-liner installer (last resort, opt-in)
#
# SECURITY WARNING: Piping curl to bash runs code you haven't reviewed.
# Read this script first: https://github.com/notabotchef/phantom-bridge/blob/main/install.sh
# The Quick Start (docker-compose.override.yml) is safer and preferred.
#
# Usage:
#   bash install.sh [OPTIONS]
#   bash <(curl -fsSL https://raw.githubusercontent.com/notabotchef/phantom-bridge/main/install.sh)
#
# Options:
#   --dry-run         Print what would be done without making any changes
#   --yes             Skip confirmation prompts (implies acceptance of security warning)
#   --path=<dir>      Override auto-detected A0 install directory
#   --mode=manual     Use git clone + execute.py instead of docker-compose.override.yml
#   --verbose         Extra output during detection and install steps
#
# Exit codes:
#   0  Success (or dry-run completed)
#   1  General error
#   2  Pre-condition not met (Docker missing, path not found, ambiguous detection)
#   3  SHA verification failure (tampered or mismatched override file)

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
readonly SCRIPT_VERSION="1.4.1"
# GHCR_IMAGE is referenced in install_docker_mode output and in docker-compose.override.yml;
# exported here so it's available if this script is sourced by a wrapper.
GHCR_IMAGE="ghcr.io/notabotchef/phantom-bridge:latest"
readonly GHCR_IMAGE
readonly OVERRIDE_URL="https://raw.githubusercontent.com/notabotchef/phantom-bridge/main/docker-compose.override.yml"
readonly GIT_URL="https://github.com/notabotchef/phantom-bridge.git"
# SHA256 of the canonical override file — updated per release.
# Pinned SHA is published in GitHub Release notes for each version.
readonly OVERRIDE_SHA256="PLACEHOLDER_SHA256_UPDATED_PER_RELEASE"
readonly PLUGIN_NAME="phantom_bridge"

# ---------------------------------------------------------------------------
# Flags (mutable during parse_args)
# ---------------------------------------------------------------------------
DRY_RUN=0
CUSTOM_PATH=""
MODE="docker"      # docker | manual
ASSUME_YES=0
VERBOSE=0

# ---------------------------------------------------------------------------
# Colours (respects NO_COLOR env per https://no-color.org/)
# ---------------------------------------------------------------------------
if [[ -z "${NO_COLOR:-}" && -t 1 ]]; then
    C_RESET='\033[0m'
    C_BOLD='\033[1m'
    C_YELLOW='\033[33m'
    C_RED='\033[31m'
    C_GREEN='\033[32m'
    C_CYAN='\033[36m'
else
    C_RESET='' C_BOLD='' C_YELLOW='' C_RED='' C_GREEN='' C_CYAN=''
fi

log()     { echo -e "${C_BOLD}[phantom-bridge]${C_RESET} $*"; }
log_ok()  { echo -e "${C_GREEN}[OK]${C_RESET} $*"; }
log_warn(){ echo -e "${C_YELLOW}[WARN]${C_RESET} $*"; }
log_err() { echo -e "${C_RED}[ERROR]${C_RESET} $*" >&2; }
log_v()   { [[ "${VERBOSE}" -eq 1 ]] && echo -e "${C_CYAN}[verbose]${C_RESET} $*" || true; }
dry_run() {
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo -e "${C_YELLOW}[dry-run]${C_RESET} would run: $*"
    else
        eval "$*"
    fi
}

# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------
parse_args() {
    for arg in "$@"; do
        case "${arg}" in
            --dry-run)   DRY_RUN=1 ;;
            --yes|-y)    ASSUME_YES=1 ;;
            --verbose)   VERBOSE=1 ;;
            --mode=docker)  MODE="docker" ;;
            --mode=manual)  MODE="manual" ;;
            --path=*)    CUSTOM_PATH="${arg#--path=}" ;;
            --help|-h)
                # Print the comment header (skip shebang on line 1, stop at first non-comment)
                awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
                exit 0
                ;;
            *)
                log_err "Unknown argument: ${arg}"
                exit 1
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# print_warning — security notice + script SHA
# ---------------------------------------------------------------------------
print_warning() {
    local script_sha
    script_sha="$(sha256sum "$0" 2>/dev/null | awk '{print $1}' || shasum -a 256 "$0" 2>/dev/null | awk '{print $1}' || echo 'unavailable')"

    echo ""
    echo -e "${C_YELLOW}${C_BOLD}============================================================${C_RESET}"
    echo -e "${C_YELLOW}${C_BOLD}  SECURITY NOTICE — Phantom Bridge Installer v${SCRIPT_VERSION}${C_RESET}"
    echo -e "${C_YELLOW}${C_BOLD}============================================================${C_RESET}"
    echo ""
    echo "  This script will:"
    echo "    - Detect your Agent Zero install directory"
    if [[ "${MODE}" == "docker" ]]; then
        echo "    - Download docker-compose.override.yml and verify its SHA256"
        echo "    - Restart your Docker Compose stack with the prebuilt image"
    else
        echo "    - Clone/update the Phantom Bridge plugin via git"
        echo "    - Run execute.py inside your A0 container"
    fi
    echo "    - Run bridge_doctor to verify the install"
    echo ""
    echo "  Script SHA256 (this file): ${script_sha}"
    echo "  Source: ${GIT_URL}/blob/main/install.sh"
    echo "  Pinned SHAs per release: ${GIT_URL}/releases"
    echo ""
    echo "  Prefer the Quick Start? It's safer and requires no script:"
    echo "    curl -O ${OVERRIDE_URL}"
    echo "    docker compose up -d"
    echo ""
    echo -e "${C_YELLOW}${C_BOLD}============================================================${C_RESET}"
    echo ""
}

# ---------------------------------------------------------------------------
# confirm — prompts for Y/n unless --yes was passed
# ---------------------------------------------------------------------------
confirm() {
    local prompt="${1:-Continue?}"
    if [[ "${ASSUME_YES}" -eq 1 ]]; then
        log_v "Auto-confirmed: ${prompt}"
        return 0
    fi
    echo -en "${C_BOLD}${prompt} [y/N] ${C_RESET}"
    local answer
    read -r answer
    [[ "${answer}" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------------------
# ensure_docker — verify docker and docker compose are available
# ---------------------------------------------------------------------------
ensure_docker() {
    if ! command -v docker &>/dev/null; then
        log_err "Docker is not installed. Install from https://docs.docker.com/get-docker/"
        exit 2
    fi
    if ! docker compose version &>/dev/null 2>&1; then
        log_err "docker compose (v2) is not available. Update Docker Desktop or install the compose plugin."
        exit 2
    fi
    log_v "Docker $(docker --version) — compose $(docker compose version --short)"
}

# ---------------------------------------------------------------------------
# detect_a0_dir — find the A0 plugins directory
# Returns: prints the path, exits 2 on failure
# ---------------------------------------------------------------------------
detect_a0_dir() {
    if [[ -n "${CUSTOM_PATH}" ]]; then
        if [[ ! -d "${CUSTOM_PATH}" ]]; then
            log_err "Path does not exist: ${CUSTOM_PATH}"
            exit 2
        fi
        echo "${CUSTOM_PATH}"
        return 0
    fi

    local candidates=(
        "./a0-data/usr/plugins"
        "${HOME}/a0/usr/plugins"
        "${HOME}/a0-data/usr/plugins"
        "/opt/a0/usr/plugins"
        "/a0/usr/plugins"
    )

    # Try to detect from running container mounts
    for container_name in "a0" "agent-zero" "a0-agent-zero-1"; do
        if docker inspect "${container_name}" &>/dev/null 2>&1; then
            local mount_path
            mount_path="$(docker inspect "${container_name}" \
                --format '{{range .Mounts}}{{if eq .Destination "/a0/usr"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"
            if [[ -n "${mount_path}" ]]; then
                local plugin_path="${mount_path}/plugins"
                if [[ -d "${plugin_path}" ]]; then
                    log_v "Detected from container '${container_name}' mount: ${plugin_path}"
                    candidates=("${plugin_path}" "${candidates[@]}")
                fi
            fi
        fi
    done

    local found=()
    for candidate in "${candidates[@]}"; do
        if [[ -d "${candidate}" ]]; then
            log_v "Candidate exists: ${candidate}"
            found+=("${candidate}")
        fi
    done

    if [[ ${#found[@]} -eq 0 ]]; then
        log_err "Could not find an Agent Zero plugins directory."
        log_err "Rerun with --path=/your/a0-data/usr/plugins"
        exit 2
    fi

    if [[ ${#found[@]} -gt 1 ]]; then
        if [[ "${ASSUME_YES}" -eq 1 ]]; then
            log_err "Multiple A0 install candidates found. Cannot auto-select under --yes."
            log_err "Rerun with --path=<dir> to specify:"
            for d in "${found[@]}"; do log_err "  ${d}"; done
            exit 2
        fi
        echo ""
        log "Multiple Agent Zero installs detected. Choose one:"
        local i=1
        for d in "${found[@]}"; do
            echo "  ${i}) ${d}"
            ((i++))
        done
        echo -n "Enter number [1-${#found[@]}]: "
        local choice
        read -r choice
        if [[ ! "${choice}" =~ ^[0-9]+$ ]] || [[ "${choice}" -lt 1 ]] || [[ "${choice}" -gt ${#found[@]} ]]; then
            log_err "Invalid choice."
            exit 2
        fi
        echo "${found[$((choice-1))]}"
        return 0
    fi

    echo "${found[0]}"
}

# ---------------------------------------------------------------------------
# install_docker_mode — download override.yml, verify SHA, restart stack
# ---------------------------------------------------------------------------
install_docker_mode() {
    local a0_dir="$1"
    local compose_dir
    # Look for compose file in parent of plugins dir or cwd
    if [[ -f "${a0_dir}/../../docker-compose.yml" ]]; then
        compose_dir="$(realpath "${a0_dir}/../..")"
    elif [[ -f "docker-compose.yml" ]]; then
        compose_dir="$(pwd)"
    else
        compose_dir="$(pwd)"
        log_warn "No docker-compose.yml found in expected locations. Placing override.yml in current directory."
    fi

    local override_dest="${compose_dir}/docker-compose.override.yml"

    log "Image: ${GHCR_IMAGE}"
    log "Compose directory: ${compose_dir}"
    log "Override destination: ${override_dest}"

    # Idempotency check — if override already present with matching SHA, skip
    if [[ -f "${override_dest}" && "${OVERRIDE_SHA256}" != "PLACEHOLDER_SHA256_UPDATED_PER_RELEASE" ]]; then
        local existing_sha
        existing_sha="$(sha256sum "${override_dest}" 2>/dev/null | awk '{print $1}' || shasum -a 256 "${override_dest}" 2>/dev/null | awk '{print $1}' || echo '')"
        if [[ "${existing_sha}" == "${OVERRIDE_SHA256}" ]]; then
            log_ok "docker-compose.override.yml already present and SHA matches — skipping download."
        else
            log_v "Existing override SHA ${existing_sha} != pinned ${OVERRIDE_SHA256} — will re-download."
            _download_and_verify_override "${override_dest}"
        fi
    else
        _download_and_verify_override "${override_dest}"
    fi

    # Restart the stack — prefer explicit -f when we found a compose file
    log "Restarting Docker Compose stack..."
    if [[ -f "${compose_dir}/docker-compose.yml" ]]; then
        dry_run "(cd '${compose_dir}' && docker compose up -d)"
    else
        dry_run "docker compose up -d"
    fi
}

_download_and_verify_override() {
    local dest="$1"
    log "Downloading docker-compose.override.yml..."
    dry_run "curl -fsSL '${OVERRIDE_URL}' -o '${dest}'"

    if [[ "${DRY_RUN}" -eq 0 && "${OVERRIDE_SHA256}" != "PLACEHOLDER_SHA256_UPDATED_PER_RELEASE" ]]; then
        local actual_sha
        actual_sha="$(sha256sum "${dest}" 2>/dev/null | awk '{print $1}' || shasum -a 256 "${dest}" 2>/dev/null | awk '{print $1}')"
        if [[ "${actual_sha}" != "${OVERRIDE_SHA256}" ]]; then
            log_err "SHA256 mismatch — override file may be tampered or from a different version."
            log_err "Expected: ${OVERRIDE_SHA256}"
            log_err "Actual:   ${actual_sha}"
            rm -f "${dest}"
            exit 3
        fi
        log_ok "SHA256 verified: ${actual_sha}"
    elif [[ "${OVERRIDE_SHA256}" == "PLACEHOLDER_SHA256_UPDATED_PER_RELEASE" ]]; then
        log_warn "SHA pinning skipped (no pinned value in this script version — use a tagged release for verified installs)."
    fi
}

# ---------------------------------------------------------------------------
# install_manual_mode — git clone/pull + execute.py in container
# ---------------------------------------------------------------------------
install_manual_mode() {
    local a0_dir="$1"
    local plugin_dir="${a0_dir}/${PLUGIN_NAME}"

    if [[ -d "${plugin_dir}/.git" ]]; then
        log "Existing git clone found at ${plugin_dir} — pulling latest..."
        dry_run "git -C '${plugin_dir}' pull --ff-only"
    else
        log "Cloning Phantom Bridge into ${plugin_dir}..."
        dry_run "git clone --depth=1 '${GIT_URL}' '${plugin_dir}'"
    fi

    # Run execute.py inside the container
    local container_name
    for name in "a0" "agent-zero" "a0-agent-zero-1"; do
        if docker inspect "${name}" &>/dev/null 2>&1; then
            container_name="${name}"
            break
        fi
    done

    if [[ -z "${container_name:-}" ]]; then
        log_warn "No running A0 container found. Run execute.py manually after starting the container:"
        log_warn "  docker exec -it a0 python /a0/usr/plugins/${PLUGIN_NAME}/execute.py"
        return 0
    fi

    log "Running execute.py in container '${container_name}'..."
    dry_run "docker exec '${container_name}' python /a0/usr/plugins/${PLUGIN_NAME}/execute.py"
}

# ---------------------------------------------------------------------------
# post_install_check — run bridge_doctor inside container
# ---------------------------------------------------------------------------
post_install_check() {
    local container_name=""
    for name in "a0" "agent-zero" "a0-agent-zero-1"; do
        if docker inspect "${name}" &>/dev/null 2>&1; then
            container_name="${name}"
            break
        fi
    done

    if [[ -z "${container_name}" ]]; then
        log_warn "No running container found — skipping bridge_doctor check."
        log_warn "After starting the container, run:"
        log_warn "  docker exec ${container_name:-a0} python /a0/usr/plugins/${PLUGIN_NAME}/tools/bridge_doctor.py"
        return 0
    fi

    log "Running bridge_doctor in container '${container_name}'..."
    if [[ "${DRY_RUN}" -eq 0 ]]; then
        if docker exec "${container_name}" python "/a0/usr/plugins/${PLUGIN_NAME}/tools/bridge_doctor.py" --quiet; then
            log_ok "bridge_doctor: HEALTHY"
        else
            log_warn "bridge_doctor reports degraded state. Running verbose output:"
            docker exec "${container_name}" python "/a0/usr/plugins/${PLUGIN_NAME}/tools/bridge_doctor.py" --verbose || true
            echo ""
            log_warn "Troubleshooting: https://github.com/notabotchef/phantom-bridge#troubleshooting"
        fi
    else
        log_v "[dry-run] would run: docker exec ${container_name} python /a0/usr/plugins/${PLUGIN_NAME}/tools/bridge_doctor.py --quiet"
    fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    print_warning

    if [[ "${ASSUME_YES}" -eq 0 ]]; then
        # 5-second countdown for piped-from-curl safety
        echo -e "${C_BOLD}Proceeding in 5 seconds — Ctrl+C to abort.${C_RESET}"
        for i in 5 4 3 2 1; do
            echo -n "  ${i}..."
            sleep 1
        done
        echo ""
        confirm "Install Phantom Bridge?" || { log "Aborted."; exit 0; }
    fi

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "DRY RUN mode — no filesystem changes will be made."
    fi

    ensure_docker

    log "Detecting Agent Zero install directory..."
    local a0_plugins_dir
    a0_plugins_dir="$(detect_a0_dir)"
    log_ok "A0 plugins directory: ${a0_plugins_dir}"

    # Idempotency: already installed at the right version?
    local existing_version=""
    local plugin_yaml="${a0_plugins_dir}/${PLUGIN_NAME}/plugin.yaml"
    if [[ -f "${plugin_yaml}" ]]; then
        existing_version="$(grep '^version:' "${plugin_yaml}" | awk '{print $2}' || true)"
        if [[ "${existing_version}" == "${SCRIPT_VERSION}" ]]; then
            log_ok "Phantom Bridge v${SCRIPT_VERSION} is already installed — nothing to do."
            exit 0
        fi
    fi

    if [[ "${MODE}" == "docker" ]]; then
        install_docker_mode "${a0_plugins_dir}"
    else
        install_manual_mode "${a0_plugins_dir}"
    fi

    post_install_check

    echo ""
    echo -e "${C_GREEN}${C_BOLD}============================================================${C_RESET}"
    echo -e "${C_GREEN}${C_BOLD}  Phantom Bridge v${SCRIPT_VERSION} installed successfully!${C_RESET}"
    echo -e "${C_GREEN}${C_BOLD}============================================================${C_RESET}"
    echo ""
    echo "  Next steps:"
    echo "    1. Open A0 at http://localhost:5050"
    echo "    2. Click the Phantom Bridge icon in the sidebar"
    echo "    3. Tell A0: 'open the browser bridge'"
    echo ""
}

main "$@"
