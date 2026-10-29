#!/usr/bin/env python3
"""Render Linux systemd units without secrets. Does not start or restart services."""
import argparse
import grp
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile


def unit_quote(value):
    value = str(value)
    if any(c in value for c in '\n\r\0'):
        raise ValueError('invalid unit argument')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render(template, project, data, python, user, group, script):
    # Scalar directives do not share ExecStart's argument unquoting rules.
    for identity in (user, group):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*[$]?', identity):
            raise ValueError('unsupported service identity')
    workdir = str(project)
    if not Path(workdir).is_absolute() or any(c in workdir for c in '\n\r\0'):
        raise ValueError('invalid working directory')
    scalars = {'USER': user, 'GROUP': group, 'WORKDIR': workdir.replace('%', '%%')}
    for name, value in scalars.items():
        template = template.replace('@' + name + '@', value)
    values = {'DATA': data,
              'PYTHON': python, 'PROJECT_SCRIPT': Path(project) / script}
    for name, value in values.items():
        template = template.replace('@' + name + '@', unit_quote(value))
    return template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=str(Path(__file__).parent / 'data'))
    a = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise SystemExit('Run as the intended non-root service user on Linux; sudo is used only for unit installation.')
    project, data = Path(__file__).resolve().parent, Path(a.data_dir).expanduser().resolve()
    if not (data / 'config' / 'credentials.json').is_file():
        raise SystemExit('Run configure.py first.')
    user, group = pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name
    units = []
    for name, script in [('muse-qq-adapter', 'adapter.py'), ('muse-qq-bridge', 'run_bridge.py')]:
        body = render((project / 'deploy' / (name + '.service.in')).read_text(),
                      project, data, sys.executable, user, group, script)
        destination = Path('/etc/systemd/system') / (name + '.service')
        if destination.exists() and destination.read_text() != body:
            raise SystemExit('Existing unit differs; review it manually. No service was restarted.')
        units.append((destination, body))
    with tempfile.TemporaryDirectory(prefix='muse-qq-units-') as tmp:
        for destination, body in units:
            source = Path(tmp) / destination.name; source.write_text(body)
            subprocess.run(['sudo', 'install', '-m', '644', str(source), str(destination)], check=True)
        subprocess.run(['sudo', 'systemd-analyze', 'verify', *[str(p) for p, _ in units]], check=True)
    subprocess.run(['sudo', 'systemctl', 'daemon-reload'], check=True)
    print('Units installed and checked. They were not started.')
    print('After stopping foreground copies: sudo systemctl enable --now muse-qq-adapter muse-qq-bridge')


if __name__ == '__main__':
    main()
