// SPDX-License-Identifier: BSD-2-Clause-Patent
// Guacamole: the UFS root window as an IOBlockStorageDevice. IOStorageFamily
// turns this into an IOMedia, which is what APFS's mount root requires.
// The IOBlockStorageDevice half is bound to the target kernelcache's own
// implementation (see scripts/data/native-ufs-original-bindings.S); reads are
// served by sweet_ufs's working UFS transport.
#include <IOKit/IOLib.h>
#include <IOKit/IOPlatformExpert.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/storage/IOBlockStorageDevice.h>
#include <kern/debug.h>
#include <kern/thread.h>
#include <vm/vm_kern.h>
#include <vm/vm_map.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSIterator.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSString.h>
#include <IOKit/IOBSD.h>
#if defined(GUAC_MEDIA_VERIFIED_ROOT) && GUAC_MEDIA_VERIFIED_ROOT
#include "verified_root.h"
extern "C" {
extern const struct vr_crypto vr_xnu_crypto;
extern const uint8_t verified_root_manifest[], verified_root_key[];
extern const uint64_t verified_root_manifest_bytes;
extern uint64_t sweet_ufs_media_bytes(void);
}
#endif


extern "C" int sweet_ufs_media_read(uint64_t off, void *dst, uint64_t len);
extern "C" void sweet_stage_field(uint64_t, uint64_t, uint64_t, uint64_t);
uint64_t guac_media_report[3] = {
	0xdead0000ULL, 0, 0
};
static char guac_media_bsdname[32];
static uint32_t guac_media_bdev;
volatile int guac_media_stage;

#if defined(GUAC_MEDIA_VERIFIED_ROOT) && GUAC_MEDIA_VERIFIED_ROOT
/* Owner-signed block policy for this window: the manifest and key are copied
 * out of the immutable kernel image into read-only kernel memory once, then
 * every read is checked against the signed per-block table. */
static struct vr_view guac_verified;
static vm_offset_t guac_verified_storage;
static vm_size_t guac_verified_bytes;
volatile uint64_t guac_verified_blocks_read;
volatile uint64_t guac_verified_failures;
static bool guac_verified_ready;

static bool
guac_media_verified_init(void)
{
	if (guac_verified.hashes != nullptr) {
		return true;
	}
	uint64_t manifestBytes = verified_root_manifest_bytes;
	vm_size_t tableBytes = round_page((vm_size_t)manifestBytes);
	if (tableBytes < manifestBytes || tableBytes > UINT64_MAX - 4096) {
		return false;
	}
	guac_verified_bytes = tableBytes + 4096;
	if (kmem_alloc(kernel_map, &guac_verified_storage, guac_verified_bytes,
	    (kma_flags_t)(KMA_KOBJECT | KMA_DATA), VM_KERN_MEMORY_SECURITY) != KERN_SUCCESS) {
		return false;
	}
	memcpy((void *)guac_verified_storage, verified_root_manifest, manifestBytes);
	uint8_t *key = (uint8_t *)(guac_verified_storage + tableBytes);
	memcpy(key, verified_root_key, VR_KEY_BYTES);
	if (vm_map_protect(kernel_map, guac_verified_storage,
	    guac_verified_storage + guac_verified_bytes, VM_PROT_READ, TRUE) != KERN_SUCCESS) {
		return false;
	}
	if (vr_open((const uint8_t *)guac_verified_storage, manifestBytes, key, 1,
	    &vr_xnu_crypto, &guac_verified) != VR_OK) {
		return false;
	}
	/* The signed envelope describes exactly the window this device serves. */
	return guac_verified.image_bytes == sweet_ufs_media_bytes();
}
#endif


extern "C" const char *
guac_media_name(void)
{
	return guac_media_bsdname[0] ? guac_media_bsdname : nullptr;
}

