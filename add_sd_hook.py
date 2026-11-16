#!/usr/bin/env python3
"""
add_sd_hook.py - Add an "SD card hook" to the appfs.jffs2 image of an
Akaso Brave 7, without rebuilding the filesystem.

What it does
------------
On the camera, the init script mounts appfs.jffs2 on /app and runs the
shell script /app/bootapp, which loads the kernel modules and starts the
main application with "./main_app &".

This script produces a copy of appfs.jffs2 in which bootapp has a few more
lines, right after "./main_app &": a background loop that waits up to 60 s
for the SD card to be mounted (main_app mounts it on /app/sd) and, if a file
named hook.sh is present at the root of the card, runs it with sh and
writes its output to hook.log on the card. Without a card, or without
hook.sh, the loop simply ends: the camera behaves exactly as before.

How it does it (why no mkfs.jffs2)
----------------------------------
The vendor image is compressed with JFFS2 compressor #8 (LZMA), which the
standard mtd-utils do not provide. A full rebuild with mkfs.jffs2 comes out
larger than the flash partition. Instead, this script uses the fact that
JFFS2 is a log-structured filesystem: every node carries a version number,
and when the kernel mounts the filesystem it keeps, for each byte range of
a file, the node with the highest version. So we:

  1. scan the image and find the directory entry "bootapp" in the root
     directory, which gives its inode number;
  2. rebuild the current content of bootapp from its data nodes
     (uncompressing them);
  3. insert the hook after "./main_app &";
  4. append ONE new, uncompressed data node for the same inode, with a
     higher version, at the end of the used space;
  5. re-scan the result and check that bootapp now reads back as expected.

The original image is kept byte for byte: only bytes that were free (0xFF)
are written. The image grows by a few hundred bytes.

Requirements: Python 3.8 or newer, standard library only (zlib, lzma).

Usage:
  python3 add_sd_hook.py appfs.jffs2 appfs_hook.jffs2
  python3 add_sd_hook.py appfs.jffs2 appfs_hook.jffs2 --show       # print new bootapp
  python3 add_sd_hook.py appfs.jffs2 --dry-run --show              # write nothing
  python3 add_sd_hook.py appfs.jffs2 out.jffs2 --bootapp-src bootapp_extracted

--bootapp-src is a fallback for an image whose bootapp uses a compressor this
script does not implement: extract the file with another tool (for example
jefferson) and pass it here. Its size is checked against the image.

Tested against the appfs.jffs2 of firmware Brave7-V1.0.20230106. Other
firmware versions may differ: the script refuses to go on if anything does
not look as expected.
"""

import argparse
import hashlib
import lzma
import struct
import sys
import time
import zlib
from pathlib import Path

# ---------------------------------------------------------------------------
# JFFS2 on-flash format (little-endian, as on the camera)
# ---------------------------------------------------------------------------
#
# Every node starts with a 12-byte common header:
#
#   u16 magic      0x1985
#   u16 nodetype   see below
#   u32 totlen     length of the whole node, header included
#   u32 hdr_crc    CRC of the first 8 bytes
#
# Nodes are 4-byte aligned and never cross an eraseblock boundary.
# Unused flash reads as 0xFF.
#
# Bit 0x2000 of nodetype is the "accurate" bit: on NOR flash the kernel
# marks a node obsolete by clearing it, so a node without this bit must be
# ignored (and its hdr_crc no longer matches, which is expected).

JFFS2_MAGIC = 0x1985
JFFS2_MAGIC_BE_BYTES = b"\x19\x85"
NODE_ACCURATE = 0x2000
NODETYPE_DIRENT = 0xE001
NODETYPE_INODE = 0xE002
NODETYPE_CLEANMARKER = 0x2003
NODETYPE_PADDING = 0x2004

COMMON_HDR_SIZE = 12

# struct jffs2_raw_dirent (40 bytes + name):
#   common header (12)
#   u32 pino       parent directory inode
#   u32 version
#   u32 ino        inode of the target (0 = entry deleted)
#   u32 mctime
#   u8  nsize      name length
#   u8  type       DT_REG, DT_DIR, ...
#   u8  unused[2]
#   u32 node_crc   CRC of the first 32 bytes
#   u32 name_crc   CRC of the name
#   u8  name[nsize]
DIRENT_FMT = "<HHII IIII BBH II"
DIRENT_SIZE = struct.calcsize(DIRENT_FMT)           # 40

