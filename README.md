# GPU Fan Control

Automatic PWM fan curve control for NVIDIA GPUs on Linux.

Reads GPU core + memory temperatures via `nvidia-smi` and writes PWM values to motherboard fan headers via the `hwmon` subsystem — either a Super I/O driver (it87, nct6775, or compatible) or the Dell EC driver (`dell_smm`). Each GPU's blower fan is independently controlled.

## Features

- **Auto-detects GPUs** — queries `nvidia-smi` to find all GPUs on the system (1, 2, 3, 4, 5+)
- **Auto-discovers hwmon path** — finds the fan chip's hwmon directory via glob, survives reboot renumbering
- **Two hardware modes, auto-detected** — Super I/O (`pwmN_enable` files, re-enforced each cycle) or Dell SMM (no enable files; writes only on duty change plus a slow re-enforcement, because rapid EC writes stall the fan)
- **User-defined fan mapping** — explicit GPU-to-PWM assignment via `GPU_TO_PWM` dict or `--gpu-map` (no guessing)
- **PCI pinning** — `--gpu-pci 0=0000:03:00.0` refuses startup if an nvidia-smi index is not the physical card you expect, so GPU renumbering can never point a fan curve at the wrong card
- **Linear temperature curve** — configurable min/max temperature and PWM thresholds
- **Memory temp monitoring** — uses the hotter of core or memory temp (HBM memory often runs hotter than the core); tolerates `[N/A]` readings on cards that don't report memory temp
- **Acoustic smoothing** — limits PWM step size per cycle to prevent audible fan "revving" (ramps up fast for safety, coasts down slowly)
- **Tach stall watchdog** — if a commanded duty above `TACH_ARM_DUTY` pairs with a failed tach read (fan stopped), forces `PWM_MAX` immediately
- **Fail-loud telemetry loss** — forces `PWM_MAX` on all mapped fans when GPU temps become unreadable, then restarts the service to clear driver state
- **Graceful shutdown** — restores BIOS fan control on SIGINT/SIGTERM
- **Error recovery** — auto-restarts after repeated nvidia-smi failures to clear NVML driver state
- **Rotating log files** — 5 MB max, 3 backups

## Hardware Requirements

