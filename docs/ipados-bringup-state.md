# iPadOS 17.7.11 / XNU 10063 bring-up state (guacamole)

Last updated: 2026-10-26 (docs-layout rootfs + console scroll verified on
hardware; the `IOSleep(100)` experiment observed: the init thread still never
runs — the timeout wake is the missing piece).

## What already works

1. UEFI/DarwinBoot hand-off, kernel entry, bsd_init, setconf.
2. `sweet_ufs` reads the SHNGROOT window correctly (host image and device
   bytes match; NXSB, checkpoint area and volume superblocks verified).
3. The root window is published as a real `IOMedia`:
   - `drivers/GuacUfsMedia/GuacUfsMedia.cpp` subclasses
     `IOBlockStorageDevice` using absolute bindings to the target
     kernelcache (`scripts/data/native-storage-bindings.S`).
   - The vtable was identified structurally (unique 176/176 kernel-export
     match). The metaclass, ctor, dtor and every used method are bound to
     their real entry points.
   - IOStorageFamily publishes `disk0` (whole, 0x16A400000 bytes, 4096).
4. `/chosen/root-matching` selects the APFS volume media (`disk1s1`,
   major 1 minor 2) exactly like the sheng reference.
5. APFS opens the volume, reads the container, and the **live root mounts**
   (`vfs_mountroot` returns 0). The post-mount `FSIOC_KERNEL_ROOTAUTH`
   ioctl returns EAUTH; `guac_skip_rootauth=1` lets boot continue.
6. Boot-stage markers reached so far: `set_rootvnode` (0x28), end of the
   auth block (0x29=41), devfs/siginit (0x49=73), end of bsd_init (0x2A=42).

## Where it stops (2026-10-26, after the platform timer fix)

The former fork blocker is gone. `ArmCpu` enabled and dispatched Apple/QEMU's
virtual-timer INTID (27); SM8150 integrates the arch-timer outputs at
17/18/19/16, so the deadline wake never fired and the boot thread parked in
`machine_switch_context` forever. `ArmCpu` now reads `virtual-timer-intid`
from the ADT intc node (19 here; 27 remains the QEMU/sheng fallback) and the
100 ms sleep wakes: the boot continues past `bsd_utaskbootstrap`, `bsdinit_task`
runs, and `load_init_program` execs launchd (observed on hardware).

The boot now stops later, in userspace bring-up:

1. The IPSW trust caches are IM4P containers; DarwinBoot refused them as
   `EFI_COMPROMISED_DATA`. `scripts/pack-trust-bundle.py` now produces the
   module-table bundle `kernel.macho.trustcache` (byte-identical to UniXNU's
   original-static-system bundle) and the kernel builds the trust-cache
   runtime (`CONFIG_XNU_TRUST_CACHE_RUNTIME=y`).
2. AMFI's `ConfigurationSettings` then panics with `AMFI: No PMGR?` until an
   `ApplePMGRNub` service exists. `drivers/GuacPMGR/GuacPMGR.cpp` publishes a
   real provider-less instance (the original readAon/writeAon helpers return 0
   when `nub+0x88` is NULL), wired through `XNU_SHENG_PMGR`.
3. The non-sleepable RW-lock invariant (`lck_rw_lock_shared` with preemption
   enabled) is guarded at the two remaining raw sites: the PPL trust-cache
   runtime lock and the code-signing profile rbtree lock in `pmap.c`
   (`pmap_lock()` already disables preemption itself under `!XNU_MONITOR`).

Next walls after that are the launchd exec and the userland (SystemOS/Data
mounts, display).

## Root authentication: what sheng does

Per <https://unixnu.com/zh/concepts/security>:
- UniXNU keeps Apple's security components and only provides the inputs
  they check (trust cache, chip seed, DT identity, PMGR, root policy).
- The root policy is the owner-signed VerifiedRoot package
  (`XNU_VERIFIED_ROOT_PROBE`, `XNU_OWNER_ROOT_AUDIT`,
  `XNU_OWNER_ROOT_TRUST`, `kits/VerifiedRoot`), plus
  `offline-owner-*` patches that publish `boot-manifest-hash`.
