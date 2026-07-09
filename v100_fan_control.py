import subprocess
import time
import os

# --- CONFIGURATION ---
# Map NVIDIA GPU Index to the corresponding PWM file
FAN_MAP = {
    0: "/sys/class/hwmon/hwmon5/pwm2", # Top V100 (x16 slot)
    1: "/sys/class/hwmon/hwmon5/pwm1"  # Middle V100 (x4 slot)
}

TEMP_MIN = 40     # Temp at which fans start ramping
TEMP_MAX = 75     # Temp at which fans hit 100%
PWM_MIN = 50      # Minimum fan speed (0-255)
PWM_MAX = 255     # 100% speed
POLL_INTERVAL = 3 # Seconds between checks
# ---------------------

def enable_manual_mode():
    """Forces the motherboard headers into manual PWM control."""
    for pwm_file in FAN_MAP.values():
        enable_file = f"{pwm_file}_enable"
        try:
            with open(enable_file, 'w') as f:
                f.write('1')
            print(f"Enabled manual control for {enable_file}")
        except PermissionError:
            print("Permission denied: Script must be run with sudo.")
            exit(1)
        except Exception as e:
            print(f"Error enabling manual mode for {enable_file}: {e}")

def get_gpu_temps():
    """Reads temps from NVIDIA GPUs and returns a dict of {gpu_index: temperature}."""
    temps = {}
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,temperature.gpu", "--format=csv,noheader"], 
            encoding='utf-8'
        )
        for line in output.strip().split('\n'):
            if not line: continue
            idx_str, temp_str = line.split(',')
            temps[int(idx_str.strip())] = int(temp_str.strip())
    except Exception as e:
        print(f"Error reading GPU temps: {e}")
    return temps

def calculate_pwm(temp):
    """Maps the temperature to a PWM value based on the curve."""
    if temp <= TEMP_MIN: return PWM_MIN
    if temp >= TEMP_MAX: return PWM_MAX

    temp_range = TEMP_MAX - TEMP_MIN
    pwm_range = PWM_MAX - PWM_MIN
    temp_percent = (temp - TEMP_MIN) / temp_range

    return int(PWM_MIN + (temp_percent * pwm_range))

def set_fan_speed(pwm_file, pwm_value):
    try:
        with open(pwm_file, 'w') as f:
            f.write(str(pwm_value))
    except Exception as e:
        print(f"Error writing to {pwm_file}: {e}")

if __name__ == "__main__":
    print("Initializing V100 fan controls...")
    enable_manual_mode()

    while True:
        gpu_temps = get_gpu_temps()

        for gpu_idx, temp in gpu_temps.items():
            if gpu_idx in FAN_MAP:
                target_pwm = calculate_pwm(temp)
                set_fan_speed(FAN_MAP[gpu_idx], target_pwm)

        time.sleep(POLL_INTERVAL)