- NVIDIA GPU(s) with nvidia-smi available
- Motherboard fan control exposed via `hwmon`:
  - Super I/O chip with PWM fan headers (it87, it8620e, nct6775, or compatible), **or**
  - Dell EC via `dell_smm` (see [Dell SMM mode](#dell-smm-mode-dell-precision-t5810))
- Linux kernel with the appropriate `hwmon` driver loaded
- Root privileges (the script writes directly to `/sys/class/hwmon/`)

## Prerequisites

```bash
# Install hardware monitoring tools
sudo apt install lm-sensors python3

# Load the Super I/O driver (replace it87 with your chip's driver)
sudo modprobe it87

# Verify the hwmon path exists
ls /sys/devices/platform/*/hwmon/hwmon*/pwm*
```

To identify your Super I/O chip:

```bash
sudo sensors-detect   # Answer YES to probing
# Look for lines like: "it87.2832" or "nct6775.2560"
```

## Quick Start

1. **Clone the repo:**

```bash
sudo git clone https://github.com/theminor/v100-fan-control.git /opt/v100-fan-control
```

2. **Verify fan-to-GPU mapping** (critical — wrong mapping = wrong fan on the hot GPU):

   a. **Temporarily set all fans to 255** (full speed) by adding `GPU_TO_PWM = {0: 1, 1: 1}` (or whatever channel exists) to force all assigned GPUs to max. Actually, the easiest way:

   ```bash
   # Run with --debug to see the mapping, then test physically
   sudo python3 /opt/v100-fan-control/v100_fan_control.py --debug
   ```

   b. **Unplug one blower cable at a time.** The fan that stops tells you which GPU index that PWM header controls.

   c. **Update `GPU_TO_PWM`** in the script to match your physical wiring. For example, if GPU 0 (x16 slot) is wired to PWM 2 and GPU 1 (x4 slot) is wired to PWM 1:

   ```python
   GPU_TO_PWM = {
       0: 2,  # GPU 0 → PWM channel 2
       1: 1,  # GPU 1 → PWM channel 1
   }
   ```

   Add or remove entries for your GPU count. The script validates that every assigned PWM channel exists and warns about any GPUs without assignments.

3. **Edit other settings** if needed (defaults work for most setups):

   ```python
   # Temperature curve
   TEMP_MIN = 40       # °C — fans at minimum below this
   TEMP_MAX = 75       # °C — fans at 100% above this
   PWM_MIN = 30        # PWM value (0-255) at TEMP_MIN
   PWM_MAX = 255       # PWM value (0-255) at TEMP_MAX

   # Smoothing
   MAX_STEP_UP = 85    # Max PWM increase per tick
   MAX_STEP_DOWN = 4   # Max PWM decrease per tick
   ```

4. **Test run** (dry run with debug logging):

   ```bash
   sudo python3 /opt/v100-fan-control/v100_fan_control.py --debug
   ```

   Check the console output and `/var/log/v100-fan.log` to confirm temps and PWM values look reasonable.

5. **When happy, set up as a systemd service** (see below).

## systemd Service

### Option A: Symlink (recommended)

Keep the service file in the repo and symlink it into systemd's directory. This way, `git pull` updates the service file automatically:

```bash
# Remove any existing service file
sudo rm -f /etc/systemd/system/v100-fan-control.service

# Create a symlink from systemd's directory to the repo file
sudo ln -s /opt/v100-fan-control/v100-fan-control.service /etc/systemd/system/v100-fan-control.service

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable v100-fan-control.service
sudo systemctl start v100-fan-control.service
```

### Option B: Copy

Copy the service file into systemd's directory:

```bash
sudo cp /opt/v100-fan-control/v100-fan-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable v100-fan-control.service
sudo systemctl start v100-fan-control.service
```

### Verify

```bash
sudo systemctl status v100-fan-control.service
sudo journalctl -u v100-fan-control.service -f
```

## GPU-to-PWM Mapping

The `GPU_TO_PWM` dict in the script explicitly maps each GPU index (from `nvidia-smi`) to its motherboard PWM channel number. This is the **only** place where hardware-specific wiring is defined.

```python
GPU_TO_PWM = {
    0: 2,  # GPU 0 (x16 slot) → PWM channel 2
    1: 1,  # GPU 1 (x4 slot) → PWM channel 1
}
```

For more GPUs, just add entries:

```python
GPU_TO_PWM = {
    0: 2,  # GPU 0 → PWM 2
    1: 1,  # GPU 1 → PWM 1
    2: 3,  # GPU 2 → PWM 3
    3: 4,  # GPU 3 → PWM 4
}
```

The script validates that:
- Every assigned PWM channel exists on the motherboard (errors out if not)
- Every assigned GPU is present (warns if not)
- Every detected GPU has an assignment (warns if not)

The hwmon path and available PWM channels are **auto-discovered** — you only need to specify the GPU-to-PWM mapping.

## Dell SMM mode (Dell Precision T5810)

Dell desktops have no Super I/O fan chip the OS can touch: fan control lives in the EC and is reached through the `dell_smm` hwmon driver via SMM BIOS calls. The script auto-detects this (no `pwmN_enable` files) and switches behavior: direct PWM writes, re-enforced every poll cycle.

Three things differ from Super I/O boards and matter:

1. **The stock kernel hides the 5th fan channel.** `DELL_SMM_NO_FANS` is 4, so the SYS_FAN3 "PCIe Fan" header does not exist in Linux. An upstream patch ([PATCH] hwmon: (dell-smm) Extend fan support to 5 fans) raises the limit to 5; on our T5810 it is installed as a DKMS module (`dell-smm-hwmon-5fan`). Verify before use:

   ```bash
   ls /sys/devices/platform/dell_smm_hwmon/hwmon/hwmon*/pwm5
   ```

   If `pwm5` is missing, you need the 5-fan module — without it the PCIe header cannot be controlled from Linux at all.

2. **Duty is quantized by the EC, writes are commands, and tach errors are not always "stopped".** Writes 0–255 collapse into coarse EC levels; measured on the V100 PCIe blower (tach truth, not write-back):

   | duty written | settled tach (BIOS mode = Low) |
   |---|---|
   | ≥ 192 | ~4,100 RPM |
   | 112–191 | ~505 RPM |
   | < 112 | ~130 RPM or below the tach's readable floor (reads fail with `-EINVAL`; fan may still turn) |

   Use `--pwm-min 160`. Do not trust `cat pwmN` read-back (it quantizes too); trust `fanN_input`.

3. **BIOS Fan Control mode must be "Low".** With BIOS mode "Auto" the EC reclaims the channel seconds after any write (observed sawtooth 1,300→~170 RPM on a 30 s cycle) and ignores sustained control; "Low" mode holds our commanded level indefinitely. There is no Linux-side way to disable the EC's auto policy on this platform (no `pwmN_enable` interface), so this is a **deployment prerequisite**, not a preference.

4. **Rapid repeated writes stall the fan — Dell mode is a 3-level thermostat.** Each SMM duty write is an EC *command* with a ~10–25 s kick/transition; writing the same value every second drove the blower to a stop. Worse, this card's idle/load temperatures (≈43/≈53 °C) straddle the EC's two usable levels, so an interpolated duty curve **limit-cycles** (verified: fan flapped every ~40 s under a 100 W burn). Dell mode therefore commands one of three levels via `dell_duty()`: temp ≥ `DELL_T_HOT` → full (255), temp ≤ `DELL_T_COOL` → low (160), in between → hold the current level. Writes happen only on level *change* (plus a 3600 s ceiling refresh and tach-deviation re-issue after a 25 s kick grace). Super I/O keeps its original linear-curve-every-cycle behavior.

5. **The EC keeps its own policy** (in Low mode, a fixed baseline; the earlier "sniff" excursions belong to Auto). The daemon reacts to GPU temperature, which the EC cannot see.

Channel map for the T5810 (per the 5-fan patch, verified by ear and tach):

| channel | header | physical |
|---|---|---|
| pwm1 | CPU_FAN | CPU heatsink |
| pwm2 | — | second-CPU phantom on single-socket boards (always 0) |
| pwm3 | SYS_FAN1 (top) | Right DIMM bank fan |
| pwm4 | SYS_FAN2 (middle) | Left DIMM bank fan |
| pwm5 | SYS_FAN3 (bottom) | **PCIe Fan** — where a modded PCIe V100 blower goes |

T5810 example (our AI rig — PCIe V100 at nvidia-smi index 0, bus 03:00.0, blower on SYS_FAN3):

```bash
sudo python3 v100_fan_control.py --debug \
    --hwmon-device dell_smm_hwmon \
    --gpu-map "0:5" \
    --gpu-pci 0=0000:03:00.0 \
    --pwm-min 160
```

And as a service (`/etc/systemd/system/v100-fan-control.service`):

```ini
[Unit]
Description=V100 PCIe blower fan control (Dell SMM pwm5)
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 /home/john/v100-fan-control/v100_fan_control.py --hwmon-device dell_smm_hwmon --gpu-map 0:5 --gpu-pci 0=0000:03:00.0 --pwm-min 160 --cpu-pwm 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### CPU fan channel (`--cpu-pwm 1`)

BIOS "Low" mode pins the CPU fan too (~1,030 RPM regardless of load) — a 30-minute all-core soak on the T5810 hit **95 °C with continuous throttling** at that pin. `--cpu-pwm 1` adds an independent two-level thermostat driven by coretemp: at/above `CPU_T_HOT` (default 75 °C) command HIGH, at/below `CPU_T_COOL` (68 °C) return to LOW, hold in between. Measured CPU-channel duty ladder on the T5810 is **binary**: ≤160 → ≈1,015 RPM, ≥192 → ≈4,300 RPM (no usable middle). Ladders are fan- and board-specific — measure before choosing duties on any other machine; the same write-on-change, kick-grace, and tach-deviation discipline applies.

## Command-Line Options

CLI flags override the corresponding `SETTINGS` constants, so one script copy serves multiple machines:

| Flag | Overrides | Example |
|---|---|---|
| `--hwmon-device NAME` | `HWMON_DEVICE` | `dell_smm_hwmon` |
| `--gpu-map GPU:PWM[,...]` | `GPU_TO_PWM` | `"0:5"` or `"0:2,1:1"` |
| `--gpu-pci GPU=PCI` | (new check) | `0=0000:03:00.0`, repeatable; startup fails on mismatch |
| `--pwm-min N` | `PWM_MIN` | `160` on Dell SMM / V100 blower |
| `--pwm-max N` | `PWM_MAX` | `255` |
| `--poll-interval S` | `POLL_INTERVAL` | `3` |
| `--cpu-pwm N` | enables CPU channel | `1` on the T5810 — two-level CPU thermostat via coretemp |
| `--debug` | — | verbose logging every cycle |

## Settings Reference

All settings are at the top of `v100_fan_control.py` under the `# SETTINGS` section:

| Setting | Default | Description |
|---|---|---|
| `HWMON_DEVICE` | `it87.2832` | Super I/O chip identifier. Change if your motherboard uses a different chip (e.g., `nct6775.2560`). |
| `TEMP_MIN` | 40 | °C below which fans run at `PWM_MIN` |
| `TEMP_MAX` | 75 | °C above which fans run at `PWM_MAX` |
| `PWM_MIN` | 30 | Minimum fan speed (0–255). Lower values may cause blower stall on 120mm fans. |
| `PWM_MAX` | 255 | Maximum fan speed (0–255) |
| `POLL_INTERVAL` | 3 | Seconds between temperature reads |
| `MAX_STEP_UP` | 85 | Max PWM increase per cycle (fast ramp for safety) |
| `MAX_STEP_DOWN` | 4 | Max PWM decrease per cycle (slow coast to prevent revving) |
| `LOG_FILE` | `/var/log/v100-fan.log` | Log file path |
| `LOG_MAX_BYTES` | 5 MB | Log rotation size |
| `LOG_BACKUP_COUNT` | 3 | Number of rotated log files to keep |
| `CPU_T_HOT` / `CPU_T_COOL` | 75 / 68 | With `--cpu-pwm`: °C thresholds for the two-level CPU thermostat (hold between) |
| `CPU_DUTY_LOW` / `CPU_DUTY_HIGH` | 160 / 224 | CPU channel duties (T5810-measured ladder: ≈1,015 / ≈4,300 RPM) |
| `DELL_T_HOT` | 50 | Dell mode: °C at/above which the blower is commanded full |
| `DELL_T_COOL` | 44 | Dell mode: °C at/below which the blower is commanded low; between = hold |
| `DELL_DUTY_LEVELS` | (96, 160, 255) | Duty written for EC levels 0/1/2 |
| `TACH_ARM_DUTY` | 96 | Above this commanded duty, tach failures count toward stall escalation |
| `TACH_ERRORS_TO_ESCALATE` | 3 | Consecutive tach failures (armed) before forcing `PWM_MAX` |
| `DELL_REENFORCE_S` | 3600 | Dell SMM: hard ceiling seconds for same-level re-writes (normally only level *changes* write) |
| `FAILSAFE_TEMP_ERRORS` | 3 | Consecutive unreadable-temp cycles before forcing `PWM_MAX` on all mapped fans |
| `RESTART_AFTER_TEMP_ERRORS` | 10 | Consecutive unreadable cycles before exiting nonzero for systemd restart |

## Troubleshooting

- **"Permission denied"** — Run with `sudo`. The script needs write access to `/sys/class/hwmon/hwmon*/pwm*` and `*_enable` files.
- **"Could not find hwmon path for 'it87.2832'"** — Your motherboard uses a different Super I/O chip. Change `HWMON_DEVICE` in the script to match your chip (check `sensors-detect` output).
- **"nvidia-smi timed out"** — GPU driver may be hung. The script auto-restarts after 10 consecutive failures.
- **Fans not responding** — The BIOS may have reverted the `*_enable` flag. The script re-enforces it each cycle, but you may also need to disable "Q-Fan Control" or "Hardware Monitor" in the BIOS.
- **Fans revving up and down** — Increase `MAX_STEP_DOWN` or increase `POLL_INTERVAL` to give fans more time to settle between commands.
- **Fewer PWM channels than GPUs** — The script will skip unassigned GPUs. Consider a PCIe fan controller or a motherboard with more headers.
- **Dell: `pwm5` missing** — stock kernel's `dell_smm` only creates 4 fan channels; install the 5-fan `dell-smm-hwmon` DKMS module (see Dell SMM mode above).
- **Dell: duty read-back doesn't match what you wrote** — normal; the EC quantizes. Watch `fanN_input` RPM instead.
- **Dell: fan stopped after writing low values** — duties ≤ 64 stall the blower on the PCIe channel; keep `--pwm-min 160`.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Author

[theminor](https://github.com/theminor)