- Their own audits record that the unsealed restore OS root is refused by
  Apple's components and that they never booted iPadOS userspace from it.
  There is **no ADT switch** that disables APFS's root snapshot/auth check.

Our restore image (044-76040) is unsealed and has `num_snapshots = 0`, so
APFS's root-snapshot path cannot succeed. The only self-contained way to
the live fs is the kext patch in `scripts/patch-apfs-rootauth.py`:
- `root_hash_authentication_required` returns "not required"
  (`mov w20,#1` -> `#0`).
- `apfs_find_named_root_snapshot_xid` returns ENOENT instead of EAUTH when
  the forwarded payload is missing (`mov w19,#81` -> `#2`).
Both are applied to the prelinked kernel in `scripts/build-ipados.sh` and
are idempotent. This is a deliberate deviation from the official model.

## Build / patch pitfalls (important)

- The work tree is `out/guacamole/tree/.cache/xnu-work`, a clone of
  `refer/xnu` at `d8b8029` (xnu-10063.141.1).
- Patch order is **not** filename order: unixnu-named patches assume they
  are applied before the numeric `0xxx/09xx` patches. The tree's
  `.guacamole-applied` stamp now lists every patch that is already applied;
  do not delete it, or the loop re-applies in the wrong order and fails.
- Three superseded patches are intentionally absent: `0912-...kext-start`,
  `0913-...kext-init`, `native-dt-panic-region.patch`.
- The APFS kext bytes are patched after `emit_prelink.py`; if the target
  kernelcache changes, re-derive the vtable and the two code sites.
- The text console scrolls since 0986 (`vcattach` allocates the paint buffer
  once the VM is up and replays the early output) — verified on hardware
  2026-10-26: one screen shows the whole boot log through the apfs mount. The
  `sweet_stage_*` framebuffer fields are
  fixed position by design (they do not scroll), so keep using them for
  boot-stage markers and fault dumps:
  - `sweet_stage_slot(slot, value)` draws slot 0..3 at framebuffer rows
    1850/1950/2050/2150. Slot 0 = boot thread, slot 1 = `bsdinit_task`,
    slot 2 = exec.
  - `sweet_stage_field()` still draws the older four-line block (2400..).
  - The panic screen does not preserve those fields; do not rely on panic
    text after a full screen either.

## Stage map currently in the tree

| value | point |
|------:|-------|
| 10 | vfs_mountroot returned 0 |
| 20/30 | before/after IOSecureBSDRoot |
| 40 | set_rootvnode done |
| 41 | root-auth ioctl block done |
| 42 | end of bsd_init |
| 70..75 | imageboot/devfs/siginit/utaskbootstrap/pal_kernel_announce |
| 80..86 | bsd_utaskbootstrap: before cloneproc, after, proc_find, zalloc, signalend, ipc_task_enable, clear_return_wait |
| 90..97 | cloneproc: before forkproc, before/after fork_create_child, before pinsertchild, inside fork_create_child (task_create, proc_set_task, main_thread_create, final yield) |
| 43/44/45/46/47/50 (slot 1) | bsdinit_task: entry, current_proc, process_name, vm_init_before_launchd, bsd_do_post, before load_init_program |
| 60/61/80/90 (slot 2) | load_init_program loop and exec failure |

## Rootfs per the UniXNU userdata docs (2026-10-26)

The userdata rootfs now follows <https://unixnu.com/zh/guide/userdata>: the
image holds the UNCHANGED `OS` and `Cryptex1,SystemOS` disk images of the
IPSW — whole disk images, the OS one including its own GPT — behind the
SHNGROOT manifest, instead of the previously flashed image that stripped each
disk image down to its Apple APFS partition.

- Builder: `scripts/build-userdata-root.py` (Linux; reads the DMGs with
  `scripts/local-linux/udif.py`, resolves IPSW members and verifies them
  against board.toml `[ipsw.sha256]` through the upstream
  `unixnu/scripts/unixnu/ipsw.py` Ipsw helper, refuses images larger than the
  partition, writes `userdata-root.json` with the documented fields). The old
  APFS-slice builder `build-userdata-shngroot.py` was removed.
