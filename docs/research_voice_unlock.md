# Nghiên cứu Kỹ thuật: Khả thi & Kiến trúc Voice Unlock trên CachyOS (KDE Plasma 6)

Tài liệu này đánh giá tính khả thi kỹ thuật, lỗ hổng kiến trúc và các phương án triển khai cho bài toán: **Mở khóa laptop ở trạng thái locked bằng giọng nói (Voice Unlock)** trên CachyOS (Linux kernel 6.x, KDE Plasma 6 Wayland, PipeWire).

---

## 1. Phân tích Hiện trạng Hệ thống Thực tế (Target System)

Khảo sát trực tiếp trên môi trường CachyOS hiện tại:
- **OS & Kernel**: CachyOS Linux (Arch-based rolling).
- **Desktop Environment**: KDE Plasma 6 Wayland (`kwin_wayland`, `kscreenlocker 6.7.4`).
- **Audio Server**: PipeWire (`/usr/bin/pipewire`, `/usr/bin/wireplumber`) chạy dưới quyền user (`UID 1000`).
- **Session Manager**: `systemd-logind` quản lý session Wayland (`session 3`, `seat0`).
- **PAM Configuration**: Cấu hình PAM mặc định của KDE nằm tại `/usr/lib/pam.d/kde` (chưa bị ghi đè tại `/etc/pam.d/kde`).
- **Python**: Python 3.14 (hệ thống) & Python 3.11 (`uv`).

---

## 2. Double-Check Kỹ thuật: 5 Lỗ hổng Trọng yếu trong `plan.md`

`plan.md` đề xuất tích hợp `pam_exec.so` vào `/etc/pam.d/kde` để chạy script ghi âm (3-5s) + Vosk STT. Dưới đây là các xung đột kỹ thuật đã được xác minh qua source code và tài liệu chính thức:

### 2.1. Nghịch lý Logic trong PAM Control Flags (Lỗi Fatal)
- **Trong `plan.md`**:
  ```pam
  auth required pam_exec.so /opt/voice-2fa/voice_check.sh
  auth required pam_unix.so
  ```
  Kèm phát biểu: *"Nếu voice fail/timeout → vẫn có thể unlock bằng password như bình thường (không bị khóa cứng)."*
