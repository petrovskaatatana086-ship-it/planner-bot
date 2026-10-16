#!/usr/bin/python3
"""Root-owned receiver. Its only input is a revision and a bounded Docker archive."""

import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import urllib.request
import uuid

MAX_ARCHIVE = 512 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
APP = Path('/opt/calendar-bot')
STATE = Path('/var/lib/calendar-bot-deploy')
COMPOSE = Path('/etc/calendar-bot/compose.yml')
PROTECTED = [
    Path('/etc/nginx/nginx.conf'),
    *[Path('/etc/nginx/sites-available') / name for name in [
        'default', 'tatyana-petrovskaya.ru', 'checking.tatyana-petrovskaya.ru',
        'currency.tatyana-petrovskaya.ru', 'planner.tatyana-petrovskaya.ru',
        'calendar.tatyana-petrovskaya.ru',
    ]],
    Path('/etc/systemd/system/planner.service'),
    Path('/etc/systemd/system/counterparty-check.service'),
]


def validate_revision(value):
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Expected a 40-character lowercase commit SHA')
    return value


def receive_archive(stream, destination):
    total = 0
    with destination.open('xb') as output:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE:
                raise ValueError('Image archive exceeds 512 MiB')
            output.write(chunk)
    if not total:
        raise ValueError('Empty image archive')


def validate_archive(path, revision):
    """Inspect without extracting. Reject extra image tags and unsafe outer paths."""
    total = 0
    manifest = None
    with tarfile.open(path, mode='r|gz') as archive:
        for entry in archive:
            name = PurePosixPath(entry.name)
            if name.is_absolute() or '..' in name.parts or not (entry.isfile() or entry.isdir()):
                raise ValueError('Unsafe image archive entry')
            total += entry.size
            if total > MAX_UNPACKED:
                raise ValueError('Unpacked image exceeds 1 GiB')
            if entry.name == 'manifest.json':
                if manifest is not None or entry.size > 1024 * 1024:
                    raise ValueError('Invalid Docker manifest')
                manifest = json.loads(archive.extractfile(entry).read())
            if entry.name == 'index.json':
                if entry.size > 1024 * 1024:
                    raise ValueError('Invalid OCI index')
                index = json.loads(archive.extractfile(entry).read())
                for item in index.get('manifests', []):
                    annotations = item.get('annotations', {})
                    named = annotations.get('io.containerd.image.name')
                    if named and named not in [f'calendar-bot:{revision}', f'docker.io/library/calendar-bot:{revision}']:
                        raise ValueError('Unexpected OCI image name')
                    ref = annotations.get('org.opencontainers.image.ref.name')
                    if ref and ref not in [revision, f'calendar-bot:{revision}', f'docker.io/library/calendar-bot:{revision}']:
                        raise ValueError('Unexpected OCI image tag')
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError('Archive must contain exactly one image')
    if manifest[0].get('RepoTags') != [f'calendar-bot:{revision}']:
        raise ValueError('Archive may tag only the requested calendar-bot revision')
    return total


def copy_database(source, destination):
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite integrity check failed')


def schema_revision(database):
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return tuple(row[0] for row in db.execute('SELECT version_num FROM alembic_version ORDER BY version_num'))