- Built image: `out/guacamole/userdata/userdata-root.img` — 9,382,658,048
  bytes (8.74 GiB) in the 21.76 GiB partition: manifest at 0, OS window
  6,077,580,800 bytes at 0x100000, SystemOS window 3,303,014,400 bytes at
  0x16a600000. The SystemOS window hash matches the IPSW pin; the OS window
  begins with the disk image's own protective MBR + GPT and the APFS partition
  sits at window offset 0x4400 (LBA 34).
- The APFS partition bytes are IDENTICAL to the previously flashed slice
  (verified byte for byte against `out/guacamole/userdata/userdata-shngroot.img`),
  so the mounted volume UUID is still `04769D26-2CA0-4825-B443-E2E5F88300F2`
  and no ADT/boot-arg change is needed.
- Kernel side needs nothing: sweet_ufs only reads the manifest at partition
  block 0 and serves the window bytes as-is; the prelinked IOStorageFamily
  carries `IOGUIDPartitionScheme` with `IOPropertyMatch {"Whole"=true}`,
  probe 0xfa0 and `Content Mask GUID_partition_scheme` (verified in the
  `__PRELINK_INFO` of `out/ipsw/kernelcache.decompressed`), so the scheme cuts
  the APFS partition out of the whole-image window and apfs claims exactly the
  bytes it already mounted in the live-root boot.
- `rd=` stays absent (UUID root-matching); `rd=ufsroot0`/`rd=disk0` would now
  resolve to the whole GPT media and fail.
- Data volume: the docs' Data ramdisk seed (`--data-ramdisk`, grown with
  `scripts/seed-data-grow.py`) still needs macOS `hdiutil`/`diskutil` and is
  not wired yet; the current boot does not reach it (process 1 never runs).

The phone runs this image since 2026-10-26 (`fastboot flash userdata` of the
9,382,658,048-byte image; the old slice image remains as
`out/guacamole/userdata/userdata-shngroot.img`). The boot log verified the
full docs chain on hardware: `SWEET UFS: "userdata" on LUN 0` →
`IOService:/GuacUfsMedia/IOBlockStorageDriver/UniXNU Guacamole UFS root
Media/IOGUIDPartitionScheme/…/AppleFSContainer…/DarwinUpdate21H461_J120AP…` →
`BSD root: disk1s1, major 1, minor 3` → `apfs_vfsop_mountroot:2955: mountroot
called!` → container `xid 418` opened, checkpoint search, volume mount →
`handle_mount:817: disk1s1 vol-uuid: 04769D26-2CA0-4825-B443-E2E5F88300F2 …
block count: 1483776 (unencrypted; flags: 0x1)`. The volume UUID was also
verified in the built image: the volume superblock (APSB, obj oid 0x402,
xid 0x1a2) carries it raw at offset +0xd0.

## Useful commands

```sh
./scripts/build-local-linux.sh                     # kernel + prelink + payload + image
python3 scripts/build-userdata-root.py \
  --ipsw '/home/invalidname/下载/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw'   # docs layout userdata
fastboot boot out/guacamole/boot/boot-32-ipados-local.img
python3 scripts/patch-apfs-rootauth.py <prelinked.macho>   # idempotent
```

Boot args currently relevant: `root-live-fs=1`,
`allow-root-hash-mismatch=1`, `guac_skip_rootauth=1`, `cpus=1` (last test),
`rd=` absent (UUID root matching), `boot-uuid=04769D26-2CA0-4825-B443-E2E5F88300F2`.

## Next steps

1. ~~Observe the `IOSleep(100)` experiment~~ Done 2026-10-26 (session log 7).
2. ~~Port the platform timer/GIC pieces~~ Done 2026-10-26 (session log 8):
   the virtual-timer INTID now comes from the ADT (`virtual-timer-intid`, 19
   on SM8150) and the deadline wake works; the clone blocker is gone.
3. Bring up the userspace entry: the trust bundle (session log 9), the AMFI
   PMGR nub (session log 10) and the PMGR/lock guards (session log 11) are in
   the current image; the next boot shows the next wall (launchd exec,
   SystemOS/Data mounts, display).
