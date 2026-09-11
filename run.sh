#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
SERVICE_NAME="voice-unlock.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

# Check virtual environment
if [ ! -x "${PYTHON_BIN}" ]; then
    echo "❌ Error: Virtual environment not found at ${VENV_DIR}."
    echo "👉 Please create it using: uv venv --python 3.11 ${VENV_DIR}"
    exit 1
fi

# Initialize config if not present
if [ ! -f "${SCRIPT_DIR}/config.json" ] && [ -f "${SCRIPT_DIR}/config.example.json" ]; then
    cp "${SCRIPT_DIR}/config.example.json" "${SCRIPT_DIR}/config.json"
fi

# Ensure config file is protected (read/write only by owner)
if [ -f "${SCRIPT_DIR}/config.json" ]; then
    chmod 600 "${SCRIPT_DIR}/config.json" 2>/dev/null || true
fi

# Export CUDA shared libraries from venv if present
CUDA_LIB_PATHS="${VENV_DIR}/lib/python3.11/site-packages/nvidia/cudnn/lib:${VENV_DIR}/lib/python3.11/site-packages/nvidia/cublas/lib"
if [ -d "${VENV_DIR}/lib/python3.11/site-packages/nvidia/cudnn/lib" ]; then
    export LD_LIBRARY_PATH="${CUDA_LIB_PATHS}:${LD_LIBRARY_PATH}"
fi

print_usage() {
    echo "=========================================================="
    echo "              OpenTheDoor - Voice Unlock                  "
    echo "=========================================================="
    echo "Usage: ./run.sh [command]"
    echo ""
    echo "Commands:"
    echo "  test              Run interactive microphone and STT test"
    echo "  test-unit         Run automated audit & unit test suite"
    echo "  download-model    Pre-download AI Faster-Whisper model"
    echo "  run               Run the daemon in foreground (live unlock)"
    echo "  dry-run           Run the daemon in foreground (without actual unlock)"
    echo "  autostart [on|off] Turn auto-start on boot ON or OFF"
    echo "  install-service   Install and start systemd user service"
    echo "  status-service    Check the systemd user service status"
    echo "  stop-service      Stop the systemd user service"
    echo "  logs              Stream systemd service logs"
    echo "  help              Show this help message"
    echo "=========================================================="
}

case "$1" in
    test)
        echo "🎙️  Starting microphone and STT test mode..."
        exec "${PYTHON_BIN}" "${SCRIPT_DIR}/voice_unlock_daemon.py" --test-mic
        ;;
    test-unit)
        echo "🧪 Running automated audit and unit tests..."
        exec "${PYTHON_BIN}" -m unittest discover -s "${SCRIPT_DIR}/tests" -p "test_*.py"
        ;;
    download-model)
        echo "📥 Pre-downloading Faster-Whisper model..."
        exec "${PYTHON_BIN}" "${SCRIPT_DIR}/voice_unlock_daemon.py" --download-model
        ;;
    run)
        echo "🚀 Starting Voice Unlock daemon in foreground..."
        exec "${PYTHON_BIN}" "${SCRIPT_DIR}/voice_unlock_daemon.py" --verbose
        ;;
    dry-run)
        echo "🧪 Starting Voice Unlock daemon in dry-run mode (simulated unlock)..."
        exec "${PYTHON_BIN}" "${SCRIPT_DIR}/voice_unlock_daemon.py" --dry-run --verbose
        ;;
    autostart)
        ACTION="${2:-status}"
        case "${ACTION}" in
            on|enable)
                echo "⚙️  Enabling auto-start on boot..."
                systemctl --user enable "${SERVICE_NAME}"
                echo "✅ Voice Unlock will now automatically start when you boot/login!"
                ;;
            off|disable)
                echo "⚙️  Disabling auto-start on boot..."
                systemctl --user disable "${SERVICE_NAME}"
                echo "🛑 Voice Unlock will NOT start automatically on boot."
                ;;
            status)
                if systemctl --user is-enabled "${SERVICE_NAME}" &>/dev/null; then
                    echo "🟢 Autostart on boot: ENABLED (Tự động chạy khi khởi động máy)"
                else
                    echo "🔴 Autostart on boot: DISABLED (Đang tắt tự động chạy)"
                fi
                ;;
            *)
                echo "Usage: ./run.sh autostart [on|off|status]"
                exit 1
                ;;
        esac
        ;;
    install-service)
        echo "📦 Installing systemd user service..."
        mkdir -p "${USER_SYSTEMD_DIR}"
        cp -f "${SCRIPT_DIR}/${SERVICE_NAME}" "${USER_SYSTEMD_DIR}/${SERVICE_NAME}"
        systemctl --user daemon-reload
        systemctl --user enable --now "${SERVICE_NAME}"
        echo "✅ Service enabled and started successfully!"
        echo ""
        systemctl --user status "${SERVICE_NAME}" --no-pager
        ;;
    status-service)
        systemctl --user status "${SERVICE_NAME}" --no-pager
        ;;
    stop-service)
        echo "🛑 Stopping systemd user service..."
        systemctl --user stop "${SERVICE_NAME}"
        echo "✅ Service stopped."
        ;;
    logs)
        journalctl --user -u "${SERVICE_NAME}" -f -n 50
        ;;
    help|--help|-h|"")
        print_usage
        ;;
    *)
        echo "❌ Unknown command: $1"
        print_usage
        exit 1
        ;;
esac
