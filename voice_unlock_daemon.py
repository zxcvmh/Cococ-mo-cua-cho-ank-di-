#!/usr/bin/env python3
"""
Voice Unlock Daemon for CachyOS / KDE Plasma 6 (Wayland)
Powered by AI Faster-Whisper + Silero VAD.
Listens for screen lock state via D-Bus (org.freedesktop.ScreenSaver)
and speech-to-text recognition via Faster-Whisper to unlock session with loginctl.
"""

import argparse
import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time

from dbus_fast.aio import MessageBus
from dbus_fast import BusType
import faster_whisper.vad
from faster_whisper import WhisperModel
import numpy as np
from rapidfuzz import fuzz
import sounddevice as sd

logger = logging.getLogger("voice-unlock")
_active_chime_procs = []


def _preload_cuda_libraries():
    """Preload bundled NVIDIA CUDA/cuDNN shared libraries if present in virtualenv."""
    import ctypes
    import glob

    venv_dir = Path(__file__).resolve().parent / ".venv"
    if venv_dir.is_dir():
        for so_file in glob.glob(str(venv_dir / "lib/python*/site-packages/nvidia/*/lib/*.so*")):
            try:
                ctypes.CDLL(so_file, mode=ctypes.RTLD_GLOBAL)
            except Exception:
                pass


_preload_cuda_libraries()