class Deployment:
    def __init__(self, revision):
        self.revision = validate_revision(revision)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.attempt = STATE / f'{stamp}-{revision[:12]}-{uuid.uuid4().hex[:6]}'
        self.attempt.mkdir(mode=0o700)
        self.archive = self.attempt / 'image.tar.gz'
        self.snapshot = self.attempt / 'before.sqlite3'
        self.database = APP / 'data/planner.db'
        self.image = f'calendar-bot:{revision}'
        self.previous = f'calendar-bot:rollback-{stamp}-{uuid.uuid4().hex[:6]}'
        self.migrator = f'calendar-bot-migrate-{uuid.uuid4().hex[:12]}'
        self.stopped = False
        self.snapshot_ready = False
        self.app_may_have_written = False
        self.schema_before = None
        self.schema_after = None

    def command(self, args, image=None, timeout=180, check=True):
        env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
               'CALENDAR_IMAGE': image or self.image}
        result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout)
        with (self.attempt / 'commands.log').open('a', encoding='utf-8') as log:
            log.write(json.dumps(args) + '\n' + result.stdout + result.stderr + '\n')
        if check and result.returncode:
            raise RuntimeError(f'Command failed ({result.returncode}); details: {self.attempt}/commands.log')
        return result

    def compose(self, *args, image=None, timeout=180):
        return self.command(['/usr/bin/docker', 'compose', '--project-name', 'calendar-bot',
                             '--project-directory', str(APP), '-f', str(COMPOSE), *args],
                            image=image, timeout=timeout)

    def health(self):
        for url in ['http://127.0.0.1:8012/health',
                    'https://calendar.tatyana-petrovskaya.ru/health']:
            with urllib.request.urlopen(url, timeout=15) as response:
                if response.status != 200 or json.load(response).get('status') != 'ok':
                    raise RuntimeError('Health check failed')

    def restore_before_migration(self):
        # This is used only before any candidate bot process could accept new bookings.
        target = self.database.with_name(f'planner.restore-{uuid.uuid4().hex}.db')
        owner = self.database.stat()
        copy_database(self.snapshot, target)
        os.chown(target, owner.st_uid, owner.st_gid)
        target.chmod(0o600)
        for suffix in ['-wal', '-shm', '-journal']:
            self.database.with_name(self.database.name + suffix).unlink(missing_ok=True)
        target.replace(self.database)

    def stop_migrator(self):
        result = self.command(['/usr/bin/docker', 'rm', '--force', self.migrator], check=False)
        if result.returncode and 'No such container' not in result.stderr:
            raise RuntimeError('Cannot confirm migration container is stopped; manual recovery required')

    def rollback(self):
        if not self.stopped:
            return 'Current bot was not stopped'
        self.stop_migrator()
        self.compose('stop', 'bot')
        if not self.app_may_have_written:
            if self.snapshot_ready:
                self.restore_before_migration()
        elif self.schema_before != self.schema_after:
            return ('Manual recovery required: migration changed the schema; candidate stopped, '
                    'database retained to preserve new bookings. Snapshot: ' + str(self.snapshot))
        # Keep current data if the candidate might have processed real updates.
        self.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                     '--wait', '--wait-timeout', '90', 'bot', image=self.previous)
        self.health()
        return 'Previous image restored; database preserved' if self.app_may_have_written else 'Previous image and pre-migration database restored'

    def clean_old_images(self):
        result = self.command(['/usr/bin/docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}',
                               '--filter', 'reference=calendar-bot:*'])
        for tag in result.stdout.splitlines():
            owned = re.fullmatch(r'calendar-bot:(?:[0-9a-f]{40}|rollback-[0-9TZ]+-[0-9a-f]{6})', tag)
            if owned and tag not in [self.image, self.previous]:
                self.command(['/usr/bin/docker', 'image', 'rm', tag], check=False)

    def clean_old_attempts(self):
        attempts = sorted(p for p in STATE.iterdir() if p.is_dir() and not p.is_symlink()
                          and re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}-[0-9a-f]{6}', p.name))
        for old in attempts[:-14]:
            if old != self.attempt and old.resolve().parent == STATE.resolve():
                shutil.rmtree(old)

    def run(self, stream):
        try:
            if shutil.disk_usage(STATE).free < 1536 * 1024 * 1024:
                raise RuntimeError('At least 1.5 GiB of free disk space is required')
            receive_archive(stream, self.archive)
            unpacked = validate_archive(self.archive, self.revision)
            # Leave room for the image store, an unpacked copy, snapshots and the other sites.
            required = 1024 * 1024 * 1024 + 2 * unpacked + 2 * self.database.stat().st_size
            if shutil.disk_usage(STATE).free < required:
                raise RuntimeError('Insufficient free disk space for this image and a 1 GiB reserve')
            self.command(['/usr/bin/docker', 'load', '--input', str(self.archive)], timeout=300)
            info = json.loads(self.command(['/usr/bin/docker', 'image', 'inspect', self.image]).stdout)[0]
            if info.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != self.revision:
                raise RuntimeError('Image revision label does not match the requested commit')
            if info.get('Os') != 'linux' or info.get('Architecture') != 'amd64':
                raise RuntimeError('Expected a Linux amd64 image')
            protected = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
            self.compose('config', '--quiet')
            container = self.compose('ps', '--quiet', 'bot').stdout.strip()
            if not re.fullmatch(r'[0-9a-f]{12,64}', container):
                raise RuntimeError('Exactly one existing calendar-bot container is required')
            old_id = self.command(['/usr/bin/docker', 'inspect', '--format', '{{.Image}}', container]).stdout.strip()
            self.command(['/usr/bin/docker', 'image', 'tag', old_id, self.previous])
            self.schema_before = schema_revision(self.database)
            self.stopped = True
            self.compose('stop', 'bot')
            copy_database(self.database, self.snapshot)
            self.snapshot_ready = True
            self.compose('run', '--rm', '--no-deps', '--pull', 'never', '--name', self.migrator,
                         'bot', 'python', '-m', 'alembic', 'upgrade', 'head')
            self.schema_after = schema_revision(self.database)
            self.app_may_have_written = True
            self.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                         '--wait', '--wait-timeout', '90', 'bot')
            self.health()
            if any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest for p, digest in protected.items()):
                raise RuntimeError('Protected site/service configuration unexpectedly changed')
            (APP / 'REVISION').write_text(self.revision + '\n', encoding='ascii')
            report = {'revision': self.revision, 'image': self.image, 'previous_image': self.previous,
                      'snapshot': str(self.snapshot), 'status': 'healthy'}
            (self.attempt / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            temporary = STATE / 'current.pending'
            temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
            temporary.replace(STATE / 'current.json')
            self.stopped = False  # A completed release must not roll back for cleanup errors.
            try:
                self.clean_old_images()
                self.clean_old_attempts()
            except Exception:
                print('Deployment succeeded; image cleanup needs an administrator check.', flush=True)
            print(f'Healthy deployment: {self.revision}', flush=True)
        except BaseException as error:
            try:
                recovery = self.rollback()
            except BaseException as failure:
                recovery = f'Automatic recovery failed: {failure}; inspect {self.attempt}'
            (self.attempt / 'failure.txt').write_text(f'{error}\n{recovery}\n', encoding='utf-8')
            print(f'Deployment failed: {error}\n{recovery}', file=sys.stderr, flush=True)
            raise
        finally:
            self.archive.unlink(missing_ok=True)


def main():
    import fcntl  # Linux server only; keep helpers importable for unit tests on Windows.
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit('Run as root with exactly one commit SHA')
    revision = validate_revision(sys.argv[1])
    os.umask(0o077)
    def interrupted(*_):
        raise RuntimeError('Deployment interrupted')
    for signum in [signal.SIGTERM, signal.SIGHUP]:
        signal.signal(signum, interrupted)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'deploy.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another calendar-bot deployment is active')
        Deployment(revision).run(sys.stdin.buffer)


if __name__ == '__main__':
    main()
