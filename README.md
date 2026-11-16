# Akaso Brave 7 — SD card hook

**A root shell on your own Akaso Brave 7 action camera, without opening the housing.**

This repository holds two small Python tools and the documentation needed to:

1. get the official firmware package and split it into its parts;
2. add an **SD card hook** to the camera's application filesystem: at every boot, if a script named `hook.sh` is at the root of the SD card, the camera runs it as root;
3. flash that one modified partition with the camera's own bootloader update mode;
4. use the hook, for example to start a telnet shell, with a clear view of what that implies.

The camera keeps working exactly as before. Without `hook.sh` on the card, nothing changes at all.

> **No vendor file is included.** This repository contains no firmware, no part of a firmware, and no link to the vendor's servers. You need your own copy of the official package (see [Getting the firmware](#1-getting-the-firmware)).

---

## Contents

- [Disclaimer](#disclaimer)
- [Supported hardware](#supported-hardware)
- [Requirements](#requirements)
- [1. Getting the firmware](#1-getting-the-firmware)
- [2. Inside the `.sw` package](#2-inside-the-sw-package)
- [3. `split_sw.py`: split the package](#3-split_swpy-split-the-package)
- [4. Patching a JFFS2 image without rebuilding it](#4-patching-a-jffs2-image-without-rebuilding-it)
- [5. `add_sd_hook.py`: add the hook](#5-add_sd_hookpy-add-the-hook)
- [6. Flashing the patched partition](#6-flashing-the-patched-partition)
- [7. Using the hook](#7-using-the-hook)
- [8. A telnet shell: read this first](#8-a-telnet-shell-read-this-first)
- [9. Example `hook.sh` that starts telnet](#9-example-hooksh-that-starts-telnet)
- [10. Recovery](#10-recovery)
- [References](#references)

---

## Disclaimer

- This project is not affiliated with, endorsed by or supported by Akaso or any of its suppliers. All trademarks belong to their owners.
- You modify **your own device at your own risk**. A wrong file in the wrong place can make the camera unbootable, and modifying the firmware may void your warranty.
- Everything here was done and tested on a single camera running one firmware version (see below). Read the whole document before touching anything.
- The goal is interoperability and getting the most out of hardware you own. Please don't use it on devices that aren't yours.

## Supported hardware

| | |
|---|---|
| Camera | Akaso Brave 7, the HiSilicon model (Wi-Fi hotspot named `B7_A1…` or `AKASO_Brave7_…`) |
| SoC | HiSilicon Hi3559V200, "HiMobileCam" SDK |
| Firmware | `Brave7-V1.0.20230106` (the camera reports `Brave7-20230106-V2.0`) |
| Flash | 16 MiB SPI NOR, 64 KiB eraseblocks |

Other Brave 7 revisions (hotspots `AKASO_B7_C1_…`, `AKASO_B7_C2_…`, `AKASO_Brave7_B1…`) use other chips or other firmware. They are **not** covered, and the tools will refuse to run if the files don't look as expected.

## Requirements

- Python **3.8 or newer**. Both scripts use **only the standard library** (`zlib`, `lzma`, `gzip`, `struct`…): there is nothing to install.
- A **FAT32**-formatted micro SD card. The bootloader can't read exFAT, which is the default format of most cards over 32 GB.
- Optional, for exploring the filesystems: [jefferson](https://github.com/onekey-sec/jefferson), a JFFS2 extractor that understands the vendor's LZMA compression. Install it in a virtual environment:

  ```sh
  python3 -m venv venv
  . venv/bin/activate
  pip install jefferson
  jefferson -d appfs_extracted appfs.jffs2
  ```

---

## 1. Getting the firmware

The reference package is:

| File | Size | MD5 |
|---|---|---|
| `upgrade_Brave7_Brave7-V1.0.20230106.sw` | 14 921 134 bytes | `af64716973a68f38d43e5338cefe3959` |

Check the MD5 of whatever you obtain **before** going further.

### Option A: from your own SD card

After an official update through the phone app, the camera leaves the `.sw` file (and the split partition images) at the root of the SD card and never deletes them. If your camera was updated this way, you may already have it.

### Option B: the way the app does it

The Android app (Akaso Go) never embeds firmware. When it checks for updates, it asks the vendor's update service and downloads the package from the link it gets back. You can do the same from a computer:

- **Find the service.** Decompile the app (for example with [jadx](https://github.com/skylot/jadx)). The HTTP client is built with Retrofit and OkHttp. Look for the Retrofit interface of the vendor API that declares the *firmware version* requests, and for the dependency-injection module that sets its base URL. The service uses plain HTTP.
- **Send the same headers.** An OkHttp interceptor adds two headers to every request: `timeZone` (an IANA name such as `Europe/Paris`) and `language` (a short code such as `en`, which the app also passes as a query parameter). An account token is added only when the user is signed in. The version check works **without an account**.
- **Use the right model name.** The app doesn't ask the camera for this name. It derives it from the camera's **Wi-Fi hotspot name**: for example, a hotspot named `B7_A1a_…` gives the model `Brave7_A1a`. The mapping is in the app's protocol classes.
- **Read the answer.** The response is JSON. Among other fields it gives the download link of a `.zip`, the zip's MD5, and a version name. The zip contains the `.sw`.

Notes:

- The vendor may change or withdraw entries at any time. At the time of writing, the app no longer *offers* the update to this model, because the version name the service returns doesn't match the pattern the app expects. The download link was still there.
- To check that your requests match the app's, you can also capture the app's own traffic during an update check (it's plain HTTP).

---

## 2. Inside the `.sw` package

A `.sw` is a plain container: a fixed header, a list of entries back to back, and a 4-byte trailer. **There's no cryptographic signature**, only a CRC32. All integers are little-endian.

### Header

| Offset | Type | Field | Value in the reference package |
|---|---|---|---|
| `0x000` | u32 | head magic | `0x08122515` |
| `0x004` | u32 | CRC32 of bytes `[8, len-4)` (`zlib.crc32`) | `0x94493680` |
| `0x008` | u32 | (always 0) | |
| `0x00C` | u32 | total length of the file | 14 921 134 |
| `0x010` | u8 | `compress` | 0 |
| `0x011` | u8 | `pktType` | 0 |
| `0x014` | char[64] | model | `Brave7` |
| `0x054` | char[64] | version | `Brave7-V1.0.20230106` |
| `0x094` | char[512] | `bootargs` (copied from `config`) | |
| `0x294` | char[…] | `bootcmd` (copied from `config`) | |
| `0x490` | u32 | (unknown) | 0 |
| `0x494` | u32 | `ConfigOffset`: offset of the `config` entry | `0x4c4` |
| `0x498` | u32 | `PartitionCnt`: entries after `config` | 8 |
| `0x49C` | u32[] | absolute offset of each of those entries | |
| end − 4 | u32 | tail magic | `0x0812251D` |

### Entries

Each entry is `char name[32]`, `u32 OriDataLen`, `u32 DataLen`, then `DataLen` bytes of data. The two lengths are equal, since nothing is compressed at this level. The field names are the ones the camera prints in its own debug log.

| # | Name | Size | Content | Flash partition |
|---|---|---|---|---|
| 0 | `config` | 612 | small U-Boot script: `setenv bootargs …`, `setenv bootcmd …`, **including the partition table** | — |
| 1 | `u-boot.bin` | 184 956 | bootloader | 384 KiB |
| 2 | `rawparam` | 65 536 | **factory settings** | 64 KiB |
| 3 | `rawparambak` | 65 536 | copy of `rawparam` | 64 KiB |
| 4 | `media_app_zip.bin` | 2 681 632 | LiteOS image for the second core, `gziphead` | 2 624 KiB |
| 5 | `resImage_zip` | 84 643 | resources, `gziphead` | 320 KiB |
| 6 | `uImage` | 2 349 539 | Linux 4.9.37 kernel | 2 304 KiB |
| 7 | `rootfs.jffs2` | 2 047 912 | root filesystem (busybox, init scripts) | 2 240 KiB |
| 8 | `appfs.jffs2` | 7 439 184 | application filesystem, mounted on `/app` | 8 256 KiB (8 454 144 bytes) |

Two more partitions exist in flash but aren't in the package: `password` and `WiFiType`, 64 KiB each.

The `_zip` entries aren't plain gzip files. They start with a 16-byte `gziphead` header (`u32` compressed size, `u32` uncompressed size, the 8 ASCII bytes `gziphead`), followed by a standard gzip stream.

### How the camera uses it

With `pktType = 0`, which the vendor code calls the *"u-boot method"*, an update works in two stages:

1. **Linux** (the main application) receives the package from the app over Wi-Fi, checks it (magics, CRC, model, version not older than the installed one) and **splits it into files at the root of the SD card**, one per entry, named after the entries. It then sets a "pending update" flag and reboots.
2. **U-Boot** reads `config` from the card, rebuilds the partition table from it, and for every partition **erases it and writes the file of the same name, if one is at the root of the card**. Partitions without a file are left alone. The blue LED blinks while it writes.

U-Boot also starts this second stage when **the "up" button is held while the camera is switched on**. That's what [section 6](#6-flashing-the-patched-partition) uses: no network, no app, and a way to recover that only depends on the bootloader.

---

## 3. `split_sw.py`: split the package

`split_sw.py` checks a `.sw` and writes each entry to its own file. It never modifies the package.

```sh
python3 split_sw.py upgrade_Brave7_Brave7-V1.0.20230106.sw --info        # show, write nothing
python3 split_sw.py upgrade_Brave7_Brave7-V1.0.20230106.sw -o sw_parts   # extract
python3 split_sw.py upgrade_Brave7_Brave7-V1.0.20230106.sw --unpack-gzip # also unpack *_zip
```

| Option | Effect |
|---|---|
| `-o DIR` | output directory (default `./sw_parts`); must be empty unless `--force` |
| `--info` | print the header and the entry table only |
| `--unpack-gzip` | for `gziphead` entries, also write `<name>.unpacked` |
| `--ignore-crc` | go on even if the CRC is wrong (for analysis only) |
| `--force` | allow writing into a non-empty directory |

It checks the head and tail magics, the length field and the CRC32. It follows the offset table, checks that the entries are contiguous and fit in the file, and refuses entry names that could escape the output directory. It also writes `manifest.json`, with every header field and the offset, size and MD5 of each entry.

> ⚠️ The extracted files carry the **exact names of the flash partitions**. Never copy the output directory to the root of an SD card: on the next "up" + power start, U-Boot would flash all of it, including `u-boot.bin` and the factory `rawparam`.

---

## 4. Patching a JFFS2 image without rebuilding it

`appfs.jffs2` is a JFFS2 filesystem (little-endian, 64 KiB eraseblocks). Two facts drive the approach.

**1. A rebuild doesn't fit.** The vendor compressed the data nodes with JFFS2 compressor **#8 (LZMA)**, an extension that the standard `mkfs.jffs2` from mtd-utils doesn't have. Extracting the image and rebuilding it with `mkfs.jffs2` gives an image a few kilobytes *larger* than the 8 454 144-byte partition, whatever the options.

**2. JFFS2 is log-structured.** The filesystem is a sequence of nodes. Each data node carries an inode number, a **version number**, an offset in the file and the data for that range. When the kernel mounts the filesystem, it scans all nodes and keeps, for every byte of every file, the node with the **highest version**. Changing a file therefore only takes **appending a new node** with a higher version: the older one becomes obsolete.

So the patch:

1. scans the image the way the kernel does: magic `0x1985` on 4-byte boundaries, header CRC, then a jump over the node;
2. finds the directory entry (`0xE001`) of `bootapp` in the root directory, which gives its inode number;
3. rebuilds the current content of `bootapp` from its data nodes (`0xE002`), uncompressing them (none, zlib or LZMA);
4. builds **one uncompressed data node** with the new content and a higher version number, keeping the owner and permissions;
5. writes it in the free space (`0xFF`) right after the last node, without crossing an eraseblock boundary;
6. scans the result again and checks that `bootapp` now reads back exactly as intended.

The original bytes are left untouched: the image only grows by a few hundred bytes, well within the partition. A few format details that matter:

- the JFFS2 CRC is a CRC32 with a seed of 0 and no final inversion: `zlib.crc32(b, 0xFFFFFFFF) ^ 0xFFFFFFFF`;
- a data node mustn't span more than one 4 KiB page of the file, nor cross an eraseblock;
- on NOR flash, an obsolete node has bit `0x2000` of its type cleared, so a scanner must skip it.

Once flashed, the partition doesn't stay identical to the image: the main application rewrites a file in `/app` at every boot, and the JFFS2 garbage collector moves blocks around. To compare the flash with an image, compare the **extracted files**, not the raw bytes.

---

## 5. `add_sd_hook.py`: add the hook

```sh
# 1. get the original appfs out of the package
python3 split_sw.py upgrade_Brave7_Brave7-V1.0.20230106.sw -o sw_parts

# 2. look at what would be done, write nothing
python3 add_sd_hook.py sw_parts/appfs.jffs2 --dry-run --show

# 3. build the patched image
python3 add_sd_hook.py sw_parts/appfs.jffs2 appfs_hook.jffs2
```

The script adds these lines to `/app/bootapp`, right after the line that starts the main application (`./main_app &`):

```sh
# Optional SD hook: runs only if the file exists, never blocks main_app
( i=0
  while [ $i -lt 60 ]; do
    if [ -f /app/sd/hook.sh ]; then
      sh /app/sd/hook.sh > /app/sd/hook.log 2>&1
      break
    fi
    i=$((i+1)); sleep 1
  done ) &
```

- The loop runs **in the background**, so the camera's own startup is never delayed.
- It polls for up to 60 seconds because the main application mounts the SD card on `/app/sd` itself, a few seconds after it starts.
- The hook runs with `sh`, because FAT has no executable bit. Its output goes to `hook.log` on the card.
- Without a card or without `hook.sh`, the loop just ends.

| Option | Effect |
|---|---|
| `--dry-run` | do everything, including the read-back check, but write nothing |
| `--show` | print the new `bootapp` |
| `--timeout N` | seconds to wait for `hook.sh` (default 60) |
| `--bootapp-src FILE` | original `bootapp` extracted by another tool, for an image using a compressor the script doesn't handle |
| `--partition-size N` | partition size to check against (default 8 454 144) |
| `--force` | overwrite the output file |

The script stops if anything is unexpected: corrupted nodes, `bootapp` missing, the anchor line missing or present twice, the hook already present, an image that would no longer fit, or a result that doesn't read back correctly.

With the reference firmware, `bootapp` is inode 5, 296 bytes, LZMA-compressed. The patch appends a single uncompressed node (version 2, 539 bytes of data), and the patched image is 7 439 792 bytes. Extracted with jefferson, the patched image differs from the original in `bootapp` only.

---

## 6. Flashing the patched partition

Only `appfs` is flashed. The bootloader, kernel and root filesystem stay as they are, so the recovery path ([section 10](#10-recovery)) remains available.

**Before you start**

- Charge the battery, or keep the camera on USB power.
- Back up the photos and videos on the card.
- Keep a copy of the original `config` and `appfs.jffs2`.

**Steps**

1. Format the card as **FAT32**.
2. Put **exactly two files** at the root of the card:
   - `config`: the **original** one, from `split_sw.py`;
   - `appfs.jffs2`: the **patched** image, renamed to this exact name.
3. Make sure no other file at the root has a partition name (`u-boot.bin`, `rawparam`, `rawparambak`, `media_app_zip.bin`, `resImage_zip`, `uImage`, `rootfs.jffs2`, `password`, `WiFiType`). Leftovers from an earlier official update count too: move them into a sub-folder.
4. Camera off: **hold the "up" button**, then press **power**. The blue LED blinks while U-Boot writes the partition, then the camera boots normally.
5. Check that the camera, its menus and its Wi-Fi work as before.
6. **Remove `config` and `appfs.jffs2` from the root of the card**, or move them into a sub-folder.

**Rules that keep the camera safe**

- **Never put `u-boot.bin` on the card.** A damaged bootloader is the one thing that can't be fixed without opening the camera.
- **Never modify `config`.** It holds the partition table that U-Boot uses to know where to write. U-Boot doesn't check file sizes: a file larger than its partition would overwrite the next one.
- **`rawparam` and `rawparambak` from the package contain factory settings.** Flashing them resets every setting of the camera.
- U-Boot flashes **any** file at the root of the card that has a partition name, each time the camera is started with "up" held.

---

## 7. Using the hook

Once the patched `appfs` is installed:

- put a `hook.sh` at the root of the card, **with Unix line endings (LF)**: a file saved with Windows line endings (CRLF) won't run;
- start the camera: within a few seconds the hook runs **as root**, and its output is written to `hook.log` on the card;
- to disable it, remove `hook.sh` from the card or use another card. Nothing else is needed.

Facts about the system that are useful in a hook:

- the root filesystem has a full **busybox** (`telnetd`, `nc`, `ftpd`, `inetd`…), but no network service runs by default;
- `/dev` is a tmpfs (RAM). `/` and `/app` are JFFS2 **on flash**, so avoid writing to them: flash has limited write cycles, and a bad write there persists;
- the card is mounted on `/app/sd`;
- the camera is the Wi-Fi access point at `192.168.0.1`.

Before experimenting, it's worth making a **full flash backup** from a shell:

```sh
mkdir -p /app/sd/flash_backup
cat /proc/mtd > /app/sd/flash_backup/proc_mtd.txt
for n in 0 1 2 3 4 5 6 7 8 9; do cat /dev/mtd$n > /app/sd/flash_backup/mtd$n.bin; done
```

The `mtdN.bin` names don't match any partition name, so U-Boot will never flash them by accident. `mtd1.bin` holds **your current settings** and `mtd9.bin` the Wi-Fi chip type: keep them safe, they exist nowhere else. `mtd8.bin` (`password`) holds an identifier **unique to your camera**. Don't publish it.

---

## 8. A telnet shell: read this first

Starting `telnetd` from the hook gives a **root shell over Wi-Fi with no password**. Before you do it, make sure you understand what that means.

- **Anyone who can join the camera's hotspot gets full root access**, without any authentication. The root account has an empty password. Telnet itself is unencrypted.
- **The hotspot is weak by default.** Its default key is the same on every unit and widely known, and the access point only offers WPA/WPA2 with **TKIP**, an outdated cipher. Anyone within Wi-Fi range should be treated as a potential user of your shell.
- **Root can brick the camera.** With root access, anyone can erase or overwrite any flash partition, including the bootloader, or write hardware registers. A careless command is enough. The SoC tool `himm` writes physical memory and registers, so don't use it lightly.
- **Flash wear and persistence.** Anything written to `/` or `/app` lands on the NOR flash. By default the busybox shell saves its command history to `/.ash_history`, on flash. The example below avoids that.
- **Turn it off when you don't need it.** Don't leave the hook active when using the camera in public places. Removing `hook.sh` from the card is enough, and changing the hotspot key is a good idea.
- **You are responsible** for what happens on a device you opened up this way.

If that's acceptable for your use, here's how to do it properly.

---

## 9. Example `hook.sh` that starts telnet

Two details matter on this system:

- **`/dev/pts` isn't mounted** by the stock init. Without pseudo-terminals, `telnetd` accepts the connection and closes it immediately. The hook mounts it first.
- **Shell history.** `unset HISTFILE` isn't enough with busybox `ash`: it then falls back to `$HOME/.ash_history`, which is `/` on flash. The login shell is started through a tiny wrapper in `/dev` (RAM) that sets `HISTFILE=/dev/null`.

Save this as `hook.sh` at the root of the card, **with LF line endings**:

```sh
#!/bin/sh
# SD hook: start a telnet shell and log some system info.
# Output of this script goes to hook.log on the SD card.

echo "=== hook.sh started: $(date)"
id

# telnetd needs pseudo-terminals, but the stock init does not mount /dev/pts.
[ -c /dev/ptmx ] || mknod /dev/ptmx c 5 2
mkdir -p /dev/pts
grep -q devpts /proc/mounts || mount -t devpts devpts /dev/pts
ls -l /dev/ptmx
grep devpts /proc/mounts

# Telnet login shell: keep the command history out of the flash.
# The wrapper lives in /dev (tmpfs, RAM only). busybox ash falls back to
# $HOME/.ash_history when HISTFILE is unset, so point it to /dev/null instead.
printf '#!/bin/sh\nexport HISTFILE=/dev/null\nexec /bin/sh\n' > /dev/telnet_shell
chmod 755 /dev/telnet_shell
rm -f /.ash_history

# Start the daemon (it forks into the background) and check that it runs.
/usr/sbin/telnetd -l /dev/telnet_shell && echo "telnetd started"
sleep 1
ps | grep -v grep | grep telnetd

# Network configuration, for the log.
ifconfig

echo "=== hook.sh done"
```

Then, from a device connected to the camera's hotspot:

```sh
telnet 192.168.0.1
```

You land in a root shell (`uid=0(root)`). Check `hook.log` on the card if it doesn't work. To close the access, remove `hook.sh` from the card and restart the camera.

---

## 10. Recovery

As long as U-Boot is intact, the camera can be restored **without opening it**:

1. on a FAT32 card, put the **original** `config` and the **original** `appfs.jffs2` (from `split_sw.py`) at the root, and nothing else with a partition name;
2. camera off, hold **up**, press **power**, and wait for the blue LED to stop blinking.

If a broader restore is needed, add the other original partition files the same way, **except `u-boot.bin`**. Remember that the original `rawparam` resets every setting. To restore your own settings instead, use your backup `mtd1.bin`, renamed to `rawparam`.

To get rid of the hook, flashing the original `appfs.jffs2` this way is enough.

---

## References

- [TheNitek/ActionCam](https://github.com/TheNitek/ActionCam): the HiMobileCam HTTP API (`/cgi-bin/hisnet/…`) documented for another camera built on the same SDK. Most of it applies to the Brave 7.
- [onekey-sec/jefferson](https://github.com/onekey-sec/jefferson): JFFS2 extractor with LZMA support.
- [JFFS2 on-flash format](https://www.kernel.org/doc/html/latest/filesystems/jffs2.html) and `include/uapi/linux/jffs2.h` in the Linux sources.

## License

The scripts and this document are released under the MIT License. They contain no code or data from the camera's firmware.