# struct jffs2_raw_inode (68 bytes + data):
#   common header (12)
#   u32 ino
#   u32 version
#   u32 mode       st_mode (type + permissions)
#   u16 uid, u16 gid
#   u32 isize      file size after this node is applied
#   u32 atime, mtime, ctime
#   u32 offset     where the data of this node goes in the file
#   u32 csize      size of the (compressed) data stored in the node
#   u32 dsize      size of that data once uncompressed
#   u8  compr      compressor id
#   u8  usercompr
#   u16 flags
#   u32 data_crc   CRC of the stored data
#   u32 node_crc   CRC of the first 60 bytes
INODE_FMT = "<HHII III HH I III III BBH II"
INODE_SIZE = struct.calcsize(INODE_FMT)             # 68
INODE_CRC_SPAN = INODE_SIZE - 8                     # node_crc covers 60 bytes

# Compressor ids. #8 (LZMA) is a vendor extension, also found in OpenWrt.
COMPR_NONE = 0x00
COMPR_ZERO = 0x01
COMPR_ZLIB = 0x06
COMPR_LZMA = 0x08

# LZMA settings of the JFFS2 LZMA compressor: raw stream, lc=0 lp=0 pb=0.
# Python's lzma module wants a 13-byte ".lzma" header in front of it:
# properties byte, dictionary size, uncompressed size.
LZMA_LC, LZMA_LP, LZMA_PB = 0, 0, 0
LZMA_PROPS = (LZMA_PB * 5 + LZMA_LP) * 9 + LZMA_LC
LZMA_DICT_SIZE = 0x2000

# A data node must not cross a page boundary of the file (4 KiB).
PAGE_SIZE = 4096

ROOT_INO = 1

# Camera-specific defaults (firmware Brave7-V1.0.20230106).
DEFAULT_ERASEBLOCK = 0x10000          # 64 KiB SPI NOR eraseblocks
DEFAULT_PARTITION_SIZE = 8454144      # "appfs" partition: 8256 KiB
DEFAULT_TARGET = "bootapp"
DEFAULT_ANCHOR = "./main_app &"

HOOK_MARKER = "# Optional SD hook"

# The lines added to bootapp. "{timeout}" is filled in from --timeout.
# - it runs in a background subshell, so main_app is never delayed;
# - it polls for the file because main_app mounts the SD card itself, a few
#   seconds after bootapp has started it;
# - the hook is run with "sh" because FAT has no executable bit;
# - all its output goes to hook.log on the card, the only way to see what
#   happened without a serial console.
HOOK_TEMPLATE = """\
# Optional SD hook: runs only if the file exists, never blocks main_app
( i=0
  while [ $i -lt {timeout} ]; do
    if [ -f /app/sd/hook.sh ]; then
      sh /app/sd/hook.sh > /app/sd/hook.log 2>&1
      break
    fi
    i=$((i+1)); sleep 1
  done ) &
"""


class ImageError(Exception):
    """Raised whenever the image does not look like what we expect."""


def jffs2_crc(data):
    """
    JFFS2 uses a CRC32 with a seed of 0 and no final inversion. zlib's crc32
    inverts on the way in and on the way out, hence the two XORs.
    """
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def align4(value):
    return (value + 3) & ~3


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan(img):
    """
    Walk the whole image and return the valid nodes.

    The walk is the same as the kernel's: look for the magic on every 4-byte
    boundary, check the header CRC, then jump over the node. Returns a dict
    with the parsed dirents and inode nodes, plus some statistics and the
    offset just after the last valid node ("used_end").
    """
    if img[:2] == JFFS2_MAGIC_BE_BYTES:
        raise ImageError("this is a big-endian JFFS2 image; the camera uses "
                         "little-endian, wrong file?")

    dirents, inodes = [], []
    stats = {"cleanmarker": 0, "padding": 0, "obsolete": 0,
             "bad_crc": 0, "other": 0}
    used_end = 0
    pos = 0
    size = len(img)

    while pos + COMMON_HDR_SIZE <= size:
        magic, nodetype, totlen, hdr_crc = struct.unpack_from("<HHII", img, pos)
        if magic != JFFS2_MAGIC:
            pos += 4
            continue

        # The header CRC is computed with the accurate bit set. If the node
        # was marked obsolete, recompute with the bit restored.
        if not nodetype & NODE_ACCURATE:
            check = struct.pack("<HHI", magic, nodetype | NODE_ACCURATE, totlen)
            if jffs2_crc(check) == hdr_crc and COMMON_HDR_SIZE <= totlen <= size - pos:
                stats["obsolete"] += 1
                used_end = max(used_end, pos + totlen)
                pos += align4(totlen)
            else:
                pos += 4
            continue

        if jffs2_crc(img[pos:pos + 8]) != hdr_crc or totlen < COMMON_HDR_SIZE \
                or pos + totlen > size:
            stats["bad_crc"] += 1
            pos += 4
            continue

        node = img[pos:pos + totlen]
        if nodetype == NODETYPE_DIRENT:
            dirents.append(parse_dirent(node, pos))
        elif nodetype == NODETYPE_INODE:
            inodes.append(parse_inode(node, pos))
        elif nodetype == NODETYPE_CLEANMARKER:
            stats["cleanmarker"] += 1
        elif nodetype == NODETYPE_PADDING:
            stats["padding"] += 1
        else:
            stats["other"] += 1      # summary, xattr...: not needed here

        used_end = max(used_end, pos + totlen)
        pos += align4(totlen)

    return {"dirents": [d for d in dirents if d is not None],
            "inodes": [i for i in inodes if i is not None],
            "stats": stats, "used_end": used_end}


