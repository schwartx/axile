#!/usr/bin/env bash
# 仅启动文档用公共 Axile 与离线替身；不会连接任何柜台。
set -euo pipefail
DOCS_SUPPORT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DOCS_AXILE_ROOT="$(cd -- "$DOCS_SUPPORT_DIR/../../.." && pwd)"
DOCS_RUNTIME_DIR="${1:-/tmp/axile-docs-demo}"
mkdir -p -- "$DOCS_RUNTIME_DIR"
DOCS_RUNTIME_DIR="$(cd -- "$DOCS_RUNTIME_DIR" && pwd)"
UV_PROJECT_ENVIRONMENT="$DOCS_RUNTIME_DIR/.venv"   uv sync --project "$DOCS_AXILE_ROOT" --frozen --no-dev --extra tqsdk
if [[ ! -e "$DOCS_RUNTIME_DIR/config.toml" ]]; then
  cat > "$DOCS_RUNTIME_DIR/config.toml" <<'TOML'
sqlalchemy_database_uri = "sqlite+aiosqlite:///./axile.db"
environment = "local"
app_log_dir = "./logs"
exe_err_feishu_key = ""
algorithm_modules = []
algorithm_directories = []
TOML
fi
cd -- "$DOCS_RUNTIME_DIR"
export AXILE_DOCS_OFFLINE=1
export AXILE_DOCS_STATE_DIR="$DOCS_RUNTIME_DIR"
export AXILE_CONFIG_TOML="$DOCS_RUNTIME_DIR/config.toml"
export PYTHONPATH="$DOCS_SUPPORT_DIR/harness"
exec "$DOCS_RUNTIME_DIR/.venv/bin/axile" --host 127.0.0.1   --port "${AXILE_DOCS_PORT:-8019}" --algorithm-dir "$DOCS_RUNTIME_DIR/user_algorithms"