extern "C" int
guac_media_rootdev(const char *name, uint32_t *dev)
{
	if (name == nullptr || dev == nullptr || guac_media_bdev == 0) {
		return -1;
	}
	if (strcmp(name, guac_media_bsdname) != 0) {
		return -1;
	}
	*dev = guac_media_bdev;
	return 0;
}
extern "C" uint64_t sweet_ufs_media_bytes(void);
extern "C" uint32_t sweet_ufs_media_block_size(void);

class GuacUfsMedia : public IOBlockStorageDevice {
	OSDeclareDefaultStructors(GuacUfsMedia)
public:
	bool start(IOService *provider) override;
	IOReturn doAsyncReadWrite(IOMemoryDescriptor *buffer, UInt64 block,
	    UInt64 count, IOStorageAttributes *attributes,
	    IOStorageCompletion *completion) override;
	IOReturn doEjectMedia() override;
	IOReturn doFormatMedia(UInt64 byteCapacity) override;
	UInt32 doGetFormatCapacities(UInt64 *capacities,
	    UInt32 capacitiesMaxCount) const override;
	char *getVendorString() override;
	char *getProductString() override;
	char *getRevisionString() override;
	char *getAdditionalDeviceInfoString() override;
	IOReturn reportBlockSize(UInt64 *blockSize) override;
	IOReturn reportEjectability(bool *isEjectable) override;
	IOReturn reportMaxValidBlock(UInt64 *maxBlock) override;
	IOReturn reportMediaState(bool *mediaPresent, bool *changedState) override;
	IOReturn reportRemovability(bool *isRemovable) override;
	IOReturn reportWriteProtection(bool *isWriteProtected) override;
};

OSDefineMetaClassAndStructors(GuacUfsMedia, IOBlockStorageDevice)

bool
GuacUfsMedia::start(IOService *provider)
{
	if (!IOBlockStorageDevice::start(provider)) {
		return false;
	}
	return true;
}

