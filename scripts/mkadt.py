#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the SWEET xnu needs to boot.

Only what the early boot path actually reads (see NOTES.md); every MMIO
address comes from the device's live FDT (see tools/devices.py for the
per-board profile).

Usage:  mkadt.py OUT.adt [--device sweet|guacamole] [--dump] [--qemu] [--highmem]
"""
import hashlib, os, struct, sys, zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt      # noqa: E402
import devices  # noqa: E402

# /arm-io children are relative to this; it must not be 0 (serial_init bails)
SOC_BASE = 0x01000000

SWEET_ECID = 0x5357454554000001  # 'SWEET' + 1, synthetic
# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76): GICD/GICR and the generic timer's CNTFRQ
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27

BOOT_MANIFEST_HASH = hashlib.sha384(b'sweet boot manifest').digest()

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW. MobileGestalt
# derives product type, device class, home-button-less UI, artwork etc. from
# the root identity and /product of the real D22AP device tree. That tree is
# an Apple artifact (kept out of git): extract it from the IPSW with
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.environ.get('UNIXNU_D22AP_ADT', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'base-d22ap.adt'))


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def ipad_product(dev):
    """The unchanged iPad7,1 /product identity, with this board's model."""
    return [
        ('product-name', cstr('iPad Pro (12.9-inch) (2nd generation)')),
        ('model', cstr(dev['board_compat'])),
        ('product-description', cstr('iPad Pro (12.9-inch) (2nd generation)')),
        ('artwork-device-idiom', cstr('pad')),
        ('artwork-device-subtype', u32(2732)),
        ('artwork-scale-factor', u32(2)),
        ('artwork-display-gamut', cstr('P3')),
        ('compatible-device-fallback', cstr('iPad6,7')),
        ('thin-bezel', u32(1)),
    ]


