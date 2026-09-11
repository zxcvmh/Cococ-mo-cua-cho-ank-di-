"""
Unit and Audit Tests for OpenTheDoor Voice Unlock Daemon (AI Faster-Whisper + Silero VAD)
Tests security, resilience, edge cases, fuzzy matching, and resource cleanup.
"""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

# Ensure project root is in python path
import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import voice_unlock_daemon as daemon_mod
from voice_unlock_daemon import (
    normalize_text,
    load_config,
    get_active_session_id,
    is_on_battery,
    get_dpms_states,
    check_display_woke_up,
)
import download_model


class MockSegment:
    """Mock segment produced by faster_whisper transcribe()."""

    def __init__(self, text: str, start: float = 0.0, end: float = 1.0):
        self.text = text
        self.start = start
        self.end = end


class MockTranscriptionInfo:
    """Mock info metadata produced by faster_whisper transcribe()."""

    def __init__(self, language: str = "vi", language_probability: float = 0.99, duration: float = 1.0):
        self.language = language
        self.language_probability = language_probability
        self.duration = duration


class MockWhisperModel:
    """Full-coverage mock representing faster_whisper.WhisperModel."""

    def __init__(
        self,
        model_size_or_path: str = "small",
        device: str = "cpu",
        device_index: int = 0,
        compute_type: str = "int8",
        cpu_threads: int = 0,
        num_workers: int = 1,
        download_root: str | None = None,
        local_files_only: bool = False,
        **kwargs,
    ):
        self.model_size_or_path = model_size_or_path
        self.device = device
        self.device_index = device_index
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.num_workers = num_workers
        self.download_root = download_root
        self.local_files_only = local_files_only
        self.kwargs = kwargs
        self.transcribe_text = "cốc cốc mở cửa cho anh đê"
        self.transcribe_called = False
        self.transcribe_kwargs = {}

    def transcribe(self, audio, **kwargs):
        self.transcribe_called = True
        self.transcribe_kwargs = kwargs
        text = getattr(self, "transcribe_text", "")
        segments = [MockSegment(text)] if text else []
        info = MockTranscriptionInfo()
        return iter(segments), info

    def detect_language(self, audio):
        return "vi", [("vi", 0.99)]


class TestTextNormalization(unittest.TestCase):
    """Test text normalization and sanitization."""

    def test_normalize_empty_and_spaces(self):
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text("   "), "")
        self.assertEqual(normalize_text("\n\t  "), "")

    def test_normalize_punctuation(self):
        self.assertEqual(normalize_text("Cốc cốc, mở cửa cho anh đê!"), "cốc cốc mở cửa cho anh đê")
        self.assertEqual(normalize_text("...vừng ơi, mở ra???"), "vừng ơi mở ra")
        self.assertEqual(normalize_text("open - the - door."), "open the door")

    def test_normalize_casing_and_spacing(self):
        self.assertEqual(normalize_text("  CỐC   CỐC   MỞ   CỬA  "), "cốc cốc mở cửa")
        self.assertEqual(normalize_text("Open  The   DOOR"), "open the door")