IOReturn
GuacUfsMedia::doAsyncReadWrite(IOMemoryDescriptor *buffer, UInt64 block,
    UInt64 count, IOStorageAttributes *, IOStorageCompletion *completion)
{
	uint64_t bytes = sweet_ufs_media_bytes();
	uint64_t bsize = sweet_ufs_media_block_size();
	uint64_t off = block * 512;
	uint64_t len = count * 512;
	uint64_t transferred = 0;
	IOReturn result = kIOReturnSuccess;

	if (buffer == nullptr || completion == nullptr || count == 0 ||
	    off > bytes || len > bytes - off) {
		return kIOReturnBadArgument;
	}
	if (buffer->getDirection() != kIODirectionIn) {
		return kIOReturnNotWritable;
	}
	if (buffer->getLength() < len) {
		return kIOReturnBadArgument;
	}
#if defined(GUAC_MEDIA_VERIFIED_ROOT) && GUAC_MEDIA_VERIFIED_ROOT
	/*
	 * Owner-verified path: read the aligned block span once per policy block,
	 * authenticate it against the signed table, then expose only that CPU-owned
	 * copy. A failed check returns no data and never reaches the filesystem.
	 */
	if (guac_verified_ready) {
		uint64_t stop = off + len;
		uint64_t aligned = off & ~((uint64_t)VR_BLOCK_BYTES - 1);
		void *blockBuffer = IOMalloc(VR_BLOCK_BYTES);
		if (blockBuffer == nullptr) {
			result = kIOReturnNoMemory;
		}
		for (uint64_t pos = aligned; blockBuffer != nullptr && pos < stop; ) {
			uint64_t readEnd = pos + VR_BLOCK_BYTES;
			if (readEnd > bytes) {
				readEnd = bytes;
			}
			uint32_t want = (uint32_t)(readEnd - pos);
			if (sweet_ufs_media_read(pos, blockBuffer, want) != 0) {
				result = kIOReturnIOError;
				break;
			}
			if (vr_verify_block(&guac_verified, pos / VR_BLOCK_BYTES, blockBuffer,
			    want, &vr_xnu_crypto) != VR_OK) {
				__atomic_fetch_add(&guac_verified_failures, 1, __ATOMIC_RELAXED);
				result = kIOReturnNotPrivileged;
				break;
			}
			__atomic_fetch_add(&guac_verified_blocks_read, 1, __ATOMIC_RELAXED);
			uint64_t copyStart = pos > off ? pos : off;
			uint64_t copyEnd = readEnd < stop ? readEnd : stop;
			if (copyEnd > copyStart &&
			    buffer->writeBytes(copyStart - off,
			    (uint8_t *)blockBuffer + (copyStart - pos),
			    (vm_size_t)(copyEnd - copyStart)) != copyEnd - copyStart) {
				result = kIOReturnUnderrun;
				break;
			}
			if (copyEnd > copyStart) {
				transferred = copyEnd - off;
			}
			pos = readEnd;
		}
		if (blockBuffer != nullptr) {
			IOFree(blockBuffer, VR_BLOCK_BYTES);
		}
	} else {
		/* Same traffic as the plain path while no signed policy covers the window. */
		uint64_t chunk = (uint64_t)bsize * 16;
		if (chunk == 0) {
			result = kIOReturnNotReady;
		} else {
			void *bounce = IOMalloc((vm_size_t)(len < chunk ? len : chunk));
			if (bounce == nullptr) {
				result = kIOReturnNoMemory;
			}
			uint64_t done = 0;
			for (; bounce != nullptr && done < len; ) {
				uint64_t want = (len - done) < chunk ? (len - done) : chunk;
				if (sweet_ufs_media_read(off + done, bounce, want) != 0) {
					result = kIOReturnIOError;
					break;
				}
				if (buffer->writeBytes(done, bounce, (vm_size_t)want) != want) {
					result = kIOReturnUnderrun;
					break;
				}
				done += want;
				transferred = done;
			}
			if (bounce != nullptr) {
				IOFree(bounce, (vm_size_t)(len < chunk ? len : chunk));
			}
		}
	}
#else
	/* UFS path I/O is 4 KiB aligned; copy through a per-call bounce. */
	uint64_t chunk = (uint64_t)bsize * 16;
	if (chunk == 0) {
		return kIOReturnNotReady;
	}
	void *bounce = IOMalloc((vm_size_t)(len < chunk ? len : chunk));
	if (bounce == nullptr) {
		return kIOReturnNoMemory;
	}
	uint64_t done = 0;
	while (done < len) {
		uint64_t want = (len - done) < chunk ? (len - done) : chunk;
		int error = sweet_ufs_media_read(off + done, bounce, want);
		if (error != 0) {
			result = kIOReturnIOError;
			break;
		}
		if (buffer->writeBytes(done, bounce, (vm_size_t)want) != want) {
			result = kIOReturnUnderrun;
			break;
		}
		done += want;
	}
	IOFree(bounce, (vm_size_t)(len < chunk ? len : chunk));
#endif
	if (completion->action != nullptr) {
		completion->action(completion->target, completion->parameter, result,
		    result == kIOReturnSuccess ? len : transferred);
	}
	return result;
}

IOReturn GuacUfsMedia::doEjectMedia(void) { return kIOReturnUnsupported; }
IOReturn GuacUfsMedia::doFormatMedia(UInt64) { return kIOReturnUnsupported; }
UInt32
GuacUfsMedia::doGetFormatCapacities(UInt64 *, UInt32) const
{
	return 0;
}
char *GuacUfsMedia::getVendorString(void) { return (char *)"UniXNU"; }
char *GuacUfsMedia::getProductString(void) { return (char *)"Guacamole UFS root"; }
char *GuacUfsMedia::getRevisionString(void) { return (char *)"1.0"; }
char *GuacUfsMedia::getAdditionalDeviceInfoString(void) { return (char *)""; }

IOReturn
GuacUfsMedia::reportBlockSize(UInt64 *blockSize)
{
	/* IOStorageFamily and APFS address the device in 512-byte sectors. */
	*blockSize = 512;
	return kIOReturnSuccess;
}

