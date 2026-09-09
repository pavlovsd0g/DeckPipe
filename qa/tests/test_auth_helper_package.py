import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]


class HelperPackageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' and (ROOT / 'desktop/node_modules/@tauri-apps/cli/tauri.js').is_file(), 'Installed Windows Tauri CLI required')
    def test_tauri_selects_desktop_main_when_native_helper_is_also_a_bin(self):
        # Exercise the installed CLI's actual discovery without compiling or
        # packaging anything. Both auto-discovered Cargo binaries are present.
        with tempfile.TemporaryDirectory(dir=os.environ.get('DECKPIPE_AUTH_TEST_DIR')) as directory:
            probe = Path(directory)
            crate = probe / 'src-tauri'
            (crate / 'src/bin').mkdir(parents=True)
            for name in ('Cargo.toml', 'Cargo.lock', 'tauri.conf.json'):
                shutil.copy2(ROOT / 'desktop/src-tauri' / name, crate / name)
            (crate / 'src/main.rs').write_text('fn main() {}')
            (crate / 'src/lib.rs').write_text('')
            (crate / 'src/bin/deckpipe-auth-host.rs').write_text('fn main() {}')
            (probe / 'ui').mkdir()
            (probe / 'ui/index.html').write_text('<html></html>')
            runner = probe / 'noop.cmd'
            runner.write_text('@exit /b 0\n')
            env = dict(os.environ, CARGO_TARGET_DIR=str(probe / 'target'), CARGO_NET_OFFLINE='true', CI='true')
            metadata = subprocess.run(['cargo', 'metadata', '--offline', '--no-deps', '--format-version', '1'], cwd=crate, env=env, text=True, capture_output=True, timeout=30, check=True)
            package = json.loads(metadata.stdout)['packages'][0]
            targets = {target['name']: target for target in package['targets'] if 'bin' in target['kind']}
            self.assertEqual(set(targets), {'deckpipe', 'deckpipe-auth-host'})
            result = subprocess.run(['node', str(ROOT / 'desktop/node_modules/@tauri-apps/cli/tauri.js'), 'build', '--no-bundle', '--runner', str(runner), '--ci'], cwd=probe, env=env, text=True, capture_output=True, timeout=45)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(f"Built application at: {probe / 'target/release/deckpipe.exe'}", output)
            self.assertEqual(package['default_run'], 'deckpipe')

    def test_clean_release_build_bootstraps_native_helper_before_tauri(self):
        script = (ROOT / 'release/build.ps1').read_text(encoding='utf-8')
        bootstrap = "& $helperBuild -SourceRoot $plan.SourceRoot -TargetDirectory $plan.CargoTargetDir -Release"
        self.assertIn(bootstrap, script)
        self.assertLess(script.index('Export-TrackedSourceToTemp -Plan $plan'), script.index(bootstrap))
        self.assertLess(script.index(bootstrap), script.index('& npm run build'))
        self.assertIn("Join-Path $plan.SourceRoot 'release\\auth-helper\\Build-AuthHelper.ps1'", script)
        self.assertIn("'build failed: native auth helper was not produced in exported source'", script)

    def test_unsigned_archive_is_reproducible_and_firefox_manifest_is_restricted(self):
        spec = importlib.util.spec_from_file_location('auth_package', ROOT / 'release/auth-helper/package_helper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entries = {name: (ROOT / 'extension' / name).read_bytes() for name in module.EXTENSION_FILES}
        with tempfile.TemporaryDirectory() as directory:
            one, two = Path(directory) / 'one.xpi', Path(directory) / 'two.xpi'
            module.archive(one, entries)
            module.archive(two, entries)
            self.assertEqual(hashlib.sha256(one.read_bytes()).digest(), hashlib.sha256(two.read_bytes()).digest())
            with zipfile.ZipFile(one) as archive:
                self.assertEqual(set(archive.namelist()), set(module.EXTENSION_FILES))
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['permissions'], ['nativeMessaging', 'cookies'])
                self.assertEqual(manifest['browser_specific_settings']['gecko']['id'], 'deckpipe-auth@deckpipe.local')
                self.assertEqual(manifest['browser_specific_settings']['gecko']['data_collection_permissions']['required'], ['authenticationInfo'])
                self.assertNotIn('content_scripts', manifest)
                self.assertNotIn('externally_connectable', manifest)
                self.assertNotIn('host_permissions', manifest)

    def test_tauri_bundles_fixed_setup_path_and_native_executable(self):
        config = json.loads((ROOT / 'desktop/src-tauri/tauri.conf.json').read_text())
        self.assertEqual(config['bundle']['resources']['../../release/auth-helper/'], 'auth-helper/')
        self.assertEqual(config['bundle']['resources']['../../extension/'], 'auth-helper/extension/')
        self.assertIn('binaries/deckpipe-auth-host', config['bundle']['externalBin'])
        self.assertTrue((ROOT / 'release/auth-helper/README.md').is_file())
        main = (ROOT / 'desktop/src-tauri/src/main.rs').read_text(encoding='utf-8')
        self.assertIn('fn auth_open_setup(', main)
        self.assertIn('resources.join("auth-helper")', main)
        self.assertNotIn('path: String', main)