4. After process 1 runs: OS services (SystemOS/Data mounts) and the display
   path (`GopFramebuffer` + `NativeLegacyDisplay`, Aloha GOP) per
   <https://unixnu.com/zh/porting/display>.

## Restoring this exact source state

The cumulative diff of the working tree against `refer/xnu` commit
`d8b80295118ef25ac3a784134bcf95cd8e88109f` is stored as
`patches/xnu/zzz-current-state.patch` (~496 KiB). It applies to a clean
checkout in one step and contains every source change of this session,
including the direct edits that are not in the numbered patch files.

Restore procedure:

```sh
git -C out/guacamole/tree/refer/xnu worktree add ../../.cache/xnu-work2 d8b8029
git -C out/guacamole/tree/.cache/xnu-work2 apply <repo>/patches/xnu/zzz-current-state.patch
# then either point the build at xnu-work2, or move patches/xnu aside and
# let the build script skip the numbered series, or pre-populate
# .cache/xnu-work2/.guacamole-applied with every patch stamp.
```

The authoritative working tree is also committed in its own git repo
(`out/guacamole/tree/.cache/xnu-work`, HEAD `eef255a` at the time of writing;
the tree HEAD advances with every session, and `zzz-current-state.patch` is
regenerated from it, currently ~517 KiB). Do not delete it before the
cumulative patch has been re-applied somewhere.

## Session log: last experiments

1. `cpus=1` + original `thread_yield_internal(1)`: boot thread stops at
   stage 0x61 (97), init thread never reaches 43 -> the yield waits for a
   thread that never becomes runnable.
2. `IOSleep(100)` instead of the yield: built and booted, but the result
   was not observed before shutdown. Next session should read the field
   rows (slot 1) from this image.
3. If slot 1 still shows nothing, port the platform pieces from the
   reference UniXNU-sheng sources (`ArmDTPlatform`, `ArmCpu` timer/GIC/IPI,
   SMMU) as the docs require, instead of extending the 8796-era patches.
4. 2026-10-26, rootfs: the userdata image was rebuilt per the UniXNU docs —
   whole `OS` + `SystemOS` disk images behind the SHNGROOT manifest
   (`scripts/build-userdata-root.py`; the old APFS-slice builder was
   removed). The decoded OS window matches the previously flashed slice byte
   for byte from the APFS partition on; the new bytes only prepend the disk
   image's own GPT, which the prelinked `IOGUIDPartitionScheme` cuts out
   (personality verified in the kernelcache `__PRELINK_INFO`). Needs a
   `fastboot flash userdata` before the next boot.
5. 2026-10-26, console: the console overwrote its last row because the only
   `gc_enable(TRUE)` ran before the VM was up (paint buffer allocation
   skipped, `gc_scroll_up` early-returns on `!gc_buffer_size`), and every
   post-VM path (`vcattach` with `gc_graphics_boot`, the stale-`gc_acquired`
   acquire) was suppressed. 0986 makes `vcattach` allocate the buffer once
   the VM is up and replay the messages printed so far, restoring the stock
   scroll path. Built into `boot-32-ipados-local.img`; verified on hardware
   (entry 6).
6. 2026-10-26, hardware boot of the docs-layout rootfs + 0986 (photo
   IMG_20261026_114912): the whole-image chain verified end to end —
   `IOGUIDPartitionScheme` cut the APFS partition out of the OS window
   (`disk0s1`, block size 4096, block count 1,483,776 = the partition's exact
   byte count), apfs opened the container (`xid 418` = 0x1a2, matching the
   superblock bytes), the patched snapshot lookup returned ENOENT (2) and the
   live root mounted; `BSD root: disk1s1, major 1, minor 3` with
   `vol-uuid 04769D26-2CA0-4825-B443-E2E5F88300F2` (verified raw at APSB+0xd0
   in the built image). The console scrolled: the entire boot log was on one
   screen.
7. 2026-10-26, `IOSleep(100)` experiment observed (same photo): stage slot 0
   = 0x61 (97), slot 1 never drawn. The boot thread's parked stack (heartbeat
   stack-walk rows 2400..2760) symbolicates as `fork_create_child+0x190` →
   `delay_for_interval+0x8c` → `thread_block_reason+0x100` →
   `thread_invoke+0x4ac` → `machine_switch_context`. The heartbeat at row
   2900 read 0x8e8 = 2280 beats ≈ 38 minutes wedged while the heartbeat
   thread itself kept running. Cross-thread wakeups work (the IOKit root wait
   completed); the timer-interrupt/deadline wake does not — that is the
   blocker to port.
