#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

echo "=========================================================="
echo "      Cococ-mo-cua-cho-ank-di- (Voice Unlock) Setup       "
echo "=========================================================="

# Check uv package manager
if command -v uv &>/dev/null; then
    UV_BIN="uv"
elif [ -x "${HOME}/.local/bin/uv" ]; then
    UV_BIN="${HOME}/.local/bin/uv"
else
    echo "Installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV_BIN="${HOME}/.local/bin/uv"
fi

# Create virtualenv with Python 3.11
echo "[1/4] Creating virtual environment with Python 3.11..."
"${UV_BIN}" venv --python 3.11 "${VENV_DIR}"

PYTHON_BIN="${VENV_DIR}/bin/python"

# Install core dependencies
echo "[2/4] Installing core dependencies..."
"${UV_BIN}" pip install --python "${PYTHON_BIN}" \
    faster-whisper \
    sounddevice \
    rapidfuzz \
    dbus-fast \
    numpy

# Check for NVIDIA GPU and install CUDA runtimes
if command -v nvidia-smi &>/dev/null; then
    echo "[3/4] NVIDIA GPU detected. Installing CUDA runtime libraries..."
    "${UV_BIN}" pip install --python "${PYTHON_BIN}" \
        nvidia-cublas-cu12 \
        nvidia-cudnn-cu12 \
        nvidia-cuda-nvrtc-cu12 || echo "Notice: CUDA libraries optional. Will run on CPU if unavailable."
else
    echo "[3/4] No NVIDIA GPU detected. Running with optimized CPU int8 inference."
fi

# Initialize configuration
if [ ! -f "${SCRIPT_DIR}/config.json" ]; then
    cp "${SCRIPT_DIR}/config.example.json" "${SCRIPT_DIR}/config.json"
    chmod 600 "${SCRIPT_DIR}/config.json"
fi

# Pre-download Whisper model
echo "[4/4] Pre-downloading Whisper AI model..."
"${SCRIPT_DIR}/run.sh" download-model || true

echo "=========================================================="
echo "Setup completed successfully!"
echo "Next steps:"
echo "  1. Test your mic:        ./run.sh test"
echo "  2. Install auto-service: ./run.sh install-service"
echo "=========================================================="
