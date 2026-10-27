#!/usr/bin/env python3
"""Boot the guacamole kernel under QEMU virt with the real iPadOS rootfs.

Adapted from reAAAq's run_umi_qemu.py: stage an ESP with the built
UniXNU/DarwinBoot firmware plus this board's kernel, trust bundle and Data
seed, expose the OS/SystemOS windows of userdata-root.img as read-only
VirtIO disks, and hand the run to the upstream qemu-entry/gdb-stop.py
observer. Serial output lands in <out>/gdb-uart.log; GDB stops make the
launchd/dyld death directly inspectable on the host.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

ROOT = Path('/home/invalidname/UniXNU-guacamole')
TREE = ROOT / 'out/guacamole/tree'
QEMU_ENTRY = TREE / 'scripts/qemu-entry'
FIRMWARE_BUILD = TREE / 'uefi/Build/SurfaceDuo1Pkg/DEBUG_CLANGPDB/AARCH64'
OS_VOLUME_UUID = '04769D26-2CA0-4825-B443-E2E5F88300F2'

DEFAULT_ARGS = ('-v debug=0x14e serial=3 keepsyms=1 kt_fixed_zones=120 '
                'rd=uuid boot-uuid=' + OS_VOLUME_UUID + ' '
                '-apfs_shared_datavolume native-ufs-root=1 sheng-pmgr=2 '
                'kernel_stack_pages=32 launchd_unsecure_cache=1 ignition_level=1 '
                'wdt=-1 root-live-fs=1 allow-root-hash-mismatch=1')


def verify_window(disk, window, cache_path):
    st = disk.stat()
    offset, size = window['offset'], window['size']
    if not disk.is_file() or not 0 <= offset < offset + size <= st.st_size or offset % 512 or size % 512:
        raise ValueError(f'bad volume window {window["name"]}')
    identity = {'path': str(disk), 'size': st.st_size, 'offset': offset, 'window_bytes': size}
    cached = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if cached.get('identity') != identity or cached.get('sha256') != window['sha256']:
        digest = hashlib.sha256()
        with disk.open('rb') as stream:
            stream.seek(offset)
            left = size
            while left:
                data = stream.read(min(left, 4 << 20))
                if not data:
                    raise ValueError('incomplete volume window')
                digest.update(data)
                left -= len(data)
        if digest.hexdigest() != window['sha256']:
            raise ValueError(f'volume window {window["name"]} sha mismatch')
        cache_path.write_text(json.dumps({'identity': identity, 'sha256': window['sha256']}, indent=2) + '\n')
    return {'backing_file': str(disk), 'offset': offset, 'bytes': size, 'sha256': window['sha256']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel', type=Path,
                   default=ROOT / 'out/guacamole/kernel/kernel-ipados-17711-prelinked.macho')
    p.add_argument('--trust-cache', type=Path, default=ROOT / 'out/guacamole/payload/trust.bundle')
    p.add_argument('--data-seed', type=Path, default=ROOT / 'data-seed.dbr')
    p.add_argument('--userdata-json', type=Path, default=ROOT / 'out/guacamole/userdata/userdata-root.json')
    p.add_argument('--out', type=Path, default=ROOT / 'out/guacamole/qemu')
    p.add_argument('--stop', default='_handle_breakpoint',
                   help='first GDB stop symbol (the user BRK handler = the dyld trap)')
    p.add_argument('--also-stop', action='append', default=[])
    p.add_argument('--read-symbol', action='append', default=[], metavar='SYMBOL:BYTES')
    p.add_argument('--read-address', action='append', default=[], metavar='HEX_ADDRESS:HEX_BYTES')
    p.add_argument('--args', default=DEFAULT_ARGS)
    p.add_argument('--timeout', type=int, default=240)
    p.add_argument('--owned-memory-mib', type=int, default=512)
    p.add_argument('--finish', action='store_true', help='do not stop; just capture the serial log')
    p.add_argument('--console', action='store_true', default=True, help='capture the virtio kernel console')
    p.add_argument('--no-console', dest='console', action='store_false')
    a = p.parse_args()

    kernel, out = a.kernel.resolve(), a.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    esp = out / 'esp'
    payload = esp / 'EFI/UniXNU/Payloads'
    payload.mkdir(parents=True)
    (esp / 'EFI/BOOT').mkdir(parents=True)

    shutil.copyfile(FIRMWARE_BUILD / 'UniXNUPkg/Application/UniXNU/UniXNU/DEBUG/UniXNU.efi',
                    esp / 'EFI/BOOT/BOOTAA64.EFI')
    shutil.copyfile(FIRMWARE_BUILD / 'DarwinBoot.efi', payload / 'DarwinBoot.efi')
    shutil.copyfile(kernel, payload / 'kernel.macho')
    if a.trust_cache.is_file():
        shutil.copyfile(a.trust_cache, payload / 'kernel.macho.trustcache')
    data_input = None
    if a.data_seed.is_file():
        sys.path.insert(0, str(TREE / 'drivers/DarwinBoot/Test'))
        from packed_ramdisk import inspect
        data_input, header = inspect(a.data_seed.resolve())
        if not data_input.get('packed'):
            raise SystemExit('unexpected Data seed format')
        # The macOS-built seed carries its own GPT, so NXSB sits past the
        # protective partition table; only the packed form matters here.
        shutil.copyfile(a.data_seed, payload / 'kernel.macho.ramdisk')

    # GDB observer patched for the SHNGROOT window layout: the OS and
    # SystemOS windows of userdata-root.img as disjoint read-only nodes.
    manifest = json.loads(a.userdata_json.read_text())
    windows = {w['name']: w for w in manifest['windows']}
    os_window, system_window = windows['OS'], windows['SystemOS']
    disk = Path(manifest.get('image') or (ROOT / 'out/guacamole/userdata/userdata-root.img'))
    root_input = verify_window(disk, os_window, out / 'qemu-os-window.json')
    verify_window(disk, system_window, out / 'qemu-systemos-window.json')

    fdt = out / 'qemu-virt.dtb'
    subprocess.run(['qemu-system-aarch64', '-M', 'virt,gic-version=3,dumpdtb=' + str(fdt),
                    '-cpu', 'cortex-a710', '-m', '1024', '-display', 'none'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    content = (QEMU_ENTRY / 'gdb-stop.py').read_text()
    assert content.count('\nimport argparse\n') == 1
    content = content.replace('\nimport argparse\n',
                              '\nimport argparse\nimport sys\nsys.path.insert(0, ' +
                              repr(str(QEMU_ENTRY)) + ')\n')
    before = '        first_sector = stream.read(512)'
    assert content.count(before) == 1
    content = content.replace(before, f'        stream.seek({os_window["offset"]})\n' + before)
    before = "block_input = dict(name=disk.name, bytes=disk.stat().st_size, format='raw', read_only=True,"
    assert content.count(before) == 1
    content = content.replace(before,
                              f"block_input = dict(name=disk.name, bytes={os_window['size']}, "
                              f"offset={os_window['offset']}, format='raw', read_only=True,")
    before = "'driver': 'raw', 'node-name': 'offline-block', 'read-only': True,"
    assert content.count(before) == 1
    content = content.replace(before, before + f" 'offset': {os_window['offset']}, 'size': {os_window['size']},")
    # second node = the SystemOS window from the same file
    before = 'or extra_disk == disk'
    assert content.count(before) == 1
    # The OS and SystemOS windows are verified disjoint SHA-256 windows of the
    # one backing file, so reusing the same raw file is correct here.
    content = content.replace(before, 'or (extra_disk == disk and False)')
    before = '        extra_sector = stream.read(512)'
    assert content.count(before) == 1
    content = content.replace(before, f'        stream.seek({system_window["offset"]})\n' + before)
    before = "extra_block_input = dict(name=extra_disk.name, bytes=extra_disk.stat().st_size,"
    assert content.count(before) == 1
    content = content.replace(before,
                              f"extra_block_input = dict(name=extra_disk.name, bytes={system_window['size']}, "
                              f"offset={system_window['offset']},")
    before = "'driver': 'raw', 'node-name': 'offline-extra', 'read-only': True,"
    assert content.count(before) == 1
    content = content.replace(before, before + f" 'offset': {system_window['offset']}, 'size': {system_window['size']},")
    gdb_script = out / 'gdb-stop-root-window.py'
    gdb_script.write_text(content)

    subprocess.run([
        sys.executable, str(QEMU_ENTRY / 'make-dt.py'),
        '--output', str(payload / 'kernel.macho.adt'), '--armvirt', '--armcpu',
        '--qemu-accel', 'tcg', '--qemu-vmm-present', '--nvram-proxy',
        '--crypto-hash-method', 'sha2-384', '--osenvironment', 'ramdisk',
        '--amfi-platform-only',
        '--owned-memory-size', hex(a.owned_memory_mib << 20), '--dram-size', '0x100000000',
        '--virtio-mmio-fdt', str(fdt),
        '--root-match-volume-uuid', OS_VOLUME_UUID,
        '--trust-cache', *( ['--data-ramdisk'] if data_input else []),
    ], check=True, stdout=subprocess.DEVNULL)

    (esp / 'EFI/UniXNU/unixnu.toml').write_text(
        'version = 1\n[boot]\ntimeout = 1\ndefault = "qemu"\n'
        '[kits]\nautoload = false\n[[driver]]\npath = "Payloads/DarwinBoot.efi"\n'
        '[[entry]]\nid = "qemu"\ntitle = "guacamole on QEMU"\ntype = "xnu"\n'
        'path = "Payloads/kernel.macho"\n' +
        ('initrd = "Payloads/kernel.macho.ramdisk"\n' if data_input else '') +
        f'args = "{a.args}"\n')

    # The observer's stop argument is an address; resolve kernel symbols with
    # llvm-nm (this host has no xcrun).
    symbols = {}
    for line in subprocess.check_output(['llvm-nm', '-n', str(payload / 'kernel.macho')],
                                        text=True).splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3 and fields[1] not in ('a', 'A'):
            try:
                symbols[fields[2]] = int(fields[0], 16)
            except ValueError:
                pass

    def sym(name):
        if name in symbols:
            return hex(symbols[name])
        if name.startswith('0x'):
            return name
        raise SystemExit(f'missing kernel symbol: {name}')

    command = [sys.executable, str(gdb_script),
               sym(a.stop), '--fixture', str(out), '--esp', str(esp),
               '--kernel', str(payload / 'kernel.macho'), '--qemu-accel', 'tcg',
               '--qemu-memory-mib', '4096', '--gic-version', '3',
               '--timeout', str(a.timeout),
               '--firmware', '/usr/share/AAVMF/AAVMF_CODE.fd',
               '--virtio-block-image', str(disk),
               '--virtio-extra-block-image', str(disk)]
    # The kernel console under virt is the virtio-serial tty; QEMU connects to
    # this host-side unix socket, so serve it and capture everything.
    console_sock = out / 'console.sock'
    console_log = out / 'console.log'
    console_thread = None
    if a.console:
        import socket as socketmod
        import threading
        server = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
        server.bind(str(console_sock))
        server.listen(1)
        server.settimeout(a.timeout)
        sink = console_log.open('wb')

        def _serve():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    sink.write(data)
                    sink.flush()

        console_thread = threading.Thread(target=_serve, daemon=True)
        console_thread.start()
        command += ['--virtio-console-socket', str(console_sock)]
    for name in a.also_stop:
        command += ['--also-stop', sym(name)]
    for request in a.read_symbol:
        nm, size = request.rsplit(':', 1)
        command += ['--read-memory', f'{sym(nm)}:{int(size, 0):x}']
    for request in a.read_address:
        command += ['--read-memory', request]
    if a.finish:
        command.append('--finish-function')

    (out / 'command.json').write_text(json.dumps(command, indent=2))
    with (out / 'runner.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    print(f'QEMU exit={result.returncode}; log: {out}/gdb-uart.log; runner: {out}/runner.log')
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