class TestFuzzyMatchingAndEdgeCases(unittest.TestCase):
    """Test fuzzy matching logic for false positive prevention and dialect tolerance."""

    def setUp(self):
        self.config = {
            "passphrases": [
                "cốc cốc mở cửa cho anh đê",
                "cốc cốc mở cửa cho anh đi",
                "cốc cốc mở cửa",
                "mở cửa cho anh đê",
            ],
            "similarity_threshold": 0.80,
            "model_size": "small",
        }
        with patch.object(daemon_mod, "WhisperModel", side_effect=MockWhisperModel):
            self.daemon = daemon_mod.VoiceUnlockDaemon(self.config, dry_run=True)

    def test_empty_and_noise_inputs(self):
        """Empty, whitespace, noise, and gibberish must never trigger match."""
        for text in ["", "   ", "...", "???", "[noise]", "<unk>", "cough", "ah", "uhm"]:
            matched, score, phrase = self.daemon.evaluate_match(text)
            self.assertFalse(matched, f"Noise '{text}' should not match but scored {score:.2f}")

    def test_single_word_false_positives(self):
        """CRITICAL: Single words must NEVER match multi-word passphrases!"""
        single_words = [
            "cốc",
            "mở",
            "cửa",
            "cho",
            "anh",
            "đê",
            "đi",
            "door",
        ]
        for word in single_words:
            matched, score, phrase = self.daemon.evaluate_match(word)
            self.assertFalse(
                matched,
                f"Single word '{word}' must NEVER trigger unlock (got score {score:.2f}, phrase '{phrase}')"
            )

    def test_partial_phrases_false_positives(self):
        """Incomplete phrases must not trigger unlock."""
        partial_phrases = [
            "cốc cốc",
            "mở cửa",
            "cho anh",
            "anh đê",
            "cốc cốc mở",
            "cho anh đi",
        ]
        for phrase in partial_phrases:
            # "cốc cốc mở cửa" is an explicit passphrase, but "cốc cốc", "cho anh" are partials
            matched, score, p = self.daemon.evaluate_match(phrase)
            self.assertFalse(
                matched,
                f"Incomplete phrase '{phrase}' should not trigger unlock (score {score:.2f})"
            )

    def test_random_conversation_false_positives(self):
        """Sentences containing some words in different order or context must not match."""
        unrelated = [
            "rót cho anh cốc nước",
            "đi ra ngoài mua cốc cà phê",
            "anh mở cửa xe đi",
            "hôm nay trời đẹp quá",
            "alo 1 2 3",
            "thế à bạn ơi",
        ]
        for s in unrelated:
            matched, score, p = self.daemon.evaluate_match(s)
            self.assertFalse(
                matched,
                f"Unrelated sentence '{s}' should not match (score {score:.2f}, phrase '{p}')"
            )

    def test_exact_phrase_matches(self):
        """Exact passphrases must match with 1.0 confidence."""
        for p in self.config["passphrases"]:
            matched, score, matched_p = self.daemon.evaluate_match(p)
            self.assertTrue(matched, f"Exact phrase '{p}' must match")
            self.assertAlmostEqual(score, 1.0, places=2)
            self.assertEqual(matched_p, p)

    def test_exact_match_priority_over_fuzzy(self):
        """
        CRITICAL AUDIT TEST:
        When input is 'cốc cốc mở cửa cho anh đi', it must match 'cốc cốc mở cửa cho anh đi'
        with score 1.0 instead of returning early on 'cốc cốc mở cửa cho anh đê' with 0.928.
        """
        input_text = "cốc cốc mở cửa cho anh đi"
        matched, score, phrase = self.daemon.evaluate_match(input_text)
        self.assertTrue(matched)
        self.assertEqual(score, 1.0)
        self.assertEqual(phrase, "cốc cốc mở cửa cho anh đi")

    def test_exact_match_prefers_longer_contained_phrase(self):
        """
        When input contains both a shorter and a longer passphrase,
        e.g. 'cốc cốc mở cửa cho anh đê' contains 'cốc cốc mở cửa',
        it should match the longer, more specific phrase.
        """
        input_text = "cốc cốc mở cửa cho anh đê"
        matched, score, phrase = self.daemon.evaluate_match(input_text)
        self.assertTrue(matched)
        self.assertEqual(score, 1.0)
        self.assertEqual(phrase, "cốc cốc mở cửa cho anh đê")

    def test_casing_and_punctuation_matches(self):
        """Uppercase and punctuated passphrases must match."""
        test_phrases = [
            ("Cốc cốc mở cửa cho anh đê!", "cốc cốc mở cửa cho anh đê"),
            ("Cốc cốc, mở cửa cho anh đi...", "cốc cốc mở cửa cho anh đi"),
            ("CỐC CỐC MỞ CỬA", "cốc cốc mở cửa"),
            ("Mở Cửa Cho Anh Đê", "mở cửa cho anh đê"),
        ]
        for text, expected in test_phrases:
            matched, score, matched_p = self.daemon.evaluate_match(text)
            self.assertTrue(matched, f"Phrase '{text}' should match '{expected}'")

    def test_phrases_with_surrounding_speech(self):
        """Spoken phrases with prefix or suffix filler words must match."""
        spoken = [
            "ê này cốc cốc mở cửa cho anh đê nhanh lên",
            "dạ cốc cốc mở cửa cho anh đi bạn ơi",
            "này mở cửa cho anh đê ngay",
        ]
        for text in spoken:
            matched, score, matched_p = self.daemon.evaluate_match(text)
            self.assertTrue(matched, f"Spoken sentence '{text}' should match a passphrase")
            self.assertGreaterEqual(score, 0.80)

    def test_phonetic_and_dialect_variations(self):
        """Slight phonetic STT errors or accent spelling differences must be tolerated."""
        variations = [
            ("cốc cốc mở của cho anh đê", "cốc cốc mở cửa cho anh đê"),
            ("cốc cốc mở cửa cho anh đề", "cốc cốc mở cửa cho anh đê"),
        ]
        for text, target in variations:
            matched, score, matched_p = self.daemon.evaluate_match(text)
            self.assertTrue(
                matched,
                f"Dialect variation '{text}' should match '{target}' (got score {score:.2f})"
            )

    def test_fuzzy_match_selects_highest_scoring_phrase(self):
        """
        If no exact match exists, sliding window should evaluate ALL passphrases
        and select the one with highest score rather than early-exiting on the first
        phrase that barely passes the threshold.
        """
        input_text = "cốc cốc mở của cho anh đề"
        matched, score, phrase = self.daemon.evaluate_match(input_text)
        self.assertTrue(matched)
        self.assertGreaterEqual(score, self.daemon.threshold)
        self.assertEqual(phrase, "cốc cốc mở cửa cho anh đê")


