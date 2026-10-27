#!/bin/bash
# Build the plaintext Data seed (a packed DBRDISK1 ramdisk) for the guacamole
# bring-up. macOS only: needs hdiutil/diskutil to create an APFS container.
#
# The seed is a small APFS container holding the iOS Data skeleton directories
# under its root (the data root), exactly what the owner DATA mount expects.
# Outputs:
#   <out>/data-seed.dbr          packed seed (only nonzero pages)
#   <out>/data-seed.json         volume UUID/size/identity for the build config
set -euo pipefail

OUT=${1:-build/data-seed}
SIZE=${DATA_SEED_SIZE:-512m}
VOLNAME=${DATA_SEED_VOLNAME:-Data}
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
WORK=$(mktemp -d)
trap 'cd /; hdiutil detach "$MOUNT" -force >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

echo "== creating APFS container image ($SIZE)"
hdiutil create -size "$SIZE" -fs APFS -volname "$VOLNAME" -nospotlight -quiet "$WORK/data.dmg"

echo "== converting to raw"
hdiutil convert -quiet -format UDRW -o "$WORK/data-raw.dmg" "$WORK/data.dmg"

echo "== attaching"
mkdir -p "$WORK/mnt"
hdiutil attach -readwrite -nobrowse -noautoopen -mountpoint "$WORK/mnt" "$WORK/data-raw.dmg" >/dev/null
MOUNT="$WORK/mnt"
sleep 1

echo "== creating the /private/var skeleton at the volume root"
cd "$MOUNT"
mkdir -p db/dslocal/nodes/Default/users \
         db/dslocal/nodes/Default/groups \
         db/dslocal/nodes/Default/config \
         db/dslocal/nodes/Default/sharepoints \
         db/ConfigurationProfiles \
         db/timezone \
         db/keychain \
         db/lockdown \
         db/analyticsd \
         db/diagnostics \
         db/uuidtext \
         db/systemstats \
         db/powerlog \
         db/os_analyticsd \
         db/com.apple.xpc.roleaccountd.staging \
         db/audit \
         db/empty \
         db/backups \
         cores \
         run \
         tmp \
         vm \
         log \
         root \
         mobile/Library/Preferences \
         mobile/Library/Caches \
         mobile/Library/Logs \
         mobile/Containers \
         mobile/Media \
         mobile/Documents \
         containers/Data/Application \
         containers/Data/System \
         containers/Data/PluginKitPlugin \
         containers/Shared/SystemGroup \
         containers/Shared/AppGroup \
         PersonaVolumes \
         preferences \
         keybags/backup \
         backups \
         folders \
         select \
         protected \
         staged_system_apps \
         networksd \
         MobileDevice \
         "Managed Preferences" \
         at \
         cron \
         spool \
         mail \
         lib \
         local \
         netboot \
         rwho \
         yp \
         empty \
         autofs
chmod 1777 tmp
chmod 0755 run vm
chmod 0700 root || true
chmod 0755 log backups || true

echo "== adding the Preboot volume (libignition preboot stage, role lookup 0x10)"
CONTAINER_DISK=$(diskutil info -plist "$MOUNT" | plutil -extract ParentWholeDisk raw -)
diskutil apfs addVolume "$CONTAINER_DISK" APFS Preboot -nospotlight >/dev/null
sleep 1
if [ -d "/Volumes/Preboot" ]; then
  # libignition binds /private/preboot and reads cryptex staging metadata from here
  mkdir -p "/Volumes/Preboot/usr/standalone/firmware" \
           "/Volumes/Preboot/cryptex1" \
           "/Volumes/Preboot/cryptex1/OS.staged" \
           "/Volumes/Preboot/cryptex1/app" \
           "/Volumes/Preboot/cryptex1/flag" \
           "/Volumes/Preboot/cryptex1/firmware"
  # try to set the APFS preboot role; role-based lookup needs the flag
  PREBOOT_DEV=$(diskutil info -plist "/Volumes/Preboot" | plutil -extract DeviceIdentifier raw -) || PREBOOT_DEV=""
  if [ -n "$PREBOOT_DEV" ]; then
    diskutil apfs changeVolumeRole "$PREBOOT_DEV" preboot 2>/dev/null \
      || diskutil apfs changeVolumeRole "$PREBOOT_DEV" p 2>/dev/null \
      || echo "NOTE: could not set preboot role via diskutil; role lookup may still fail"
  fi
else
  echo "WARNING: Preboot volume did not auto-mount at /Volumes/Preboot"
fi

echo "== reading the volume identity"
VOLUME_UUID=$(diskutil info -plist "$MOUNT" | plutil -extract VolumeUUID raw -)
VOLUME_NAME=$(diskutil info -plist "$MOUNT" | plutil -extract VolumeName raw -)
cd "$REPO"
UNMOUNTED=0
hdiutil detach "$MOUNT" -quiet && UNMOUNTED=1

echo "== packing DBRDISK1 (only nonzero pages)"
python3 - "$WORK/data-raw.dmg" "$OUT/data-seed.dbr" "$REPO/scripts" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import packed_ramdisk
packed_ramdisk.pack(Path(sys.argv[1]), Path(sys.argv[2]))
PY

python3 - "$OUT" "$VOLUME_UUID" "$VOLUME_NAME" <<'PY'
import hashlib, json, sys
from pathlib import Path
out, uuid, name = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
dbr = out / 'data-seed.dbr'
report = dict(volume_uuid=uuid.upper(), volume_name=name, seed=dbr.name,
              seed_bytes=dbr.stat().st_size,
              seed_sha256=hashlib.sha256(dbr.read_bytes()).hexdigest(),
              source_size=next(out.glob('*.size')).read_text().strip() if any(out.glob('*.size')) else None)
(out / 'data-seed.json').write_text(json.dumps(report, indent=2) + '\n')
print('PASS: data seed', report['volume_uuid'], report['seed_sha256'], report['seed_bytes'])
PY
if [ "$UNMOUNTED" = 0 ]; then hdiutil detach "$MOUNT" -force -quiet || true; fi
ls -la "$OUT"
