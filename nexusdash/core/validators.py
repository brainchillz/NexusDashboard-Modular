"""Extracted verbatim from NexusStationDashboard app.py (Stage 1 split).
Routes converted @app.route -> @bp.route; logic unchanged."""
import re
# ─── Input validation ─────────────────────────────────────────────────
# Argument-list execution stops shell injection. These additional checks
# stop argument injection (values that look like flags) and config-file
# injection (newlines used to inject extra /etc/exports or smb.conf lines).
# Every pattern ends in \Z, not $: with re.match, `$` also matches just before
# a trailing newline, so 'tank\n' passed a `$`-anchored RE_POOL. The repo
# convention has been \Z since the mount feature; the older patterns here
# were only converted in 3.4.3 (tests/test_review_fixes.py pins it).

RE_POOL    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')
RE_DATASET = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_./-]*\Z')
RE_SNAP    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_./-]*@[a-zA-Z0-9][a-zA-Z0-9_.:-]*\Z')
RE_PROP    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]*\Z')
RE_IQN     = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._:-]*\Z')
RE_BSNAME  = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')
RE_CHAP    = re.compile(r'^[A-Za-z0-9._:+-]{1,64}\Z')
RE_PATH    = re.compile(r'^/[^\x00\n\r]*\Z')
RE_DISK    = re.compile(r'^/?[a-zA-Z0-9][a-zA-Z0-9_./-]*\Z')
# Pool member identifiers as shown by `zpool status` (bare names, /dev paths,
# and /dev/disk/by-id/... which can contain ':').
RE_DEVICE  = re.compile(r'^/?[A-Za-z0-9][A-Za-z0-9_./:-]*\Z')
VDEV_ADD_ROLES = {'', 'mirror', 'raidz', 'raidz1', 'raidz2', 'raidz3', 'spare', 'cache', 'log'}
RE_SIZE    = re.compile(r'^[0-9]+[KkMmGgTt]?[Bb]?\Z')
RE_NUM     = re.compile(r'^[0-9]+\Z')
RE_IP      = re.compile(r'^[0-9a-fA-F:.]+\Z')
RE_HOST    = re.compile(r'^[a-zA-Z0-9_.:*/-]+\Z')
RE_NFSOPTS = re.compile(r'^[a-zA-Z0-9_,=.-]+\Z')
RE_SHARE   = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')
RE_USER    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')
RE_USERS   = re.compile(r'^[a-zA-Z0-9_,. @-]+\Z')
RE_GROUP   = re.compile(r'^[a-z_][a-z0-9_-]*\Z')
RE_ACL     = re.compile(r'^[A-Za-z0-9_,.@ +-]*\Z')   # user / @group access lists
RE_HOSTS   = re.compile(r'^[A-Za-z0-9_,.: /-]*\Z')    # hosts allow/deny (IPs, subnets, names)
RE_MASK    = re.compile(r'^[0-7]{3,4}\Z')
RE_SERVICE = re.compile(r'^[a-zA-Z0-9@._-]+\Z')
RE_DEVNAME = re.compile(r'^[a-zA-Z0-9]+\Z')  # bare block-device name, e.g. sda / nvme0n1
RE_COMMENT = re.compile(r'^[^\n\r]*\Z')
VDEV_TYPES = {'', 'mirror', 'raidz', 'raidz1', 'raidz2', 'raidz3'}

# Plain-disk format & mount (a standard disk → partition → filesystem → mount).
# Filesystems the dashboard will create on a disk. Anything not here is refused.
MOUNT_FSTYPES = {'ext4', 'xfs', 'vfat', 'exfat'}
# Optional filesystem label (passed to mkfs); keep it conservative. \Z (not $)
# so a trailing newline can't sneak through — $ matches before a final '\n'.
RE_FSLABEL = re.compile(r'^[A-Za-z0-9_.-]{1,32}\Z')
# A leaf mount-point name (the dir created under a fixed base); no '/', no '..'.
RE_MOUNTNAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
# Where the dashboard is allowed to mount disks. A fixed allowlist keeps the
# mount target away from system paths (/, /etc, ...). The wrapper re-checks this.
MOUNT_BASES = ('/mnt', '/media')
# A filesystem UUID as reported by blkid/lsblk (ext/xfs hex-dash, vfat 8-char).
RE_UUID = re.compile(r'^[A-Za-z0-9-]{1,64}\Z')
# fstypes that are members of another subsystem and must never be offered as a
# plain mountable filesystem (they belong to ZFS/LVM/MD/swap).
NON_MOUNTABLE_FSTYPES = {'zfs_member', 'LVM2_member', 'linux_raid_member', 'swap'}


# llama.cpp inference server — managed like a system service (status/control via
# the shared service endpoints) plus its own page for model + CLI-arg editing.