IOReturn
GuacUfsMedia::reportEjectability(bool *isEjectable)
{
	*isEjectable = false;
	return kIOReturnSuccess;
}

IOReturn
GuacUfsMedia::reportMaxValidBlock(UInt64 *maxBlock)
{
	*maxBlock = sweet_ufs_media_bytes() / 512 - 1;
	return kIOReturnSuccess;
}

IOReturn
GuacUfsMedia::reportMediaState(bool *mediaPresent, bool *changedState)
{
	*mediaPresent = true;
	if (changedState != nullptr) {
		*changedState = false;
	}
	return kIOReturnSuccess;
}

IOReturn
GuacUfsMedia::reportRemovability(bool *isRemovable)
{
	*isRemovable = false;
	return kIOReturnSuccess;
}

IOReturn
GuacUfsMedia::reportWriteProtection(bool *isWriteProtected)
{
	*isWriteProtected = true;
	return kIOReturnSuccess;
}

static void
guac_media_scan_thread(void *, wait_result_t)
{
	IOSleep(2000);
	OSDictionary *matching = IOService::serviceMatching("IOMedia");
	OSIterator *iterator = matching ? IOService::getMatchingServices(matching) : nullptr;
	uint64_t count = 0, name = 0, size = 0;
	while (iterator != nullptr && count < 8) {
		IOService *service = OSDynamicCast(IOService, iterator->getNextObject());
		if (service == nullptr) {
			break;
		}
		if (count == 0) {
			OSString *bsd = OSDynamicCast(OSString, service->getProperty("BSD Name"));
			OSNumber *bytes = OSDynamicCast(OSNumber, service->getProperty("Size"));
			if (bsd != nullptr) {
				const char *text = bsd->getCStringNoCopy();
				for (unsigned i = 0; i < 8 && text[i] != '\0'; i++) {
					name = (name << 8) | (unsigned char)text[i];
				}
			}
			if (bytes != nullptr) {
				size = bytes->unsigned64BitValue();
			}
		}
		count++;
	}
	if (iterator != nullptr) {
		iterator->release();
	}
	if (matching != nullptr) {
		matching->release();
	}
	sweet_stage_field(count, name, size, 0x5343414eULL);

	thread_terminate(current_thread());
}

