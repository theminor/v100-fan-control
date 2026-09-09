# V100 GPU Fan Control

Automated PWM fan curve control for NVIDIA Tesla V100 GPUs on Linux.

Reads GPU core + memory temperatures via `nvidia-smi` and writes PWM values to motherboard fan headers via the `it87` hardware monitor driver. Each V100 blower fan is independently controlled.

## Features

- **Linear temperature curve** — configurable min/max temperature and PWM thresholds
- **Memory temp monitoring** — uses the hotter of core or memory temp (HBM memory on V100s often runs 3–15°C hotter than the core)
- **Acoustic smoothing** — limits PWM step size per cycle to prevent audible fan "revving" (ramps up fast for safety, coasts down slowly)
- **Graceful shutdown** — restores BIOS fan control on SIGINT/SIGTERM
- **Dynamic hwmon discovery** — auto-finds the it87 hwmon path via glob, survives reboot renumbering
- **BIOS override enforcement** — re-enforces manual mode each cycle to counter BIOS SMM reversion
- **Error recovery** — auto-restarts after 10 consecutive nvidia-smi failures to clear NVML driver state
- **Rotating log files** — 5 MB max, 3 backups

## Hardware

- **Motherboard:** Gigabyte Z690 AORUS PRO (DDR5) — it87 Super I/O chip
- **GPUs:** Dual NVIDIA Tesla V100 (one in x16 slot, one in x4 slot)
- **Driver:** `it87` kernel module (from [linux/hwmon/it87](https://github.com/torvalds/linux/blob/master/drivers/hwmon/it87.c))
- **OS:** Ubuntu 24.04

## Prerequisites

```bash
# Install hardware monitoring tools
sudo apt install lm-sensors python3

# Load the it87 driver
sudo modprobe it87

# Verify the hwmon path exists (adjust hwmonN as needed)
ls /sys/devices/platform/it87.2832/hwmon/hwmon*/pwm*
```

## Quick Start

1. **Clone and edit settings** (scroll to the `SETTINGS` section in the script):

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

2. **Verify fan-to-GPU mapping** (critical — wrong mapping = wrong fan on the hot GPU):
   - Run the script with all fans at 255
   - Unplug one blower cable at a time
   - The fan that stops tells you which GPU index that PWM header controls
   - Update `FAN_MAP` in the script if needed

3. **Test run** (dry run with debug logging):

```bash
sudo python3 v100_fan_control.py --debug
```

Check the console output and `/var/log/v100-fan.log` to confirm temps and PWM values look reasonable.

4. **When happy, set up as a systemd service** (see below).

## systemd Service

Create `/etc/systemd/system/v100-fan-control.service`:

```ini
[Unit]
Description=V100 GPU Fan Control
After=network-online.target nvidia-drivers-start.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/v100-fan-control/v100_fan_control.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=v100-fan

# Security hardening
NoNewPrivileges=false
ProtectSystem=false
ProtectHome=yes

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable v100-fan-control.service
sudo systemctl start v100-fan-control.service
sudo systemctl status v100-fan-control.service
```

View logs: `journalctl -u v100-fan-control.service -f`

## Settings Reference

All settings are at the top of `v100_fan_control.py` under the `# SETTINGS` section:

| Setting | Default | Description |
|---|---|---|
| `TEMP_MIN` | 40 | °C below which fans run at `PWM_MIN` |
| `TEMP_MAX` | 75 | °C above which fans run at `PWM_MAX` |
| `PWM_MIN` | 30 | Minimum fan speed (0–255). Lower values may cause 120mm blower stall. |
| `PWM_MAX` | 255 | Maximum fan speed (0–255) |
| `POLL_INTERVAL` | 3 | Seconds between temperature reads |
| `MAX_STEP_UP` | 85 | Max PWM increase per cycle (fast ramp for safety) |
| `MAX_STEP_DOWN` | 4 | Max PWM decrease per cycle (slow coast to prevent revving) |
| `LOG_FILE` | `/var/log/v100-fan.log` | Log file path |
| `LOG_MAX_BYTES` | 5 MB | Log rotation size |
| `LOG_BACKUP_COUNT` | 3 | Number of rotated log files to keep |

## FAN_MAP Verification

The `FAN_MAP` dictionary maps NVIDIA GPU indices (from `nvidia-smi`) to motherboard PWM files. **This must match your physical wiring.**

Example (default):
```python
FAN_MAP = {
    0: os.path.join(BASE_PATH, "pwm2"),  # GPU 0 (x16 slot, top V100)
    1: os.path.join(BASE_PATH, "pwm1"),  # GPU 1 (x4 slot, middle V100)
}
```

To verify:
1. Run `sudo python3 v100_fan_control.py --debug`
2. All fans will spin up to max (BIOS will be overridden)
3. Unplug one V100 blower cable — the fan that stops is the one that PWM header controls
4. Unplug the other — the remaining one is the other GPU
5. Swap the `FAN_MAP` entries if the mapping is reversed

## Troubleshooting

- **"Permission denied"** — Run with `sudo`. The script needs write access to `/sys/class/hwmon/hwmon*/pwm*` and `*_enable` files.
- **"Could not find the it87.2832 hardware monitor path"** — The it87 module isn't loaded: `sudo modprobe it87`. Check `dmesg | grep it87` for probe failures.
- **"nvidia-smi timed out"** — GPU driver may be hung. The script auto-restarts after 10 consecutive failures. You can also manually: `sudo systemctl restart nvidia-drivers-start.service`.
- **Fans not responding** — The BIOS may have reverted the `*_enable` flag. The script re-enforces it each cycle, but you may also need to disable "Q-Fan Control" in the BIOS.
- **Fans revving up and down** — Increase `MAX_STEP_DOWN` or increase `POLL_INTERVAL` to give fans more time to settle between commands.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Author

[theminor](https://github.com/theminor)
