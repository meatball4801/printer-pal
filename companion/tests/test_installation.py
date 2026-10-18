import argparse
import json
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import DEFAULT_CONFIG, validate_config
from install import DETECT_PREFIX, Installer, choose_config, lan_scopes, available_port, report_detection, supported_system
from build_release import build


def apt_policy(missing=()):
    """A stand-in runner: apt-cache reports a candidate for every package except `missing`."""
    def run(command, **_):
        if command[:2] == ['apt-cache', 'policy']:
            text = ''.join(f'{name}:\n  Installed: (none)\n  Candidate: {"(none)" if name in missing else "1.0"}\n'
                           for name in command[2:])
            return subprocess.CompletedProcess(command, 0, stdout=text)
        return subprocess.CompletedProcess(command, 0, stdout='Status: inactive\n')
    return run


def options(**values):
    return argparse.Namespace(**{**dict(mode=None, source_url=None, name=None, port=None), **values})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runner = Mock(side_effect=apt_policy())
        ufw_detection = patch('install.shutil.which', return_value='/usr/sbin/ufw')
        ufw_detection.start()
        self.addCleanup(ufw_detection.stop)
        self.installer = Installer(self.root, self.runner)

    def tearDown(self):
        self.temp.cleanup()

    def commands(self):
        return [call.args[0] for call in self.runner.call_args_list]

    def test_fresh_install_enables_boot_and_starts_service(self):
        self.installer.install(DEFAULT_CONFIG)
        self.assertIn(['systemctl', 'enable', 'avahi-daemon.service', 'print-monitor.service', 'print-monitor-network.timer'], self.commands())
        self.assertIn(['systemctl', 'restart', 'print-monitor.service'], self.commands())
        self.assertTrue((self.root / 'opt/print-monitor/camera_source.py').exists())
        self.assertEqual((self.root / 'etc/print-monitor/config.json').stat().st_mode & 0o777, 0o640)
        unit = (self.root / 'etc/systemd/system/print-monitor.service').read_text()
        self.assertIn('Restart=always', unit)
        self.assertIn('StartLimitIntervalSec=0', unit)

    def test_firewall_scopes_exclude_public_ipv4_broad_ipv6_and_virtual_interfaces(self):
        addresses = [{'ifname': 'eth0', 'addr_info': [
            {'local': '192.168.1.199', 'prefixlen': 24},
            {'local': '2600:1700:d840:a3d0::1', 'prefixlen': 64},
            {'local': 'fe80::1', 'prefixlen': 64},
            {'local': '8.8.8.8', 'prefixlen': 24},
            {'local': '192.168.1.199', 'prefixlen': 0},
            {'local': '2600::1', 'prefixlen': 32}]},
            {'ifname': 'docker0', 'addr_info': [{'local': '172.17.0.1', 'prefixlen': 16}]}]
        self.assertEqual(lan_scopes(addresses, {'eth0'}), [
            ('eth0', '192.168.1.0/24'), ('eth0', '2600:1700:d840:a3d0::/64'), ('eth0', 'fe80::/64')])

    def test_active_ufw_adds_only_selected_port_on_physical_lan(self):
        (self.root / 'sys/class/net/eth0/device').mkdir(parents=True)
        defaults = self.root / 'etc/default/ufw'
        defaults.parent.mkdir(parents=True)
        defaults.write_text('IPV6=yes\n')
        def run(command, **kwargs):
            output = 'Status: active\n' if command == ['ufw', 'status'] else json.dumps([
                {'ifname': 'eth0', 'addr_info': [{'local': '192.168.1.199', 'prefixlen': 24}]}])
            return subprocess.CompletedProcess(command, 0, stdout=output)
        self.runner.side_effect = run
        with patch('install.shutil.which', return_value='/usr/sbin/ufw'):
            self.installer.configure_firewall(32188)
        self.assertIn(['ufw', 'allow', 'in', 'on', 'eth0', 'from', '192.168.1.0/24', 'to', 'any',
                       'port', '32188', 'proto', 'tcp', 'comment', 'Printer Pal'], self.commands())
        self.assertFalse(any('disable' in command or 'reset' in command or 'enable' in command for command in self.commands()))

    def test_inactive_ufw_is_not_changed(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, stdout='Status: inactive\n')
        with patch('install.shutil.which', return_value='/usr/sbin/ufw'):
            self.installer.configure_firewall(32187)
        self.assertEqual(self.commands(), [['ufw', 'status']])

    def test_update_preserves_phone_pairing_and_existing_configuration(self):
        state = self.root / 'var/lib/print-monitor/state.json'
        state.parent.mkdir(parents=True)
        state.write_text('{"device_id":"existing","tokens":["existing-hash"]}')
        previous = validate_config({'mode': 'mjpeg', 'source_url': 'http://127.0.0.1:8080/stream', 'port': 8081, 'name': 'Existing Pi'})
        config = choose_config(previous, options(), installed=True, port_check=lambda _: False)
        self.installer.install(config)
        self.installer.install(config)
        self.assertEqual(json.loads(state.read_text())['tokens'], ['existing-hash'])
        self.assertEqual(json.loads((self.root / 'etc/print-monitor/config.json').read_text()), previous)

    def test_http_upgrade_revokes_old_tokens_but_preserves_identity(self):
        config_file = self.root / 'etc/print-monitor/config.json'
        config_file.parent.mkdir(parents=True)
        config_file.write_text('{"mode":"picamera2","port":32187}')
        state = self.root / 'var/lib/print-monitor/state.json'
        state.parent.mkdir(parents=True)
        state.write_text('{"device_id":"original-device","tokens":["old-plaintext-token-hash"]}')
        self.installer.install(DEFAULT_CONFIG)
        saved = json.loads(state.read_text())
        self.assertEqual(saved['device_id'], 'original-device')
        self.assertEqual(saved['tokens'], [])
        self.assertTrue(json.loads(config_file.read_text())['tls'])

    def test_apt_never_waits_for_a_question(self):
        self.installer.apt('install', '-y', 'python3')
        env = self.runner.call_args.kwargs['env']
        self.assertEqual(env['DEBIAN_FRONTEND'], 'noninteractive')
        self.assertEqual(env['LC_ALL'], 'C')

    def test_apt_output_is_shown_as_it_runs(self):
        live = Installer(Path('/'))
        with patch('builtins.print') as output:
            code, text = live.run_apt(['/bin/sh', '-c', 'echo Unpacking ffmpeg; echo Setting up ffmpeg; exit 3'], {})
        self.assertEqual(code, 3)
        self.assertIn('Setting up ffmpeg', text)
        self.assertEqual([call.args[0] for call in output.call_args_list], ['Unpacking ffmpeg\n', 'Setting up ffmpeg\n'])

    def test_package_lock_retries_but_other_failures_stop(self):
        self.runner.side_effect = [subprocess.CompletedProcess([], 100, stdout='', stderr='Could not get lock /var/lib/apt/lists/lock'),
                                   subprocess.CompletedProcess([], 0, stdout='', stderr='')]
        with patch('install.time.sleep'):
            self.installer.apt('update')
        self.assertEqual(self.runner.call_count, 2)
        self.runner.reset_mock()
        self.runner.side_effect = [subprocess.CompletedProcess([], 100, stdout='', stderr='Repository signature verification failed')]
        with self.assertRaises(subprocess.CalledProcessError):
            self.installer.apt('update')
        self.assertEqual(self.runner.call_count, 1)

    def test_existing_stream_keeps_its_service_and_uses_another_port(self):
        config = choose_config(DEFAULT_CONFIG, options(mode='mjpeg', source_url='http://127.0.0.1:8080/stream'), port_check=lambda p: p != 8080)
        self.assertEqual(config['port'], 32187)
        self.installer.install(config)
        self.assertNotIn('python3-picamera2', next(c for c in self.commands() if c[:2] == ['apt-get', 'install']))
        touched_services = [c for c in self.commands() if c[:2] in (['systemctl', 'stop'], ['systemctl', 'restart'])]
        self.assertTrue(all(c[2] in ('print-monitor.service', 'print-monitor-network.service') for c in touched_services))
        advertisement = ET.fromstring((self.root / 'etc/avahi/services/print-monitor.service').read_text())
        self.assertEqual(advertisement.find('service/port').text, '32187')

    def test_offline_upstream_still_gets_distinct_companion_port(self):
        config = choose_config(DEFAULT_CONFIG, options(mode='mjpeg', source_url='http://localhost:8080/stream'), port_check=lambda _: True)
        self.assertEqual(config['port'], 32187)

    def test_busy_default_and_upstream_ports_are_skipped(self):
        config = choose_config(DEFAULT_CONFIG, options(), port_check=lambda p: p != 32187)
        self.assertEqual(config['port'], 32188)
        config = choose_config(DEFAULT_CONFIG, options(mode='mjpeg', source_url='http://localhost:32187/stream'), port_check=lambda _: True)
        self.assertEqual(config['port'], 32188)

    def test_busy_explicit_port_fails_before_install(self):
        with self.assertRaisesRegex(ValueError, 'already in use'):
            choose_config(DEFAULT_CONFIG, options(port=8081), port_check=lambda _: False)
        with self.assertRaisesRegex(ValueError, 'already in use'):
            choose_config(DEFAULT_CONFIG, options(port=8081), installed=True, port_check=lambda _: False)

    @unittest.skipUnless(socket.has_dualstack_ipv6(), 'Dual stack is unavailable')
    def test_ipv6_only_listener_prevents_selecting_its_port(self):
        with socket.socket(socket.AF_INET6) as listener:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind(('::', 0))
            listener.listen()
            self.assertFalse(available_port(listener.getsockname()[1]))

    def test_image_install_does_not_start_or_create_shared_identity(self):
        self.installer.install(DEFAULT_CONFIG, image=True)
        self.assertFalse((self.root / 'var/lib/print-monitor/state.json').exists())
        self.assertFalse(any(c[0] == 'apt-get' or c[:2] in (['systemctl', 'start'], ['systemctl', 'restart']) for c in self.commands()))
        self.assertIn(['systemctl', 'enable', 'avahi-daemon.service', 'print-monitor.service', 'print-monitor-network.timer'], self.commands())

    def test_discovery_name_is_valid_xml(self):
        self.installer.install({**DEFAULT_CONFIG, 'name': 'Printer <A> & B'})
        advertisement = ET.fromstring((self.root / 'etc/avahi/services/print-monitor.service').read_text())
        self.assertEqual(advertisement.find('name').text, 'Printer <A> & B on %h')

    def test_config_rejects_invalid_sources_and_loop(self):
        for url in ['rtsp://camera/video', 'http://user:password@camera/stream', 'http://localhost:32187/stream']:
            with self.assertRaises(ValueError):
                validate_config({'mode': 'mjpeg', 'source_url': url})
        for port in [True, 80, 70000]:
            with self.assertRaises(ValueError):
                validate_config({'port': port})

    def apt_install(self):
        return next(c for c in self.commands() if c[:2] == ['apt-get', 'install'])

    def test_undetected_camera_skips_packages_the_system_lacks(self):
        self.runner.side_effect = apt_policy(missing={'python3-picamera2'})
        self.installer.install({**DEFAULT_CONFIG, 'mode': 'auto'})
        self.assertNotIn('python3-picamera2', self.apt_install())
        self.assertIn('ffmpeg', self.apt_install())

    def test_pi_camera_mode_explains_a_missing_picamera2(self):
        self.runner.side_effect = apt_policy(missing={'python3-picamera2'})
        with self.assertRaisesRegex(ValueError, '--source-url'):
            self.installer.install({**DEFAULT_CONFIG, 'mode': 'picamera2'})
        self.assertFalse(any(c[:2] == ['apt-get', 'install'] for c in self.commands()))

    def test_supported_system_accepts_debian_family(self):
        self.assertFalse(supported_system(self.root))
        (self.root / 'etc').mkdir()
        (self.root / 'etc/debian_version').write_text('12.5\n')
        self.assertTrue(supported_system(self.root))

    def detection(self, *arguments, **keywords):
        with patch('builtins.print') as output:
            self.assertEqual(report_detection(*arguments, **keywords), 0)
        line = output.call_args.args[0]
        self.assertTrue(line.startswith(DETECT_PREFIX))
        return json.loads(line[len(DETECT_PREFIX):])

    def test_detection_reports_existing_feed_without_installing(self):
        feed = {'mode': 'mjpeg', 'source_url': 'http://127.0.0.1:8080/?action=stream', 'device': None}
        report = self.detection(DEFAULT_CONFIG, False, detect=lambda _: (feed, 'Reusing a working local MJPEG camera feed.'))
        self.assertEqual(report['mode'], 'mjpeg')
        self.assertEqual(report['source_url'], feed['source_url'])
        self.assertFalse(report['installed'])
        self.runner.assert_not_called()

    def test_detection_without_a_camera_reports_auto(self):
        fallback = {'mode': 'picamera2', 'source_url': None, 'device': None}
        report = self.detection(DEFAULT_CONFIG, False, detect=lambda _: (fallback, 'No working feed or USB camera found.'))
        self.assertEqual(report['mode'], 'auto')

    def test_detection_on_installed_companion_keeps_its_source(self):
        saved = {**DEFAULT_CONFIG, 'mode': 'usb', 'device': '/dev/video0', 'port': 32188}
        report = self.detection(saved, True, detect=lambda _: self.fail('should not rescan'))
        self.assertTrue(report['installed'])
        self.assertEqual((report['mode'], report['port']), ('usb', 32188))

    def test_standalone_bundle_is_reproducible_and_self_verifies(self):
        first = build(self.root / 'one')
        second = build(self.root / 'two')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        result = subprocess.run(['bash', str(first), '--check'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('payload verified', result.stdout)

    def test_development_installer_uses_its_own_server_but_keeps_the_checksum(self):
        build(self.root / 'dev', base_url='http://mac.local:8765/')
        text = (self.root / 'dev' / 'install.sh').read_text()
        self.assertIn('local url="http://mac.local:8765/printer-pal-install-', text)
        self.assertNotIn('--proto', text)
        self.assertNotIn('@DIGEST@', text)
        release = (self.root / 'release-check')
        build(release)
        self.assertIn("--proto '=https'", (release / 'install.sh').read_text())
        with self.assertRaises(ValueError):
            build(self.root / 'bad', base_url='http://mac.local/"; rm -rf /; "')

    def test_one_line_installer_pins_the_release_checksum(self):
        release = self.root / 'release'
        bundle = build(release)
        bootstrap = release / 'install.sh'
        text = bootstrap.read_text()
        self.assertNotIn('@VERSION@', text)
        self.assertNotIn('@DIGEST@', text)
        self.assertIn(f"local version='{bundle.stem.rsplit('-', 1)[1]}'", text)
        self.assertIn('install.sh', (release / 'SHA256SUMS').read_text())
        # Stand-in curl serves the locally built installer for the release URL.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        served = self.root / 'served.run'
        fake = bin_dir / 'curl'
        fake.write_text('#!/usr/bin/env bash\nwhile [[ $# -gt 1 ]]; do [[ $1 == -o ]] && out=$2; shift; done\n'
                        f'[[ $1 == https://github.com/*/{bundle.name} ]] || exit 22\ncp "{served}" "$out"\n')
        fake.chmod(0o755)
        env = {'PATH': f'{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin'}

        served.write_bytes(bundle.read_bytes())
        result = subprocess.run(['bash', str(bootstrap), '--check'], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('payload verified', result.stdout)

        served.write_bytes(bundle.read_bytes() + b'\n# tampered\n')
        result = subprocess.run(['bash', str(bootstrap), '--check'], text=True, capture_output=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('failed its checksum', result.stderr)
        self.assertNotIn('payload verified', result.stdout)