extern "C" int
guac_ufs_media_publish(void)
{
	IOService *provider;
	GuacUfsMedia *media;
	OSDictionary *matching;
	guac_media_stage = 1;
	matching = IOService::serviceMatching("IOPlatformExpertDevice");
	if (matching == nullptr) {
		return -1;
	}
	guac_media_stage = 2;
	provider = IOService::waitForMatchingService(matching, 5000000000ULL);
	matching->release();
	if (provider == nullptr) {
		return -2;
	}
	guac_media_stage = 3;
	media = new GuacUfsMedia;
	if (media == nullptr) {
		return -3;
	}
	if (!media->init(nullptr)) {
		media->release();
		return -4;
	}
	guac_media_stage = 4;
	if (!media->attach(provider)) {
		media->release();
		return -5;
	}
	printf("GUACMEDIA: attach done, registering\n");
	guac_media_stage = 5;
	media->registerService(kIOServiceSynchronous);
	guac_media_stage = 6;
	printf("GUACMEDIA: registered\n");
#if defined(GUAC_MEDIA_VERIFIED_ROOT) && GUAC_MEDIA_VERIFIED_ROOT
	guac_verified_ready = guac_media_verified_init();
	printf("GUACMEDIA: owner policy %s (%llu blocks, %llu bytes)\n",
	    guac_verified_ready ? "verified" : "absent",
	    (unsigned long long)guac_verified.block_count,
	    (unsigned long long)guac_verified.image_bytes);
#endif
	{
		/*
		 * The kernel console overwrites its last row, so park the answer in
		 * the fixed framebuffer fields instead: media count, first BSD name
		 * packed eight bytes per line, and its size.
		 */
			OSDictionary *matching = IOService::serviceMatching("IOMedia");
		OSIterator *iterator = matching ? IOService::getMatchingServices(matching) : nullptr;
		uint64_t count = 0, name = 0, size = 0;
		while (iterator != nullptr && count < 8) {
			IOService *service = OSDynamicCast(IOService, iterator->getNextObject());
			if (service == nullptr) {
				break;
			}
			if (count == 0) {
				OSString *bsd = OSDynamicCast(OSString, service->getProperty("BSD Name"));
				OSNumber *bytes = OSDynamicCast(OSNumber, service->getProperty("Size"));
				if (bsd != nullptr) {
					const char *text = bsd->getCStringNoCopy();
					for (unsigned i = 0; i < 8 && text[i] != '\0'; i++) {
						name = (name << 8) | (unsigned char)text[i];
					}
				}
				if (bytes != nullptr) {
					size = bytes->unsigned64BitValue();
				}
				OSNumber *maj = OSDynamicCast(OSNumber, service->getProperty("BSD Major"));
				OSNumber *min = OSDynamicCast(OSNumber, service->getProperty("BSD Minor"));
				if (bsd != nullptr) {
					strlcpy(guac_media_bsdname, bsd->getCStringNoCopy(), sizeof(guac_media_bsdname));
				}
				if (maj != nullptr && min != nullptr) {
					guac_media_bdev = (maj->unsigned32BitValue() << 24) |
					    (min->unsigned32BitValue() & 0xffffff);
				}
			}
			count++;
		}
		if (iterator != nullptr) {
			iterator->release();
		}
		if (matching != nullptr) {
			matching->release();
		}
		guac_media_report[0] = count;
		guac_media_report[1] = name;
		guac_media_report[2] = size;
	}
	{
		thread_t thread = nullptr;
		if (kernel_thread_start(guac_media_scan_thread, nullptr, &thread) == KERN_SUCCESS) {
			thread_deallocate(thread);
		}
	}
	return 0;
}

#if defined(GUAC_MEDIA_VERIFIED_ROOT) && GUAC_MEDIA_VERIFIED_ROOT
extern "C" int native_ufs_block_authenticates(IOService *provider, const char *uuid,
    uint8_t *digest);

/* The mounted volume is owner-authenticated only when its UUID equals the one
 * the signed envelope binds and this window's reads have been verified. */
static bool
guac_media_authenticates(const char *uuid)
{
	if (!guac_verified_ready || uuid == nullptr || strlen(uuid) != 36) {
		return false;
	}
	static const char hex[] = "0123456789ABCDEF";
	char expected[37];
	unsigned at = 0;
	for (unsigned i = 0; i < 16; ++i) {
		if (i == 4 || i == 6 || i == 8 || i == 10) {
			expected[at++] = '-';
		}
		expected[at++] = hex[guac_verified.volume_uuid[i] >> 4];
		expected[at++] = hex[guac_verified.volume_uuid[i] & 15];
	}
	expected[at] = 0;
	return strcmp(uuid, expected) == 0;
}

/*
 * Root acceptance query used by bsd_init's owner-root audit/trust decision:
 * resolve the mounted media by its BSD device, then require the block
 * transport below it (this driver, or NativeUfs's windows) to authenticate the
 * media's signed volume UUID.
 */
