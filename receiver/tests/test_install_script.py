"""Checks on lxc/install.sh that do not need a container to run.

The installer is shell, so nothing type-checks it and a mistake surfaces only
on a live deploy — after the services have been stopped.
"""

import pathlib
import re

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent.parent / 'lxc' / 'install.sh'


@pytest.fixture(scope='module')
def script():
    return SCRIPT.read_text(encoding='utf-8')


class TestUiBuildCache:
    """Rebuilding takes about a minute and is byte-identical when ui/ has not
    changed, so the build is skipped on a matching source hash."""

    def test_the_stamp_lives_where_rsync_cannot_delete_it(self, script):
        """static/ is protected from the --delete sync; anywhere else the
        stamp would vanish on every deploy and the cache never hit."""
        assert '$APP_DIR/static/.source-hash' in script
        assert "--filter 'protect static/'" in script

    def test_the_hash_ignores_build_output(self, script):
        """node_modules and dist change without the sources changing."""
        section = re.search(r'UI_HASH=\$\((.*?)\)\n', script, re.S).group(1)
        assert 'node_modules' in section
        assert 'dist' in section

    def test_the_stamp_is_written_only_after_a_successful_build(self, script):
        """Writing it before would make a failed build look current."""
        build_at = script.index('npm run build')
        stamp_at = script.index("> \"$UI_STAMP\"")
        assert build_at < stamp_at

    def test_a_missing_build_forces_a_rebuild(self, script):
        """A stamp without the output it describes must not count as current."""
        assert '-f "$APP_DIR/static/index.html"' in script


class TestLocale:
    def test_a_utf8_locale_is_exported_before_apt(self, script):
        """The image sets LANG without generating it, so every apt call and
        every Perl maintainer script prints three lines of complaint."""
        export_at = script.index('export LANG=C.UTF-8')
        apt_at = script.index('apt-get install -y -qq --no-install-recommends')
        assert export_at < apt_at

    def test_it_degrades_rather_than_failing(self, script):
        """An image without C.UTF-8 should still install."""
        assert 'C.UTF-8 is unavailable' in script


class TestSafety:
    def test_the_installer_refuses_to_run_on_a_proxmox_host(self, script):
        assert '/etc/pve' in script

    def test_services_stay_stopped_when_the_migration_fails(self, script):
        """Starting them against a half-migrated database would write rows the
        new schema cannot hold."""
        section = script[script.index('Checking the log schema'):]
        assert 'exit 1' in section.split('Starting services')[0]

    def test_an_existing_config_is_never_overwritten(self, script):
        assert 'Keeping existing $ENV_FILE' in script
