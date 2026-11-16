"""PEANUTS: pak version 9 (Unreal Engine 4.25; uepak.py V9) on synthetic paks, no game files: the 0xcd footer, a frozen index (a memory image with frozen pointers) and one
that is not frozen, stored and Zlib files, a pointer that leaves the index, and the pak beside the one that holds the shader code archives (inline_paks)."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'dm/gfx/tools'))
import uepak


class Image:
    """A frozen memory image: fixed-size records first, their out-of-line data appended; a pointer is (offset from its own position << 1) | 1"""
    def __init__(self, size): self.b = bytearray(size)
    def alloc(self, data):
        self.b += bytes(-len(self.b) % 8); at = len(self.b); self.b += data; return at
    def ptr(self, at, target): struct.pack_into('<q', self.b, at, (target - at) << 1 | 1 if target is not None else 0)
    def array(self, at, target, n): self.ptr(at, target); struct.pack_into('<ii', self.b, at + 8, n, n)
    def fstr(self, at, s):
        d = s.encode('utf-16-le') + b'\0\0'; self.array(at, self.alloc(d) if s else None, len(s) + 1 if s else 0)
    def set(self, at, elems, size, free=()):
        """A TSet header at `at` over len(elems) + len(free) slots of `size` bytes; the slots in `free` are unallocated. → each element's position"""
        n = len(elems) + len(free); data = self.alloc(bytes(size * n)); self.array(at, data, n)
        bits = bytearray((n + 7) // 8); live = [i for i in range(n) if i not in free]
        for i in live: bits[i >> 3] |= 1 << (i & 7)
        fl = self.alloc(bytes(bits)); self.ptr(at + 16, fl); struct.pack_into('<ii', self.b, at + 24, n, n)
        return [data + size * i for i in live]


def entry_head(off, size, raw, meth, blocks, bsz):
    """The serialized FPakEntry the pak repeats in front of a file's data (53 bytes, + 4 + 16 per block when compressed)"""
    h = struct.pack('<qqqI', off, size, raw, meth) + bytes(20)
    if meth: h += struct.pack('<I', len(blocks)) + b''.join(struct.pack('<qq', s, t) for s, t in blocks)
    return h + struct.pack('<BI', 0, bsz)


def make_pak(path, files, frozen=True, mount='../../../', ruin=None):
    """A version 9 pak of {name: (bytes, compress)}; ruin(index bytearray) damages the index before it is written"""
    data, recs = bytearray(), []
    for name, (b, comp) in files.items():
        off = len(data)
        if comp:
            blks = [zlib.compress(b[i:i + 16]) for i in range(0, len(b), 16)]   # 16-byte compression blocks: several per file
            head = 53 + 4 + 16 * len(blks); pos, ranges = head, []
            for c in blks: ranges.append((pos, pos + len(c))); pos += len(c)
            data += entry_head(off, sum(map(len, blks)), len(b), 1, ranges, 16) + b''.join(blks)
            recs.append((name, off, sum(map(len, blks)), len(b), 1, ranges, 16))
        else:
            data += entry_head(off, len(b), len(b), 0, [], 0) + b
            recs.append((name, off, len(b), len(b), 0, [], 0))
    if frozen:
        im = Image(0x58); im.fstr(0, mount)
        ents = im.alloc(bytes(0x50 * len(recs))); im.array(0x10, ents, len(recs))
        for i, (_, off, size, raw, meth, ranges, bsz) in enumerate(recs):
            o = ents + 0x50 * i; struct.pack_into('<qqq', im.b, o, off, size, raw)
            im.array(o + 0x30, im.alloc(b''.join(struct.pack('<qq', s, t) for s, t in ranges)) if ranges else None, len(ranges))
            struct.pack_into('<IIB', im.b, o + 0x40, bsz, meth, 0)
        dirs = {}
        for i, r in enumerate(recs): d, _, f = r[0].rpartition('/'); dirs.setdefault(d + '/' if d else '/', []).append((f, i))
        for de, (dn, fs) in zip(im.set(0x20, list(dirs), 0x50, free=(1,)), dirs.items()):   # slot 1 of the directory set is free: its flag bit is clear
            im.fstr(de, dn)
            for fe, (fn, i) in zip(im.set(de + 16, fs, 0x20), fs): im.fstr(fe, fn); struct.pack_into('<i', im.b, fe + 16, i)
        index = im.b
    else:
        m = mount.encode() + b'\0'; index = bytearray(struct.pack('<i', len(m)) + m + struct.pack('<i', len(recs)))
        for name, off, size, raw, meth, ranges, bsz in recs:
            n = name.encode() + b'\0'; index += struct.pack('<i', len(n)) + n + entry_head(off, size, raw, meth, ranges, bsz)
    if ruin: ruin(index)
    foot = bytes(16) + b'\0' + struct.pack('<IIQQ', uepak.PAK_MAGIC, 9, len(data), len(index)) + bytes(20) + bytes([frozen]) + b'Zlib'.ljust(32, b'\0') + bytes(32 * 4)
    Path(path).write_bytes(bytes(data) + bytes(index) + foot)


FILES = {'Game/Content/ShaderArchive-Global-GLSL_SWITCH.ushaderbytecode': (bytes(range(200)), False),
         'Game/Content/Maps/Level.uexp': (b'cooked package bytes ' * 9, True),
         'Game/Game.uproject': (b'{}', True),
         'root.txt': (b'at the mount point', False)}


class PakV9(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.d = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def check(self, frozen):
        p = self.d / 'pakchunk0-Switch.pak'; make_pak(p, FILES, frozen)
        with open(p, 'rb') as f:
            self.assertEqual(uepak.magic_back(f), (0xcd, 9))
            self.assertEqual(uepak.index_frozen(f), frozen)
            self.assertEqual(uepak.footer(f)[3][0], 'Zlib')
        pk = uepak.Pak(str(p))
        self.assertEqual((pk.version, pk.mount, pk.count), (9, '../../../', len(FILES)))
        self.assertEqual(set(pk.files), set(FILES))
        for name, (b, comp) in FILES.items():
            self.assertEqual(pk.read(name), b, name)
            self.assertEqual(pk.entry(name)['method'], 'Zlib' if comp else '')
        self.assertGreater(len(pk.entry('Game/Content/Maps/Level.uexp')['blocks']), 1)

    def test_frozen_index(self): self.check(True)
    def test_serialized_index(self): self.check(False)

    def test_pointer_outside_the_index(self):
        def ruin(index): struct.pack_into('<q', index, 0x10, (len(index) + 0x1000) << 1 | 1)   # the entries array far past the end
        p = self.d / 'bad.pak'; make_pak(p, FILES, ruin=ruin)
        with self.assertRaises(ValueError): uepak.Pak(str(p))

    def test_word_that_is_no_pointer(self):
        def ruin(index): struct.pack_into('<q', index, 0x10, 0x40)   # even: not a frozen pointer
        p = self.d / 'bad.pak'; make_pak(p, FILES, ruin=ruin)
        with self.assertRaises(ValueError): uepak.Pak(str(p))

    def test_unknown_method(self):
        def ruin(index):   # the first entry names compression method 3, which the footer does not
            fp = 0x10 + (struct.unpack_from('<q', index, 0x10)[0] >> 1); struct.pack_into('<I', index, fp + 0x44, 3)
        p = self.d / 'bad.pak'; make_pak(p, FILES, ruin=ruin)
        with self.assertRaises(ValueError): uepak.Pak(str(p))

    def test_pak_beside_the_archives(self):
        """Cooked packages only, beside a version 9 pak with the shader code archives: not inline. Alone, or in another folder: inline"""
        a, b = self.d / 'pakchunk0-Switch.pak', self.d / 'pakchunk1-Switch.pak'
        make_pak(a, FILES); make_pak(b, {'Game/Content/Maps/Other.uexp': (b'cooked', True)})
        self.assertFalse(uepak.inline_shaders(str(a))); self.assertTrue(uepak.inline_shaders(str(b)))
        self.assertEqual(uepak.inline_paks([str(a), str(b)]), {str(a): False, str(b): False})
        self.assertEqual(uepak.unreal_paks(str(self.d), inline=False), [str(a), str(b)])
        self.assertEqual(uepak.unreal_paks(str(self.d), inline=True), [])
        (self.d / 'other').mkdir(); c = self.d / 'other/pakchunk1-Switch.pak'; make_pak(c, {'Game/Content/Maps/Other.uexp': (b'cooked', True)})
        self.assertEqual(uepak.inline_paks([str(a), str(c)]), {str(a): False, str(c): True})
        self.assertEqual(uepak.inline_paks([str(b)]), {str(b): True})


if __name__ == '__main__':
    unittest.main()
