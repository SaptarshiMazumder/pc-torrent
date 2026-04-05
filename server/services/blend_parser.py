"""
Lightweight .blend file parser to extract frame range metadata.

Parses the binary .blend format (legacy + newer 5.x headers) without
requiring Blender installed. Handles gzip/zstd-compressed files
transparently.

Only extracts: frame_start, frame_end, frame_step from the first Scene
block's embedded RenderData struct via SDNA introspection.
"""

import gzip
import io
import struct
import zipfile
from pathlib import Path

try:
    import zstandard as zstd
    _ZSTD_AVAILABLE = True
except ImportError:
    _ZSTD_AVAILABLE = False

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class BlendParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _read_header(data: bytes):
    """Parse .blend header (legacy and newer format variants).

    Legacy header (format v0):
        BLENDER + ptr(1) + endian(1) + version(3)  => 12 bytes total.
    Newer header (format v1):
        BLENDER + header_size(2) + ptr(1='-') + format(2='01') + endian(1='v') + version(4)
        => 17 bytes today.

    Returns (pointer_size, endian_char, version_int, header_size, bhead_layout).
    pointer_size: 4 or 8
    endian_char:  '<' (little) or '>' (big)
    version_int:  e.g. 300 for Blender 3.0 or 500 for Blender 5.0
    header_size:  block stream start offset (12 or 17)
    bhead_layout: "legacy" (BHead4/SmallBHead8) or "large8" (LargeBHead8)
    """
    if len(data) < 12:
        raise BlendParseError(f"File too small to be a .blend file ({len(data)} bytes)")

    magic = data[:7]
    if magic != b"BLENDER":
        hex_preview = " ".join(f"{b:02x}" for b in data[:12])
        raise BlendParseError(f"Not a .blend file (bad magic). Got: {hex_preview}")

    ptr_code = data[7:8]

    # Format v0 header.
    if ptr_code in (b"_", b"-"):
        pointer_size = 4 if ptr_code == b"_" else 8
        endian_code = data[8:9]
        if endian_code == b"v":
            endian_char = "<"
        elif endian_code == b"V":
            endian_char = ">"
        else:
            raise BlendParseError(f"Unknown endianness code: {endian_code!r}")

        version_str = data[9:12].decode("ascii", errors="replace")
        try:
            version_int = int(version_str)
        except ValueError:
            version_int = 0

        return pointer_size, endian_char, version_int, 12, "legacy"

    # Format v1 header (Blender 5.x).
    # BLENDER17-01v0500
    if len(data) >= 17:
        header_size_str = data[7:9].decode("ascii", errors="replace")
        fmt_version_str = data[10:12].decode("ascii", errors="replace")
        ptr_marker = data[9:10]
        endian_code = data[12:13]

        if header_size_str.isdigit() and fmt_version_str.isdigit() and ptr_marker == b"-":
            header_size = int(header_size_str)
            file_format_version = int(fmt_version_str)
            if file_format_version == 1:
                if header_size < 17 or len(data) < header_size:
                    raise BlendParseError("Invalid Blender v1 header size")
                if endian_code not in (b"v", b"V"):
                    raise BlendParseError(f"Unknown endianness code: {endian_code!r}")

                version_str = data[13:17].decode("ascii", errors="replace")
                try:
                    version_int = int(version_str)
                except ValueError:
                    version_int = 0

                endian_char = "<" if endian_code == b"v" else ">"
                return 8, endian_char, version_int, header_size, "large8"

    hex_preview = " ".join(f"{b:02x}" for b in data[:17])
    raise BlendParseError(f"Unsupported or unknown .blend header format: {hex_preview}")


