from schier_mount import TelescopeMount
import yaml

with open('telescope_config.yaml', 'r') as f:
    data = yaml.safe_load(f)


rotse = TelescopeMount(calibration_data=data)


print(rotse.get_current_position())