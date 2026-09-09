#!/usr/bin/env python3
"""
V100 GPU Fan Control Script

Author: theminor (https://github.com/theminor)
License: MIT
Repo: https://github.com/theminor/v100-fan-control

Reads GPU core + memory temps from nvidia-smi and controls
motherboard PWM fan headers via /sys/class/hwmon/.

Each GPU has its own dedicated blower fan, independently controlled.
Uses the MAX of core and memory temp per GPU (memory often runs hotter).

Designed to run as a systemd service (v100-fan-control.service).
On graceful shutdown, restores BIOS fan control.

Hardware notes:
  - Requires nvidia-smi and the it87 (or compatible) kernel module
  - Requires root (sudo) — needs write access to /sys/class/hwmon/
  - Supports any number of GPUs (1, 2, 3, 4, 5+) — limited only by
    the number of PWM channels available on the motherboard

Quick start:
  1. Install it87 driver: sudo apt install lm-sensors && sudo sensors-detect
  2. Verify hwmon path: ls /sys/devices/platform/<chip>/hwmon/hwmon*/
  3. Verify fan mapping (see FAN_MAP section below)
  4. Run: sudo python3 v100_fan_control.py --debug
  5. If happy, set up as a systemd service (see README)
"""

import subprocess
import time
import os
import sys
import signal
import logging
import argparse
import glob

# ============================================================
#  SETTINGS — Edit these for your hardware
# ============================================================

# --- Hardware detection ---
# The Super I/O chip name (e.g., it87.2832, it8620e.2560, nct6775.2560).
# Leave as default if it works; change if your motherboard uses a different chip.
HWMON_DEVICE = "it87.2832"

# --- Temperature curve ---
# Fans run at PWM_MIN when temp is at or below TEMP_MIN.
# Fans run at PWM_MAX when temp is at or above TEMP_MAX.
# Between those points the curve is linear.
TEMP_MIN = 40       # °C — fans at minimum below this
TEMP_MAX = 75       # °C — fans at 100% above this
PWM_MIN = 30        # PWM value (0-255). 60 prevents 120mm blower stall.
PWM_MAX = 255       # PWM value (0-255) at TEMP_MAX
POLL_INTERVAL = 3   # Seconds between temperature reads

# --- Acoustic Smoothing ---
# Prevents fans from jumping abruptly between speeds, which causes
# audible "revving." The fan speed is ramped in steps each cycle.
MAX_STEP_UP = 85    # Max PWM increase per tick (ramps up quickly for safety)
MAX_STEP_DOWN = 4   # Max PWM decrease per tick (coasts down slowly to stop revving)

# --- Logging ---
LOG_FILE = "/var/log/v100-fan.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB before rotation
LOG_BACKUP_COUNT = 3

# ============================================================
#  FAN MAP — Auto-detected + user verification
# ============================================================

def get_hwmon_base():
    """Finds the dynamic hwmon directory for the specific ITE chip.

    The physical device address (e.g., it87.2832) stays constant across reboots.
    The hwmonN suffix (hwmon3, hwmon5, etc.) may change, so we glob to find it.
    """
    pattern = f"/sys/devices/platform/{HWMON_DEVICE}/hwmon/hwmon*/"
    paths = glob.glob(pattern)
    if paths:
        return paths[0]
    else:
        print(
            f"Error: Could not find hwmon path for '{HWMON_DEVICE}'. "
            "Check the HWMON_DEVICE setting and verify the driver is loaded: "
            "lsmod | grep <driver_name>",
            file=sys.stderr,
        )
        sys.exit(1)


BASE_PATH = get_hwmon_base()


def get_available_pwm_channels():
    """Discover all PWM channels available on the motherboard.

    Returns a sorted list of (pwm_number, pwm_file) tuples,
    e.g., [(1, '/sys/.../pwm1'), (2, '/sys/.../pwm2'), ...].

    Filters to only files named exactly 'pwm' followed by digits
    (ignoring files like 'pwm3_auto_point1_temp').
    """
    all_files = glob.glob(os.path.join(BASE_PATH, "pwm*"))
    pwm_files = []
    for fpath in all_files:
        name = os.path.basename(fpath)
        if name.startswith("pwm"):
            num_str = name[3:]
            if num_str.isdigit():
                pwm_files.append((int(num_str), fpath))
    return sorted(pwm_files, key=lambda x: x[0])