def parse_dirent(node, pos):
    if len(node) < DIRENT_SIZE:
        return None
    (_, _, _, _, pino, version, ino, mctime, nsize, dtype, _,
     node_crc, name_crc) = struct.unpack_from(DIRENT_FMT, node, 0)
    name = node[DIRENT_SIZE:DIRENT_SIZE + nsize]
    if jffs2_crc(node[:DIRENT_SIZE - 8]) != node_crc or jffs2_crc(name) != name_crc:
        return None
    return {"pos": pos, "pino": pino, "version": version, "ino": ino,
            "name": name.decode("utf-8", errors="replace"), "type": dtype}


def parse_inode(node, pos):
    if len(node) < INODE_SIZE:
        return None
    (_, _, _, _, ino, version, mode, uid, gid, isize, atime, mtime, ctime,
     offset, csize, dsize, compr, usercompr, flags,
     data_crc, node_crc) = struct.unpack_from(INODE_FMT, node, 0)
    if jffs2_crc(node[:INODE_CRC_SPAN]) != node_crc:
        return None
    data = bytes(node[INODE_SIZE:INODE_SIZE + csize])
    if len(data) != csize or jffs2_crc(data) != data_crc:
        return None
    return {"pos": pos, "ino": ino, "version": version, "mode": mode,
            "uid": uid, "gid": gid, "isize": isize, "atime": atime,
            "mtime": mtime, "ctime": ctime, "offset": offset, "csize": csize,
            "dsize": dsize, "compr": compr, "usercompr": usercompr,
            "flags": flags, "data": data}


# ---------------------------------------------------------------------------
# Reading a file back
# ---------------------------------------------------------------------------

def uncompress(node):
    """Return the uncompressed data of an inode node."""
    compr, data, dsize = node["compr"], node["data"], node["dsize"]
    if compr == COMPR_NONE:
        out = data
    elif compr == COMPR_ZERO:
        out = b"\0" * dsize
    elif compr == COMPR_ZLIB:
        out = zlib.decompress(data)
    elif compr == COMPR_LZMA:
        header = struct.pack("<BIQ", LZMA_PROPS, LZMA_DICT_SIZE, dsize)
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        out = decoder.decompress(header + data, max_length=dsize)
    else:
        raise ImageError(f"compressor 0x{compr:02x} is not implemented "
                         f"(use --bootapp-src)")
    if len(out) != dsize:
        raise ImageError(f"node at 0x{node['pos']:x}: uncompressed to "
                         f"{len(out)} bytes, expected {dsize}")
    return out


def find_file(scan_result, name, parent=ROOT_INO):
    """
    Return the inode number of `name` in directory `parent`. If the name was
    written several times, the dirent with the highest version wins; ino 0
    means the file was deleted.
    """
    matches = [d for d in scan_result["dirents"]
               if d["pino"] == parent and d["name"] == name]
    if not matches:
        raise ImageError(f"no directory entry {name!r} in inode {parent}")
    latest = max(matches, key=lambda d: d["version"])
    if latest["ino"] == 0:
        raise ImageError(f"{name!r} is marked as deleted in this image")
    return latest["ino"]