- **Thực tế kỹ thuật ([Linux-PAM Documentation - pam.conf(5)](https://man7.org/linux/man-pages/man5/pam.conf.5.html))**:
  Control flag `required` quy định: nếu module trả về mã khác `PAM_SUCCESS`, toàn bộ stack `auth` sẽ bị đánh dấu thất bại. Cho dù người dùng sau đó gõ đúng password trong `pam_unix.so`, PAM vẫn từ chối đăng nhập.
  => **Hậu quả**: Nếu nói sai, mic nhiễu hoặc im lặng, người dùng bị **khóa cứng** khỏi màn hình đồ họa.
- **Nếu đổi sang `sufficient`**: Voice đúng sẽ unlock ngay lập tức (không cần gõ password). Nhưng nếu voice fail, nó mới rơi xuống `pam_unix.so`. Đây là quan hệ **OR** (Voice HOẶC Password), không phải 2FA (AND).

### 2.2. Vấn đề Trigger & UX: PAM không phải là Background Daemon
- **Cơ chế KDE Lockscreen**: Màn hình khóa của KDE Plasma 6 (`kscreenlocker_greet`) không chạy PAM liên tục. PAM conversation chỉ được kích hoạt khi:
  1. Màn hình khóa được bật lên.
  2. Người dùng tương tác (nhập ký tự hoặc nhấn Enter).
- **Hậu quả**: Khi laptop tắt màn hình (DPMS off) hoặc đang khóa chờ, không có tiến trình nào thu âm mic. Người dùng không thể "nói câu thần chú từ xa" để mở máy. Người dùng phải lại gần, bấm phím để đánh thức màn hình, sau đó hệ thống mới gọi `pam_exec`, mic mới bắt đầu thu 3-5 giây.
- **Hiện tượng treo UI**: `pam_exec.so` chạy đồng bộ (blocking). Trong suốt 3-5 giây `arecord` ghi âm + 1-2 giây STT, giao diện nhập mật khẩu của kscreenlocker sẽ bị đơ hoặc chờ, gây trải nghiệm giật lag nghiêm trọng.

### 2.3. Lỗi Môi trường Âm thanh (Audio Host is Down) do PAM Sanitization
- **Tài liệu `pam_exec(8)`**: `pam_exec` chủ động xóa sạch biến môi trường của tiến trình cha, chỉ truyền một số biến giới hạn (`PAM_USER`, `PAM_SERVICE`, `PAM_TTY`...).
- **Cơ chế PipeWire/ALSA**: Mọi client ALSA/PipeWire chạy ở user space cần biến `XDG_RUNTIME_DIR=/run/user/<UID>` để tìm socket `/run/user/<UID>/pipewire-0`.
- **Kiểm chứng thực tế**:
  ```bash
  $ env -i arecord -d 1 /tmp/test.wav
  # Output: arecord: main:850: audio open error: Host is down
  ```
  => Script trong `plan.md` sẽ lập tức crash với lỗi `Host is down` nếu không được inject thủ công `XDG_RUNTIME_DIR`.

### 2.4. Quyền Thực thi của `kscreenlocker_greet`
- `kscreenlocker_greet` chạy với quyền user thường (`zxcvmh`), không phải root (không có SUID bit).
- Nếu script ghi âm cần ghi log hoặc đọc config ở `/opt/voice-2fa/` mà phân quyền root-only, script sẽ văng lỗi permission denied âm thầm, dẫn đến PAM trả về lỗi và chặn mở khóa.

### 2.5. Trạng thái Suspend / Sleep của Laptop
- Khi gập máy hoặc laptop vào chế độ `suspend` (S3 hoặc s2idle):
  - CPU tạm dừng hoạt động, bus âm thanh ngắt nguồn.
  - Không có bất kỳ phần mềm STT nào có thể chạy được trên Linux phổ thông khi CPU đang ngủ (trừ khi phần cứng có chip DSP Always-On Voice chuyên dụng hỗ trợ Wake-on-Voice ở cấp độ BIOS/ACPI).
  - Do đó, tính năng mở khóa bằng giọng nói chỉ khả dụng khi máy ở trạng thái **Screen Lock (Idle/DPMS off)**, không khả dụng khi máy đã vào **Suspend**.

---

## 3. So sánh Hai Hướng Tiếp Cận Kiến Trúc

| Tiêu chí | Hướng 1: PAM Hook (`plan.md`) | Hướng 2: Background Daemon + D-Bus `loginctl` |
|---|---|---|
| **Bản chất** | Can thiệp vào chuỗi PAM của hệ thống | Daemon chạy nền quản lý sự kiện khóa màn hình |
| **Độ rủi ro hệ thống** | **Rất cao** (có thể brick màn hình login nếu script lỗi) | **Không có rủi ro** (PAM hoàn toàn nguyên vẹn) |
| **Trải nghiệm Hands-free** | ❌ Không (phải đánh thức màn hình để trigger PAM) |  Có (nói là mở ngay khi màn hình đang tắt/khóa) |
| **Khả năng Fallback Password** | Phức tạp, dễ xung đột logic flag PAM |  Luôn hoạt động 100% bình thường |
| **Tác động đến UI** | Làm đơ UI nhập password 3-5s mỗi lần mở màn hình | Không ảnh hưởng UI, mở khóa tức thì qua D-Bus |
| **Bảo mật** | Không phân biệt được chủ nhân vs người lạ (STT text match) | Tương tự (cần cân nhắc Wake Word / Speaker ID) |

---

## 4. Chi tiết Hướng tiếp cận Khuyến nghị (Background Daemon)

Nếu mục tiêu là **mở khóa laptop bằng giọng nói**:

```
[Màn hình bị khóa (KDE Plasma 6)]
        │
        ▼
[Voice Unlock Daemon (User Service / Python)]
        │  (Lắng nghe D-Bus: org.freedesktop.ScreenSaver.ActiveChanged)
        │
        ├─ Screen UNLOCKED ──► Tạm dừng mic (tiết kiệm pin & bảo mật mic)
        │
        └─ Screen LOCKED ──► Mở mic stream với lightweight wake-word / Vosk
                 │
                 ▼
          Nhận diện đúng passphrase
                 │
                 ▼
          Gọi D-Bus: org.freedesktop.login1.Session.Unlock
          hoặc chạy `loginctl unlock-session $XDG_SESSION_ID`
                 │
                 ▼
          KWin / kscreenlocker tự động gỡ màn hình khóa!
```

### Ưu điểm vượt trội:
1. **An toàn tuyệt đối**: Không chỉnh sửa bất kỳ file nào trong `/etc/pam.d/`. Nếu script chết, máy vẫn dùng password bình thường.
2. **Tiết kiệm pin**: Daemon chỉ kích hoạt mic khi màn hình đang bị khóa (`ScreenSaver.Active == true`). Khi đang dùng máy bình thường, mic hoàn toàn tắt.
3. **Mượt mà**: Không gây độ trễ (delay) cho khung nhập mật khẩu thông thường.

---

## 5. Kết luận & Nguồn tham khảo

1. **Linux-PAM System Administrators' Guide**: Các quy định về control flags (`required`, `requisite`, `sufficient`, `optional`).
   - Source: `man pam.conf`, `man pam_exec`
2. **systemd-logind D-Bus API Specification**:
   - Source: [systemd org.freedesktop.login1](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html)
3. **KDE Plasma Screen Locker Architecture**:
   - Package: `kscreenlocker` (source: `kwin_wayland` integration & `PamWorker`).
