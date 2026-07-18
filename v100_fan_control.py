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
from datetime import datetime

# --- CONFIGURATION ---

# Map NVIDIA GPU Index to the corresponding motherboard PWM file.
# CRITICAL: Verify each fan is mapped to the correct GPU!
#   - Set all to 255, then unplug one blower at a time.
#   - The one that stops tells you which GPU that PWM controls.
#
# Found via the it87 driver (https://github.com/frankcrawford/it87.git):
#   /sys/class/hwmon/hwmon5/pwm1 -> Fan header 1
#   /sys/class/hwmon/hwmon5/pwm2 -> Fan header 2
#
# TODO: VERIFY MAPPING IS CORRECT BEFORE LEAVING UNATTENDED
FAN_MAP = {
    0: "/sys/class/hwmon/hwmon5/pwm2",  # GPU 0 (x16 slot, top V100)
    1: "/sys/class/hwmon/hwmon5/pwm1",  # GPU 1 (x4 slot, middle V100)
}

# Temperature curve parameters (per GPU, independent):
TEMP_MIN = 40       # °C — fans at minimum below this
TEMP_MAX = 75       # °C — fans at 100% above this
PWM_MIN = 50        # PWM value (0-255) at TEMP_MIN
PWM_MAX = 255       # PWM value (0-255) at TEMP_MAX
POLL_INTERVAL = 3   # Seconds between temperature reads

# Logging
LOG_FILE = "/var/log/v100-fan.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB before rotation
LOG_BACKUP_COUNT = 3
# ---------------------

def parse_args():
    parser = argparse.ArgumentParser(description="V100 GPU Fan Control")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose logging (temps + PWM changes)")
    return parser.parse_args()

def setup_logging():
    """Configure logging to both file and stdout."""
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("v100-fan")
    logger.setLevel(logging.INFO)

    # File handler with rotation
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
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


def enable_manual_mode():
    """Force motherboard fan headers into manual PWM control (disable BIOS curve)."""
    for gpu_idx, pwm_file in FAN_MAP.items():
        enable_file = f"{pwm_file}_enable"
        try:
            # Read current state first
            with open(enable_file, 'r') as f:
                current = f.read().strip()
            if current == '1':
                logger.info(f"GPU {gpu_idx} fan already in manual mode ({enable_file})")
                continue

            with open(enable_file, 'w') as f:
                f.write('1')
            logger.info(f"Enabled manual control for GPU {gpu_idx} fan ({enable_file})")
        except PermissionError:
            logger.error("Permission denied: script must be run as root.")
            sys.exit(1)
        except FileNotFoundError:
            logger.error(f"File not found: {enable_file}. Check hwmon path.")
            sys.exit(1)
        except Exception as e:
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
    """
    Read GPU core AND memory temps from nvidia-smi.
    Returns dict of {gpu_index: max(core_temp, memory_temp)}.

    Using max(core, memory) because memory can run 3-15°C hotter
    than the core, and the BIOS curve can't see it anyway.
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
            # Use the hotter sensor — if memory is running hot, fans should respond
            temps[idx] = max(core_temp, mem_temp)
    except subprocess.TimeoutExpired:
        logger.error("nvidia-smi timed out")
    except subprocess.CalledProcessError as e:
        logger.error(f"nvidia-smi failed (rc={e.returncode}): {e.output}")
    except Exception as e:
        logger.error(f"Error reading GPU temps: {e}")
    return temps


def calculate_pwm(temp):
    """
    Map temperature to PWM value using a linear curve.

    temp <= TEMP_MIN → PWM_MIN
    temp >= TEMP_MAX → PWM_MAX
    Otherwise: linear interpolation
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
    """Write PWM value to the motherboard fan header."""
    try:
        with open(pwm_file, 'w') as f:
            f.write(str(pwm_value))
    except FileNotFoundError:
        logger.error(f"GPU {gpu_idx}: PWM file not found: {pwm_file}")
    except Exception as e:
        logger.error(f"GPU {gpu_idx}: Error writing PWM to {pwm_file}: {e}")


def verify_fan_mapping():
    """Log a warning if mapping hasn't been verified yet."""
    logger.info(
        "FAN MAPPING: GPU 0 -> %s, GPU 1 -> %s. "
        "Verify by setting fans to 255 and unplugging each blower to confirm.",
        FAN_MAP.get(0, "N/A"),
        FAN_MAP.get(1, "N/A"),
    )

if __name__ == "__main__":
    args = parse_args()

    # Re-create logger with appropriate level
    logger = logging.getLogger("v100-fan")
    if args.debug:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)  # Only errors + warnings

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    logger.info("=" * 60)
    logger.info("V100 Fan Control starting (debug=%s)", args.debug)
    logger.info("Curve: %d°C→PWM %d, %d°C→PWM %d", TEMP_MIN, PWM_MIN, TEMP_MAX, PWM_MAX)
    logger.info("Poll interval: %ds", POLL_INTERVAL)
    verify_fan_mapping()
    enable_manual_mode()
    logger.info("Fan control loop started")

    try:
        while True:
            gpu_temps = get_gpu_temps()

            if gpu_temps:
                for gpu_idx, temp in gpu_temps.items():
                    if gpu_idx in FAN_MAP:
                        target_pwm = calculate_pwm(temp)
                        set_fan_speed(gpu_idx, FAN_MAP[gpu_idx], target_pwm)

                # Debug-only logging
                if args.debug:
                    temp_summary = ", ".join(
                        f"GPU{i}:{gpu_temps[i]}°C" for i in sorted(gpu_temps)
                    )
                    pwm_summary = ", ".join(
                        f"GPU{i}:{calculate_pwm(gpu_temps[i])}"
                        for i in sorted(gpu_temps) if i in FAN_MAP
                    )
                    logger.info("Temps: %s | PWM: %s", temp_summary, pwm_summary)
            else:
                logger.warning("No GPU temps read — skipping this cycle")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        restore_bios_control()

