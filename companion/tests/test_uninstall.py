from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control

UFW_STATUS = """Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 32187/tcp on eth0          ALLOW IN    192.168.1.0/24             # Printer Pal
[ 3] 8080/tcp                   ALLOW IN    Anywhere                   # OctoPrint
[ 4] 1990/udp on eth0           ALLOW IN    192.168.1.0/24             # Printer Pal discovery
[10] 32187/tcp on tailscale0    ALLOW IN    100.64.0.0/10              # Printer Pal
"""


class UninstallTests(unittest.TestCase):
    def test_removes_only_what_the_installer_added(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in control.INSTALLED_PATHS:
                path = root / item.lstrip('/')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('installed')
            (root / 'etc/print-monitor').unlink()
            (root / 'etc/print-monitor/tls').mkdir(parents=True)
            (root / 'etc/print-monitor/tls/key.pem').write_text('secret')
            kept = [root / 'etc/avahi/services/octoprint.service', root / 'opt/other/app.py', root / 'usr/local/bin/other']
            for path in kept:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('keep')

            def runner(command, **_):
                stdout = UFW_STATUS if command[:3] == ['ufw', 'status', 'numbered'] else ''
                return subprocess.CompletedProcess(command, 0, stdout=stdout)
            mock = Mock(side_effect=runner)
            with patch('control.shutil.which', return_value='/usr/sbin/ufw'), patch('builtins.print'):
                self.assertEqual(control.uninstall(root, mock), 0)

            for item in control.INSTALLED_PATHS:
                self.assertFalse((root / item.lstrip('/')).exists(), item)
            for path in kept:
                self.assertTrue(path.exists(), path)
            commands = [call.args[0] for call in mock.call_args_list]
            deletes = [command[-1] for command in commands if command[:3] == ['ufw', '--force', 'delete']]
            self.assertEqual(deletes, ['10', '4', '2'])
            self.assertIn(['nft', 'delete', 'table', 'inet', 'printer_pal'], commands)
            self.assertIn(['userdel', 'print-monitor'], commands)
            self.assertEqual(commands[0][:3], ['systemctl', 'disable', '--now'])


if __name__ == '__main__':
    unittest.main()