def empty_nvram_proxy(variables=()):
    """A fresh volatile CHRP bank for IODTNVRAM, with no controller (sheng format).

    variables are CHRP "name=value" pairs stored in the common partition, which
    IONVRAMCHRPHandler unserializes into the Apple NVRAM variables.
    """
    def header(name, size):
        raw = bytearray(struct.pack('<BBH12s', 0x70, 0, size // 16, name))
        checksum = sum(raw)
        while checksum > 255:
            checksum = (checksum & 255) + (checksum >> 8)
        raw[1] = checksum
        return raw

    image = bytearray(8192)
    image[:16] = header(b'nvram', 32)
    image[32:48] = header(b'common', 16 + 2048)
    at = 48
    for name, value in variables:
        entry = name.encode() + b'=' + value.encode() + b'\0'
        image[at:at + len(entry)] = entry
        at += len(entry)
    image[at] = 0
    free_at = 48 + 2048
    image[free_at:free_at + 16] = header(b'free', len(image) - free_at)
    struct.pack_into('<I', image, 16, zlib.adler32(image[20:]))
    return [('nvram-bank-size', struct.pack('<I', len(image))),
            ('nvram-bank-count', struct.pack('<I', 1)),
            ('nvram-current-bank', struct.pack('<I', 0)),
            ('nvram-proxy-data', bytes(image))]


def build(seed=None, qemu=False, highmem=False, device='sweet', dram_size=None,
          owned_memory_size=None, ipad_identity=False, root_match_uuid=None,
          ipad_chip_id=0x207, trust_cache=False, data_ramdisk=False, packed_data_ramdisk=False,
          persistent_data=False):
    """qemu=True: same tree, but with QEMU virt's GIC addresses and CNTFRQ (tools/qemu-sweet.py).
    dram_size overrides the profile's kernel RAM size (e.g. 0x180000000 on an 8 GB guacamole).
    owned_memory_size emits /chosen/darwinboot-memory-size for the contiguous kernel window."""
    if owned_memory_size is not None and (
            not 0x10000000 <= owned_memory_size <= 0x200000000 or owned_memory_size & 0x1FFFFF):
        raise SystemExit('owned-memory-size must be 256 MiB..8 GiB in 2 MiB steps')
    dev = devices.get(device)
    phys_base, mem_size = (dev['high_phys_base'], dev['high_mem_size']) if highmem else (dev['phys_base'], dev['mem_size'])
    if dram_size is not None:
        mem_size = dram_size
    gicd, gicr, timebase = (QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE) if qemu else (dev['gicd'], dev['gicr'], dev['timebase'])
    fb, fb_size = dev['fb'], dev['fb_size']
    gicd_size, gicr_size = dev['gicd_size'], dev['gicr_size']
    ufs, ufs_size = dev['ufs'], dev['ufs_size']
    vtimer_intid = QEMU_VTIMER_INTID if qemu else dev['vtimer_intid']
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    root_matching = None
    if root_match_uuid:
        root_matching = (
            '<dict><key>IOProviderClass</key><string>IOMedia</string>'
            '<key>IOPropertyMatch</key><dict><key>UUID</key><string>'
            + root_match_uuid.upper() + '</string></dict></dict>')

    # iBoot forwards the sealed system volume's IM4P root hash here; APFS's
    # import_iboot_forwarded_roothash reads /chosen/system-volume-auth-blob.
    system_volume_auth_blob = None
    for candidate in (Path(__file__).resolve().parents[1] / 'build/root-hash/SystemVolume.im4p',
                      Path('build/root-hash/SystemVolume.im4p')):
        if candidate.is_file():
            system_volume_auth_blob = candidate.read_bytes()
            break
    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        *([('darwinboot-memory-size', u64(owned_memory_size))] if owned_memory_size else []),
        ('embedded-panic-log-size', u32(dev.get('panic_size', 0x80000))),
        # ArmDTPlatform::start requires a usable CHRP NVRAM proxy here, and
        # IODTNVRAM picks its handler from these /chosen properties.
        *empty_nvram_proxy([('root-live-fs', '1'),
                             ('allow-root-hash-mismatch', '1')]),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('firmware-version', cstr('sweet-shim')),
        ('system-firmware-version', cstr('sweet-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists (bind mounts /private/preboot/<hex>/usr/standalone/firmware,
        # tolerating a missing directory). Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without
        # it; real A11+ device trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: display-scale (u32, default 0) comes
        # from /chosen, the pixel size from IOMobileFramebuffer. Scale 0 makes
        # SpringBoard's FBSDisplayMode "infxinf@0x" invalid. 1080x2400 @3x =
        # 360x800 points (like iPhone X class devices).
        ('display-scale', u32(2 if ipad_identity else 3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm; without
        # them CFPreferences cannot use kCFPreferencesCurrentHost (no ByHost
        # files), so e.g. DataMigration never records LastSystemVersion and
        # migrates on every boot. Synthetic ECID (not the phone's); chip-id
        # of the A11 that the iPhone X userland expects.
        ('unique-chip-id', struct.pack('<Q', SWEET_ECID)),
        ('chip-id', u32(ipad_chip_id if ipad_identity else 0x8015)),
        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1):
        # MobileGestalt reads these from /chosen, and mobileactivationd's
        # DeviceType then takes its native "Hactivation" branch (state
        # Activated without an activation record; no SEP, no baseband here).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
        *([('amfi-only-platform-code', u32(1))] if ipad_identity else []),
        # iBoot normally writes the root volume UUID here; IOKitBSDInit turns it
        # into the IORegistry "boot-uuid" resource and matches the media whose
        # "boot-uuid-media" property carries the same value.
        *([('root-matching', cstr(root_matching))] if root_matching else []),
        ('root-live-fs', u32(1)),
        ('allow-root-hash-mismatch', u32(1)),
    ] + ([('ephemeral-storage', u32(1))] if (data_ramdisk and not persistent_data) else []),
    [node('memory-map', [ph()] + ([('TrustCache', bytes(16))] if trust_cache else []) +
        ([('PackedRAMDisk' if packed_data_ramdisk else 'RAMDisk', bytes(16))] if data_ramdisk else [])),
        # Local boot policy (kern_csr.c): SIP relaxed like `csrutil disable`
        # (lp-sip0: untrusted kexts, unrestricted FS, task_for_pid, kernel
        # debugger, dtrace, nvram), lp-sip1: unauthenticated (unsealed) root.
        # The iOS shared cache is in no trust cache, so dyld can only map it
        # with CSR_ALLOW_UNRESTRICTED_FS (vm_unix.c shared region checks).
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))]),
        # AppleImage4's pmap_cs init looks this up from /chosen; DarwinBoot has
        # no manifest to export, so publish an empty node instead.
        node('manifest-properties', [ph()])])

    # SWEET SMP: all cores in physical-core order as one homogeneous cluster
    # (sweet: 0-5 A55 + 6-7 A76; guacamole: 0-3 A55 + 4-7 A76). The kernel
    # defaults to one and uses cpus=N to select an initial prefix (N <= cpus).
    # QEMU retains its original single-CPU tree: its MPIDRs differ.
    cpu_nodes = []
    for core in range(1 if qemu else dev['cpus']):
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr(os.environ.get('GUAC_CPU_DEVICE_TYPE', 'cpu'))),
            ('compatible', cstr(dev['cpu_compat']) + cstr('ARM,v8')),
            ('reg', u32(dev['cpu_regs'][core])),  # actual MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0)),  # descriptive; see ml_parse_cpu_topology
            # AppleARMCPU validates cluster-type as the 'E'/'P' value; all cores as
            # one P cluster keeps a single cluster type for the native SMP port.
            *([('cluster-type', u32(0x50)),
               ('reg-private', u64(0)),
               ('l2-access-penalty', u32(0)),
               ('l2-cache-size', u32(0x1000)),
               ('l3-cache-size', u32(0x8000)),
               ('no-aic-ipi-required', u32(1)),
               ('interrupts', u32(0x40 + core, 0, 0x41 + core))]
              if os.environ.get('GUAC_APPLE_CPU_PROPS', '0') == '1' else []),
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),  # distinct per-core L2 nodes in live FDT
            ('l3-cache-id', u32(0)),     # shared L3
            ('state', cstr('running' if core == 0 else 'waiting')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32(dev['cpu_clocks'][core])),
            ('bus-frequency', u32(dev['bus_frequency'])),
            ('memory-frequency', u32(dev['memory_frequency'])),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    if os.environ.get('GUAC_CPU_NODES'):
        cpu_nodes = cpu_nodes[:int(os.environ['GUAC_CPU_NODES'])]

    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0)),
                         *([('max_cpus', u32(len(cpu_nodes)))] if os.environ.get('GUAC_APPLE_CPU_PROPS', '0') == '1' else [])], cpu_nodes)

    # AppleARMCPU validates the CPU configuration against the platform's CPU
    # start conduit; sheng's working ADT publishes /psci, guacamole's did not.
    psci = None if qemu else node('psci', [ph(), ('compatible', cstr('arm,psci-1.0')),
                                            ('method', cstr('smc'))])

    # ArmCpu maps the redistributor through /arm-io/intc (sheng's node name);
    # pe_fiq's /arm-io/gic lookup failing is non-fatal on this port.
    gic = node('intc', [
        ph(),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(vtimer_intid)),
        # GICR frame spacing for the ArmCpu frame walk: the guacamole live FDT
        # (and QEMU virt) both use 0x20000 per core.
        ('redistributor-stride', u64(0x20000)),
        ('reg', u64(gicd - SOC_BASE, gicd_size, gicr - SOC_BASE, gicr_size)),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('sweet-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        # No hardware: watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu and
        # otherwise crashes trying to set the PMU boot stage (customer builds
        # ignore the -wdt_no_pmu boot-arg).
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer.framework counts /arm-io children
        # with this device_type and reads the canvas size from the node
        # (the presented iPhone X size, see SweetFramebuffer.cpp);
        # the kernel's AppleCLCD matches the compatible string.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(1125)),
            ('canvas-height', u32(2436)),
        ])])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(fb, fb_size)),
        ('width', u32(dev['fb_w'])),
        ('height', u32(dev['fb_h'])),
        ('stride', u32(dev['fb_stride'])),
    ])

    # bsd/dev/sweet_ufs.c: the UFS host ABL leaves running (absolute physical reg).
    ufs = node('sweet-ufs', [
        ph(),
        ('compatible', cstr('qcom,ufshc')),
        ('reg', u64(ufs, ufs_size)),
    ])

    # pmap_compute_max_asids requires the node even when no overrides are set.
    # An absent pmap-max-asids property selects XNU's own MAX_ASIDS default.
    # No effaceable storage: keybagd then reads /private/var/keybags/systembag.kb
    # as a plain plist instead of decrypting it with the effaceable key.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])
    # IODTPlatformExpert publishes /options as IODTNVRAM; without the node the
    # platform expert asserts in processTopLevel().
    options = node('options', [ph()])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (D22AP: 2, no Home button; absent = 0 = Home-button device).
    buttons = node('buttons', [
        ph(),
        ('button-names', (cstr('hold') + cstr('voldown') + cstr('volup'))
            if ipad_identity else (cstr('volup') + cstr('voldown') + cstr('hold'))),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    if ipad_identity:
        product = [node('product', [ph()] + ipad_product(dev))]
    else:
        product_props = d22ap_product()
        product = [node('product', [ph()] + product_props)] if product_props else []

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). The board/SOC strings (devices.py) stay for the
    # SweetPlatformExpert matching and for the live FDT audits.
    if ipad_identity:
        identity = [
            ('compatible', cstr('J120AP') + cstr('iPad7,1') + cstr(dev['board_compat']) + cstr(dev['soc_compat'])),
            ('model', cstr('iPad7,1')),
            ('target-type', cstr('J120')),
            ('target-sub-type', cstr('J120AP')),
        ]
    else:
        identity = [
            ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr(dev['board_compat']) + cstr(dev['soc_compat'])),
            ('model', cstr('iPhone10,3')),
            ('target-type', cstr('D22')),
            ('target-sub-type', cstr('D22AP')),
        ]

    pram = node('pram', [ph(), ('reg', u64(dev['panic'], dev.get('panic_size', 0x80000)))])
    # Fixed low DRAM window for the UFS DMA buffers: the kernel-managed range
    # starts above 4 GiB and kmem_alloc_contig can sleep forever there.
    ufs_dma = node('ufs-dma', [ph(), ('reg', u64(dev['ufs_dma'], dev.get('ufs_dma_size', 0x20000)))])
    return node('device-tree', [
        ph(),
        *identity,
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, options, defaults, cpus] + ([psci] if psci is not None else []) + [arm_io, fbcon, buttons, pram, ufs_dma] + product + ([] if qemu else [ufs]))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('out')
    ap.add_argument('--device', default='sweet', choices=sorted(devices.DEVICES),
                    help='hardware profile (tools/devices.py)')
    ap.add_argument('--qemu', action='store_true', help='QEMU virt GIC/CNTFRQ instead of the board')
    ap.add_argument('--highmem', action='store_true', help='kernel RAM from the upper DDR bank (phone default)')
    ap.add_argument('--dram-size', type=lambda s: int(s, 0), default=None,
                    help='override the kernel RAM size, e.g. 0x180000000 for an 8 GB guacamole')
    ap.add_argument('--owned-memory-size', type=lambda s: int(s, 0), default=None,
                    help='DarwinBoot contiguous window in bytes (256 MiB..8 GiB, 2 MiB steps)')
    ap.add_argument('--ipad-identity', action='store_true',
                    help='use the iPad7,1/J120AP identity and /product nodes')
    ap.add_argument('--chip-id', type=lambda s: int(s, 16), default=None,
                    help='/chosen/chip-id for the iPad identity (hex)')
    ap.add_argument('--root-match-volume-uuid', default=None,
                    help='/chosen/root-matching Data volume UUID')
    ap.add_argument('--trust-cache', action='store_true',
                    help='Zeroed TrustCache sidecar handoff placeholder (the loader fills the real range)')
    ap.add_argument('--data-ramdisk', action='store_true', help='Zeroed dense RAMDisk handoff placeholder')
    ap.add_argument('--packed-data-ramdisk', action='store_true', help='Zeroed PackedRAMDisk (DBRDISK1) handoff placeholder')
    ap.add_argument('--persistent-data', action='store_true', help='Data store persists in the userdata window')
    ap.add_argument('--dump', action='store_true')
    args = ap.parse_args()
    if args.dump and os.path.exists(args.out):
        # `mkadt FILE --dump` inspects FILE. Rebuilding first would silently
        # rewrite it with the default device's profile (this once replaced a
        # guacamole ADT with sweet's 1080x2400 sweet-fbcon values).
        blob = open(args.out, 'rb').read()
        adt.dump(adt.parse(blob)[0])
        print(f'{args.out}: {len(blob)} bytes (existing file, not rebuilt)', file=sys.stderr)
        sys.exit(0)
    root = build(qemu=args.qemu, highmem=args.highmem, device=args.device,
                 dram_size=args.dram_size, owned_memory_size=args.owned_memory_size,
                 ipad_identity=args.ipad_identity,
                 root_match_uuid=args.root_match_volume_uuid,
                 ipad_chip_id=args.chip_id or 0x207,
                 trust_cache=args.trust_cache,
                 data_ramdisk=args.data_ramdisk or args.packed_data_ramdisk,
                 packed_data_ramdisk=args.packed_data_ramdisk,
                 persistent_data=args.persistent_data)
    blob = adt.serialize(root)
    open(args.out, 'wb').write(blob)
    if args.dump:
        adt.dump(adt.parse(blob)[0])
    print(f'{args.out}: {len(blob)} bytes ({args.device})', file=sys.stderr)