def discover_all_gpus():
    """Query nvidia-smi to find all GPUs on the system.

    Returns a sorted list of GPU indices.
    """
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            encoding="utf-8",
            timeout=10,
        )
        indices = [int(line.strip()) for line in output.strip().split("\n") if line.strip()]
        return sorted(indices)
    except Exception as e:
        logger.error(f"Error discovering GPUs: {e}")
        sys.exit(1)


# Build dynamic FAN_MAP: GPU 0 -> PWM 1, GPU 1 -> PWM 2, etc.
# If there are more GPUs than PWM channels, the extras will be skipped.
AVAILABLE_PWMS = get_available_pwm_channels()
GPU_INDICES = discover_all_gpus()

# Build the mapping
FAN_MAP = {}
for i, gpu_idx in enumerate(GPU_INDICES):
    if i < len(AVAILABLE_PWMS):
        pwm_num, pwm_file = AVAILABLE_PWMS[i]
        FAN_MAP[gpu_idx] = pwm_file
    else:
        logger.warning(
            "GPU %d has no available PWM channel — skipping. "
            "You have %d GPUs but only %d PWM channels.",
            gpu_idx, len(GPU_INDICES), len(AVAILABLE_PWMS),
        )

# Verify we have at least one mapping
if not FAN_MAP:
    logger.error(
        "No GPU-to-PWM mappings found. "
        "Check that HWMON_DEVICE is correct and PWM channels exist."
    )
    sys.exit(1)

# ============================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="V100 GPU Fan Control — automatic PWM fan curve for NVIDIA GPUs"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose logging (temps + PWM changes every cycle)"
    )
    return parser.parse_args()


def setup_logging():
    """Configure logging to both file and stdout."""
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("v100-fan")
    logger.setLevel(logging.INFO)

    # File handler with rotation
    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(fh)

    # Console handler (visible in journalctl)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(ch)

    return logger


logger = setup_logging()


def enable_manual_mode(initial_setup=False):
    """Force motherboard fan headers into manual PWM control.

    The Super I/O driver exposes an *_enable file per PWM channel.
    Setting it to '1' disables the BIOS fan curve and lets us
    write PWM values directly.
    """
    for gpu_idx, pwm_file in FAN_MAP.items():
        enable_file = f"{pwm_file}_enable"
        try:
            with open(enable_file, 'w') as f:
                f.write('1')
            if initial_setup:
                logger.info("Enabled manual control for GPU %d fan (%s)", gpu_idx, enable_file)
        except PermissionError:
            logger.error("Permission denied: script must be run as root.")
            sys.exit(1)
        except Exception as e:
            if initial_setup:
                logger.error("Error enabling manual mode for %s: %s", enable_file, e)
                sys.exit(1)


def restore_bios_control():
    """Restore BIOS fan control by disabling manual PWM mode."""
    logger.info("Restoring BIOS fan control...")
    for gpu_idx, pwm_file in FAN_MAP.items():
        enable_file = f"{pwm_file}_enable"
        try:
            with open(enable_file, 'w') as f:
                f.write('0')
            logger.info("Restored BIOS control for GPU %d fan (%s)", gpu_idx, enable_file)
        except FileNotFoundError:
            logger.warning("File not found on shutdown: %s", enable_file)
        except Exception as e:
            logger.error("Error restoring BIOS control for %s: %s", enable_file, e)


def graceful_shutdown(signum, frame):
    """Handle SIGINT/SIGTERM — restore BIOS control before exiting."""
    sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
    logger.info("Received %s — shutting down gracefully...", sig_name)
    restore_bios_control()
    logger.info("Fan control stopped. BIOS curve is now active.")
    sys.exit(0)


def get_gpu_temps():
    """Read GPU core AND memory temps from nvidia-smi.

    Returns dict of {gpu_index: max(core_temp, memory_temp)}.

    We use the hotter of core or memory because:
    - HBM memory on V100s can run 3–15°C hotter than the core
    - The motherboard BIOS curve can't see the GPU memory temp
    - A hot memory chip will throttle performance even if the core is cool
    """
    temps = {}
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,temperature.gpu,temperature.memory",
                "--format=csv,noheader",
            ],
            encoding="utf-8",
            timeout=10,
        )
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            idx = int(parts[0].strip())
            core_temp = int(parts[1].strip())
            mem_temp = int(parts[2].strip())
            temps[idx] = max(core_temp, mem_temp)
    except subprocess.TimeoutExpired:
        logger.error("nvidia-smi timed out")
    except subprocess.CalledProcessError as e:
        logger.error("nvidia-smi failed (rc=%d): %s", e.returncode, e.output.strip())
    except Exception as e:
        logger.error("Error reading GPU temps: %s", e)
    return temps