8. 2026-10-26, platform timer (commit `bada65e`): the cut wire was the
   virtual-timer PPI INTID. `ArmCpu` enabled/dispatched Apple/QEMU's INTID 27
   and dispatched only 27, while SM8150 wires the arch-timer outputs to
   17/18/19/16 — the OnePlus downstream `sm8150.dtsi` and the phone's own FDT
   both say `<1 1>, <1 2>, <1 3>, <1 0>` (CNTV = INTID 19). `ArmCpu` now reads
   `virtual-timer-intid` from the ADT intc node (27 stays the QEMU/sheng
   default), the ADT carries `redistributor-stride` 0x20000, and slot 3 draws
   the timer count / GIC-ready / registered / INTID state. Hardware boot:
   the panic went away and the boot advanced past `bsd_utaskbootstrap`.
   Hardware readout of slot 3 after the fix: `0x0133…` with the low digits
   rising — INTID 19, GIC ready and CPU registered all set, timer interrupts
   counted — so the deadline wake is proven on the phone. The media-scan field
   shows 4 media, `disk0`, 6,077,563,392 bytes (storage chain healthy). The
   next walls were the userspace ones (entries 9-11); with those fixed the
   boot now reaches the launchd/libignition userspace bootstrap and waits
   there (no panic; last log line mentions `libignition`).
9. 2026-10-26, trust bundle: the IPSW trust caches are IM4P/DER containers
   (`trst` OS, `trcs` Cryptex1,SystemOS); DarwinBoot's ValidTrustCaches
   requires the module-table bundle, so the raw files were refused with
   `EFI_COMPROMISED_DATA`. `scripts/pack-trust-bundle.py` unpacks each IM4P,
   validates the loader rules (magic 2, 24-byte entries, ascending cdhashes,
   type 1..4) and concatenates them: 61,356 bytes, sha256 `7416ca4f…`,
   byte-identical to UniXNU's original-static-system bundle. The ADT gets the
   zeroed `/chosen/memory-map/TrustCache` placeholder (`mkadt --trust-cache`),
   the payload carries the bundle as `kernel.macho.trustcache`, and
   `CONFIG_XNU_TRUST_CACHE_RUNTIME=y` builds the kernel-side runtime (commit
   `e99d35b`). Bootloader accepted it.
10. 2026-10-26, AMFI PMGR (commit `4eaa0e0`): the next panic was AMFI's
    `ConfigurationSettings.cpp:235` `AMFI: No PMGR?`. Disassembly of the
    prelinked AMFI code shows `serviceMatching("ApplePMGRNub")` →
    `waitForMatchingService(…, 30 s)` → `safeMetaCast(ApplePMGRNub)`, then
    `readAon() | 4` → `writeAon()`; the original helpers read the provider at
    `nub+0x88` and return 0 when it is NULL. `drivers/GuacPMGR/GuacPMGR.cpp`
    therefore allocates the real (prelinked) ApplePMGRNub by name, inits,
    attaches to the platform expert and registers it; `XNU_SHENG_PMGR=y`
    selects it (bsd_init calls `sheng_pmgr_run` before calendar init).
11. 2026-10-26, lock invariants (commit `dfb5a14` + 0988/0989): two panics
    traced to `lck_rw_lock_shared` taking non-sleepable locks with preemption
    enabled: `ppl_trust_cache_rt_lock` and `pmap_cs_profiles_rbtree_lock`
    (both run through the PPL entry on Apple silicon, which is what disables
    preemption). All six raw lock sites in `pmap.c` now call
    `disable_preemption()/enable_preemption()`; `pmap_lock()` already does it
    itself under `!XNU_MONITOR`. The panic path also draws the panicking
    thread's 12-frame backtrace once (0988) after clearing the strip below the
    console, so a photo carries the whole trail.