def _iter_blocks(data: bytes, pointer_size: int, endian: str, header_start: int, bhead_layout: str):
    """Yield (code, size, sdna_index, count, old_ptr, block_data) for each file block."""
    offset = header_start

    if bhead_layout == "large8":
        # LargeBHead8 on-disk layout (32 bytes):
        #   code:       4 bytes
        #   sdna_index: 4 bytes (uint32)
        #   old_ptr:    8 bytes
        #   size:       8 bytes (int64)
        #   count:      8 bytes (int64)
        block_header_size = 32
        while offset + block_header_size <= len(data):
            code = data[offset:offset + 4]
            if code == b"ENDB":
                break
            sdna_index = struct.unpack_from(f"{endian}I", data, offset + 4)[0]
            size = struct.unpack_from(f"{endian}q", data, offset + 16)[0]
            count = struct.unpack_from(f"{endian}q", data, offset + 24)[0]
            if size < 0:
                break
            old_ptr = struct.unpack_from(f"{endian}Q", data, offset + 8)[0]
            block_start = offset + block_header_size
            if block_start + size > len(data):
                break
            block_data = data[block_start:block_start + size]
            yield code, int(size), int(sdna_index), int(count), int(old_ptr), block_data
            next_offset = block_start + size
            if next_offset <= offset:
                break
            offset = next_offset
        return

    # Legacy BHead4/SmallBHead8 layout.
    #   code:       4 bytes
    #   size:       4 bytes (uint32)
    #   old_ptr:    pointer_size bytes
    #   sdna_index: 4 bytes (uint32)
    #   count:      4 bytes (uint32)
    #   data:       <size> bytes
    block_header_size = 4 + 4 + pointer_size + 4 + 4

    while offset + block_header_size <= len(data):
        code = data[offset:offset + 4]
        if code == b"ENDB":
            break
        size = struct.unpack_from(f"{endian}I", data, offset + 4)[0]
        sdna_index = struct.unpack_from(f"{endian}I", data, offset + 8 + pointer_size)[0]
        count = struct.unpack_from(f"{endian}I", data, offset + 12 + pointer_size)[0]
        if pointer_size == 8:
            old_ptr = struct.unpack_from(f"{endian}Q", data, offset + 8)[0]
        else:
            old_ptr = struct.unpack_from(f"{endian}I", data, offset + 8)[0]
        block_start = offset + block_header_size
        if block_start + size > len(data):
            break
        block_data = data[block_start:block_start + size]
        yield code, size, sdna_index, count, int(old_ptr), block_data
        next_offset = block_start + size
        if next_offset <= offset:
            break
        offset = next_offset


# ---------------------------------------------------------------------------
# SDNA parser
# ---------------------------------------------------------------------------

def _parse_sdna(block_data: bytes, endian: str):
    """Parse a DNA1 block into usable struct definitions.

    Returns:
        names:   list[str]          – field name strings
        types:   list[str]          – type name strings
        t_lens:  list[int]          – byte-size of each type
        structs: list[(type_idx, [(type_idx, name_idx), ...])]
        struct_by_name: dict[str, int]  – type name → structs index
    """
    buf = block_data
    pos = 0

    def _align(p, n=4):
        r = p % n
        return p + (n - r) if r else p

    # "SDNA"
    if buf[pos:pos + 4] != b"SDNA":
        raise BlendParseError("DNA1 block does not start with SDNA")
    pos += 4

    # "NAME" + names
    if buf[pos:pos + 4] != b"NAME":
        raise BlendParseError("Expected NAME identifier in SDNA")
    pos += 4
    nr_names = struct.unpack_from(f"{endian}I", buf, pos)[0]
    pos += 4
    names = []
    for _ in range(nr_names):
        end = buf.index(b"\x00", pos)
        names.append(buf[pos:end].decode("ascii", errors="replace"))
        pos = end + 1
    pos = _align(pos)

    # "TYPE" + type names
    if buf[pos:pos + 4] != b"TYPE":
        raise BlendParseError("Expected TYPE identifier in SDNA")
    pos += 4
    nr_types = struct.unpack_from(f"{endian}I", buf, pos)[0]
    pos += 4
    types = []
    for _ in range(nr_types):
        end = buf.index(b"\x00", pos)
        types.append(buf[pos:end].decode("ascii", errors="replace"))
        pos = end + 1
    pos = _align(pos)

    # "TLEN" + type lengths
    if buf[pos:pos + 4] != b"TLEN":
        raise BlendParseError("Expected TLEN identifier in SDNA")
    pos += 4
    t_lens = []
    for _ in range(nr_types):
        t_lens.append(struct.unpack_from(f"{endian}H", buf, pos)[0])
        pos += 2
    pos = _align(pos)

    # "STRC" + struct definitions
    if buf[pos:pos + 4] != b"STRC":
        raise BlendParseError("Expected STRC identifier in SDNA")
    pos += 4
    nr_structs = struct.unpack_from(f"{endian}I", buf, pos)[0]
    pos += 4

    structs = []
    struct_by_name = {}
    for si in range(nr_structs):
        type_idx = struct.unpack_from(f"{endian}H", buf, pos)[0]
        pos += 2
        nr_fields = struct.unpack_from(f"{endian}H", buf, pos)[0]
        pos += 2
        fields = []
        for _ in range(nr_fields):
            f_type = struct.unpack_from(f"{endian}H", buf, pos)[0]
            pos += 2
            f_name = struct.unpack_from(f"{endian}H", buf, pos)[0]
            pos += 2
            fields.append((f_type, f_name))
        structs.append((type_idx, fields))
        struct_by_name[types[type_idx]] = si

    return names, types, t_lens, structs, struct_by_name


