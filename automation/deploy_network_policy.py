from pathlib import Path
import json, shutil, subprocess, os
root = Path('/home/bench-user/bench-eval')
automation = (root / 'current').resolve() / 'automation'
staged = Path('/tmp/network_doctor.py')
subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', '/tmp', '-p', 'test_network_doctor.py'], check=True)
backup = root / 'maintenance' / 'network-policy-20261009'
backup.mkdir(parents=True, exist_ok=True)
for path in [automation / 'network_doctor.py', root / 'config/device.json']:
    target = backup / path.name
    if not target.exists():
        shutil.copy2(path, target)
config_path = root / 'config/device.json'
config = json.loads(config_path.read_text())
config['management']['enforce_controller_subnets'] = False
config['management']['route_policy'] = 'ssh_authenticated_controller_any_network'
config['management']['note'] = 'Controller subnet admission disabled by owner; VPN route differences are advisory.'
temp_config = config_path.with_suffix('.json.network-policy.tmp')
temp_config.write_text(json.dumps(config, indent=2) + '\n')
os.replace(str(temp_config), str(config_path))
target = automation / 'network_doctor.py'
staged_target = target.with_suffix('.py.network-policy.tmp')
shutil.copy2(staged, staged_target)
os.replace(str(staged_target), str(target))
print('DEPLOYED', target)
print('BACKUP', backup)
subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', str(automation), '-p', 'test_bench_eval.py'], check=True)
subprocess.run([str(root / 'bin/bench-eval'), 'doctor'], check=False)
