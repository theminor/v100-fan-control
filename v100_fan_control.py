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

Two hardware modes, auto-detected by what the driver exposes:
  - Super I/O (it87 on the Z690 Desktop rig): the driver has a
    pwmN_enable file per channel; we re-write enable=1 before every
    PWM write to hold manual control against BIOS reverts (unchanged
    original behavior).
  - Dell SMM (dell_smm on the Precision T5810 AI rig): the driver
    exposes NO pwmN_enable files and the EC quantizes 0-255 writes
    into coarse duty levels (and STOPS the fan below ~duty 70).
    Direct writes still work; the EC re-seeks its own duty policy
    around us, which this loop out-runs by rewriting every cycle.
    Requires the 5-fan dell-smm-hwmon DKMS module to see the SYS_FAN3
    "PCIe Fan" channel (pwm5) on a T5810. See README "Dell SMM mode".

Hardware notes:
  - Requires nvidia-smi and the it87 (or compatible) kernel module,
    OR the dell_smm kernel driver (see README for the T5810 5-fan module)
  - Requires root (sudo) — needs write access to /sys/class/hwmon/
  - Supports any number of GPUs (1, 2, 3, 4, 5+) — limited only by
    the number of PWM channels available on the motherboard
  - --gpu-pci lets you pin a mapping to a physical PCI address so a
    GPU re-numbering can never point a fan curve at the wrong card

Quick start:
  1. Install it87 driver: sudo apt install lm-sensors && sudo sensors-detect
     (For Dell SMM mode, see the README instead.)
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
#  SETTINGS — Edit these for your hardware (CLI flags override)
# ============================================================

# --- Hardware detection ---
# The Super I/O chip name (e.g., it87.2832, it8620e.2560, nct6775.2560).
# Dell systems use "dell_smm_hwmon" (its hwmon 'name' file is "dell_smm").
# Leave as default if it works; change if your motherboard uses a different chip.
HWMON_DEVICE = "it87.2832"

# --- Temperature curve ---
# Fans run at PWM_MIN when temp is at or below TEMP_MIN.
# Fans run at PWM_MAX when temp is at or above TEMP_MAX.
# Between those points the curve is linear.
TEMP_MIN = 40       # °C — fans at minimum below this
TEMP_MAX = 75       # °C — fans at 100% above this
PWM_MIN = 30        # PWM value (0-255). 60 prevents 120mm blower stall.
#   NOTE (Dell SMM / T5810): the EC quantizes duty and STOPS the PCIe-blower
#   below ~duty 70; measured levels: 255→~4000 RPM, 160→~1240, 128→~505,
#   96→~132, ≤64→stop. Use --pwm-min 160 there.
PWM_MAX = 255       # PWM value (0-255) at TEMP_MAX
POLL_INTERVAL = 3   # Seconds between temperature reads

# --- Acoustic Smoothing ---
# Prevents fans from jumping abruptly between speeds, which causes
# audible "revving." The fan speed is ramped in steps each cycle.
MAX_STEP_UP = 85    # Max PWM increase per tick (ramps up quickly for safety)
MAX_STEP_DOWN = 4   # Max PWM decrease per tick (coasts down slowly to stop revving)

# --- Stall / failure safety ---
# Above this commanded duty a fan MUST be spinning: if its paired
# fanN_input then errors (tach read fails = fan stopped on Dell SMM)
# we force PWM_MAX. Below it, a missing tach is normal (fan parked).
TACH_ARM_DUTY = 96
# If GPU temps have been unreadable this many cycles in a row, force
# PWM_MAX on every mapped fan before trying anything else ("fail loud").
FAILSAFE_TEMP_ERRORS = 3
# Original behavior: after this many consecutive unreadable cycles,
# exit nonzero so systemd restarts the service and clears driver state.
RESTART_AFTER_TEMP_ERRORS = 10

# --- Logging ---
LOG_FILE = "/var/log/v100-fan.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB before rotation
LOG_BACKUP_COUNT = 3

# ============================================================
#  FAN MAP — User-defined GPU-to-PWM assignments
# ============================================================