def calculate_pwm(temp):
    """Map temperature to PWM value using a linear curve.

    temp <= TEMP_MIN → PWM_MIN (fans at minimum)
    temp >= TEMP_MAX → PWM_MAX (fans at 100%)
    Otherwise: linear interpolation between the two
    """
    if temp <= TEMP_MIN:
        return PWM_MIN
    if temp >= TEMP_MAX:
        return PWM_MAX
    temp_range = TEMP_MAX - TEMP_MIN
    pwm_range = PWM_MAX - PWM_MIN
    temp_percent = (temp - TEMP_MIN) / temp_range
    return int(PWM_MIN + (temp_percent * pwm_range))


def set_fan_speed(gpu_idx, pwm_file, pwm_value):
    """Write PWM value to the motherboard fan header.

    We unconditionally re-enforce manual mode right before each write.
    The BIOS SMM (System Management Mode) can silently revert the
    *_enable flag without updating sysfs, so we re-lock it each cycle.
    """
    try:
        enable_file = f"{pwm_file}_enable"
        with open(enable_file, 'w') as ew:
            ew.write('1')

        with open(pwm_file, 'w') as f:
            f.write(str(pwm_value))
    except Exception as e:
        logger.error("GPU %d: Error writing PWM to %s: %s", gpu_idx, pwm_file, e)


if __name__ == "__main__":
    args = parse_args()
    logger.setLevel(logging.INFO if args.debug else logging.WARNING)

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    logger.info("=" * 60)
    logger.info("V100 Fan Control starting (debug=%s)", args.debug)
    logger.info("HWMON device: %s", HWMON_DEVICE)
    logger.info("GPU count: %d, PWM channels available: %d",
                len(GPU_INDICES), len(AVAILABLE_PWMS))
    logger.info("Curve: %d°C→PWM %d, %d°C→PWM %d", TEMP_MIN, PWM_MIN, TEMP_MAX, PWM_MAX)
    logger.info("Smoothing: max step up=%d, max step down=%d", MAX_STEP_UP, MAX_STEP_DOWN)
    logger.info("Poll interval: %ds", POLL_INTERVAL)

    # Print the auto-detected mapping
    logger.info("Auto-detected fan mapping:")
    for gpu_idx, pwm_file in FAN_MAP.items():
        logger.info("  GPU %d -> %s", gpu_idx, os.path.basename(pwm_file))

    logger.info(
        "Verify mapping by setting fans to 255 and unplugging each blower."
    )
    enable_manual_mode(initial_setup=True)
    logger.info("Fan control loop started")

    consecutive_errors = 0
    current_pwms = {}  # Tracks the actual running speed of each fan for smoothing

    try:
        while True:
            gpu_temps = get_gpu_temps()

            if gpu_temps:
                consecutive_errors = 0  # Reset counter on success
                for gpu_idx, temp in gpu_temps.items():
                    if gpu_idx in FAN_MAP:
                        target_pwm = calculate_pwm(temp)

                        # Initialize tracking on first loop
                        if gpu_idx not in current_pwms:
                            current_pwms[gpu_idx] = target_pwm

                        # Acoustic Smoothing Logic
                        diff = target_pwm - current_pwms[gpu_idx]

                        if diff > 0:
                            # Needs to speed up — limit the jump to MAX_STEP_UP
                            step = min(diff, MAX_STEP_UP)
                        elif diff < 0:
                            # Needs to slow down — limit the drop to MAX_STEP_DOWN
                            step = max(diff, -MAX_STEP_DOWN)
                        else:
                            step = 0

                        smoothed_pwm = current_pwms[gpu_idx] + step
                        current_pwms[gpu_idx] = smoothed_pwm

                        set_fan_speed(gpu_idx, FAN_MAP[gpu_idx], smoothed_pwm)

                        # Debug output so you can watch the smoothing in real-time
                        if args.debug and step != 0:
                            logger.info(
                                "GPU %d: Temp=%d°C | Target=%d | Actual=%d (Step: %d)",
                                gpu_idx, temp, target_pwm, smoothed_pwm, step,
                            )

            else:
                consecutive_errors += 1
                logger.warning("No GPU temps read (Error #%d) — skipping cycle", consecutive_errors)

                if consecutive_errors >= 10:
                    logger.error(
                        "Continuous NVML failures detected. "
                        "Forcing service restart to clear driver state..."
                    )
                    sys.exit(1)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        restore_bios_control()
