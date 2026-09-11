#!/usr/bin/env python3
"""
Pre-download faster-whisper model to local cache.
Ensures daemon starts immediately without download delay.
"""

import argparse
import json
import logging
from pathlib import Path
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("download-model")


def main():
    parser = argparse.ArgumentParser(description="Pre-download faster-whisper model")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path(__file__).parent / "config.json",
        help="Path to config.json (default: config.json in script directory)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Whisper model size (default: from config.json or 'small')",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Computation device ('cpu', 'cuda', or 'auto', default: from config.json or 'cpu')",
    )
    parser.add_argument(
        "--compute-type",
        default=None,
        help="Quantization type (default: from config.json or 'int8')",
    )
    args = parser.parse_args()

    # Load defaults from config if available
    cfg = {}
    if args.config and Path(args.config).is_file():
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            logger.debug(f"Could not read config {args.config}: {e}")

    model_size = args.model or cfg.get("model_size", "small")
    device = args.device or cfg.get("device", "cpu")
    compute_type = args.compute_type or cfg.get("compute_type", "int8")

    logger.info(f"Downloading faster-whisper model '{model_size}'...")
    logger.info(f"Device: {device} | Compute type: {compute_type}")
    start_time = time.monotonic()

    try:
        from faster_whisper import WhisperModel

        try:
            model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
            )
        except Exception as dev_err:
            if device != "cpu":
                logger.warning(
                    f"Failed to load on device '{device}': {dev_err}. "
                    "Falling back to device='cpu', compute_type='int8'..."
                )
                model = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                )
            else:
                raise

        elapsed = time.monotonic() - start_time
        logger.info(f"✅ Model '{model_size}' successfully downloaded and loaded in {elapsed:.1f}s!")
    except Exception as e:
        logger.error(f"❌ Failed to download model '{model_size}': {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