def normalize_text(text: str) -> str:
    """Lowercase and strip punctuation/extra whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s\d]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_config(config_path: str | Path) -> dict:
    """Load configuration JSON, enforce secure file permissions, and resolve paths."""
    config_path = Path(config_path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Security check: warn and secure if config is readable/writable by other users
    try:
        mode = os.stat(config_path).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            logger.warning(
                f"Config file {config_path} has permissions {oct(mode)[-3:]}. "
                "Securing to 0600 (owner-only) to protect passphrases."
            )
            try:
                os.chmod(config_path, 0o600)
            except OSError as pe:
                logger.warning(f"Could not update permissions on {config_path}: {pe}")
    except Exception as e:
        logger.debug(f"Could not verify config file permissions: {e}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    base_dir = config_path.parent
    if "chime_file" in config:
        config["chime_file"] = str((base_dir / config["chime_file"]).resolve())

    # Defaults
    config.setdefault(
        "passphrases",
        [
            "cốc cốc mở cửa cho anh đê",
            "cốc cốc mở cửa cho anh đi",
            "cốc cốc mở cửa",
            "mở cửa cho anh đê",
        ],
    )
    config.setdefault("similarity_threshold", 0.80)
    config.setdefault("model_size", "small")
    config.setdefault("device", "cuda")
    config.setdefault("compute_type", "float16")
    config.setdefault("battery_timeout_seconds", 600)
    config.setdefault("chime_enabled", True)
    config.setdefault("autostart", True)
    config.setdefault("sample_rate", 16000)
    config.setdefault("block_size", 512)  # 32ms chunks at 16kHz for Silero VAD
    config.setdefault("speech_threshold", 0.50)
    config.setdefault("silence_threshold", 0.30)
    config.setdefault("silence_timeout", 0.25)  # ~8 chunks = 256ms
    config.setdefault("min_speech_duration", 0.4)
    config.setdefault("max_speech_duration", 10.0)
    return config


def get_active_session_id() -> str:
    """
    Determine the active desktop session ID via env or loginctl.
    Validates output to ensure safe arguments for subprocess calls.
    """
    session_id = os.environ.get("XDG_SESSION_ID", "").strip()
    if session_id and re.match(r"^[a-zA-Z0-9_-]+$", session_id):
        return session_id

    # Try loginctl show-user Display
    try:
        uid = os.getuid()
        res = subprocess.run(
            ["loginctl", "show-user", str(uid), "-p", "Display", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        val = res.stdout.strip()
        if val and val != "0" and re.match(r"^[a-zA-Z0-9_-]+$", val):
            return val
    except Exception as e:
        logger.debug(f"Failed to query loginctl show-user: {e}")

    # Fallback: scan sessions for seat0
    try:
        res = subprocess.run(
            ["loginctl", "--no-legend", "list-sessions"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        for line in res.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 6 and "seat0" in parts and "user" in parts:
                sid = parts[0]
                if re.match(r"^[a-zA-Z0-9_-]+$", sid):
                    return sid
    except Exception as e:
        logger.debug(f"Failed to parse list-sessions: {e}")

    return ""


def is_on_battery() -> bool:
    """Check /sys/class/power_supply/ to see if running on battery."""
    power_supply_dir = Path("/sys/class/power_supply")
    if not power_supply_dir.is_dir():
        return False

    has_battery = False
    battery_discharging = False
    ac_online = False

    try:
        for ps in power_supply_dir.iterdir():
            type_file = ps / "type"
            online_file = ps / "online"
            status_file = ps / "status"

            ps_type = type_file.read_text().strip() if type_file.is_file() else ""

            if ps_type in ("Mains", "USB", "AC"):
                if online_file.is_file() and online_file.read_text().strip() == "1":
                    ac_online = True

            if ps_type == "Battery":
                has_battery = True
                if status_file.is_file() and status_file.read_text().strip() == "Discharging":
                    battery_discharging = True

        if ac_online:
            return False
        if battery_discharging:
            return True
        if has_battery and not ac_online:
            return True
    except Exception as e:
        logger.debug(f"Error checking power supply: {e}")

    return False


def get_dpms_states() -> dict[str, str]:
    """Read DRM connector DPMS states."""
    states = {}
    drm_dir = Path("/sys/class/drm")
    if not drm_dir.is_dir():
        return states

    try:
        for path in drm_dir.glob("*/dpms"):
            connector = path.parent.name
            states[connector] = path.read_text().strip()
    except Exception as e:
        logger.debug(f"Error reading DRM DPMS: {e}")
    return states


def check_display_woke_up(last_states: dict[str, str], current_states: dict[str, str]) -> bool:
    """Return True if any display transitioned to 'On'."""
    for conn, cur_state in current_states.items():
        prev_state = last_states.get(conn, "")
        if cur_state == "On" and prev_state and prev_state != "On":
            return True
    return False


def play_chime(chime_file: str):
    """Play chime sound using PipeWire or ALSA asynchronously with zombie reaping."""
    global _active_chime_procs
    if not chime_file or not os.path.isfile(chime_file):
        return

    _active_chime_procs = [p for p in _active_chime_procs if p.poll() is None]

    players = [
        ["pw-play", chime_file],
        ["paplay", chime_file],
        ["aplay", "-q", chime_file],
    ]
    for cmd in players:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _active_chime_procs.append(proc)
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug(f"Error playing sound with {cmd[0]}: {e}")


class VoiceUnlockDaemon:
    def __init__(self, config: dict, dry_run: bool = False, verbose: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.verbose = verbose

        self.passphrases = [p.strip() for p in config.get("passphrases", []) if p.strip()]
        self.threshold = float(config.get("similarity_threshold", 0.80))
        self.model_size = config.get("model_size", "small")
        self.device = config.get("device", "cpu")
        self.compute_type = config.get("compute_type", "int8")

        self.battery_timeout = int(config.get("battery_timeout_seconds", 600))
        self.chime_enabled = bool(config.get("chime_enabled", True))
        self.chime_file = config.get("chime_file", "")
        self.sample_rate = int(config.get("sample_rate", 16000))
        self.block_size = int(config.get("block_size", 512))
        self.speech_threshold = float(config.get("speech_threshold", 0.50))
        self.silence_threshold = float(config.get("silence_threshold", 0.30))
        self.silence_timeout = float(config.get("silence_timeout", 0.25))
        self.min_speech_duration = float(config.get("min_speech_duration", 0.4))
        self.max_speech_duration = float(config.get("max_speech_duration", 10.0))

        logger.info(
            f"Loading faster-whisper model '{self.model_size}' "
            f"(device={self.device}, compute_type={self.compute_type})..."
        )
        try:
            self.model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception as e:
            if self.device != "cpu":
                logger.warning(
                    f"Failed to load Whisper model on device '{self.device}': {e}. "
                    "Falling back to device='cpu', compute_type='int8'..."
                )
                self.device = "cpu"
                self.compute_type = "int8"
                self.model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type="int8",
                )
            else:
                raise

        logger.info("Faster-Whisper model loaded successfully.")

        logger.info("Loading Neural Silero VAD (ONNX)...")
        self.vad_model = faster_whisper.vad.get_vad_model()
        logger.info("Silero VAD model loaded successfully.")

        self.screen_locked = False
        self.mic_active = False
        self.timed_out_on_battery = False
        self.lock_start_time = 0.0
        self.last_dpms_states = get_dpms_states()

        self._lock = threading.RLock()
        self.audio_queue = queue.Queue()
        self.stream = None
        self.worker_thread = None
        self.stop_worker_event = threading.Event()

        self.loop = None
        self.bus = None
        self.session_id = get_active_session_id()
        logger.info(f"Initialized daemon with session ID: {self.session_id or '(auto-detect)'}")

    def evaluate_match(self, text: str) -> tuple[bool, float, str]:
        """
        Compare recognized text with configured passphrases using robust
        word-boundary and word-level pairwise matching.

        Two-stage matching:
          Stage 1: Exact containment scan for all passphrases. If any matches,
                   return (True, 1.0, phrase) immediately (preferring longest match).
          Stage 2: Sliding window fuzzy matching across all passphrases to find
                   the highest best_score before comparing against self.threshold.
        """
        norm_text = normalize_text(text)
        if not norm_text:
            return False, 0.0, ""

        text_words = norm_text.split()
        if not text_words:
            return False, 0.0, ""

        padded_text = f" {norm_text} "

        # Bước 1: Quét kiểm tra exact containment (padded_phrase in padded_text)
        # cho TẤT CẢ các passphrases trước. Nếu có bất kỳ câu nào khớp tuyệt đối
        # thì trả về ngay (True, 1.0, phrase).
        exact_matches = []
        for phrase in self.passphrases:
            norm_phrase = normalize_text(phrase)
            if not norm_phrase:
                continue
            padded_phrase = f" {norm_phrase} "
            if padded_phrase in padded_text:
                exact_matches.append((phrase, len(norm_phrase)))

        if exact_matches:
            # Ưu tiên passphrase dài nhất để tránh câu ngắn che câu dài
            exact_matches.sort(key=lambda item: item[1], reverse=True)
            return True, 1.0, exact_matches[0][0]

        # Bước 2: Nếu không có exact match, mới chạy sliding window fuzzy match
        # và tìm candidate tốt nhất trên toàn bộ danh sách passphrases.
        # Khi nhiều câu đạt ngưỡng threshold, ưu tiên câu dài hơn (cụ thể hơn)
        best_score = 0.0
        best_phrase = ""
        best_key = None

        for phrase in self.passphrases:
            norm_phrase = normalize_text(phrase)
            if not norm_phrase:
                continue

            phrase_words = norm_phrase.split()
            if not phrase_words:
                continue

            # Reject if spoken text has fewer words than target passphrase
            L = len(phrase_words)
            if len(text_words) < L:
                continue

            # Word-level pairwise sliding window
            for i in range(len(text_words) - L + 1):
                window = text_words[i : i + L]
                word_scores = [fuzz.ratio(pw, tw) / 100.0 for pw, tw in zip(phrase_words, window)]

                # If any word in the window has < 50% similarity, it's not a candidate
                if any(ws < 0.50 for ws in word_scores):
                    continue

                avg_score = sum(word_scores) / len(word_scores)
                # Khi đạt ngưỡng threshold, câu có nhiều từ hơn (khớp trọn vẹn hơn) được ưu tiên
                candidate_key = (
                    avg_score >= self.threshold,
                    L if avg_score >= self.threshold else 0,
                    avg_score,
                )
                if best_key is None or candidate_key > best_key:
                    best_key = candidate_key
                    best_score = avg_score
                    best_phrase = phrase

        # So sánh best_score cao nhất với self.threshold
        if best_score >= self.threshold:
            return True, best_score, best_phrase

        return False, best_score, best_phrase

    def _audio_callback(self, indata, frames, time_info, status):
        """Audio stream callback capturing float32 audio chunks."""
        if status and self.verbose:
            logger.debug(f"Audio stream status: {status}")
        chunk = indata[:, 0].copy()
        self.audio_queue.put(chunk)

    def start_mic(self):
        """Start microphone input stream and worker thread with thread safety."""
        with self._lock:
            if self.mic_active:
                return

            logger.info("Enabling microphone stream...")
            self.audio_queue = queue.Queue()
            self.stop_worker_event.clear()

            try:
                self.stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    blocksize=self.block_size,
                    dtype="float32",
                    channels=1,
                    callback=self._audio_callback,
                )
                self.stream.start()
                self.mic_active = True

                self.worker_thread = threading.Thread(
                    target=self._recognition_worker,
                    daemon=True,
                    name="WhisperRecognitionWorker",
                )
                self.worker_thread.start()
                logger.info("Microphone is ACTIVE and listening for passphrase with Silero VAD.")
            except Exception as e:
                logger.error(f"Failed to start audio stream: {e}", exc_info=True)
                if self.stream is not None:
                    try:
                        self.stream.stop()
                        self.stream.close()
                    except Exception:
                        pass
                    self.stream = None
                self.mic_active = False

    def stop_mic(self):
        """Stop microphone stream and worker thread with deadlock protection."""
        with self._lock:
            if not self.mic_active and self.stream is None:
                return

            logger.info("Disabling microphone stream...")
            self.stop_worker_event.set()

            # Wait for worker thread if called from another thread
            if self.worker_thread and self.worker_thread.is_alive():
                if threading.current_thread() != self.worker_thread:
                    self.worker_thread.join(timeout=1.5)
            self.worker_thread = None

            if self.stream is not None:
                try:
                    self.stream.stop()
                except Exception as e:
                    logger.debug(f"Error stopping stream: {e}")
                try:
                    self.stream.close()
                except Exception as e:
                    logger.debug(f"Error closing stream: {e}")
                self.stream = None

            self.mic_active = False
            logger.info("Microphone is STOPPED (Privacy & battery protected).")

    def trigger_unlock(self, matched_phrase: str, score: float):
        """Perform unlock action and stop listening."""
        logger.info(
            f"🎉 MATCH CONFIRMED: '{matched_phrase}' (Score: {score:.2f}) -> Triggering Unlock!"
        )

        if self.chime_enabled and self.chime_file:
            play_chime(self.chime_file)

        # Stop mic immediately to avoid duplicate triggers and preserve privacy
        self.stop_mic()

        # Build secure unlock command
        session_id = get_active_session_id()
        if session_id and not session_id.startswith("-"):
            cmd = ["loginctl", "unlock-session", session_id]
        else:
            # Safe systemd fallback: unlock all graphical sessions of this user
            cmd = ["loginctl", "unlock-sessions"]

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would execute: {' '.join(cmd)}")
        else:
            logger.info(f"Executing: {' '.join(cmd)}")
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if res.returncode == 0:
                    logger.info("Session unlock request sent successfully.")
                else:
                    logger.warning(f"loginctl returned error {res.returncode}: {res.stderr.strip()}")
            except Exception as e:
                logger.error(f"Failed to call loginctl: {e}")

    def _recognition_worker(self):
        """
        Background worker using Neural Silero VAD (ONNX) in real-time,
        pre-roll buffer (~0.25s), trailing silence tracking, and Faster-Whisper transcription.
        """
        chunk_duration = self.block_size / self.sample_rate
        silence_chunks_needed = max(1, int(round(self.silence_timeout / chunk_duration)))
        min_speech_chunks = max(1, int(round(self.min_speech_duration / chunk_duration)))
        max_speech_chunks = max(min_speech_chunks, int(round(self.max_speech_duration / chunk_duration)))

        # Pre-roll buffer to preserve the first consonant/syllable (~0.25s = 8 chunks)
        preroll_len = max(2, int(round(0.25 / chunk_duration)))
        preroll_buffer = deque(maxlen=preroll_len)

        is_speaking = False
        speech_chunks = []
        silence_chunks_count = 0

        while not self.stop_worker_event.is_set():
            try:
                chunk = self.audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if len(chunk) != self.block_size:
                if len(chunk) < self.block_size:
                    chunk = np.pad(chunk, (0, self.block_size - len(chunk))).astype(np.float32)
                else:
                    chunk = chunk[: self.block_size].astype(np.float32)

            try:
                prob_array = self.vad_model(chunk)
                prob = float(prob_array[0]) if not hasattr(prob_array[0], "__len__") else float(prob_array[0][0])
            except Exception as ve:
                if self.verbose:
                    logger.debug(f"VAD error: {ve}")
                continue

            if not is_speaking:
                preroll_buffer.append(chunk)
                if prob >= self.speech_threshold:
                    is_speaking = True
                    silence_chunks_count = 0
                    speech_chunks = list(preroll_buffer)
                    if self.verbose:
                        logger.debug(f"Speech onset detected (VAD prob: {prob:.4f})")
            else:
                speech_chunks.append(chunk)
                if prob < self.silence_threshold:
                    silence_chunks_count += 1
                else:
                    silence_chunks_count = 0

                # Check phrase completion condition: silence after speech or max duration
                reached_silence = silence_chunks_count >= silence_chunks_needed
                reached_max = len(speech_chunks) >= max_speech_chunks

                if reached_silence or reached_max:
                    total_chunks = len(speech_chunks)
                    audio_data = np.concatenate(speech_chunks).astype(np.float32)

                    # Reset speech accumulator
                    is_speaking = False
                    speech_chunks = []
                    silence_chunks_count = 0
                    preroll_buffer.clear()

                    # Only transcribe if audio meets minimum duration
                    if total_chunks >= min_speech_chunks:
                        dur = len(audio_data) / self.sample_rate
                        logger.info(f"Captured {dur:.2f}s of speech. Transcribing with Faster-Whisper...")
                        try:
                            segments, info = self.model.transcribe(
                                audio_data,
                                language="vi",
                                beam_size=1,
                                temperature=0.0,
                                vad_filter=False,
                            )
                            text = " ".join([seg.text.strip() for seg in segments]).strip()
                        except Exception as te:
                            logger.error(f"Error during transcription: {te}", exc_info=True)
                            text = ""

                        if text:
                            logger.info(f"STT Output: '{text}'")
                            matched, score, phrase = self.evaluate_match(text)
                            if matched:
                                self.trigger_unlock(phrase, score)
                                break
                            elif self.verbose:
                                logger.debug(
                                    f"No match for '{text}' (Best candidate: '{phrase}', score: {score:.2f})"
                                )
                        else:
                            if self.verbose:
                                logger.debug("VAD / Whisper produced empty text (non-speech noise).")

    def on_screensaver_active_changed(self, active: bool):
        """D-Bus signal callback when lock screen state changes."""
        logger.info(f"D-Bus ScreenSaver.ActiveChanged: active={active}")
        self.screen_locked = active

        if active:
            # Screen locked
            self.lock_start_time = time.monotonic()
            self.timed_out_on_battery = False
            self.last_dpms_states = get_dpms_states()
            on_bat = is_on_battery()
            logger.info(f"Screen is LOCKED. Power state: {'BATTERY' if on_bat else 'AC POWER'}.")
            self.start_mic()
        else:
            # Screen unlocked (either via voice or manual password entry)
            logger.info("Screen is UNLOCKED. Turning off mic immediately.")
            self.timed_out_on_battery = False
            self.stop_mic()

    async def monitor_power_and_display_loop(self):
        """Periodic background task to handle battery timeout, display wake-up, and stream health."""
        while True:
            await asyncio.sleep(2.0)
            if not self.screen_locked:
                continue

            on_bat = is_on_battery()
            now = time.monotonic()
            cur_dpms = get_dpms_states()
            woke_up = check_display_woke_up(self.last_dpms_states, cur_dpms)
            self.last_dpms_states = cur_dpms

            # 1. Handle Battery Timeout
            if self.mic_active and on_bat:
                elapsed = now - self.lock_start_time
                if elapsed >= self.battery_timeout:
                    logger.warning(
                        f"Battery timeout ({self.battery_timeout}s) reached while on battery. "
                        "Pausing microphone to conserve power."
                    )
                    self.stop_mic()
                    self.timed_out_on_battery = True

            # 2. Handle Wake up / Plug-in during battery timeout
            if self.timed_out_on_battery and not self.mic_active:
                if woke_up or not on_bat:
                    reason = "Display woke up (activity detected)" if woke_up else "AC power connected"
                    logger.info(f"{reason}. Resuming microphone and resetting timer.")
                    self.timed_out_on_battery = False
                    self.lock_start_time = time.monotonic()
                    self.start_mic()

            # 3. Fault-tolerance: Stream health check & PipeWire restart recovery
            if not self.timed_out_on_battery:
                if self.mic_active:
                    stream_alive = False
                    try:
                        stream_alive = self.stream is not None and getattr(self.stream, "active", False)
                    except Exception:
                        stream_alive = False

                    if not stream_alive:
                        logger.warning("Microphone stream is inactive or was disconnected. Reconnecting...")
                        self.stop_mic()
                        await asyncio.sleep(1.0)
                        self.start_mic()
                else:
                    # Screen locked and not on timeout, but mic not running (e.g. device was busy on lock)
                    logger.info("Screen is locked but mic is inactive. Attempting restart...")
                    self.start_mic()

    async def start_dbus_listener(self):
        """Connect to session bus and listen for ScreenSaver signals."""
        logger.info("Connecting to session D-Bus...")
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()

        try:
            introspection = await self.bus.introspect(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
            )
            proxy = self.bus.get_proxy_object(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                introspection,
            )
            interface = proxy.get_interface("org.freedesktop.ScreenSaver")

            interface.on_active_changed(self.on_screensaver_active_changed)

            initial_active = await interface.call_get_active()
            logger.info(f"Current ScreenSaver active state: {initial_active}")
            self.on_screensaver_active_changed(initial_active)

        except Exception as e:
            logger.error(f"Error setting up ScreenSaver D-Bus interface: {e}", exc_info=True)
            raise

    async def run(self):
        """Main async daemon run loop with signal handling, reconnects, and guaranteed cleanup."""
        self.loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def handle_signal():
            logger.info("Received termination signal, shutting down gracefully...")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self.loop.add_signal_handler(sig, handle_signal)
            except NotImplementedError:
                signal.signal(sig, lambda s, f: handle_signal())

        monitor_task = None
        try:
            connected = False
            for attempt in range(5):
                try:
                    await self.start_dbus_listener()
                    connected = True
                    break
                except Exception as e:
                    logger.warning(
                        f"D-Bus connection attempt {attempt + 1}/5 failed: {e}. Retrying in 2s..."
                    )
                    await asyncio.sleep(2.0)

            if not connected:
                raise RuntimeError("Failed to connect to D-Bus ScreenSaver service after 5 attempts.")

            monitor_task = asyncio.create_task(self.monitor_power_and_display_loop())
            logger.info("Voice Unlock Daemon is running. Waiting for screen lock events...")

            while not stop_event.is_set():
                await asyncio.sleep(1.0)
                if self.bus is not None and not getattr(self.bus, "connected", True):
                    logger.warning("D-Bus connection dropped. Attempting reconnection...")
                    self.stop_mic()
                    try:
                        await self.start_dbus_listener()
                        logger.info("Reconnected to D-Bus successfully.")
                    except Exception as re_err:
                        logger.debug(f"D-Bus reconnect failed: {re_err}")
                        await asyncio.sleep(3.0)

        finally:
            logger.info("Executing clean shutdown routine...")
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
            self.stop_mic()
            if self.bus:
                try:
                    self.bus.disconnect()
                except Exception as e:
                    logger.debug(f"Error disconnecting D-Bus: {e}")
            logger.info("Shutdown complete.")

    def run_test_mic(self):
        """Interactive test mode: test mic capture, Neural Silero VAD, and Faster-Whisper recognition directly."""
        chunk_duration = self.block_size / self.sample_rate
        silence_chunks_needed = max(1, int(round(self.silence_timeout / chunk_duration)))
        min_speech_chunks = max(1, int(round(self.min_speech_duration / chunk_duration)))
        max_speech_chunks = max(min_speech_chunks, int(round(self.max_speech_duration / chunk_duration)))
        preroll_len = max(2, int(round(0.25 / chunk_duration)))
        preroll_buffer = deque(maxlen=preroll_len)

        print("=" * 60)
        print("VOICE UNLOCK - TEST MIC & RECOGNITION (Silero VAD + Faster-Whisper)")
        print("=" * 60)
        print(f"Passphrases: {self.passphrases}")
        print(f"Model: {self.model_size} | Device: {self.device} | Compute: {self.compute_type}")
        print(
            f"VAD thresholds: Speech >= {self.speech_threshold*100:.0f}%, "
            f"Silence < {self.silence_threshold*100:.0f}%"
        )
        print(
            f"Silence timeout: {self.silence_timeout:.2f}s ({silence_chunks_needed} chunks) | "
            f"Block size: {self.block_size} (32ms)"
        )
        print(f"Similarity threshold: {self.threshold * 100:.0f}%")
        print("Speak into your microphone now (Press Ctrl+C to exit)...")
        print("-" * 60)

        is_speaking = False
        speech_chunks = []
        silence_chunks_count = 0

        audio_q = queue.Queue()

        def test_callback(indata, frames, time_info, status):
            if status:
                print(f"[Status: {status}]", file=sys.stderr)
            audio_q.put(indata[:, 0].copy())

        stream = sd.InputStream(
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            dtype="float32",
            channels=1,
            callback=test_callback,
        )

        with stream:
            try:
                while True:
                    try:
                        chunk = audio_q.get(timeout=0.1)
                    except queue.Empty:
                        continue

                    if len(chunk) != self.block_size:
                        if len(chunk) < self.block_size:
                            chunk = np.pad(chunk, (0, self.block_size - len(chunk))).astype(np.float32)
                        else:
                            chunk = chunk[: self.block_size].astype(np.float32)

                    try:
                        prob_array = self.vad_model(chunk)
                        prob = float(prob_array[0]) if not hasattr(prob_array[0], "__len__") else float(prob_array[0][0])
                    except Exception as ve:
                        continue

                    # Visual meter representation
                    bar_len = 10
                    filled = min(bar_len, max(0, int(round(prob * bar_len))))
                    bar = "█" * filled + "░" * (bar_len - filled)
                    prob_pct = int(round(prob * 100))

                    if not is_speaking:
                        preroll_buffer.append(chunk)
                        tag = f"[VOICE: {prob_pct:2d}%]" if prob >= self.speech_threshold else f"[SILENCE: {prob_pct:2d}%]"
                        if prob >= self.speech_threshold:
                            is_speaking = True
                            silence_chunks_count = 0
                            speech_chunks = list(preroll_buffer)
                            sys.stdout.write(f"\r🎙️  |{bar}| {tag} Speech started, recording...              ")
                            sys.stdout.flush()
                        else:
                            sys.stdout.write(f"\r🎧 |{bar}| {tag} Listening for speech...                  ")
                            sys.stdout.flush()
                    else:
                        speech_chunks.append(chunk)
                        if prob < self.silence_threshold:
                            silence_chunks_count += 1
                        else:
                            silence_chunks_count = 0

                        tag = f"[VOICE: {prob_pct:2d}%]" if prob >= self.silence_threshold else f"[SILENCE: {prob_pct:2d}%]"
                        dur = len(speech_chunks) * chunk_duration

                        if prob < self.silence_threshold:
                            sys.stdout.write(
                                f"\r🔇 |{bar}| {tag} Trailing silence: {silence_chunks_count}/{silence_chunks_needed} ({dur:.1f}s)   "
                            )
                        else:
                            sys.stdout.write(
                                f"\r🎙️  |{bar}| {tag} Speaking... ({dur:.1f}s)                          "
                            )
                        sys.stdout.flush()

                        reached_silence = silence_chunks_count >= silence_chunks_needed
                        reached_max = len(speech_chunks) >= max_speech_chunks

                        if reached_silence or reached_max:
                            total_chunks = len(speech_chunks)
                            audio_data = np.concatenate(speech_chunks).astype(np.float32)

                            is_speaking = False
                            speech_chunks = []
                            silence_chunks_count = 0
                            preroll_buffer.clear()

                            if total_chunks >= min_speech_chunks:
                                dur = len(audio_data) / self.sample_rate
                                sys.stdout.write(f"\r⚙️  [TRANSCRIBING...] Processing {dur:.1f}s with Faster-Whisper...     \n")
                                sys.stdout.flush()

                                try:
                                    segments, info = self.model.transcribe(
                                        audio_data,
                                        language="vi",
                                        beam_size=1,
                                        temperature=0.0,
                                        vad_filter=False,
                                    )
                                    text = " ".join([seg.text.strip() for seg in segments]).strip()
                                except Exception as e:
                                    text = f"[Error: {e}]"

                                if text:
                                    matched, score, phrase = self.evaluate_match(text)
                                    status_str = "MATCH! 🎉" if matched else "No match"
                                    print(
                                        f"[RESULT] Text: '{text}' | Best phrase: '{phrase}' | "
                                        f"Score: {score*100:.1f}% -> {status_str}"
                                    )
                                    if matched and self.chime_enabled and self.chime_file:
                                        play_chime(self.chime_file)
                                else:
                                    print("[RESULT] (Empty / Silence)")
                                print("-" * 60)
                            else:
                                sys.stdout.write("\r                                                             \r")
                                sys.stdout.flush()

            except KeyboardInterrupt:
                print("\n\nTest completed. Exiting.")


def main():
    parser = argparse.ArgumentParser(
        description="Voice Unlock Daemon for CachyOS / KDE Plasma 6 (Faster-Whisper)"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path(__file__).parent / "config.json",
        help="Path to config.json (default: config.json in script directory)",
    )
    parser.add_argument(
        "--test-mic",
        action="store_true",
        help="Run interactive mic and STT test directly without lock screen",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate recognition and chime without calling loginctl unlock-session",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose debug logging",
    )
    parser.add_argument(
        "--download-model",
        action="store_true",
        help="Pre-download Whisper model to local cache and exit",
    )
    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"Failed to load config from {args.config}: {e}")
        sys.exit(1)

    if args.download_model:
        model_size = config.get("model_size", "small")
        device = config.get("device", "cpu")
        compute_type = config.get("compute_type", "int8")
        logger.info(f"Downloading Whisper model '{model_size}' (device={device}, compute_type={compute_type})...")
        try:
            WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as e:
            if device != "cpu":
                logger.warning(
                    f"Failed to download on device '{device}': {e}. "
                    "Falling back to device='cpu', compute_type='int8'..."
                )
                WhisperModel(model_size, device="cpu", compute_type="int8")
            else:
                raise
        logger.info(f"Model '{model_size}' downloaded successfully!")
        sys.exit(0)

    daemon = VoiceUnlockDaemon(config, dry_run=args.dry_run, verbose=args.verbose)

    if args.test_mic:
        daemon.run_test_mic()
    else:
        asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
