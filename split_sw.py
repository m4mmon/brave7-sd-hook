#!/usr/bin/env python3
"""
split_sw.py - Split an Akaso Brave 7 ".sw" firmware package into its parts.

The Akaso Brave 7 (HiSilicon Hi3559V200, "HiMobileCam" SDK) is updated with
a single ".sw" file. This file is a simple container: a fixed-size header,
then a list of entries (a small U-Boot script called "config", followed by
one raw image per flash partition), then a 4-byte trailer.

This script:
  1. checks the package (magics, total length, CRC32);
  2. prints the header and the entry table;
  3. writes every entry to its own file in an output directory;
  4. optionally unpacks the "gziphead" entries (media_app_zip.bin,
     resImage_zip) so that their content can be inspected;
  5. writes a manifest.json that describes everything it found.

It only reads the package: nothing is ever written back to it.

Requirements: Python 3.8 or newer, standard library only.

Usage:
  python3 split_sw.py upgrade_Brave7_xxx.sw                 # info + extract to ./sw_parts
  python3 split_sw.py upgrade_Brave7_xxx.sw -o parts        # choose the output dir
  python3 split_sw.py upgrade_Brave7_xxx.sw --info          # info only, write nothing
  python3 split_sw.py upgrade_Brave7_xxx.sw --unpack-gzip   # also unpack *_zip entries

IMPORTANT: the extracted files carry the exact names of the flash
partitions (u-boot.bin, rawparam, appfs.jffs2, ...). The camera's
bootloader flashes ANY file with such a name found at the root of the SD
card when the camera is started in update mode. Never copy the output
directory as-is to the root of an SD card.
"""

import argparse
import gzip
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Package layout (all integers are little-endian)
# ---------------------------------------------------------------------------
#
#   0x000  u32       head magic          0x08122515
#   0x004  u32       CRC32               zlib.crc32(data[8:-4])
#   0x008  u32       (always 0)
#   0x00C  u32       total package length, in bytes
#   0x010  u8        "compress" flag     (0 in known packages)
#   0x011  u8        "pktType"           (0 = "u-boot method", see README)
#   0x012  u8[2]     (unused)
#   0x014  char[64]  model               e.g. "Brave7"
#   0x054  char[64]  version             e.g. "Brave7-V1.0.20230106"
#   0x094  char[512] bootargs            copy of the bootargs found in "config"
#   0x294  char[508] bootcmd             copy of the bootcmd found in "config"
#   0x490  u32       (unknown, 0)
#   0x494  u32       offset of the "config" entry
#   0x498  u32       number of entries after "config" (PartitionCnt)
#   0x49C  u32[]     absolute offset of each of those entries
#   ...              entries, back to back
#   end-4  u32       tail magic          0x0812251D
#
# Each entry is:
#
#   char[32]  name        partition name, NUL padded ("appfs.jffs2", ...)
#   u32       OriDataLen  original length   (equal to DataLen in known packages)
#   u32       DataLen     length of the data that follows
#   u8[]      data
#
# The field names OriDataLen / DataLen / PartitionCnt / ConfigOffset are the
# ones printed by the camera's own update code in its debug log.

HEAD_MAGIC = 0x08122515
TAIL_MAGIC = 0x0812251D

OFF_HEAD_MAGIC = 0x000
OFF_CRC = 0x004
OFF_PKT_LEN = 0x00C
OFF_COMPRESS = 0x010
OFF_PKT_TYPE = 0x011
OFF_MODEL, LEN_MODEL = 0x014, 64
OFF_VERSION, LEN_VERSION = 0x054, 64
OFF_BOOTARGS, LEN_BOOTARGS = 0x094, 512
OFF_BOOTCMD, LEN_BOOTCMD = 0x294, 0x490 - 0x294
OFF_TABLE_UNKNOWN = 0x490
OFF_CONFIG_OFFSET = 0x494
OFF_PARTITION_CNT = 0x498
OFF_PARTITION_OFFSETS = 0x49C

HEADER_MIN_SIZE = OFF_PARTITION_OFFSETS
ENTRY_NAME_LEN = 32
ENTRY_HEADER_SIZE = ENTRY_NAME_LEN + 4 + 4

# Sanity limit: the camera has 10 flash partitions; the bootloader accepts up
# to 32. Anything above that means we are not reading a valid package.
MAX_ENTRIES = 32

# "gziphead" wrapper used by the *_zip entries. This is NOT a plain gzip file:
#
#   0x00  u32      compressed size   (= entry size - 16)
#   0x04  u32      uncompressed size
#   0x08  char[8]  "gziphead"
#   0x10  ...      standard gzip stream
GZIPHEAD_TAG = b"gziphead"
GZIPHEAD_SIZE = 16


class PackageError(Exception):
    """Raised when the file does not look like a valid .sw package."""


def u32(data, offset):
    """Read a little-endian unsigned 32-bit integer."""
    return struct.unpack_from("<I", data, offset)[0]


def cstr(data, offset, length):
    """Read a NUL-terminated string stored in a fixed-size field."""
    raw = data[offset:offset + length].split(b"\0", 1)[0]
    return raw.decode("ascii", errors="replace")


