import importlib.util
from contextlib import closing
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import unittest
from unittest.mock import MagicMock, patch
import uuid


SPEC = importlib.util.spec_from_file_location(
    'calendar_deploy_server', Path(__file__).resolve().parents[1] / 'deploy/autodeploy/deploy-server.py',
)
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)
SHA = 'a' * 40


class AutoDeployTest(unittest.TestCase):
    def setUp(self):
        # Explicit workspace directory avoids platform-specific temporary directory ACLs.
        self.root = Path(f'.test_autodeploy_{uuid.uuid4().hex}').resolve()
        self.root.mkdir()
        self.addCleanup(self.clean_workspace)

    def clean_workspace(self):
        assert self.root.parent == Path.cwd().resolve()
        assert self.root.name.startswith('.test_autodeploy_')
        shutil.rmtree(self.root)

    def make_archive(self, tags=None, name='manifest.json'):
        path = self.root / f'{uuid.uuid4().hex}.tar.gz'
        data = json.dumps([{'RepoTags': tags or [f'calendar-bot:{SHA}'], 'Config': 'config.json', 'Layers': []}]).encode()
        with tarfile.open(path, 'w:gz') as archive:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        return path

    def test_only_commit_sha_is_accepted(self):
        self.assertEqual(DEPLOY.validate_revision(SHA), SHA)
        for value in ['master', SHA + '; id', '../data', SHA.upper(), SHA + '\n']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                DEPLOY.validate_revision(value)

    def test_archive_cannot_retag_another_project(self):
        DEPLOY.validate_archive(self.make_archive(), SHA)
        for tags in [['other-project:latest'], [f'calendar-bot:{SHA}', 'other-project:latest']]:
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                DEPLOY.validate_archive(self.make_archive(tags), SHA)

    def test_archive_rejects_parent_traversal(self):
        with self.assertRaises(ValueError):
            DEPLOY.validate_archive(self.make_archive(name='../manifest.json'), SHA)

    def test_archive_size_limit_keeps_bot_untouched(self):
        with patch.object(DEPLOY, 'MAX_ARCHIVE', 4), self.assertRaises(ValueError):
            DEPLOY.receive_archive(io.BytesIO(b'oversized'), self.root / 'upload.gz')

    def test_backup_restores_rows_and_schema_revision(self):
        source = self.root / 'source.db'
        snapshot = self.root / 'snapshot.db'
        with closing(sqlite3.connect(source)) as db:
            db.execute('CREATE TABLE alembic_version(version_num TEXT)')
            db.execute('INSERT INTO alembic_version VALUES (?)', ('0005',))
            db.execute('CREATE TABLE bookings(title TEXT)')
            db.execute('INSERT INTO bookings VALUES (?)', ('Keep this booking',))
            db.commit()
        DEPLOY.copy_database(source, snapshot)
        self.assertEqual(DEPLOY.schema_revision(snapshot), ('0005',))
        with closing(sqlite3.connect(snapshot)) as restored:
            self.assertEqual(restored.execute('SELECT title FROM bookings').fetchone()[0], 'Keep this booking')

    def receiver(self):
        receiver = object.__new__(DEPLOY.Deployment)
        receiver.stopped = True
        receiver.snapshot_ready = True
        receiver.app_may_have_written = False
        receiver.schema_before = ('0005',)
        receiver.schema_after = ('0005',)
        receiver.snapshot = self.root / 'before.sqlite3'
        receiver.previous = 'calendar-bot:rollback-test'
        receiver.compose = MagicMock()
        receiver.stop_migrator = MagicMock()
        receiver.restore_before_migration = MagicMock()
        receiver.health = MagicMock()
        return receiver

    def test_migration_failure_restores_snapshot_before_restart(self):
        receiver = self.receiver()
        message = receiver.rollback()
        receiver.restore_before_migration.assert_called_once()
        self.assertIn('pre-migration database restored', message)
        self.assertEqual(receiver.compose.call_args.kwargs['image'], receiver.previous)

    def test_incomplete_backup_is_not_restored(self):
        receiver = self.receiver()
        receiver.snapshot_ready = False
        receiver.rollback()
        receiver.restore_before_migration.assert_not_called()
        receiver.health.assert_called_once()

    def test_app_failure_preserves_new_bookings_when_reverting_image(self):
        receiver = self.receiver()
        receiver.app_may_have_written = True
        message = receiver.rollback()
        receiver.restore_before_migration.assert_not_called()
        self.assertIn('database preserved', message)
        self.assertEqual(receiver.compose.call_args.kwargs['image'], receiver.previous)

    def test_schema_change_does_not_restore_over_possible_new_bookings(self):
        receiver = self.receiver()
        receiver.app_may_have_written = True
        receiver.schema_after = ('0006',)
        message = receiver.rollback()
        receiver.restore_before_migration.assert_not_called()
        receiver.health.assert_not_called()
        self.assertIn('Manual recovery required', message)
        self.assertEqual(receiver.compose.call_args.args, ('stop', 'bot'))

    def test_failure_before_stopping_does_not_restart_bot(self):
        receiver = self.receiver()
        receiver.stopped = False
        receiver.rollback()
        receiver.compose.assert_not_called()

    def test_image_cleanup_is_scoped_to_calendar_bot_tags(self):
        receiver = self.receiver()
        receiver.image = f'calendar-bot:{SHA}'
        receiver.command = MagicMock(return_value=MagicMock(stdout='\n'.join([
            receiver.image, receiver.previous, 'calendar-bot:production',
            'other-project:latest', 'calendar-bot:' + 'b' * 40,
        ])))
        receiver.clean_old_images()
        self.assertEqual(receiver.command.call_count, 2)
        self.assertEqual(receiver.command.call_args.args[0], ['/usr/bin/docker', 'image', 'rm', 'calendar-bot:' + 'b' * 40])

    def test_snapshot_rotation_cannot_remove_another_project_directory(self):
        attempts = []
        for index in range(16):
            path = self.root / f'20261017T1200{index:02d}Z-aaaaaaaaaaaa-abcdef'
            path.mkdir()
            (path / 'before.sqlite3').write_bytes(b'snapshot')
            attempts.append(path)
        unrelated = self.root / 'planner'
        unrelated.mkdir()
        (unrelated / 'keep.txt').write_text('keep', encoding='utf-8')
        receiver = self.receiver()
        receiver.attempt = attempts[-1]
        with patch.object(DEPLOY, 'STATE', self.root):
            receiver.clean_old_attempts()
        self.assertFalse(attempts[0].exists())
        self.assertFalse(attempts[1].exists())
        self.assertTrue(all(p.exists() for p in attempts[2:]))
        self.assertTrue((unrelated / 'keep.txt').exists())
