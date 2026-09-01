#!/usr/bin/env python3
"""
V100 GPU Fan Control Script
Reads GPU core + memory temps from nvidia-smi and controls
motherboard PWM fan headers via /sys/class/hwmon/.

Each V100 has its own dedicated blower fan, independently controlled.
Uses the MAX of core and memory temp per GPU (memory often runs hotter).

Designed to run as a systemd service (v100-fan-control.service).
On graceful shutdown, restores BIOS fan control.
"""

import subprocess
import time
import os
import sys
import signal
import logging
import argparse
import glob
from datetime import datetime

# --- CONFIGURATION ---

def get_hwmon_base():
    """Finds the dynamic hwmon directory for the specific ITE chip."""
    paths = glob.glob("/sys/devices/platform/it87.2832/hwmon/hwmon*/")
    if paths:
        return paths[0]
    else:
        print("Error: Could not find the it87.2832 hardware monitor path. Retrying...", file=sys.stderr)
        sys.exit(1)

BASE_PATH = get_hwmon_base()

# Map NVIDIA GPU Index to the corresponding motherboard PWM file.
FAN_MAP = {
    0: os.path.join(BASE_PATH, "pwm2"),  # GPU 0 (x16 slot, top V100)
    1: os.path.join(BASE_PATH, "pwm1"),  # GPU 1 (x4 slot, middle V100)
}

# Temperature curve parameters (per GPU, independent):
TEMP_MIN = 40       # °C — fans at minimum below this
TEMP_MAX = 75       # °C — fans at 100% above this
PWM_MIN = 30        # PWM value (0-255). 60 prevents 120mm blower stall.
PWM_MAX = 255       # PWM value (0-255) at TEMP_MAX
POLL_INTERVAL = 3   # Seconds between temperature reads

# Smoothing parameters (Acoustic Management)
MAX_STEP_UP = 85    # Max PWM increase per tick (ramps up quickly for safety)
MAX_STEP_DOWN = 4   # Max PWM decrease per tick (coasts down slowly to stop revving)

# Logging
LOG_FILE = "/var/log/v100-fan.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
# ---------------------

def parse_args():
    parser = argparse.ArgumentParser(description="V100 GPU Fan Control")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose logging (temps + PWM changes)")
    return parser.parse_args()

def setup_logging():
    from logging.handlers import RotatingFileHandler
    logger = logging.getLogger("v100-fan")
    logger.setLevel(logging.INFO)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(ch)

    return logger

logger = setup_logging()

def enable_manual_mode(initial_setup=False):
    """Force motherboard fan headers into manual PWM control."""
    for gpu_idx, pwm_file in FAN_MAP.items():
        enable_file = f"{pwm_file}_enable"
        try:
            with open(enable_file, 'w') as f:
                f.write('1')
            if initial_setup:
                logger.info(f"Enabled manual control for GPU {gpu_idx} fan ({enable_file})")
        except PermissionError:
            logger.error("Permission denied: script must be run as root.")
            sys.exit(1)
        except Exception as e:
            if initial_setup:
                logger.error(f"Error enabling manual mode for {enable_file}: {e}")
                sys.exit(1)

def restore_bios_control():
    """Restore BIOS fan control by disabling manual PWM mode."""
    logger.info("Restoring BIOS fan control...")
    for gpu_idx, pwm_file in FAN_MAP.items():
        enable_file = f"{pwm_file}_enable"
        try:
            with open(enable_file, 'w') as f:
                f.write('0')
            logger.info(f"Restored BIOS control for GPU {gpu_idx} fan ({enable_file})")
        except FileNotFoundError:
            logger.warning(f"File not found on shutdown: {enable_file}")
        except Exception as e:
            logger.error(f"Error restoring BIOS control for {enable_file}: {e}")

def graceful_shutdown(signum, frame):
    """Handle SIGINT/SIGTERM — restore BIOS control before exiting."""
    sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
    logger.info(f"Received {sig_name} — shutting down gracefully...")
    restore_bios_control()
    logger.info("Fan control stopped. BIOS curve is now active.")
    sys.exit(0)

def get_gpu_temps():
    """Read GPU core AND memory temps from nvidia-smi."""
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
        logger.error(f"nvidia-smi failed (rc={e.returncode}): {e.output.strip()}")
    except Exception as e:
        logger.error(f"Error reading GPU temps: {e}")
    return temps

def calculate_pwm(temp):
    """Map temperature to PWM value using a linear curve."""
    if temp <= TEMP_MIN: return PWM_MIN
    if temp >= TEMP_MAX: return PWM_MAX
    temp_range = TEMP_MAX - TEMP_MIN
    pwm_range = PWM_MAX - PWM_MIN
    temp_percent = (temp - TEMP_MIN) / temp_range
    return int(PWM_MIN + (temp_percent * pwm_range))

def set_fan_speed(gpu_idx, pwm_file, pwm_value):
    """Write PWM value, overriding BIOS resets unconditionally."""
    try:
        # Unconditionally enforce manual mode right before writing
        # (BIOS SMM can silently change hardware without updating sysfs)
        enable_file = f"{pwm_file}_enable"
        with open(enable_file, 'w') as ew:
            ew.write('1')

        # Set the speed
        with open(pwm_file, 'w') as f:
            f.write(str(pwm_value))
    except Exception as e:
        logger.error(f"GPU {gpu_idx}: Error writing PWM to {pwm_file}: {e}")

if __name__ == "__main__":
    args = parse_args()
    logger.setLevel(logging.INFO if args.debug else logging.WARNING)

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    logger.info("=" * 60)
    logger.info("V100 Fan Control starting (debug=%s)", args.debug)
    enable_manual_mode(initial_setup=True)
    logger.info("Fan control loop started")

    consecutive_errors = 0
    current_pwms = {}  # Tracks the actual running speed of each fan

    try:
        while True:
            gpu_temps = get_gpu_temps()

            if gpu_temps:
                consecutive_errors = 0  # Reset counter on success
                for gpu_idx, temp in gpu_temps.items():
                    if gpu_idx in FAN_MAP:
                        target_pwm = calculate_pwm(temp)

                        # Initialize tracking on the first loop
                        if gpu_idx not in current_pwms:
                            current_pwms[gpu_idx] = target_pwm

                        # Acoustic Smoothing Logic
                        diff = target_pwm - current_pwms[gpu_idx]

                        if diff > 0:
                            # Needs to speed up - limit the jump to MAX_STEP_UP
                            step = min(diff, MAX_STEP_UP)
                        elif diff < 0:
                            # Needs to slow down - limit the drop to MAX_STEP_DOWN
                            step = max(diff, -MAX_STEP_DOWN)
                        else:
                            step = 0
 
                        smoothed_pwm = current_pwms[gpu_idx] + step
                        current_pwms[gpu_idx] = smoothed_pwm

                        set_fan_speed(gpu_idx, FAN_MAP[gpu_idx], smoothed_pwm)

                        # Added to debug output so you can watch the smoothing in real-time
                        if args.debug and step != 0:
                            logger.info(f"GPU {gpu_idx}: Temp={temp}°C | Target={target_pwm} | Actual={smoothed_pwm} (Step: {step})")

            else:
                consecutive_errors += 1
                logger.warning(f"No GPU temps read (Error #{consecutive_errors}) — skipping cycle")

                if consecutive_errors >= 10:
                    logger.error("Continuous NVML failures detected. Forcing service restart to clear driver state...")
                    sys.exit(1)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        restore_bios_control()
