"""Exercise the launcher with temporary executables; never start real services."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DevStartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='bboard-dev-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copyfile(ROOT / 'dev.sh', self.root / 'dev.sh')
        (self.root / 'frontend/node_modules').mkdir(parents=True)
        binaries = self.root / 'bin'
        binaries.mkdir()
        (binaries / 'dirname').symlink_to('/usr/bin/dirname')
        self.state = self.root / 'session'
        self.log = self.root / 'commands.jsonl'
        self.env = {
            'PATH': str(binaries),
            'MOCK_SESSION': str(self.state),
            'MOCK_LOG': str(self.log),
        }
        self.executable(binaries / 'lsof', 'raise SystemExit(1)\n')
        self.executable(binaries / 'tmux', '''
import json
import os
from pathlib import Path
import sys
state = Path(os.environ['MOCK_SESSION'])
with open(os.environ['MOCK_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
command = sys.argv[1]
if command == 'has-session':
    raise SystemExit(0 if state.exists() else 1)
elif command == 'new-session':
    if state.exists():
        raise SystemExit(1)
    state.touch()
elif command == 'kill-session':
    state.unlink()
elif command not in ('send-keys', 'new-window'):
    raise SystemExit('Unexpected tmux command')
''')
        self.executable(self.root / 'backend/.venv/bin/python', '''
import json
import os
import sys
with open(os.environ['MOCK_LOG'], 'a') as log:
    log.write(json.dumps(['bootstrap', *sys.argv[1:]]) + '\\n')
raise SystemExit(int(os.environ.get('MOCK_BOOTSTRAP_EXIT', '0')))
''')

    def executable(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!{sys.executable}\n' + content)
        path.chmod(0o755)

    def launch(self, **env):
        return subprocess.run(['/bin/bash', str(self.root / 'dev.sh')],
                              env=self.env | env, cwd=self.root,
                              capture_output=True, text=True, timeout=5)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_failed_bootstrap_cleans_session_and_next_launch_starts_apps(self):
        failed = self.launch(MOCK_BOOTSTRAP_EXIT='23')
        self.assertEqual(failed.returncode, 23, failed.stderr)
        self.assertFalse(self.state.exists(), 'Failed startup must not block the next launch')
        self.assertEqual([command[0] for command in self.commands()],
                         ['has-session', 'new-session', 'bootstrap', 'kill-session'])

        self.log.write_text('')
        succeeded = self.launch()
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertTrue(self.state.exists())
        commands = self.commands()
        self.assertEqual([command[0] for command in commands],
                         ['has-session', 'new-session', 'bootstrap', 'send-keys', 'new-window', 'send-keys'])
        self.assertIn('bboard-dev:backend', commands[3])
        self.assertIn('bboard-dev:frontend', commands[5])

    def test_preexisting_session_is_preserved_without_bootstrap(self):
        self.state.write_text('existing session')
        result = self.launch(MOCK_BOOTSTRAP_EXIT='23')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text(), 'existing session')
        self.assertEqual(self.commands(), [['has-session', '-t', 'bboard-dev']])
        self.assertIn('already exists', result.stdout)
