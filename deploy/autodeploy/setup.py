"""One-time setup from the owner's computer; private key never enters Git."""

import argparse
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile

REPOSITORY = 'petrovskaatatana086-ship-it/pomoshchnik-naznacheniya-vstrechi'
DIRECTORY = Path(__file__).resolve().parent
KEY = Path.home() / '.ssh/calendar_bot_github_actions'
HOST = '85.198.65.229'


def program(name):
    if os.name == 'nt' and name in ['ssh', 'ssh-keygen']:
        executable = Path(os.environ['SystemRoot']) / 'System32/OpenSSH' / (name + '.exe')
        if executable.exists():
            return str(executable)
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f'Install {name} before running setup')
    return executable


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def ensure_key():
    KEY.parent.mkdir(parents=True, exist_ok=True)
    if not KEY.exists():
        run([program('ssh-keygen'), '-t', 'ed25519', '-N', '', '-C', 'calendar-bot-github-actions', '-f', str(KEY)])
    if not KEY.with_suffix('.pub').exists():
        raise RuntimeError(f'Public key missing: {KEY}.pub; refusing to replace an existing key')
    run([program('ssh-keygen'), '-y', '-f', str(KEY)], stdout=subprocess.DEVNULL)


def install(server_user):
    # Only these five regular files may be unpacked by the root bootstrap.
    names = ['deploy-server.py', 'ssh-command.sh', 'compose.server.yml', 'install-server.sh']
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as archive:
        for name in names:
            content = (DIRECTORY / name).read_bytes().replace(b'\r\n', b'\n')
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        public = KEY.with_suffix('.pub').read_bytes().strip() + b'\n'
        entry = tarfile.TarInfo('deploy-key.pub')
        entry.size = len(public)
        archive.addfile(entry, io.BytesIO(public))
    bootstrap = '''import pathlib,sys,tarfile,tempfile,subprocess
stage=pathlib.Path(tempfile.mkdtemp(prefix='calendar-bot-setup-'))
allowed={'deploy-server.py','ssh-command.sh','compose.server.yml','install-server.sh','deploy-key.pub'}
try:
 with tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as archive:
  for entry in archive:
   if entry.name not in allowed or not entry.isfile() or entry.size>1048576: raise RuntimeError('Invalid setup archive')
   allowed.remove(entry.name)
   (stage/entry.name).write_bytes(archive.extractfile(entry).read())
 if allowed: raise RuntimeError('Incomplete setup archive')
 subprocess.run(['/bin/bash',str(stage/'install-server.sh'),str(stage/'deploy-key.pub')],check=True)
finally:
 for item in stage.iterdir(): item.unlink()
 stage.rmdir()
'''
    options = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', f'UserKnownHostsFile="{(DIRECTORY / "known_hosts").as_posix()}"',
               '-o', 'ConnectTimeout=15']
    run([program('ssh'), *options, f'{server_user}@{HOST}',
         'sudo -n python3 -c ' + shlex.quote(bootstrap)], input=payload.getvalue())
    # Prove the new key can authenticate, but cannot open a general-purpose shell.
    probe = subprocess.run([program('ssh'), *options, '-o', 'IdentitiesOnly=yes', '-i', str(KEY),
                            f'calendar-deploy@{HOST}', 'probe'], capture_output=True, text=True)
    if probe.returncode != 1 or 'Only deploy <40-character commit SHA>' not in probe.stderr:
        raise RuntimeError('The restricted deployment key probe failed; GitHub has not been enabled')
    print('Server installed; deployment key restriction verified.')


def configure_github():
    gh = program('gh')
    run([gh, 'auth', 'status'])
    run([gh, 'secret', 'set', 'CALENDAR_DEPLOY_SSH_KEY', '--repo', REPOSITORY], input=KEY.read_bytes())
    run([gh, 'variable', 'set', 'CALENDAR_AUTODEPLOY_ENABLED', '--body', 'true', '--repo', REPOSITORY])
    print('GitHub deployment secret saved and automatic deployments enabled.')
    print('Open Actions and run "Test and deploy calendar bot" for master to verify the first deployment.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-server', action='store_true')
    parser.add_argument('--configure-github', action='store_true')
    parser.add_argument('--server-user', default='tataynka_pe')
    args = parser.parse_args()
    if args.configure_github and not args.install_server:
        parser.error('--configure-github requires --install-server so the key is verified before enabling CI')
    ensure_key()
    print(f'Dedicated private key: {KEY} (do not commit or send it in chat)')
    if args.install_server:
        install(args.server_user)
    if args.configure_github:
        configure_github()
    if not args.install_server:
        print('Key generated only. Use --install-server to install the restricted receiver.')


if __name__ == '__main__':
    main()