def _field_byte_size(names, types, t_lens, f_type_idx, f_name_idx, pointer_size):
    """Compute the byte size of a single field, accounting for pointers and arrays."""
    name = names[f_name_idx]
    base_size = t_lens[f_type_idx]

    # Pointer fields
    if name.startswith("*") or name.startswith("**"):
        base_size = pointer_size

    # Array dimensions: e.g. "name[3][4]" → multiply
    total = base_size
    rest = name
    while "[" in rest:
        idx_start = rest.index("[")
        idx_end = rest.index("]", idx_start)
        dim = int(rest[idx_start + 1:idx_end])
        total *= dim
        rest = rest[idx_end + 1:]

    # Function pointers: "(*name)()" – treated as pointer
    if "(*" in names[f_name_idx]:
        total = pointer_size

    return total


def _find_field_offset(names, types, t_lens, structs, struct_idx, field_name, pointer_size):
    """Find the byte offset of a named field within a struct.

    field_name: the raw name string (e.g. 'sfra', 'efra', 'frame_step').
    Returns (offset, type_index) or None if not found.
    """
    _, fields = structs[struct_idx]
    offset = 0
    for f_type_idx, f_name_idx in fields:
        raw_name = names[f_name_idx]
        # Strip pointer prefix and array suffix to get the base name
        clean = raw_name.lstrip("*")
        bracket = clean.find("[")
        if bracket != -1:
            clean = clean[:bracket]
        # Strip function pointer syntax
        if clean.startswith("(") and ")" in clean:
            clean = clean.split(")")[0].lstrip("(").lstrip("*")

        if clean == field_name:
            return offset, f_type_idx

        offset += _field_byte_size(names, types, t_lens, f_type_idx, f_name_idx, pointer_size)

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_blend_frame_range(file_data: bytes) -> dict:
    """Extract frame range from a .blend file's binary data.

    Args:
        file_data: Raw bytes of the .blend file (may be gzip-compressed).

    Returns:
        {
            "frame_start": int,
            "frame_end": int,
            "frame_step": int,
            "total_frames": int,
            "blender_version": int,   # e.g. 300 for 3.0
        }

    Raises:
        BlendParseError on invalid or unparseable files.
    """
    # Transparently decompress gzip (Blender 2.x default compression)
    if file_data[:2] == b"\x1f\x8b":
        try:
            file_data = gzip.decompress(file_data)
        except Exception as e:
            raise BlendParseError(f"Failed to decompress gzip .blend: {e}")

    # Transparently decompress zstd (Blender 3.0+ default compression)
    elif file_data[:4] == _ZSTD_MAGIC:
        if not _ZSTD_AVAILABLE:
            raise BlendParseError(
                "This .blend file uses zstd compression (Blender 3.0+) "
                "but the zstandard package is not installed on the server."
            )
        try:
            ctx = zstd.ZstdDecompressor()
            file_data = ctx.decompress(file_data)
        except Exception as e:
            raise BlendParseError(f"Failed to decompress zstd .blend: {e}")

    pointer_size, endian, version, header_start, bhead_layout = _read_header(file_data)

    # First pass: find DNA1 block
    dna_data = None
    for code, size, sdna_idx, count, old_ptr, bdata in _iter_blocks(
        file_data, pointer_size, endian, header_start, bhead_layout
    ):
        if code == b"DNA1":
            dna_data = bdata
            break

    if dna_data is None:
        raise BlendParseError("No DNA1 block found in .blend file")

    names, types, t_lens, structs, struct_by_name = _parse_sdna(dna_data, endian)

    # Locate Scene struct and RenderData sub-struct
    if "Scene" not in struct_by_name:
        raise BlendParseError("Scene struct not found in SDNA")

    scene_idx = struct_by_name["Scene"]

    # Find the 'r' field (RenderData) inside Scene
    r_result = _find_field_offset(names, types, t_lens, structs, scene_idx, "r", pointer_size)
    if r_result is None:
        raise BlendParseError("RenderData field 'r' not found in Scene struct")
    r_offset, r_type_idx = r_result

    # RenderData struct
    rd_type_name = types[r_type_idx]
    if rd_type_name not in struct_by_name:
        raise BlendParseError(f"RenderData type '{rd_type_name}' not found in SDNA structs")
    rd_struct_idx = struct_by_name[rd_type_name]

    # Find sfra, efra, frame_step within RenderData
    sfra_result = _find_field_offset(names, types, t_lens, structs, rd_struct_idx, "sfra", pointer_size)
    efra_result = _find_field_offset(names, types, t_lens, structs, rd_struct_idx, "efra", pointer_size)
    step_result = _find_field_offset(names, types, t_lens, structs, rd_struct_idx, "frame_step", pointer_size)

    if sfra_result is None or efra_result is None:
        raise BlendParseError("Could not find sfra/efra fields in RenderData")

    sfra_off, sfra_type = sfra_result
    efra_off, efra_type = efra_result

    # Determine int format for sfra/efra (typically 'int' = 4 bytes)
    sfra_size = t_lens[sfra_type]
    efra_size = t_lens[efra_type]

    def _int_fmt(sz):
        if sz == 4:
            return "i"
        elif sz == 2:
            return "h"
        elif sz == 8:
            return "q"
        return "i"  # default

    sfra_fmt = f"{endian}{_int_fmt(sfra_size)}"
    efra_fmt = f"{endian}{_int_fmt(efra_size)}"

    step_fmt = None
    step_off = 0
    if step_result is not None:
        step_off, step_type = step_result
        step_size = t_lens[step_type]
        step_fmt = f"{endian}{_int_fmt(step_size)}"

    # Try to resolve the active scene pointer from FileGlobal.curscene so we pick
    # the same scene Blender UI is using, not just the first Scene block.
    active_scene_old_ptr = None
    fg_idx = struct_by_name.get("FileGlobal")
    if fg_idx is not None:
        curscene_field = _find_field_offset(
            names, types, t_lens, structs, fg_idx, "curscene", pointer_size
        )
        if curscene_field is not None:
            curscene_off, _ = curscene_field
            for code, size, sdna_idx, count, old_ptr, bdata in _iter_blocks(
                file_data, pointer_size, endian, header_start, bhead_layout
            ):
                if code != b"GLOB":
                    continue
                if sdna_idx >= len(structs):
                    continue
                fg_type_name = types[structs[sdna_idx][0]]
                if fg_type_name != "FileGlobal":
                    continue
                if curscene_off + pointer_size > len(bdata):
                    continue
                if pointer_size == 8:
                    active_scene_old_ptr = struct.unpack_from(
                        f"{endian}Q", bdata, curscene_off
                    )[0]
                else:
                    active_scene_old_ptr = struct.unpack_from(
                        f"{endian}I", bdata, curscene_off
                    )[0]
                break

    # Second pass: find first SC (Scene) block and read frame data
    fallback_result = None
    for code, size, sdna_idx, count, old_ptr, bdata in _iter_blocks(
        file_data, pointer_size, endian, header_start, bhead_layout
    ):
        if code == b"SC\x00\x00" or code[:2] == b"SC":
            # Verify this block uses the Scene struct.
            if sdna_idx >= len(structs):
                continue
            block_type_name = types[structs[sdna_idx][0]]
            if block_type_name != "Scene":
                continue

            # Read sfra and efra from RenderData within Scene data
            rd_start = r_offset
            if rd_start + sfra_off + sfra_size > len(bdata):
                continue
            if rd_start + efra_off + efra_size > len(bdata):
                continue

            frame_start = struct.unpack_from(sfra_fmt, bdata, rd_start + sfra_off)[0]
            frame_end = struct.unpack_from(efra_fmt, bdata, rd_start + efra_off)[0]

            frame_step = 1
            if step_fmt is not None and rd_start + step_off + t_lens[step_result[1]] <= len(bdata):
                frame_step = struct.unpack_from(step_fmt, bdata, rd_start + step_off)[0]
                if frame_step < 1:
                    frame_step = 1

            total_frames = 0
            if frame_end >= frame_start:
                total_frames = ((frame_end - frame_start) // frame_step) + 1

            result = {
                "frame_start": frame_start,
                "frame_end": frame_end,
                "frame_step": frame_step,
                "total_frames": total_frames,
                "blender_version": version,
            }
            if (
                active_scene_old_ptr is not None
                and active_scene_old_ptr != 0
                and old_ptr == active_scene_old_ptr
            ):
                return result
            if fallback_result is None:
                fallback_result = result

    if fallback_result is not None:
        return fallback_result

    raise BlendParseError("No Scene block found in .blend file")


def parse_blend_from_file(filepath: str) -> dict:
    """Parse frame range from a .blend file on disk."""
    data = Path(filepath).read_bytes()
    return parse_blend_frame_range(data)


def parse_blend_from_zip(zip_data: bytes) -> dict:
    """Extract the first .blend file from a zip and parse its frame range."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            blend_names = [
                n
                for n in zf.namelist()
                if n.lower().endswith(".blend")
                and not n.startswith("__MACOSX/")
                and "/._" not in n
                and not n.startswith("._")
            ]
            if not blend_names:
                raise BlendParseError("No .blend file found inside the zip archive")
            # Prefer top-level .blend files first.
            blend_names.sort(key=lambda n: (n.count("/"), len(n), n.lower()))

            last_error = ""
            for blend_path in blend_names:
                try:
                    blend_data = zf.read(blend_path)
                    if len(blend_data) < 12:
                        raise BlendParseError(
                            f"Extracted .blend file is too small ({len(blend_data)} bytes): {blend_path}"
                        )
                    return parse_blend_frame_range(blend_data)
                except BlendParseError as e:
                    last_error = str(e)
                    continue

            raise BlendParseError(
                f"Could not parse any .blend file found in ZIP. Last error: {last_error or 'unknown'}"
            )
    except zipfile.BadZipFile:
        raise BlendParseError("Invalid zip archive")
    except BlendParseError:
        raise
    except Exception as e:
        raise BlendParseError(f"Failed to extract .blend from zip: {e}")


def parse_upload(file_data: bytes, filename: str) -> dict:
    """Parse frame range from an uploaded file (.blend or .zip)."""
    ext = Path(filename).suffix.lower()
    if ext == ".zip":
        return parse_blend_from_zip(file_data)
    elif ext == ".blend":
        return parse_blend_frame_range(file_data)
    else:
        raise BlendParseError(f"Unsupported file extension: {ext}")