extern "C" int
native_verified_block_root_authorized(dev_t device)
{
	OSDictionary *matching = IOService::serviceMatching("IOMedia");
	OSDictionary *properties = OSDictionary::withCapacity(2);
	OSNumber *mjr = OSNumber::withNumber(
	    static_cast<unsigned>(major(static_cast<uint32_t>(device))), 32);
	OSNumber *mnr = OSNumber::withNumber(
	    static_cast<unsigned>(minor(static_cast<uint32_t>(device))), 32);
	if (matching == nullptr || properties == nullptr || mjr == nullptr || mnr == nullptr) {
		OSSafeReleaseNULL(mnr);
		OSSafeReleaseNULL(mjr);
		OSSafeReleaseNULL(properties);
		OSSafeReleaseNULL(matching);
		return 0;
	}
	bool ready = properties->setObject(kIOBSDMajorKey, mjr) &&
	    properties->setObject(kIOBSDMinorKey, mnr) &&
	    matching->setObject("IOPropertyMatch", properties);
	mnr->release();
	mjr->release();
	properties->release();
	OSIterator *iterator = ready ? IOService::getMatchingServices(matching) : nullptr;
	matching->release();
	if (iterator == nullptr) {
		return 0;
	}
	bool authenticated = false;
	unsigned matches = 0;
	while (OSObject *object = iterator->getNextObject()) {
		IOService *media = OSDynamicCast(IOService, object);
		if (media == nullptr) {
			continue;
		}
		if (++matches != 1) {
			authenticated = false;
			break;
		}
		OSString *uuid = OSDynamicCast(OSString, media->getProperty("UUID"));
		if (uuid == nullptr || uuid->getLength() != 36) {
			continue;
		}
		unsigned depth = 0;
		for (IOService *provider = media; provider != nullptr && depth++ < 64;
		    provider = provider->getProvider()) {
			if (OSDynamicCast(GuacUfsMedia, provider) != nullptr) {
				authenticated = guac_media_authenticates(uuid->getCStringNoCopy());
				break;
			}
			int ufs = native_ufs_block_authenticates(provider, uuid->getCStringNoCopy(),
			    nullptr);
			if (ufs >= 0) {
				authenticated = ufs == 1;
				break;
			}
		}
	}
	iterator->release();
	return authenticated && matches == 1;
}
#endif

/*
 * Find a published IOMedia by UUID (the SystemOS cryptex volume) and return
 * its /dev path. Bring-up instrument: unlike native_verified_block_volume_device
 * it does not require the owner trust policy.
 */
/*
 * Find a published IOMedia by UUID (the SystemOS cryptex volume) and return
 * its /dev path. Bring-up instrument: unlike native_verified_block_volume_device
 * it does not require the owner trust policy, and it polls: the apfs attach of
 * the NativeUfs-published windows is asynchronous relative to the first exec.
 */
extern "C" int
guac_media_find_volume(const char *uuid, char *path, unsigned long size)
{
	if (uuid == nullptr || path == nullptr || size < 6 || strlen(uuid) != 36) {
		return 0;
	}
	path[0] = 0;
	for (unsigned int attempt = 0; attempt < 50; attempt++) {
		OSDictionary *matching;
		OSIterator *iterator;
		unsigned int seen = 0;
		int found = 0;

		matching = IOService::serviceMatching("IOMedia");
		iterator = matching ? IOService::getMatchingServices(matching) : nullptr;
		if (iterator != nullptr) {
			while (OSObject *object = iterator->getNextObject()) {
				IOService *service = OSDynamicCast(IOService, object);
				OSString *identity;
				OSString *name;
				if (service == nullptr) {
					continue;
				}
				identity = OSDynamicCast(OSString, service->getProperty("UUID"));
				name = OSDynamicCast(OSString, service->getProperty("BSD Name"));
				seen++;
				if (identity != nullptr && strcmp(identity->getCStringNoCopy(), uuid) == 0 &&
				    name != nullptr && name->getLength() != 0 && name->getLength() < size - 6) {
					snprintf(path, size, "/dev/%s", name->getCStringNoCopy());
					found = 1;
				}
			}
			iterator->release();
		}
		OSSafeReleaseNULL(matching);
		if (attempt == 0) {
			extern volatile uint32_t native_ufs_root_step;
			extern const char *volatile native_ufs_root_failure;
			extern volatile uint32_t native_ufs_blocks_published;
			printf("GUAC: media scan %u entries, %s; ufs step=%u published=%u failure=%s\n",
			    seen, found ? "found" : "not found yet",
			    native_ufs_root_step, native_ufs_blocks_published,
			    native_ufs_root_failure ? native_ufs_root_failure : "-");
		}
		if (found) {
			return 1;
		}
		IOSleep(100);
	}
	return 0;
}
