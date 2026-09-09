# GPU Fan Control

Automatic PWM fan curve control for NVIDIA GPUs on Linux.

Reads GPU core + memory temperatures via `nvidia-smi` and writes PWM values to motherboard fan headers via the Super I/O hardware monitor driver (it87, nct6775, or compatible). Each GPU's blower fan is independently controlled.

## Features

- **Auto-detects GPUs** — queries `nvidia-smi` to find all GPUs on the system (1, 2, 3, 4, 5+)
- **Auto-maps PWM channels** — discovers available PWM channels on the motherboard and assigns them sequentially to GPUs (GPU 0 → PWM 1, GPU 1 → PWM 2, etc.)
- **Linear temperature curve** — configurable min/max temperature and PWM thresholds
- **Memory temp monitoring** — uses the hotter of core or memory temp (HBM memory often runs hotter than the core)
- **Acoustic smoothing** — limits PWM step size per cycle to prevent audible fan "revving" (ramps up fast for safety, coasts down slowly)
- **Graceful shutdown** — restores BIOS fan control on SIGINT/SIGTERM
- **Dynamic hwmon discovery** — auto-finds the Super I/O hwmon path via glob, survives reboot renumbering
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

2. **Edit settings** in `/opt/v100-fan-control/v100_fan_control.py` if needed (default values work for most setups):

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

3. **Verify fan-to-GPU mapping** (critical — wrong mapping = wrong fan on the hot GPU):
   - Run the script with all fans at 255
   - Unplug one blower cable at a time
   - The fan that stops tells you which GPU index that PWM header controls
   - If needed, adjust your motherboard's PWM channel assignments

4. **Test run** (dry run with debug logging):

```bash
sudo python3 /opt/v100-fan-control/v100_fan_control.py --debug
```

Check the console output and `/var/log/v100-fan.log` to confirm temps and PWM values look reasonable.

5. **When happy, set up as a systemd service** (see below).

## systemd Service

Copy the service file into place:

```bash
sudo cp /opt/v100-fan-control/v100-fan-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable v100-fan-control.service
sudo systemctl start v100-fan-control.service
```

Check status:

```bash
sudo systemctl status v100-fan-control.service
```

View logs:

```bash
sudo journalctl -u v100-fan-control.service -f
```

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

## How Auto-Mapping Works

The script automatically discovers:

1. **All GPUs** — via `nvidia-smi --query-gpu=index`
2. **All PWM channels** — via glob on `/sys/devices/platform/<chip>/hwmon/hwmon*/pwm*`
3. **Assigns GPU N → PWM N+1** sequentially

For example, with 3 GPUs and 4 PWM channels:

| GPU | PWM Channel |
|---|---|
| 0 | pwm1 |
| 1 | pwm2 |
| 2 | pwm3 |

If there are more GPUs than PWM channels, the excess GPUs are skipped with a warning.

**Verify this mapping** by running the script, setting all fans to 255, and unplugging cables one at a time to confirm each fan corresponds to the correct GPU.

## Troubleshooting

- **"Permission denied"** — Run with `sudo`. The script needs write access to `/sys/class/hwmon/hwmon*/pwm*` and `*_enable` files.
- **"Could not find hwmon path for 'it87.2832'"** — Your motherboard uses a different Super I/O chip. Change `HWMON_DEVICE` in the script to match your chip (check `sensors-detect` output).
- **"nvidia-smi timed out"** — GPU driver may be hung. The script auto-restarts after 10 consecutive failures.
- **Fans not responding** — The BIOS may have reverted the `*_enable` flag. The script re-enforces it each cycle, but you may also need to disable "Q-Fan Control" or "Hardware Monitor" in the BIOS.
- **Fans revving up and down** — Increase `MAX_STEP_DOWN` or increase `POLL_INTERVAL` to give fans more time to settle between commands.
- **Fewer PWM channels than GPUs** — The script will skip excess GPUs. Consider a PCIe fan controller or a motherboard with more headers.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Author

[theminor](https://github.com/theminor)