12. 2026-10-26, documented owner-root chain on Linux: the rootfs tracking
    flow the UniXNU docs describe (owner signs the root policy; the kernel
    accepts a readonly root only on owner-verified block storage) is now
    buildable without macOS:
    - `scripts/build-owner-policy-linux.py` replaces the hdiutil/diskutil
      attach with the UDIF decoder and pins the exact inputs (OS
      6077580800/04769D26…/e5634d04…, SystemOS 3303014400/8846FE78…/
      b4908cde…), hashes every 4 KiB block, signs the VROOT01 envelope with
      the RSA-3072 owner key (OpenSSL, generated on first use) and re-verifies
      the finished manifest. Both policies PASS: 1483785 and 806400 blocks,
      OS tail 1536 bytes.
    - `scripts/pack-verified-root.py` packs both envelopes plus the shared
      anchor into `build/verified-root-xnu/libVerifiedRootProbe.a`
      (73287008 manifest bytes) through the kernel compile recipe, and the
      build links it with `-force_load` when `XNU_VERIFIED_ROOT_PROBE=1`.
    - `NativeUfs` is now built with `NATIVE_VERIFIED_BLOCK=1` +
      `NATIVE_VERIFIED_BLOCK_MULTI_POLICY=1`: it selects the single
      capacity-matching policy per window, protects the manifest/key in
      read-only kernel memory, verifies every read block against the signed
      table, and exports the UFS variant of
      `native_verified_block_root_authorized` (IOMedia → UUID → provider
      chain → `native_ufs_block_authenticates`) for bsd_init.
    - defconfig: `XNU_VERIFIED_ROOT_PROBE/AUDIT/TRUST=y` (the crypto
      prerequisites, source corecrypto14 and prelinked bootstrap, were
      already on). The bootstrap pass compiles the owner decision out (it
      links before the archives exist) and the full pass recompiles and
      links it; the Virtio-only gate now accepts the UFS transport.
    - Verified artifacts: the prelinked kernel is 124391424 bytes and
      contains exactly two VROOT01 headers (6077580800/1483785 and
      3303014400/806400) plus `_native_ufs_block_authenticates`,
      `_native_verified_block_root_authorized`, `native_verified_root_probe`
      and `_verified_root_policy_count`; bsd_init.o is stamped 1:1:1
      (probe/audit/trust) and the kernel carries the audit/trust globals.
      The 160 MiB payload pushes the boot image to 173006848 bytes, so
      `board.toml` moves the ramdisk field above the DBI MMIO window to
      physical 0x84000000-0x8e400000 (still below the 0x9c000000 DarwinBoot
      window).
    Runtime boot with the signed policies is the next verification step
    (hardware).
13. 2026-10-26, owner Data seed and the USB debug port:
    - The owner Data seed is built on a macOS runner
      (`scripts/build-data-seed-macos.sh` + `.github/workflows` seed job), packed
      as DBRDISK1 (`scripts/packed_ramdisk.py`), shipped by the payload
      (`--data-ramdisk data-seed.dbr`, ADT `PackedRAMDisk` + `persistent_data`)
      and mounted read-write at `/private/var` by `native_owner_data_prepare`.
      Confirmed on hardware: `GUAC-MEDIA: disk4 uuid 82AB0DD5-…` and the APFS
      mount of the seed before the first exec.
    - Kernel boot arguments are trimmed to the purposeful set (console/APFS/
      board/userland), removing the historical `sweet*`, duplicate `debug=`,
      and the obsolete `guac_skip_rootauth`.
    - The verified-root chain still owns the storage path: ShengUfs publishes
      both windows, every read authenticates, the owner decision and the union
      SystemOS mount succeed; only the dyld shared-cache mapping into the
      shared region remains before the userland.
    - USB debug: `drivers/QcomXnuUsbDebug` is vendored (reference module from
      xiaoleGun/QcomXnuUsbDebug) with a new `soc/Sm8150.h` profile
      (DWC3 0x0a600000, GCC 0x00100000 usb3_prim 0xf010-0xf054, GDSCR 0xf004,
      QSCRATCH 0xf8810, apps-SMMU 0x15000000, SID 0x0 unverified,
      PSCI SMC reset unqualified). The archive builds against the 10063
      compile record (`libQcomXnuUsbDebug.a`). Console/worker patches applied
      to the tree with the following misses to finish by hand:
      `osfmk/kern/printf.c` consdebug_putc/consdebug_putc_unbuffered hunks and
      one `serial_console.c` hunk (`.rej` files kept in the work tree).
      Remaining: per-file `-DXNU_QCOM_USB_DEBUG=1`, the include path, the
      archive force-load, `qcom-usb-debug=1`, host `collect-xnu-usb.py`.