class TestConfigLoading(unittest.TestCase):
    """Test configuration loading, validation, and defaults."""

    def test_load_valid_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "config.json"
            cfg_data = {
                "passphrases": ["cốc cốc mở cửa cho anh đê"],
                "similarity_threshold": 0.85,
                "model_size": "small",
                "device": "cpu",
                "compute_type": "int8",
                "battery_timeout_seconds": 300,
            }
            cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

            # Test Path instance
            loaded_path = load_config(cfg_file)
            self.assertEqual(loaded_path["similarity_threshold"], 0.85)

            # Test str instance (ensuring config_path = Path(config_path) works)
            loaded_str = load_config(str(cfg_file))
            self.assertEqual(loaded_str["similarity_threshold"], 0.85)
            self.assertEqual(loaded_str["battery_timeout_seconds"], 300)
            self.assertEqual(loaded_str["model_size"], "small")
            self.assertEqual(loaded_str["device"], "cpu")
            self.assertEqual(loaded_str["compute_type"], "int8")
            self.assertTrue(loaded_str["chime_enabled"])
            self.assertEqual(loaded_str["sample_rate"], 16000)

    def test_missing_config_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_config(Path("/nonexistent/path/config.json"))
        with self.assertRaises(FileNotFoundError):
            load_config("/nonexistent/path/config.json")

    def test_empty_config_applies_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "config.json"
            cfg_file.write_text("{}", encoding="utf-8")

            loaded = load_config(str(cfg_file))
            self.assertGreaterEqual(loaded["similarity_threshold"], 0.75)
            self.assertEqual(loaded["model_size"], "small")
            self.assertIsInstance(loaded["passphrases"], list)
            self.assertGreater(len(loaded["passphrases"]), 0)


class TestWhisperModelAndWorker(unittest.TestCase):
    """Test WhisperModel integration, fallback behavior, and recognition worker."""

    def test_whisper_model_cpu_initialization(self):
        config = {
            "passphrases": ["cốc cốc mở cửa"],
            "model_size": "small",
            "device": "cpu",
            "compute_type": "int8",
        }
        with patch.object(daemon_mod, "WhisperModel", side_effect=MockWhisperModel) as mock_cls:
            daemon = daemon_mod.VoiceUnlockDaemon(config, dry_run=True)
            mock_cls.assert_called_once_with("small", device="cpu", compute_type="int8")
            self.assertEqual(daemon.device, "cpu")
            self.assertEqual(daemon.compute_type, "int8")

    def test_whisper_model_device_fallback_on_error(self):
        """If GPU device fails during initialization, it must fallback to CPU int8."""
        config = {
            "passphrases": ["cốc cốc mở cửa"],
            "model_size": "small",
            "device": "cuda",
            "compute_type": "float16",
        }
        calls = []
        def side_effect(model_size, device, compute_type, **kwargs):
            calls.append((model_size, device, compute_type))
            if device == "cuda":
                raise RuntimeError("CUDA out of memory or device not available")
            return MockWhisperModel(model_size, device=device, compute_type=compute_type)

        with patch.object(daemon_mod, "WhisperModel", side_effect=side_effect):
            daemon = daemon_mod.VoiceUnlockDaemon(config, dry_run=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], ("small", "cuda", "float16"))
            self.assertEqual(calls[1], ("small", "cpu", "int8"))
            self.assertEqual(daemon.device, "cpu")
            self.assertEqual(daemon.compute_type, "int8")

    def test_recognition_worker_transcription_and_trigger(self):
        """Worker should process queued audio, detect speech via Silero VAD, transcribe, and trigger unlock."""
        config = {
            "passphrases": ["cốc cốc mở cửa cho anh đê"],
            "model_size": "small",
            "device": "cpu",
            "compute_type": "int8",
            "sample_rate": 16000,
            "block_size": 512,
            "speech_threshold": 0.50,
            "silence_threshold": 0.30,
            "silence_timeout": 0.1,
            "min_speech_duration": 0.1,
        }
        mock_model = MockWhisperModel()
        mock_model.transcribe_text = "cốc cốc mở cửa cho anh đê"

        with patch.object(daemon_mod, "WhisperModel", return_value=mock_model):
            daemon = daemon_mod.VoiceUnlockDaemon(config, dry_run=False)
            daemon.trigger_unlock = MagicMock()
            daemon.vad_model = MagicMock(side_effect=lambda chunk: np.array([0.95]) if chunk[0] > 0.05 else np.array([0.05]))

            # Simulate speech chunks: active speech then silence
            chunk_speech = np.ones(512, dtype=np.float32) * 0.1
            chunk_silence = np.zeros(512, dtype=np.float32)

            for _ in range(5):
                daemon.audio_queue.put(chunk_speech)
            for _ in range(8):
                daemon.audio_queue.put(chunk_silence)

            worker = threading.Thread(target=daemon._recognition_worker)
            worker.start()
            worker.join(timeout=1.5)
            daemon.stop_worker_event.set()

            daemon.trigger_unlock.assert_called_once()
            args, _ = daemon.trigger_unlock.call_args
            self.assertEqual(args[0], "cốc cốc mở cửa cho anh đê")
            self.assertAlmostEqual(args[1], 1.0, places=2)

    def test_recognition_worker_empty_text_no_trigger(self):
        """When Whisper returns empty text (noise/silence), trigger_unlock must not be called."""
        config = {
            "passphrases": ["cốc cốc mở cửa cho anh đê"],
            "model_size": "small",
            "device": "cpu",
            "compute_type": "int8",
            "sample_rate": 16000,
            "block_size": 512,
            "speech_threshold": 0.50,
            "silence_threshold": 0.30,
            "silence_timeout": 0.1,
            "min_speech_duration": 0.1,
        }
        mock_model = MockWhisperModel()
        mock_model.transcribe_text = ""

        with patch.object(daemon_mod, "WhisperModel", return_value=mock_model):
            daemon = daemon_mod.VoiceUnlockDaemon(config, dry_run=False)
            daemon.trigger_unlock = MagicMock()
            daemon.vad_model = MagicMock(side_effect=lambda chunk: np.array([0.95]) if chunk[0] > 0.05 else np.array([0.05]))

            chunk_speech = np.ones(512, dtype=np.float32) * 0.1
            chunk_silence = np.zeros(512, dtype=np.float32)

            for _ in range(5):
                daemon.audio_queue.put(chunk_speech)
            for _ in range(8):
                daemon.audio_queue.put(chunk_silence)

            worker = threading.Thread(target=daemon._recognition_worker)
            worker.start()
            worker.join(timeout=1.5)
            daemon.stop_worker_event.set()

            daemon.trigger_unlock.assert_not_called()