# Map each GPU index (from nvidia-smi) to its motherboard PWM channel number.
# The number corresponds to pwm1, pwm2, pwm3, etc. on your motherboard.
#
# THIS DICT DICTATES YOUR FAN MAPPING. Adjust the PWM channel numbers
# to match your physical wiring. Swap the numbers if fans are backwards.
# You can override this without editing the file via --gpu-map "0:5,1:2".
#
# Example for 2 GPUs where GPU 0 (x16 slot) is wired to PWM 2
# and GPU 1 (x4 slot) is wired to PWM 1:
#   GPU_TO_PWM = {0: 2, 1: 1}
#
# Example for 3 GPUs all wired sequentially (GPU 0→PWM 1, GPU 1→PWM 2, etc.):
#   GPU_TO_PWM = {0: 1, 1: 2, 2: 3}
#
# Dell T5810 AI rig (see README): GPU index 0 (PCIe V100, bus 03:00.0) →
# PWM 5 (SYS_FAN3 / "PCIe Fan"):  GPU_TO_PWM = {0: 5}
#
# Add or remove entries as needed for your GPU count.
GPU_TO_PWM = {
    0: 2,  # GPU 0 (x16 slot) → PWM channel 2
    1: 1,  # GPU 1 (x4 slot) → PWM channel 1
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="V100 GPU Fan Control — automatic PWM fan curve for NVIDIA GPUs"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose logging (temps + PWM changes every cycle)"
    )
    parser.add_argument(
        "--hwmon-device", default=None, metavar="NAME",
        help="hwmon chip device directory name, e.g. it87.2832 or "
             "dell_smm_hwmon (default: the HWMON_DEVICE constant)"
    )
    parser.add_argument(
        "--gpu-map", default=None, metavar="GPU:PWM[,GPU:PWM...]",
        help='Override GPU_TO_PWM, e.g. --gpu-map "0:5" or "0:2,1:1"'
    )
    parser.add_argument(
        "--gpu-pci", action="append", default=[], metavar="GPU=PCI",
        help="Pin a mapped GPU index to its PCI bus id (fail startup on "
             "mismatch), e.g. --gpu-pci 0=0000:03:00.0. Repeat per GPU."
    )
    parser.add_argument(
        "--pwm-min", type=int, default=None, metavar="N",
        help="Override PWM_MIN (keep >=160 on Dell SMM / PCIe blower)"
    )
    parser.add_argument(
        "--pwm-max", type=int, default=None, metavar="N",
        help="Override PWM_MAX (default 255)"
    )
    parser.add_argument(
        "--poll-interval", type=int, default=None, metavar="S",
        help="Override POLL_INTERVAL seconds"
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


def get_hwmon_base(device):
    """Finds the dynamic hwmon directory for the specific fan chip.

    The physical device address (e.g., it87.2832, dell_smm_hwmon) stays
    constant across reboots. The hwmonN suffix (hwmon3, hwmon5, etc.) may
    change, so we glob to find it.
    """
    pattern = f"/sys/devices/platform/{device}/hwmon/hwmon*/"
    paths = sorted(glob.glob(pattern))
    if paths:
        return paths[0]
    print(
        f"Error: Could not find hwmon path for '{device}'. "
        "Check the HWMON_DEVICE setting (or --hwmon-device) and verify the "
        "driver is loaded: lsmod | grep <driver_name>",
        file=sys.stderr,
    )
    sys.exit(1)


def get_available_pwm_channels(base_path):
    """Discover all PWM channels available on the motherboard.

    Returns a sorted list of (pwm_number, pwm_file) tuples,
    e.g., [(1, '/sys/.../pwm1'), (2, '/sys/.../pwm2'), ...].

    Filters to only files named exactly 'pwm' followed by digits
    (ignoring files like 'pwm3_auto_point1_temp').
    """
    all_files = glob.glob(os.path.join(base_path, "pwm*"))
    pwm_files = []
    for fpath in all_files:
        name = os.path.basename(fpath)
        if name.startswith("pwm"):
            num_str = name[3:]
            if num_str.isdigit():
                pwm_files.append((int(num_str), fpath))
    return sorted(pwm_files, key=lambda x: x[0])


def run_nvidia_smi_query(fields):
    """Run an nvidia-smi --query-gpu and return list of field lists."""
    output = subprocess.check_output(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader"],
        encoding="utf-8",
        timeout=10,
    )
    rows = []
    for line in output.strip().splitlines():
        if line.strip():
            rows.append([p.strip() for p in line.split(",")])
    return rows


def discover_all_gpus():
    """Query nvidia-smi to find all GPUs on the system.

    Returns a sorted list of GPU indices.
    """
    try:
        return sorted(int(r[0]) for r in run_nvidia_smi_query("index"))
    except Exception as e:
        print(f"Error discovering GPUs: {e}", file=sys.stderr)
        sys.exit(1)


def normalize_pci(pci):
    """Normalize a PCI id for comparison.

    nvidia-smi reports 8-digit domains (00000000:03:00.0) while lspci-style
    ids use 4 (0000:03:00.0); compare as integer tuples so either works.
    """
    try:
        head, func = pci.lower().rsplit(".", 1)
        parts = head.split(":")
        return (int(parts[0], 16), int(parts[1], 16), int(parts[2], 16), int(func))
    except (ValueError, IndexError):
        return None


def verify_gpu_pci(guards):
    """Refuse to start if a mapped GPU index is not the physical card
    the operator pinned it to. guards: {gpu_index: normalized_pci_tuple}.
    Guards against nvidia-smi index renumbering pointing a fan curve at
    the wrong card (or a card that does not exist)."""
    try:
        rows = run_nvidia_smi_query("index,pci.bus_id")
    except Exception as e:
        logger.error("nvidia-smi PCI query failed: %s", e)
        sys.exit(1)
    actual = {int(r[0]): normalize_pci(r[1]) for r in rows}
    for gpu_idx, want in guards.items():
        got = actual.get(gpu_idx)
        if got != want:
            logger.error(
                "GPU %d is at PCI %s but was pinned to %s — refusing to start "
                "(check --gpu-pci and your GPU numbering).",
                gpu_idx,
                ":".join(f"%04x" % p for p in got) if got else "NOT PRESENT",
                ":".join(f"%04x" % p for p in want),
            )
            sys.exit(1)
        logger.info("GPU %d verified at PCI %s", gpu_idx, ":".join("%04x" % p for p in want))


def parse_gpu_map(spec):
    """Parse '0:5,1:2' into {0: 5, 1: 2}."""
    try:
        pairs = [p.strip() for p in spec.split(",") if p.strip()]
        result = {}
        for p in pairs:
            g, pwm = p.split(":")
            result[int(g)] = int(pwm)
        return result
    except ValueError:
        print(f"Error: cannot parse --gpu-map '{spec}' (expected like 0:5,1:2)", file=sys.stderr)
        sys.exit(1)


# --- enable-file handling (Super I/O has pwmN_enable; Dell SMM does not) ---

def has_enable_interface(pwm_file):
    """True when the driver exposes pwmN_enable (Super I/O style)."""
    return os.path.exists(f"{pwm_file}_enable")


def set_fan_speed(gpu_idx, pwm_file, pwm_value):
    """Write PWM value to the motherboard fan header.

    On Super I/O chips we unconditionally re-enforce manual mode right
    before each write: the BIOS SMM can silently revert the *_enable flag
    without updating sysfs, so we re-lock it each cycle.
    On Dell SMM there are no *_enable files at all — direct writes are
    the only (and sufficient) interface; the EC's own policy re-seeks
    around us, which the repeating loop out-runs.
    """
    try:
        if has_enable_interface(pwm_file):
            enable_file = f"{pwm_file}_enable"
            with open(enable_file, 'w') as ew:
                ew.write('1')

        with open(pwm_file, 'w') as f:
            f.write(str(pwm_value))
    except Exception as e:
        logger.error("GPU %d: Error writing PWM to %s: %s", gpu_idx, pwm_file, e)


def tach_file_for(pwm_file):
    """The fanN_input paired with pwmN, or None if the chip lacks it."""
    name = os.path.basename(pwm_file)          # e.g. pwm5
    n = name[3:]
    candidate = os.path.join(os.path.dirname(pwm_file), f"fan{n}_input")
    return candidate if os.path.exists(candidate) else None


def read_tach(path):
    """Read fan RPM; returns None when the tach errors (fan stopped /
    no pulses) or is unreadable. Only meaningful when a duty above
    TACH_ARM_DUTY is commanded."""
    try:
        with open(path) as f:
            rpm = int(f.read().strip())
        # A few drivers return 0 rather than an error for a parked fan.
        return rpm if rpm > 0 else None
    except (ValueError, OSError):
        return None


def enable_manual_mode(fan_map, initial_setup=False):
    """Force motherboard fan headers into manual PWM control.

    The Super I/O driver exposes an *_enable file per PWM channel.
    Setting it to '1' disables the BIOS fan curve and lets us write
    PWM values directly. Drivers without enable files (Dell SMM) are
    skipped — direct PWM writes are their manual mode.
    """
    for gpu_idx, pwm_file in fan_map.items():
        if not has_enable_interface(pwm_file):
            if initial_setup:
                logger.info(
                    "GPU %d fan (%s): no enable interface (Dell SMM mode) — "
                    "using direct PWM writes with per-cycle re-enforcement",
                    gpu_idx, os.path.basename(pwm_file),
                )
            continue
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


def restore_bios_control(fan_map):
    """Restore BIOS fan control by disabling manual PWM mode.

    On Dell SMM there is no enable toggle: the EC simply resumes its own
    thermal policy on its own schedule once our write loop stops.
    """
    logger.info("Restoring BIOS fan control...")
    for gpu_idx, pwm_file in fan_map.items():
        if not has_enable_interface(pwm_file):
            logger.info(
                "GPU %d fan (%s): no enable interface — Dell EC resumes auto "
                "policy on its own once this loop stops.",
                gpu_idx, os.path.basename(pwm_file),
            )
            continue
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
    restore_bios_control(FAN_MAP)
    logger.info("Fan control stopped. BIOS curve is now active.")
    sys.exit(0)


def get_gpu_temps():
    """Read GPU core AND memory temps from nvidia-smi.

    Returns dict of {gpu_index: max(core_temp, memory_temp)}.

    We use the hotter of core or memory because:
    - HBM memory on V100s can run 3–15°C hotter than the core
    - the motherboard BIOS curve can't see the GPU memory temp
    - A hot memory chip will throttle performance even if the core is cool

    GPUs whose query result is not a number ([N/A], [Not Supported] —
    common on non-V100 cards and multi-GPU boxes) fall back to whichever
    temperature IS readable instead of breaking the whole parse.
    """
    temps = {}
    try:
        rows = run_nvidia_smi_query("index,temperature.gpu,temperature.memory")
    except subprocess.TimeoutExpired:
        logger.error("nvidia-smi timed out")
        return temps
    except subprocess.CalledProcessError as e:
        logger.error("nvidia-smi failed (rc=%d): %s", e.returncode, e.output.strip())
        return temps
    except Exception as e:
        logger.error("Error reading GPU temps: %s", e)
        return temps

    for parts in rows:
        if len(parts) < 2:
            continue

        def to_int(token):
            try:
                return int(token)
            except (ValueError, IndexError):
                return None

        idx = to_int(parts[0])
        core = to_int(parts[1]) if len(parts) > 1 else None
        mem = to_int(parts[2]) if len(parts) > 2 else None
        if idx is None:
            continue
        candidates = [t for t in (core, mem) if t is not None]
        if not candidates:
            logger.debug("GPU %d: no numeric temperature in '%s' — skipped", idx, ",".join(parts))
            continue
        temps[idx] = max(candidates)
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


# Same singleton object setup_logging() configures below; binding it here
# means every function can use it once handlers are attached in main().
logger = logging.getLogger("v100-fan")
FAN_MAP = {}       # set in main()


def main():
    global FAN_MAP
    global PWM_MIN, PWM_MAX, POLL_INTERVAL, HWMON_DEVICE, GPU_TO_PWM

    args = parse_args()

    # CLI overrides of the SETTINGS block
    if args.hwmon_device:
        HWMON_DEVICE = args.hwmon_device
    if args.gpu_map:
        GPU_TO_PWM = parse_gpu_map(args.gpu_map)
    if args.pwm_min is not None:
        PWM_MIN = args.pwm_min
    if args.pwm_max is not None:
        PWM_MAX = args.pwm_max
    if args.poll_interval is not None:
        POLL_INTERVAL = args.poll_interval

    setup_logging()  # attaches handlers to the module logger singleton
    logger.setLevel(logging.INFO if args.debug else logging.WARNING)

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    logger.info("=" * 60)
    logger.info("V100 Fan Control starting (debug=%s)", args.debug)
    logger.info("HWMON device: %s", HWMON_DEVICE)
    logger.info("Curve: %d°C→PWM %d, %d°C→PWM %d", TEMP_MIN, PWM_MIN, TEMP_MAX, PWM_MAX)
    logger.info("Smoothing: max step up=%d, max step down=%d", MAX_STEP_UP, MAX_STEP_DOWN)
    logger.info("Poll interval: %ds", POLL_INTERVAL)

    base_path = get_hwmon_base(HWMON_DEVICE)

    # Build the FAN_MAP by looking up each GPU-to-PWM assignment.
    # This validates that every assigned PWM channel exists on the motherboard.
    available_pwms = get_available_pwm_channels(base_path)
    gpu_indices = discover_all_gpus()
    pwm_channel_to_file = {pwm_num: pwm_file for pwm_num, pwm_file in available_pwms}

    fan_map = {}
    for gpu_idx, pwm_num in GPU_TO_PWM.items():
        if pwm_num in pwm_channel_to_file:
            fan_map[gpu_idx] = pwm_channel_to_file[pwm_num]
        else:
            logger.error(
                "PWM channel %d is assigned to GPU %d but does not exist. "
                "Check GPU_TO_PWM assignment. Available channels: %s",
                pwm_num, gpu_idx, [p for p, _ in available_pwms],
            )
            sys.exit(1)

    # Verify all assigned GPUs are actually present
    for gpu_idx in GPU_TO_PWM:
        if gpu_idx not in gpu_indices:
            logger.warning(
                "GPU %d is assigned in GPU_TO_PWM but not found by nvidia-smi. "
                "It will be ignored.",
                gpu_idx,
            )

    # Verify all present GPUs have a mapping
    for gpu_idx in gpu_indices:
        if gpu_idx not in GPU_TO_PWM:
            logger.warning(
                "GPU %d was found by nvidia-smi but has no PWM assignment. "
                "Add it to GPU_TO_PWM if you want it controlled.",
                gpu_idx,
            )

    if not fan_map:
        logger.error("No valid GPU-to-PWM mappings. Check GPU_TO_PWM assignment.")
        sys.exit(1)

    FAN_MAP = fan_map

    # Optional PCI pinning: refuse to start if an index is not the card we
    # think it is (GPU renumbering must never move a fan curve silently).
    guards = {}
    for spec in args.gpu_pci:
        try:
            g, pci = spec.split("=", 1)
            norm = normalize_pci(pci.strip())
            if norm is None:
                raise ValueError(spec)
            guards[int(g)] = norm
        except ValueError:
            logger.error("Cannot parse --gpu-pci '%s' (expected like 0=0000:03:00.0)", spec)
            sys.exit(1)
    if guards:
        verify_gpu_pci(guards)

    logger.info("Fan mapping:")
    for gpu_idx, pwm_file in FAN_MAP.items():
        tach = tach_file_for(pwm_file)
        logger.info(
            "  GPU %d -> %s%s",
            gpu_idx,
            os.path.basename(pwm_file),
            f" (tach: {os.path.basename(tach)})" if tach else " (no tach file)",
        )

    if not any(has_enable_interface(p) for p in FAN_MAP.values()):
        logger.info("Dell SMM mode detected (no pwmN_enable files).")

    logger.info(
        "Verify mapping by setting fans to 255 and unplugging each blower."
    )
    enable_manual_mode(FAN_MAP, initial_setup=True)
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

                        # Stall watchdog: at a commanded duty that must spin
                        # the fan, a failing tach read means stopped/no
                        # pulses — force max cooling immediately.
                        tach = tach_file_for(FAN_MAP[gpu_idx])
                        if tach and smoothed_pwm >= TACH_ARM_DUTY:
                            rpm = read_tach(tach)
                            if rpm is None:
                                logger.error(
                                    "GPU %d: commanded PWM %d but tach read "
                                    "failed — fan stopped? Forcing PWM %d",
                                    gpu_idx, smoothed_pwm, PWM_MAX,
                                )
                                current_pwms[gpu_idx] = PWM_MAX
                                set_fan_speed(gpu_idx, FAN_MAP[gpu_idx], PWM_MAX)
                            elif args.debug:
                                logger.info("GPU %d: tach=%d RPM", gpu_idx, rpm)

                        # Debug output so you can watch the smoothing in real-time
                        if args.debug and step != 0:
                            logger.info(
                                "GPU %d: Temp=%d°C | Target=%d | Actual=%d (Step: %d)",
                                gpu_idx, temp, target_pwm, smoothed_pwm, step,
                            )

            else:
                consecutive_errors += 1
                logger.warning("No GPU temps read (Error #%d) — skipping cycle", consecutive_errors)

                if consecutive_errors == FAILSAFE_TEMP_ERRORS:
                    logger.error(
                        "GPU temps unreadable — forcing PWM_MAX on all mapped "
                        "fans until telemetry recovers (fail loud)."
                    )
                    for gpu_idx, pwm_file in FAN_MAP.items():
                        current_pwms[gpu_idx] = PWM_MAX
                        set_fan_speed(gpu_idx, pwm_file, PWM_MAX)

                if consecutive_errors >= RESTART_AFTER_TEMP_ERRORS:
                    logger.error(
                        "Continuous NVML failures detected. "
                        "Forcing service restart to clear driver state..."
                    )
                    sys.exit(1)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        restore_bios_control(FAN_MAP)


if __name__ == "__main__":
    main()
