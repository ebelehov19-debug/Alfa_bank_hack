#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .local/runtime .local/models
download() {
  local url="$1" target="$2" hash="$3"
  if [[ -f "$target" ]] && echo "$hash  $target" | sha256sum --check --status; then return; fi
  curl --fail --location --retry 5 --retry-all-errors --connect-timeout 30 --continue-at - "$url" -o "$target.part"
  echo "$hash  $target.part" | sha256sum --check
  mv "$target.part" "$target"
}
download 'https://github.com/ggml-org/llama.cpp/releases/download/b11856/llama-b11856-bin-ubuntu-x64.tar.gz' .local/runtime.tar.gz 71b660dd4a90c13db899acdef59862226f7ec8d27907b7c107febc99904c00be
tar -xzf .local/runtime.tar.gz -C .local/runtime
download 'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf' .local/models/qwen2.5-1.5b-instruct-q4_k_m.gguf 6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e
echo 'Готово. Запуск: bash run-local.sh'
