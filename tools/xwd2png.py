import struct, zlib, sys

src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()

f = struct.unpack(">25I", data[0:100])
(header_size, file_version, pixmap_format, pixmap_depth, w, h,
 xoffset, byte_order) = f[0:8]
bitmap_unit, bitmap_bit_order, bitmap_pad, bits_per_pixel = f[8:12]
bytes_per_line, visual_class = f[12], f[13]
red_mask, green_mask, blue_mask = f[14], f[15], f[16]
bits_per_rgb, colormap_entries, ncolors = f[17], f[18], f[19]
win_w, win_h, win_x, win_y = f[20], f[21], f[22], f[23]

print(f"{w}x{h} depth={pixmap_depth} bpp={bits_per_pixel} stride={bytes_per_line} "
      f"ncolors={ncolors} hdr={header_size} order={'LSB' if byte_order==0 else 'MSB'}")

cmap_pos = header_size
cmap = []
for i in range(ncolors):
    base = cmap_pos + i * 12
    _pix, r, g, b = struct.unpack(">IHHH", data[base:base+10])
    cmap.append((r >> 8, g >> 8, b >> 8))

pix_off = cmap_pos + ncolors * 12
print("pix_off:", pix_off, "needed:", pix_off + h * bytes_per_line, "have:", len(data))

rows = []
step = bits_per_pixel // 8
for y in range(h):
    line = data[pix_off + y*bytes_per_line : pix_off + y*bytes_per_line + bytes_per_line]
    out = bytearray()
    for x in range(w):
        p = x * step
        if step >= 3:
            # LSBFirst: 内存里依次是 B, G, R, X
            b_, g_, r_ = line[p], line[p+1], line[p+2]
        else:
            idx = line[p]
            r_, g_, b_ = cmap[idx] if idx < len(cmap) else (0, 0, 0)
        out += bytes((r_, g_, b_))
    rows.append(b"\x00" + bytes(out))

def chunk(t, d):
    c = t + d
    return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
       + chunk(b"IEND", b""))
open(dst, "wb").write(png)
print("wrote", dst)
