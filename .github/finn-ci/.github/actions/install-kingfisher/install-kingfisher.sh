#!/usr/bin/env bash
set -euo pipefail

readonly KINGFISHER_VERSION="v1.108.0"
readonly KINGFISHER_LINUX_ARM64_SHA256="bfba78fdde41b82c2d630e1417de46fbf9c8071ccfba3c814b2c8fcfd4bf0f7c"
readonly KINGFISHER_LINUX_X64_SHA256="b9ba14a1e5ffdfa153bc591d73fa75641610820b9b6fc8faab89f3206c483e1c"
readonly KINGFISHER_RELEASE_URL="https://github.com/mongodb/kingfisher/releases/download"

report_error() {
  local message="${1:?An error message is required}"
  printf 'Error: %s\n' "$message" >&2
}

kingfisher_architecture() {
  local machine
  machine="$(uname -m)"
  case "$machine" in
    aarch64|arm64)
      printf 'arm64\n'
      ;;
    amd64|x86_64)
      printf 'x64\n'
      ;;
    *)
      report_error "Unsupported runner architecture: $machine"
      return 1
      ;;
  esac
}

kingfisher_checksum() {
  local architecture="${1:?A Kingfisher architecture is required}"
  case "$architecture" in
    arm64)
      printf '%s\n' "$KINGFISHER_LINUX_ARM64_SHA256"
      ;;
    x64)
      printf '%s\n' "$KINGFISHER_LINUX_X64_SHA256"
      ;;
    *)
      report_error "Unsupported Kingfisher architecture: $architecture"
      return 1
      ;;
  esac
}

install_kingfisher() (
  local install_dir="${1:?An installation directory is required}"
  local architecture checksum archive_path download_url temp_dir=""

  cleanup() {
    if [[ -n "$temp_dir" ]]; then
      rm -rf -- "$temp_dir"
    fi
  }

  trap cleanup EXIT
  if ! temp_dir="$(mktemp -d)"; then
    report_error "Could not create a temporary directory for Kingfisher"
    trap - EXIT
    return 1
  fi
  if ! architecture="$(kingfisher_architecture)"; then
    cleanup
    trap - EXIT
    return 1
  fi
  if ! checksum="$(kingfisher_checksum "$architecture")"; then
    cleanup
    trap - EXIT
    return 1
  fi
  archive_path="$temp_dir/kingfisher-linux-${architecture}.tgz"
  download_url="${KINGFISHER_RELEASE_URL}/${KINGFISHER_VERSION}/kingfisher-linux-${architecture}.tgz"

  if ! curl --fail --silent --show-error --location \
      --proto '=https' \
      --proto-redir '=https' \
      --tlsv1.2 \
      "$download_url" \
      --output "$archive_path"; then
    report_error "Could not download Kingfisher from ${download_url}"
    cleanup
    trap - EXIT
    return 1
  fi

  # Verify the archive before extracting or executing anything from it.
  if ! printf '%s  %s\n' "$checksum" "$archive_path" | sha256sum --check --status; then
    report_error "Kingfisher archive checksum verification failed"
    cleanup
    trap - EXIT
    return 1
  fi

  if ! tar -xzf "$archive_path" -C "$temp_dir" kingfisher; then
    report_error "Could not extract the verified Kingfisher archive"
    cleanup
    trap - EXIT
    return 1
  fi
  if ! chmod 0755 "$temp_dir/kingfisher"; then
    report_error "Could not prepare the extracted Kingfisher binary"
    cleanup
    trap - EXIT
    return 1
  fi

  # The archive is checksum-verified before this executable validation.
  if ! "$temp_dir/kingfisher" --version; then
    report_error "Downloaded Kingfisher failed executable validation"
    cleanup
    trap - EXIT
    return 1
  fi

  if [[ ! -d "$install_dir" ]]; then
    if [[ -w "$(dirname "$install_dir")" ]]; then
      if ! mkdir -p "$install_dir"; then
        report_error "Could not create the Kingfisher installation directory"
        cleanup
        trap - EXIT
        return 1
      fi
    elif ! sudo mkdir -p "$install_dir"; then
      report_error "Could not create the Kingfisher installation directory with sudo"
      cleanup
      trap - EXIT
      return 1
    fi
  fi

  if [[ -w "$install_dir" ]]; then
    if ! install -m 0755 "$temp_dir/kingfisher" "$install_dir/kingfisher"; then
      report_error "Could not install Kingfisher"
      cleanup
      trap - EXIT
      return 1
    fi
  elif ! sudo install -m 0755 "$temp_dir/kingfisher" "$install_dir/kingfisher"; then
    report_error "Could not install Kingfisher with sudo"
    cleanup
    trap - EXIT
    return 1
  fi

  cleanup
  trap - EXIT
)

if [[ "${BASH_SOURCE[0]:-}" == "$0" ]]; then
  install_kingfisher /usr/local/bin
fi
