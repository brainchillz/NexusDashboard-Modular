"""packaging/lib/stage.sh must stay in lockstep with install.sh.

stage.sh builds the package payload by extracting each /usr/local/sbin helper
VERBATIM out of install.sh's heredocs, driven by a hand-written HELPERS map.
That map is a second copy of a list, and it drifted: 3.3.0 deleted the
`model-fetch` helper but left the map entry, and `extract_block` treats a
missing heredoc as fatal — so every deb/rpm target failed. The reverse drift is
quieter and worse: a helper added to install.sh but not to the map builds a
package that installs without it (3.1.0's `updates`, 3.3.0's `gpu-tune`/`nut`
were all missing this way).

Only a tagged release runs packages:build, so nothing caught either direction
for three weeks. These tests do.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / 'install.sh'
STAGE_SH = REPO / 'packaging' / 'lib' / 'stage.sh'

# cat > "$LOCATE_HELPER" << 'HELPER'
RE_HEREDOC = re.compile(r"""^cat > "\$([A-Z0-9_]+)" << 'HELPER'""", re.M)
# LOCATE_HELPER="/usr/local/sbin/${HELPER_PREFIX}-locate-read"
RE_HELPER_PATH = re.compile(
    r'^([A-Z0-9_]+)="/usr/local/sbin/\$\{HELPER_PREFIX\}-([a-z0-9-]+)"', re.M)
# ['$LOCATE_HELPER']=nexus-dashboard-locate-read
RE_MAP_ENTRY = re.compile(r"^\s*\['\$([A-Z0-9_]+)'\]=(\S+)\s*$", re.M)


@pytest.fixture(scope='module')
def install_src():
    assert INSTALL_SH.is_file(), f'missing {INSTALL_SH}'
    return INSTALL_SH.read_text()


@pytest.fixture(scope='module')
def stage_map():
    assert STAGE_SH.is_file(), f'missing {STAGE_SH}'
    body = STAGE_SH.read_text()
    block = body.split('declare -A HELPERS=(', 1)[1].split(')', 1)[0]
    return dict(RE_MAP_ENTRY.findall(block))


def test_stage_map_covers_exactly_the_install_sh_helpers(install_src, stage_map):
    """Neither direction of drift is allowed."""
    in_install = set(RE_HEREDOC.findall(install_src))
    in_stage = set(stage_map)
    assert in_install, 'no helper heredocs found in install.sh — regex out of date?'

    missing = in_install - in_stage       # would ship a package without the helper
    stale = in_stage - in_install         # hard-fails every build target
    assert not missing, (
        'install.sh defines helpers that stage.sh will not package: '
        + ', '.join(sorted(missing)))
    assert not stale, (
        'stage.sh extracts helpers install.sh no longer defines (extract_block '
        'exits 1 on these, breaking every package build): ' + ', '.join(sorted(stale)))


def test_staged_filenames_match_the_installed_helper_names(install_src, stage_map):
    """The packaged basename must equal what install.sh would write, so a
    package upgrade lands on the same path the installer uses."""
    suffixes = dict(RE_HELPER_PATH.findall(install_src))
    for var, staged in sorted(stage_map.items()):
        assert var in suffixes, f'{var} has no /usr/local/sbin path in install.sh'
        assert staged == f'nexus-dashboard-{suffixes[var]}', (
            f'{var}: stage.sh packages {staged!r} but install.sh writes '
            f'nexus-dashboard-{suffixes[var]!r}')