def md5(data):
    return hashlib.md5(data).hexdigest()


def safe_name(name):
    """
    Entry names come from the file and end up as file names on disk.
    Refuse anything that could escape the output directory.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise PackageError(f"refusing suspicious entry name: {name!r}")
    if any(ord(c) < 0x20 for c in name):
        raise PackageError(f"refusing entry name with control chars: {name!r}")
    return name


def parse_header(data, ignore_crc=False):
    """Check the package envelope and decode the fixed header."""
    if len(data) < HEADER_MIN_SIZE + 4:
        raise PackageError("file too small to be a .sw package")

    head_magic = u32(data, OFF_HEAD_MAGIC)
    if head_magic != HEAD_MAGIC:
        raise PackageError(f"bad head magic 0x{head_magic:08x} "
                           f"(expected 0x{HEAD_MAGIC:08x})")

    pkt_len = u32(data, OFF_PKT_LEN)
    if pkt_len != len(data):
        raise PackageError(f"length field says {pkt_len} bytes, "
                           f"file has {len(data)} (truncated or padded?)")

    tail_magic = u32(data, len(data) - 4)
    if tail_magic != TAIL_MAGIC:
        raise PackageError(f"bad tail magic 0x{tail_magic:08x} "
                           f"(expected 0x{TAIL_MAGIC:08x})")

    # The CRC covers everything between the CRC field area and the tail
    # magic: bytes [8, len-4). It is a plain zlib/IEEE CRC32.
    stored_crc = u32(data, OFF_CRC)
    computed_crc = zlib.crc32(data[8:-4]) & 0xFFFFFFFF
    crc_ok = stored_crc == computed_crc
    if not crc_ok and not ignore_crc:
        raise PackageError(f"CRC mismatch: stored 0x{stored_crc:08x}, "
                           f"computed 0x{computed_crc:08x} "
                           f"(use --ignore-crc to extract anyway)")

    return {
        "head_magic": f"0x{head_magic:08x}",
        "crc32_stored": f"0x{stored_crc:08x}",
        "crc32_computed": f"0x{computed_crc:08x}",
        "crc32_ok": crc_ok,
        "pkt_len": pkt_len,
        "compress": data[OFF_COMPRESS],
        "pkt_type": data[OFF_PKT_TYPE],
        "model": cstr(data, OFF_MODEL, LEN_MODEL),
        "version": cstr(data, OFF_VERSION, LEN_VERSION),
        "bootargs": cstr(data, OFF_BOOTARGS, LEN_BOOTARGS),
        "bootcmd": cstr(data, OFF_BOOTCMD, LEN_BOOTCMD),
        "table_unknown": u32(data, OFF_TABLE_UNKNOWN),
        "config_offset": u32(data, OFF_CONFIG_OFFSET),
        "partition_cnt": u32(data, OFF_PARTITION_CNT),
        "tail_magic": f"0x{tail_magic:08x}",
    }


def read_entry(data, offset):
    """Decode one entry header and return (name, ori_len, data_len, payload)."""
    end_of_payload_area = len(data) - 4          # the tail magic is not payload
    if offset < HEADER_MIN_SIZE or offset + ENTRY_HEADER_SIZE > end_of_payload_area:
        raise PackageError(f"entry offset 0x{offset:x} is out of range")

    name = safe_name(cstr(data, offset, ENTRY_NAME_LEN))
    ori_len = u32(data, offset + ENTRY_NAME_LEN)
    data_len = u32(data, offset + ENTRY_NAME_LEN + 4)
    start = offset + ENTRY_HEADER_SIZE
    if start + data_len > end_of_payload_area:
        raise PackageError(f"entry {name!r} at 0x{offset:x} overruns the package")
    return name, ori_len, data_len, data[start:start + data_len]


def parse_entries(data, header):
    """
    Build the list of entries.

    The offsets come from the table at 0x49C. As a cross-check, the entries
    are also expected to follow each other without gaps, the last one ending
    right before the tail magic. A mismatch is reported as a warning only:
    the table is what the camera itself uses.
    """
    count = header["partition_cnt"]
    if count > MAX_ENTRIES:
        raise PackageError(f"PartitionCnt={count} is not plausible")

    offsets = [header["config_offset"]]
    for i in range(count):
        offsets.append(u32(data, OFF_PARTITION_OFFSETS + 4 * i))

    entries, warnings = [], []
    expected_next = None
    for index, offset in enumerate(offsets):
        name, ori_len, data_len, payload = read_entry(data, offset)
        if expected_next is not None and offset != expected_next:
            warnings.append(f"entry {name!r} starts at 0x{offset:x}, "
                            f"previous one ended at 0x{expected_next:x}")
        if ori_len != data_len:
            warnings.append(f"entry {name!r}: OriDataLen={ori_len} != "
                            f"DataLen={data_len} (compressed entry?)")
        expected_next = offset + ENTRY_HEADER_SIZE + data_len
        entries.append({
            "index": index,
            "name": name,
            "entry_offset": offset,
            "data_offset": offset + ENTRY_HEADER_SIZE,
            "ori_data_len": ori_len,
            "data_len": data_len,
            "md5": md5(payload),
            "payload": payload,
        })

    if expected_next != len(data) - 4:
        warnings.append(f"last entry ends at 0x{expected_next:x}, "
                        f"tail magic is at 0x{len(data) - 4:x}")

    names = [e["name"] for e in entries]
    if len(set(names)) != len(names):
        raise PackageError(f"duplicate entry names: {names}")

    return entries, warnings


def describe_payload(payload):
    """Very small content sniffer, only to make the listing more readable."""
    if payload.startswith(b"setenv"):
        return "U-Boot script (text)"
    if len(payload) >= GZIPHEAD_SIZE and payload[8:16] == GZIPHEAD_TAG:
        return "gziphead + gzip"
    if payload[:4] == b"\x27\x05\x19\x56":
        return "U-Boot uImage (Linux kernel)"
    if payload[:2] == b"\x85\x19":
        return "JFFS2, little-endian"
    if payload[:4] == b"lcyc":
        return "settings storage (HiCycle)"
    return ""


def unpack_gziphead(payload):
    """
    Unpack an entry wrapped in the 16-byte "gziphead" header.
    Returns the decompressed bytes, or None if the entry is not wrapped.
    The sizes stored in the wrapper are checked against reality.
    """
    if len(payload) < GZIPHEAD_SIZE or payload[8:16] != GZIPHEAD_TAG:
        return None
    comp_size = u32(payload, 0)
    raw_size = u32(payload, 4)
    if comp_size != len(payload) - GZIPHEAD_SIZE:
        raise PackageError(f"gziphead: compressed size {comp_size} does not "
                           f"match the entry ({len(payload) - GZIPHEAD_SIZE})")
    raw = gzip.decompress(payload[GZIPHEAD_SIZE:])
    if len(raw) != raw_size:
        raise PackageError(f"gziphead: got {len(raw)} bytes, "
                           f"header announces {raw_size}")
    return raw


def print_report(header, entries, warnings):
    print(f"Model     : {header['model']}")
    print(f"Version   : {header['version']}")
    print(f"Length    : {header['pkt_len']} bytes")
    print(f"CRC32     : {header['crc32_stored']} "
          f"({'OK' if header['crc32_ok'] else 'MISMATCH'})")
    print(f"pktType   : {header['pkt_type']}   compress: {header['compress']}")
    print(f"Entries   : config + {header['partition_cnt']}")
    print(f"bootargs  : {header['bootargs']}")
    print(f"bootcmd   : {header['bootcmd']}")
    print()
    print(f"{'#':>2}  {'offset':>10}  {'size':>10}  {'name':<20}  content")
    for e in entries:
        print(f"{e['index']:>2}  0x{e['entry_offset']:08x}  {e['data_len']:>10}  "
              f"{e['name']:<20}  {describe_payload(e['payload'])}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)


def extract(entries, header, warnings, out_dir, unpack_gzip, force):
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()) and not force:
        raise PackageError(f"{out_dir} is not empty (use --force to overwrite)")

    manifest = {"header": header, "warnings": warnings, "entries": []}
    for e in entries:
        target = out_dir / e["name"]
        target.write_bytes(e["payload"])
        item = {k: v for k, v in e.items() if k != "payload"}

        if unpack_gzip:
            raw = unpack_gziphead(e["payload"])
            if raw is not None:
                unpacked = out_dir / (e["name"] + ".unpacked")
                unpacked.write_bytes(raw)
                item["unpacked_file"] = unpacked.name
                item["unpacked_len"] = len(raw)
                item["unpacked_md5"] = md5(raw)

        manifest["entries"].append(item)
        print(f"wrote {target}")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out_dir / 'manifest.json'}")
    print()
    print("REMINDER: these files have the names of the flash partitions. Do not")
    print("copy them to the root of an SD card unless you really mean to flash them.")


def main():
    parser = argparse.ArgumentParser(
        description="Check and split an Akaso Brave 7 .sw firmware package.")
    parser.add_argument("package", type=Path, help="the .sw file to read")
    parser.add_argument("-o", "--out", type=Path, default=Path("sw_parts"),
                        help="output directory (default: ./sw_parts)")
    parser.add_argument("--info", action="store_true",
                        help="only print the header and the entry table")
    parser.add_argument("--unpack-gzip", action="store_true",
                        help="also write <name>.unpacked for gziphead entries")
    parser.add_argument("--ignore-crc", action="store_true",
                        help="go on even if the package CRC is wrong")
    parser.add_argument("--force", action="store_true",
                        help="allow writing into a non-empty output directory")
    args = parser.parse_args()

    try:
        data = args.package.read_bytes()
        header = parse_header(data, ignore_crc=args.ignore_crc)
        entries, warnings = parse_entries(data, header)
        print(f"Package   : {args.package}  (md5 {md5(data)})")
        print_report(header, entries, warnings)
        if not args.info:
            print()
            extract(entries, header, warnings, args.out, args.unpack_gzip, args.force)
    except (OSError, PackageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
