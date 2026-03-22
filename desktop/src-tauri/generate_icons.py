"""Generate placeholder icons for the Tauri app."""
import struct
import zlib
import os
import shutil

ICON_DIR = os.path.dirname(os.path.abspath(__file__)) + "/icons"
COLOR = (0x6C, 0x63, 0xFF, 0xFF)  # #6c63ff, fully opaque


def make_png(width, height, rgba):
    """Create a minimal PNG file from raw RGBA data."""
    def chunk(chunk_type, data):
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))

    # Build raw image data (filter byte 0 + RGBA pixels per row)
    raw = b""
    for y in range(height):
        raw += b"\x00"  # filter: none
        for x in range(width):
            idx = (y * width + x) * 4
            raw += bytes(rgba[idx:idx+4])

    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return header + ihdr + idat + iend


def make_solid_rgba(width, height, color):
    """Create a flat RGBA buffer filled with a single color."""
    pixel = bytes(color)
    return pixel * (width * height)


def make_ico(png32_path, output_path):
    """Create a minimal .ico file wrapping a 32x32 PNG."""
    with open(png32_path, "rb") as f:
        png_data = f.read()

    # ICO header: reserved(2) + type=1(2) + count=1(2)
    ico_header = struct.pack("<HHH", 0, 1, 1)
    # ICO directory entry: width, height, colors, reserved, planes, bpp, size, offset
    entry = struct.pack("<BBBBHHIH", 32, 32, 0, 0, 1, 32, len(png_data), 22)
    with open(output_path, "wb") as f:
        f.write(ico_header + entry + png_data)


def main():
    os.makedirs(ICON_DIR, exist_ok=True)

    sizes = {
        "32x32.png": (32, 32),
        "128x128.png": (128, 128),
        "128x128@2x.png": (256, 256),
        "icon.png": (32, 32),
    }

    for filename, (w, h) in sizes.items():
        rgba = make_solid_rgba(w, h, COLOR)
        png_bytes = make_png(w, h, rgba)
        path = os.path.join(ICON_DIR, filename)
        with open(path, "wb") as f:
            f.write(png_bytes)
        print(f"Created {filename} ({w}x{h})")

    # Create .ico from the 32x32 PNG
    ico_path = os.path.join(ICON_DIR, "icon.ico")
    make_ico(os.path.join(ICON_DIR, "32x32.png"), ico_path)
    print(f"Created icon.ico (32x32)")


if __name__ == "__main__":
    main()
