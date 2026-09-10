# GPU Fan Control

Automatic PWM fan curve control for NVIDIA GPUs on Linux.

Reads GPU core + memory temperatures via `nvidia-smi` and writes PWM values to motherboard fan headers via the Super I/O hardware monitor driver (it87, nct6775, or compatible). Each GPU's blower fan is independently controlled.

## Features

- **Auto-detects GPUs** — queries `nvidia-smi` to find all GPUs on the system (1, 2, 3, 4, 5+)
- **Auto-discovers hwmon path** — finds the Super I/O hwmon directory via glob, survives reboot renumbering
- **User-defined fan mapping** — explicit GPU-to-PWM assignment via `GPU_TO_PWM` dict (no guessing)
- **Linear temperature curve** — configurable min/max temperature and PWM thresholds
- **Memory temp monitoring** — uses the hotter of core or memory temp (HBM memory often runs hotter than the core)
- **Acoustic smoothing** — limits PWM step size per cycle to prevent audible fan "revving" (ramps up fast for safety, coasts down slowly)
- **Graceful shutdown** — restores BIOS fan control on SIGINT/SIGTERM
- **BIOS override enforcement** — re-enforces manual mode each cycle to counter BIOS SMM reversion
- **Error recovery** — auto-restarts after 10 consecutive nvidia-smi failures to clear NVML driver state
- **Rotating log files** — 5 MB max, 3 backups

## Hardware Requirements

- NVIDIA GPU(s) with nvidia-smi available
- Motherboard with Super I/O chip supporting PWM fan headers (it87, it8620e, nct6775, or compatible)
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

## Troubleshooting

- **"Permission denied"** — Run with `sudo`. The script needs write access to `/sys/class/hwmon/hwmon*/pwm*` and `*_enable` files.
- **"Could not find hwmon path for 'it87.2832'"** — Your motherboard uses a different Super I/O chip. Change `HWMON_DEVICE` in the script to match your chip (check `sensors-detect` output).
- **"nvidia-smi timed out"** — GPU driver may be hung. The script auto-restarts after 10 consecutive failures.
- **Fans not responding** — The BIOS may have reverted the `*_enable` flag. The script re-enforces it each cycle, but you may also need to disable "Q-Fan Control" or "Hardware Monitor" in the BIOS.
- **Fans revving up and down** — Increase `MAX_STEP_DOWN` or increase `POLL_INTERVAL` to give fans more time to settle between commands.
- **Fewer PWM channels than GPUs** — The script will skip unassigned GPUs. Consider a PCIe fan controller or a motherboard with more headers.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Author

[theminor](https://github.com/theminor)
