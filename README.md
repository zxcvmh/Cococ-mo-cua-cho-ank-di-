# Voice Unlock for Linux (KDE Plasma 6 / Wayland)

A privacy-focused, zero-PAM voice unlock daemon for Linux desktop environments running KDE Plasma 6 on Wayland. Powered by Neural Silero VAD and Faster-Whisper (CUDA-accelerated or optimized CPU int8).

[English](#english-documentation) | [Tiếng Việt](#tài-liệu-tiếng-việt)

---

<a name="english-documentation"></a>
## English Documentation

### Overview

Most voice authentication scripts on Linux attempt to hook into the PAM (Pluggable Authentication Modules) stack (`/etc/pam.d/`). This introduces severe systemic risks:
1. **Lockout Vulnerability**: An audio recording error, ambient noise, or STT crash can permanently lock the user out of the graphical session.
2. **Interface Freezes**: Synchronous PAM execution blocks the password input field while recording audio.
3. **Audio Sandboxing**: PAM strips user environment variables (`XDG_RUNTIME_DIR`), frequently breaking PipeWire and ALSA client connections.

**Voice Unlock** solves this by operating as an unprivileged, event-driven user daemon. It subscribes to desktop screen saver state over the session D-Bus (`org.freedesktop.ScreenSaver`), listens through PipeWire when the screen is locked, and issues unlock requests via standard `systemd-logind` APIs (`loginctl unlock-session`). PAM remains completely unmodified, ensuring password authentication always serves as an infallible fallback.

### Key Architecture & Features

- **Dual-Engine Speech Pipeline**:
  - **Neural Silero VAD (ONNX)**: Real-time voice activity detection processing 32ms frames (512 samples) with 4ms latency. Discards continuous background noise (fans, room acoustics) while instantly detecting speech onset and termination.
  - **Faster-Whisper (CTranslate2)**: Hardware-accelerated transcription on NVIDIA GPUs via CUDA float16 (~140ms latency) with automatic fallback to CPU int8 AVX-512.
- **Dynamic Word-Boundary Fuzzy Matcher**:
  - Prevents false triggers from isolated vocabulary or background conversation.
  - Supports colloquial phrases and dialect variations (e.g., configurable natural language passphrases).
- **Power and Privacy Lifecycle**:
  - Microphone access is restricted exclusively to locked screen states (`ScreenSaver.Active = true`).
  - Automatically disables audio capture after 10 minutes on battery power; resumes listening on AC power connection or display wake-up.
- **Audio Feedback**:
  - Plays an audible chime or custom startup sound upon successful recognition via native PipeWire (`pw-play`).

### Performance Benchmarks

Measured on an Intel Core i7-11370H with an NVIDIA GeForce RTX 3050 Ti Mobile (4GB VRAM):

| Component | CPU (int8) | GPU (CUDA float16) |
|---|---|---|
| Silero VAD Frame Latency | 3.8 ms | 3.8 ms (CPU ONNX) |
| Whisper Small Inference (2.0s audio) | 2,150 ms | 145 ms |
| End-to-End Latency (speech end to unlock) | ~2.5 s | ~0.45 s |
| Memory Usage (idle, mic off) | ~45 MB | ~45 MB |
| VRAM Usage | 0 MB | ~850 MB |

---

### Prerequisites

- **Operating System**: Linux (tested on CachyOS / Arch Linux, Fedora, openSUSE).
- **Desktop Environment**: KDE Plasma 6 (Wayland session) or any environment providing `org.freedesktop.ScreenSaver` and `systemd-logind`.
- **Audio Server**: PipeWire (`pipewire`, `wireplumber`, `pipewire-pulse`).
- **Python**: Python 3.10, 3.11, or 3.12 (`uv` package manager recommended).
- **Optional GPU**: NVIDIA GPU with Turing/Ampere/Ada architecture for sub-500ms response times.

---

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/zxcvmh/Cococ-mo-cua-cho-ank-di-.git
   cd Cococ-mo-cua-cho-ank-di-
   ```

2. **Run the automated setup script**:
   ```bash
   chmod +x setup.sh run.sh
   ./setup.sh
   ```
   The setup script initializes an isolated virtual environment, installs core dependencies (`faster-whisper`, `sounddevice`, `rapidfuzz`, `dbus-fast`), downloads required runtime models, and installs CUDA bindings if an NVIDIA GPU is present.

---

### Configuration

Configuration parameters are stored in `config.json` (automatically secured with `0600` permissions):

```json
{
  "passphrases": [
    "cốc cốc mở cửa cho anh đê",
    "cốc cốc mở cửa cho anh đi",
    "cốc cốc mở cửa",
    "mở cửa cho anh đê"
  ],
  "similarity_threshold": 0.80,
  "model_size": "small",
  "device": "cuda",
  "compute_type": "float16",
  "block_size": 512,
  "silence_timeout": 1.5,
  "speech_threshold": 0.50,
  "silence_threshold": 0.30,
  "autostart": true,
  "battery_timeout_seconds": 600,
  "chime_enabled": true,
  "chime_file": "assets/chime.wav"
}
```

#### Configuration Options

- `passphrases`: Array of authorized trigger phrases.
- `similarity_threshold`: Minimum sliding-window fuzzy matching score (`0.0` to `1.0`). Default `0.80`.
- `model_size`: Whisper model size (`tiny`, `base`, `small`, `medium`). Default `small`.
- `device`: Compute device (`cuda` or `cpu`). Default `cuda` with automatic fallback.
- `compute_type`: Numerical precision (`float16` for CUDA, `int8` for CPU).
- `silence_timeout`: Seconds of trailing silence before transcribing (e.g. `1.5` allows natural conversational pauses).
- `chime_file`: Path to audio file played on unlock (supports `.wav`, `.mp3`, `.ogg`).

---

### Usage

#### 1. Interactive Microphone Test
Test microphone capture, Silero VAD metering, and STT accuracy without locking your session:
```bash
./run.sh test
```

#### 2. Install Background Service
Install and register the systemd user service:
```bash
./run.sh install-service
```

#### 3. Toggle Autostart on Boot
Enable or disable automatic startup with your login session:
```bash
./run.sh autostart on       # Enable auto-start
./run.sh autostart off      # Disable auto-start
./run.sh autostart status   # Check current status
```

#### 4. Service Monitoring
```bash
./run.sh status-service     # View service status
./run.sh logs               # Stream real-time logs
./run.sh stop-service       # Stop the background daemon
```

#### 5. Automated Unit Tests
Run the built-in 27-point security and regression test suite:
```bash
./run.sh test-unit
```

---

<a name="tài-liệu-tiếng-việt"></a>
## Tài liệu Tiếng Việt

### Tổng quan

Hầu hết các giải pháp mở khóa bằng giọng nói trên Linux thường can thiệp trực tiếp vào cấu hình PAM (`/etc/pam.d/`). Hướng tiếp cận này tiềm ẩn các rủi ro hệ thống:
1. **Nguy cơ kẹt màn hình khóa**: Lỗi thiết bị âm thanh hoặc nhận diện sai có thể khóa người dùng vĩnh viễn khỏi phiên đồ họa.
2. **Làm đơ giao diện**: Thực thi đồng bộ trong PAM khiến ô nhập mật khẩu bị treo trong lúc thu âm.
3. **Mất môi trường PipeWire**: PAM tự động xóa biến môi trường (`XDG_RUNTIME_DIR`), làm gián đoạn kết nối tới audio server.

**Voice Unlock** khắc phục hoàn toàn bằng mô hình **User Background Daemon**:
- Daemon chạy dưới quyền người dùng thông thường, theo dõi trạng thái khóa màn hình qua D-Bus session (`org.freedesktop.ScreenSaver`).
- Khi màn hình khóa, daemon kích hoạt micro và phân tích luồng âm thanh.
- Khi nhận diện đúng câu khẩu lệnh, daemon gọi API của systemd (`loginctl unlock-session`).
- **Không chỉnh sửa PAM**: Mật khẩu bàn phím truyền thống luôn hoạt động bình thường 100%.

### Tính năng kỹ thuật

- **Pipeline xử lý âm thanh kép**:
  - **Neural Silero VAD (ONNX)**: Phân biệt chính xác tiếng người nói (Voice) và tiếng ồn quạt/phòng (Noise). Xử lý từng khối 32ms với độ trễ 4ms.
  - **Faster-Whisper (CTranslate2)**: Nhận diện tiếng Việt chuẩn xác bằng mô hình Whisper Small, tăng tốc qua GPU NVIDIA CUDA float16 (~140ms) hoặc CPU int8 AVX-512.
- **Thuật toán so khớp trượt theo từ (Word-level Sliding Window)**:
  - Ngăn chặn hoàn toàn hiện tượng nhận diện nhầm từ đơn lẻ hoặc câu nói ngẫu nhiên.
  - Hỗ trợ phương ngữ và biến thể khẩu ngữ (ví dụ: *"đê"* / *"đi"*).
- **Tiết kiệm pin và bảo vệ quyền riêng tư**:
  - Micro chỉ bật khi máy ở trạng thái khóa màn hình. Khi mở máy làm việc, micro hoàn toàn tắt.
  - Tự động ngắt micro sau 10 phút dùng pin nếu không có tương tác; tự bật lại khi cắm sạc hoặc màn hình sáng lại.
- **Phản hồi âm thanh**:
  - Phát chuông báo hoặc âm thanh khởi động tùy ý qua PipeWire (`pw-play`).

### Bảng đo đạc hiệu năng thực tế

Kiểm thử trên phần cứng Intel Core i7-11370H + NVIDIA GeForce RTX 3050 Ti Mobile:

| Hạng mục | Chạy CPU (int8) | Chạy GPU (CUDA float16) |
|---|---|---|
| Độ trễ khung Silero VAD | 3.8 ms | 3.8 ms (CPU ONNX) |
| Tốc độ Whisper Small (2.0s audio) | 2.150 ms | 145 ms |
| Tổng độ trễ (từ khi dứt câu đến khi mở) | ~2.5 giây | ~0.45 giây |
| RAM chiếm dụng khi chạy nền | ~45 MB | ~45 MB |
| VRAM chiếm dụng | 0 MB | ~850 MB |

---

### Yêu cầu hệ thống

- **Hệ điều hành**: Linux (đã kiểm thử trên CachyOS / Arch Linux, Fedora, openSUSE).
- **Môi trường đồ họa**: KDE Plasma 6 (phiên Wayland) hoặc các desktop environment hỗ trợ `org.freedesktop.ScreenSaver` và `systemd-logind`.
- **Hệ thống âm thanh**: PipeWire (`pipewire`, `wireplumber`).
- **Python**: Python 3.10 - 3.12 (khuyến nghị dùng trình quản lý `uv`).
- **GPU (Tùy chọn)**: Card đồ họa NVIDIA hỗ trợ CUDA để đạt độ phản hồi dưới 0.5 giây.

---

### Hướng dẫn cài đặt

1. **Clone repository**:
   ```bash
   git clone https://github.com/zxcvmh/Cococ-mo-cua-cho-ank-di-.git
   cd Cococ-mo-cua-cho-ank-di-
   ```

2. **Chạy script cài đặt tự động**:
   ```bash
   chmod +x setup.sh run.sh
   ./setup.sh
   ```

---

### Cấu hình (`config.json`)

File cấu hình tự động được thiết lập quyền bảo mật `0600` (chỉ user sở hữu có quyền đọc/ghi):

```json
{
  "passphrases": [
    "cốc cốc mở cửa cho anh đê",
    "cốc cốc mở cửa cho anh đi",
    "cốc cốc mở cửa",
    "mở cửa cho anh đê"
  ],
  "similarity_threshold": 0.80,
  "model_size": "small",
  "device": "cuda",
  "compute_type": "float16",
  "block_size": 512,
  "silence_timeout": 1.5,
  "speech_threshold": 0.50,
  "silence_threshold": 0.30,
  "autostart": true,
  "battery_timeout_seconds": 600,
  "chime_enabled": true,
  "chime_file": "assets/chime.wav"
}
```

---

### Các lệnh quản trị hệ thống

#### 1. Kiểm tra micro và nhận diện trực tiếp
```bash
./run.sh test
```

#### 2. Cài đặt dịch vụ chạy ngầm
```bash
./run.sh install-service
```

#### 3. Bật hoặc tắt tự khởi động cùng hệ thống
```bash
./run.sh autostart on       # Bật tự chạy khi bật máy
./run.sh autostart off      # Tắt tự chạy khi bật máy
./run.sh autostart status   # Xem trạng thái bật/tắt
```

#### 4. Quản lý trạng thái dịch vụ
```bash
./run.sh status-service     # Kiểm tra trạng thái
./run.sh logs               # Theo dõi log thời gian thực
./run.sh stop-service       # Dừng dịch vụ
```

#### 5. Chạy bộ kiểm thử tự động
```bash
./run.sh test-unit
```

---

### License

Mã nguồn được phân phối dưới giấy phép MIT License.