class TestDownloadModel(unittest.TestCase):
    """Test download_model.py functionality and fallback."""

    def test_download_model_success(self):
        mock_model = MockWhisperModel()
        with patch("faster_whisper.WhisperModel", return_value=mock_model), \
             patch("sys.argv", ["download_model.py", "--model", "tiny", "--device", "cpu"]):
            download_model.main()

    def test_download_model_cuda_fallback(self):
        calls = []
        def side_effect(model_size, device, compute_type, **kwargs):
            calls.append((model_size, device, compute_type))
            if device == "cuda":
                raise RuntimeError("CUDA not available")
            return MockWhisperModel(model_size, device=device, compute_type=compute_type)

        with patch("faster_whisper.WhisperModel", side_effect=side_effect), \
             patch("sys.argv", ["download_model.py", "--model", "tiny", "--device", "cuda"]):
            download_model.main()
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1], "cuda")
            self.assertEqual(calls[1][1], "cpu")


class TestSessionAndPowerDetection(unittest.TestCase):
    """Test active session ID detection and power supply status."""

    def test_session_id_from_env(self):
        with patch.dict(os.environ, {"XDG_SESSION_ID": "42"}):
            self.assertEqual(get_active_session_id(), "42")

    def test_session_id_sanitization(self):
        """Unsafe session IDs with special chars should be sanitized."""
        with patch.dict(os.environ, {"XDG_SESSION_ID": "--flag; rm -rf"}):
            sid = get_active_session_id()
            self.assertNotIn(";", sid)
            self.assertNotIn(" ", sid)

    def test_check_display_woke_up(self):
        last = {"eDP-1": "Off", "HDMI-A-1": "Off"}
        current = {"eDP-1": "On", "HDMI-A-1": "Off"}
        self.assertTrue(check_display_woke_up(last, current))
        self.assertFalse(check_display_woke_up(current, current))


class TestResourceCleanupAndSafety(unittest.TestCase):
    """Test resource cleanup during shutdown and exception handling."""

    def test_mic_stop_idempotent(self):
        config = {
            "passphrases": ["cốc cốc mở cửa cho anh đê"],
            "model_size": "small",
        }
        with patch.object(daemon_mod, "WhisperModel", side_effect=MockWhisperModel):
            daemon = daemon_mod.VoiceUnlockDaemon(config, dry_run=True)
            daemon.stop_mic()
            daemon.stop_mic()
            self.assertFalse(daemon.mic_active)


if __name__ == "__main__":
    unittest.main()