## 2026-10-27: QEMU/phone dual-config, SystemOS union in QEMU, dyld and ignition state

### One source tree, two kernel configs

`board.toml` names one defconfig; the phone builds `guacamole_10063_defconfig`
(ARM_DT platform, `NATIVE_UFS`, `SHENG_PMGR`, `NATIVE_AES`, the
`OWNER_ROOT_*`/`OWNER_CACHE_*`/`OWNER_UNION_SYSTEMOS` verified-root chain).
The QEMU kernel is the same tree and the same iPad7 kernelcache built with
`guacamole_qemu_defconfig` (ARMVIRT platform, `NATIVE_VIRTIO_BLOCK/CONSOLE`,
`GUAC_SYSTEMOS_MOUNT`, no owner chain). The two configs now both set
`CONFIG_XNU_NATIVE_AES=y`: without it the original AppleImage4 never starts,
libignition blocks on the ignition blob and launchd never reaches dyld.

Build the QEMU kernel by pointing `board.toml` at the qemu defconfig and
running `scripts/build-local-linux.sh`; the payload step then fails with
`the native UFS root kernel lacks the linked sheng PMGR integration`, which is
expected — the prelinked kernel is already produced and is copied to
`out/guacamole/qemu-kernel.macho`. Restore the defconfig afterwards.

### QEMU SystemOS mount (`patches/xnu/zzzzz-guac-qemu-systemos-union.patch`)

The original QEMU helper mounted the cryptex at `/System/Cryptexes/OS` after
`mkdir` calls. The root volume is read-only, so both `mkdir` calls fail while
their `int32_t` retvals stay zero (`GUAC-SYSOS: mkdir cryptexes -> 0/0`) and the
`namei` of the mountpoint returns ENOENT (`GUAC-MNT: namei -> 2`). The patch
mounts the SystemOS volume read-only with `MNT_UNION` at `/` instead — the same
shape as the phone's owner union path — drops the exec environment, publishes
`commpage_update_dyld_flags(0)` (the owner path publishes the dyld state) and
keeps bounded GUAC-SYSOS/GUAC-MNT probes. The mount then returns 0,
`/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64` resolves, and
`/usr/lib/libSystem.B.dylib` correctly does not exist outside the cache.

The same patch makes the phone-side `dyld |= 8` (no-ignition) selection
optional through `owner-dyld-no-ignition=<0|1>`; the per-board boot args in
`board.toml` now carry `ignition_force_dylib_root=rootfs` (string, maps to
`force dylib root: 0x3`) and `owner-dyld-no-ignition=0`.

### State reached on both sides

- Phone: union mount of the SystemOS at `/`, `ignition sequence complete`,
  `dylib_cache: opened shared cache directory`, shared-region mapping
  (`GUAC-SR: final first=0x1e134000`, `check_np <- 0x1e134000`), 4 KiB code
  signature preparation over the cache, then `brk #1` in mapped cache code
  with `x0=0xffffffffffffffed` (ENODEV) and initproc panic (signal namespace).
- QEMU: the same union, ignition and cache exposure; dyld loads the cache (the
  same `GUAC-CS` preparation, including the 114 MB cache), launchd runs in its
  own text and no panic is observed at the 300 s observer deadline.

Diagnostics used: `GUAC-SR` (shared region), `GUAC-CS` (4 KiB signature prep),
`GUAC-MNT`/`GUAC-SYSOS` (QEMU mount path). The QEMU runner gained
`--ramfb --capture-screen` (QMP screendump to `screen.png`; the build only
ships the `none` display backend) and `--vnc ADDR` (VNC backend verified) so the
guest console image can be inspected without a phone.