def read_file(scan_result, ino):
    """
    Rebuild a file from its data nodes, applying them in version order like
    the kernel does. Returns (content, latest_node).
    """
    nodes = sorted((n for n in scan_result["inodes"] if n["ino"] == ino),
                   key=lambda n: n["version"])
    if not nodes:
        raise ImageError(f"no data node for inode {ino}")
    content = bytearray()
    for node in nodes:
        chunk = uncompress(node)
        end = node["offset"] + len(chunk)
        if len(content) < end:
            content.extend(b"\0" * (end - len(content)))
        content[node["offset"]:end] = chunk
    latest = nodes[-1]
    # isize of the newest node is the size of the file (it may truncate).
    size = latest["isize"]
    if len(content) < size:
        content.extend(b"\0" * (size - len(content)))
    return bytes(content[:size]), latest


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def insert_hook(script, anchor, timeout):
    """Insert the hook block after the (single) anchor line of the script."""
    text = script.decode("utf-8")
    if HOOK_MARKER in text:
        raise ImageError("the SD hook is already present in this image")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.strip() == anchor]
    if len(hits) != 1:
        raise ImageError(f"expected exactly one line {anchor!r} in the script, "
                         f"found {len(hits)}")
    i = hits[0]
    if not lines[i].endswith(("\n", "\r")):
        lines[i] += newline                       # anchor was the last line
    block = HOOK_TEMPLATE.format(timeout=timeout).replace("\n", newline)
    lines.insert(i + 1, block)
    return "".join(lines).encode("utf-8")


def build_inode_nodes(latest, content, first_version):
    """
    Build the uncompressed data node(s) carrying the whole new content.
    One node per 4 KiB page of the file; bootapp fits in one.
    """
    now = int(time.time())
    nodes = []
    version = first_version
    for offset in range(0, max(len(content), 1), PAGE_SIZE):
        chunk = content[offset:offset + PAGE_SIZE]
        header = struct.pack("<HHI", JFFS2_MAGIC, NODETYPE_INODE,
                             INODE_SIZE + len(chunk))
        head60 = header + struct.pack("<I", jffs2_crc(header)) + struct.pack(
            "<III HH I III III BBH",
            latest["ino"], version, latest["mode"],
            latest["uid"], latest["gid"],
            len(content),                       # isize: final size of the file
            latest["atime"], now, now,          # atime, mtime, ctime
            offset, len(chunk), len(chunk),     # offset, csize, dsize
            COMPR_NONE, 0, 0)                   # compr, usercompr, flags
        assert len(head60) == INODE_CRC_SPAN
        node = head60 + struct.pack("<II", jffs2_crc(chunk), jffs2_crc(head60)) + chunk
        nodes.append(node)
        version += 1
    return nodes


def cleanmarker():
    header = struct.pack("<HHI", JFFS2_MAGIC, NODETYPE_CLEANMARKER, COMMON_HDR_SIZE)
    return header + struct.pack("<I", jffs2_crc(header))


def append_nodes(img, used_end, nodes, eraseblock):
    """
    Write the nodes in the free space after the last used node. A node never
    crosses an eraseblock boundary: if it would, it goes to the start of the
    next eraseblock, after a cleanmarker (like every block of the original).
    Only bytes that are 0xFF in the input are written.
    """
    out = bytearray(img)
    pos = align4(used_end)
    for node in nodes:
        if len(node) + COMMON_HDR_SIZE > eraseblock:
            raise ImageError("node larger than an eraseblock")
        block_end = (pos // eraseblock + 1) * eraseblock
        pieces = []
        if pos + len(node) > block_end:
            pos = block_end
            pieces.append((pos, cleanmarker()))
            pos += COMMON_HDR_SIZE
        pieces.append((pos, node))
        for at, blob in pieces:
            end = at + len(blob)
            if len(out) < end:
                out.extend(b"\xff" * (end - len(out)))
            if out[at:end] != b"\xff" * len(blob):
                raise ImageError(f"free space at 0x{at:x} is not erased (0xFF)")
            out[at:end] = blob
        pos = align4(pos + len(node))
    # Keep the image a multiple of 4 bytes, padded like erased flash.
    out.extend(b"\xff" * (align4(len(out)) - len(out)))
    return bytes(out), align4(used_end)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Add the SD card hook to an Akaso Brave 7 appfs.jffs2.")
    parser.add_argument("image", type=Path, help="original appfs.jffs2")
    parser.add_argument("output", type=Path, nargs="?",
                        help="patched image to write (not needed with --dry-run)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="seconds to wait for hook.sh on the SD (default 60)")
    parser.add_argument("--bootapp-src", type=Path,
                        help="original bootapp extracted by another tool, if "
                             "the image uses a compressor not handled here")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help=f"file to patch in the root dir (default {DEFAULT_TARGET})")
    parser.add_argument("--anchor", default=DEFAULT_ANCHOR,
                        help=f"line after which the hook goes (default {DEFAULT_ANCHOR!r})")
    parser.add_argument("--eraseblock", type=lambda s: int(s, 0),
                        default=DEFAULT_ERASEBLOCK,
                        help="eraseblock size (default 0x10000)")
    parser.add_argument("--partition-size", type=lambda s: int(s, 0),
                        default=DEFAULT_PARTITION_SIZE,
                        help=f"size of the flash partition (default {DEFAULT_PARTITION_SIZE})")
    parser.add_argument("--show", action="store_true",
                        help="print the new script")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything but write the output")
    parser.add_argument("--force", action="store_true",
                        help="overwrite the output file if it exists")
    args = parser.parse_args()

    if not args.dry_run and args.output is None:
        parser.error("an output file is required (or use --dry-run)")
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600")

    try:
        img = args.image.read_bytes()
        print(f"input  : {args.image}  {len(img)} bytes  "
              f"md5 {hashlib.md5(img).hexdigest()}")

        # 1. Scan the image and locate the target file.
        result = scan(img)
        s = result["stats"]
        print(f"scan   : {len(result['dirents'])} dirents, "
              f"{len(result['inodes'])} inode nodes, {s['cleanmarker']} cleanmarkers, "
              f"{s['obsolete']} obsolete, {s['bad_crc']} bad headers; "
              f"used space ends at 0x{result['used_end']:x}")
        if s["bad_crc"]:
            raise ImageError("the image contains corrupted node headers")

        ino = find_file(result, args.target)
        if args.bootapp_src:
            original = args.bootapp_src.read_bytes()
            latest = max((n for n in result["inodes"] if n["ino"] == ino),
                         key=lambda n: n["version"])
            if len(original) != latest["isize"]:
                raise ImageError(f"{args.bootapp_src} is {len(original)} bytes, "
                                 f"the image says {latest['isize']}")
        else:
            original, latest = read_file(result, ino)
        print(f"target : /{args.target} = inode {ino}, {len(original)} bytes, "
              f"version {latest['version']}, mode {latest['mode']:o}, "
              f"uid/gid {latest['uid']}/{latest['gid']}")

        # 2. Build the new content.
        patched = insert_hook(original, args.anchor, args.timeout)
        if args.show:
            print("-" * 72)
            print(patched.decode("utf-8"), end="")
            print("-" * 72)

        # 3. New node(s) with a version above every existing node of the file.
        max_version = max(n["version"] for n in result["inodes"] if n["ino"] == ino)
        nodes = build_inode_nodes(latest, patched, max_version + 1)

        # 4. Append them in the free space.
        out, at = append_nodes(img, result["used_end"], nodes, args.eraseblock)
        print(f"append : {len(nodes)} node(s), {sum(map(len, nodes))} bytes at 0x{at:x}; "
              f"new image {len(out)} bytes (+{len(out) - len(img)})")
        if len(out) > args.partition_size:
            raise ImageError(f"the image ({len(out)} bytes) no longer fits in the "
                             f"partition ({args.partition_size} bytes)")
        if out[:at] != img[:at]:
            raise ImageError("internal error: original data was modified")

        # 5. Read the result back with the same rules and compare.
        check = scan(out)
        if check["stats"]["bad_crc"]:
            raise ImageError("verification: corrupted node in the output")
        readback, _ = read_file(check, find_file(check, args.target))
        if readback != patched:
            raise ImageError("verification: the new file does not read back as expected")
        print("verify : OK, the patched file reads back correctly")

        if args.dry_run:
            print("dry run: nothing written")
            return 0
        if args.output.exists() and not args.force:
            raise ImageError(f"{args.output} exists (use --force to overwrite)")
        args.output.write_bytes(out)
        print(f"output : {args.output}  md5 {hashlib.md5(out).hexdigest()}")
    except (OSError, ImageError, lzma.LZMAError, zlib.error, UnicodeDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
