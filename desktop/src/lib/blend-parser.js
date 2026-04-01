/**
 * Client-side .blend file parser — extracts frame range metadata.
 *
 * Parses the binary .blend format (Blender 2.8 – 4.x) without requiring
 * Blender installed.  Handles gzip-compressed and ZIP-wrapped files.
 *
 * Port of server/blend_parser.py — same SDNA introspection algorithm.
 */

import { decompressSync, unzipSync } from "fflate";
import { decompress as decompressZstd } from "fzstd";

const ZSTD_MAGIC = [0x28, 0xb5, 0x2f, 0xfd];

export class BlendParseError extends Error {
  constructor(message) {
    super(message);
    this.name = "BlendParseError";
  }
}

// ---------------------------------------------------------------------------
// Low-level helpers
// ---------------------------------------------------------------------------

function readHeader(view) {
  if (view.byteLength < 12) {
    throw new BlendParseError(`File too small to be a .blend file (${view.byteLength} bytes)`);
  }

  const magic = String.fromCharCode(
    view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3),
    view.getUint8(4), view.getUint8(5), view.getUint8(6),
  );
  if (magic !== "BLENDER") {
    throw new BlendParseError("Not a valid .blend file (bad magic)");
  }

  const ptrCode = view.getUint8(7);
  const isLegacy = ptrCode === 0x5f /* '_' */ || ptrCode === 0x2d /* '-' */;

  // Legacy header (format v0): BLENDER + ptr + endian + vvv
  if (isLegacy) {
    const ptrSize = ptrCode === 0x2d ? 8 : 4;
    const endCode = view.getUint8(8);
    if (endCode !== 0x76 && endCode !== 0x56) {
      throw new BlendParseError(`Unknown endianness code: ${String.fromCharCode(endCode)}`);
    }
    const littleEndian = endCode === 0x76;
    const verStr = String.fromCharCode(view.getUint8(9), view.getUint8(10), view.getUint8(11));
    const version = parseInt(verStr, 10) || 0;
    return { ptrSize, littleEndian, version, dataStart: 12, bheadLayout: "legacy" };
  }

  // Newer header (format v1): BLENDER17-01v0500
  if (view.byteLength >= 17) {
    const headerSizeStr = String.fromCharCode(view.getUint8(7), view.getUint8(8));
    const ptrMarker = view.getUint8(9);
    const fmtStr = String.fromCharCode(view.getUint8(10), view.getUint8(11));
    const endCode = view.getUint8(12);

    const headerSize = parseInt(headerSizeStr, 10);
    const fmtVersion = parseInt(fmtStr, 10);
    if (!Number.isNaN(headerSize) && !Number.isNaN(fmtVersion) && ptrMarker === 0x2d && fmtVersion === 1) {
      if (headerSize < 17 || view.byteLength < headerSize) {
        throw new BlendParseError(`Invalid Blender v1 header size: ${headerSize}`);
      }
      if (endCode !== 0x76 && endCode !== 0x56) {
        throw new BlendParseError(`Unknown endianness code: ${String.fromCharCode(endCode)}`);
      }
      const littleEndian = endCode === 0x76;
      const verStr = String.fromCharCode(
        view.getUint8(13),
        view.getUint8(14),
        view.getUint8(15),
        view.getUint8(16),
      );
      const version = parseInt(verStr, 10) || 0;
      // File format v1 always uses LargeBHead8 on disk.
      return { ptrSize: 8, littleEndian, version, dataStart: headerSize, bheadLayout: "large8" };
    }
  }

  const header = new Uint8Array(view.buffer, view.byteOffset, Math.min(17, view.byteLength));
  const hexPreview = Array.from(header, (b) => b.toString(16).padStart(2, "0")).join(" ");
  throw new BlendParseError(`Unsupported or unknown .blend header format: ${hexPreview}`);
}

function* iterBlocks(view, ptrSize, littleEndian, dataStart, bheadLayout) {
  let offset = dataStart;

  if (bheadLayout === "large8") {
    // LargeBHead8 layout (32 bytes):
    // code[4], sdna_index[4], old_ptr[8], len[8], nr[8]
    const headerSize = 32;
    while (offset + headerSize <= view.byteLength) {
      const c0 = view.getUint8(offset);
      const c1 = view.getUint8(offset + 1);
      const c2 = view.getUint8(offset + 2);
      const c3 = view.getUint8(offset + 3);
      const code = String.fromCharCode(c0, c1, c2, c3);
      if (code === "ENDB") break;

      const sdnaIndex = view.getUint32(offset + 4, littleEndian);
      const oldPtr = view.getBigUint64(offset + 8, littleEndian);
      const size = Number(view.getBigInt64(offset + 16, littleEndian));
      if (size < 0) break;

      const dataOffset = offset + headerSize;
      if (dataOffset + size > view.byteLength) break;

      yield { code, size, sdnaIndex, oldPtr, dataOffset };
      const nextOffset = dataOffset + size;
      if (nextOffset <= offset) break;
      offset = nextOffset;
    }
    return;
  }

  // Legacy BHead4/SmallBHead8 layout.
  const headerSize = 16 + ptrSize;
  while (offset + headerSize <= view.byteLength) {
    const c0 = view.getUint8(offset);
    const c1 = view.getUint8(offset + 1);
    const c2 = view.getUint8(offset + 2);
    const c3 = view.getUint8(offset + 3);
    const code = String.fromCharCode(c0, c1, c2, c3);

    if (code === "ENDB") break;

    const size = view.getUint32(offset + 4, littleEndian);
    const oldPtr =
      ptrSize === 8
        ? view.getBigUint64(offset + 8, littleEndian)
        : BigInt(view.getUint32(offset + 8, littleEndian));
    const sdnaIndex = view.getUint32(offset + 8 + ptrSize, littleEndian);
    const dataOffset = offset + headerSize;
    if (dataOffset + size > view.byteLength) break;

    yield { code, size, sdnaIndex, oldPtr, dataOffset };
    const nextOffset = dataOffset + size;
    if (nextOffset <= offset) break;
    offset = nextOffset;
  }
}

// ---------------------------------------------------------------------------
// SDNA parser
// ---------------------------------------------------------------------------

function parseSDNA(view, blockOffset, blockSize, littleEndian) {
  const buf = new Uint8Array(view.buffer, view.byteOffset + blockOffset, blockSize);
  let pos = 0;

  function align4(p) {
    const r = p % 4;
    return r ? p + (4 - r) : p;
  }

  function readTag(expected) {
    const tag = String.fromCharCode(buf[pos], buf[pos + 1], buf[pos + 2], buf[pos + 3]);
    if (tag !== expected) {
      throw new BlendParseError(`Expected ${expected} in SDNA, got ${tag}`);
    }
    pos += 4;
  }

  function readUint32() {
    const dv = new DataView(buf.buffer, buf.byteOffset + pos, 4);
    const val = dv.getUint32(0, littleEndian);
    pos += 4;
    return val;
  }

  function readUint16() {
    const dv = new DataView(buf.buffer, buf.byteOffset + pos, 2);
    const val = dv.getUint16(0, littleEndian);
    pos += 2;
    return val;
  }

  // "SDNA"
  readTag("SDNA");

  // "NAME" + names
  readTag("NAME");
  const nrNames = readUint32();
  const names = [];
  for (let i = 0; i < nrNames; i++) {
    let end = pos;
    while (end < buf.length && buf[end] !== 0) end++;
    let str = "";
    for (let j = pos; j < end; j++) str += String.fromCharCode(buf[j]);
    names.push(str);
    pos = end + 1;
  }
  pos = align4(pos);

  // "TYPE" + type names
  readTag("TYPE");
  const nrTypes = readUint32();
  const types = [];
  for (let i = 0; i < nrTypes; i++) {
    let end = pos;
    while (end < buf.length && buf[end] !== 0) end++;
    let str = "";
    for (let j = pos; j < end; j++) str += String.fromCharCode(buf[j]);
    types.push(str);
    pos = end + 1;
  }
  pos = align4(pos);

  // "TLEN" + type lengths
  readTag("TLEN");
  const typeLens = [];
  for (let i = 0; i < nrTypes; i++) {
    typeLens.push(readUint16());
  }
  pos = align4(pos);

  // "STRC" + struct definitions
  readTag("STRC");
  const nrStructs = readUint32();
  const structs = [];
  const structByName = {};

  for (let si = 0; si < nrStructs; si++) {
    const typeIdx = readUint16();
    const nrFields = readUint16();
    const fields = [];
    for (let fi = 0; fi < nrFields; fi++) {
      const fType = readUint16();
      const fName = readUint16();
      fields.push({ typeIdx: fType, nameIdx: fName });
    }
    structs.push({ typeIdx, fields });
    structByName[types[typeIdx]] = si;
  }

  return { names, types, typeLens, structs, structByName };
}

// ---------------------------------------------------------------------------
// Field offset calculation
// ---------------------------------------------------------------------------

function fieldByteSize(sdna, fTypeIdx, fNameIdx, ptrSize) {
  const name = sdna.names[fNameIdx];
  let size = sdna.typeLens[fTypeIdx];

  // Pointer fields
  if (name.startsWith("*") || name.startsWith("**")) {
    size = ptrSize;
  }

  // Function pointers: "(*name)()"
  if (name.includes("(*")) {
    return ptrSize;
  }

  // Array dimensions: "name[3][4]" → multiply
  const dimRe = /\[(\d+)\]/g;
  let m;
  while ((m = dimRe.exec(name)) !== null) {
    size *= parseInt(m[1], 10);
  }

  return size;
}

function findFieldOffset(sdna, structIdx, fieldName, ptrSize) {
  const { fields } = sdna.structs[structIdx];
  let offset = 0;

  for (const { typeIdx, nameIdx } of fields) {
    const rawName = sdna.names[nameIdx];

    // Clean name: strip pointer prefix, array suffix, function pointer syntax
    let clean = rawName.replace(/^\*+/, "");
    const bracket = clean.indexOf("[");
    if (bracket !== -1) clean = clean.substring(0, bracket);
    if (clean.startsWith("(") && clean.includes(")")) {
      clean = clean.split(")")[0].replace(/^\(/, "").replace(/^\*+/, "");
    }

    if (clean === fieldName) {
      return { offset, typeIdx };
    }

    offset += fieldByteSize(sdna, typeIdx, nameIdx, ptrSize);
  }

  return null;
}

// ---------------------------------------------------------------------------
// Decompression + ZIP extraction
// ---------------------------------------------------------------------------

function isGzip(bytes) {
  return bytes.length >= 2 && bytes[0] === 0x1f && bytes[1] === 0x8b;
}

function isZstd(bytes) {
  return (
    bytes.length >= 4 &&
    bytes[0] === ZSTD_MAGIC[0] &&
    bytes[1] === ZSTD_MAGIC[1] &&
    bytes[2] === ZSTD_MAGIC[2] &&
    bytes[3] === ZSTD_MAGIC[3]
  );
}

function isZip(bytes) {
  return bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b;
}

function decompressBlend(uint8) {
  if (isGzip(uint8)) {
    try {
      return decompressSync(uint8);
    } catch (e) {
      throw new BlendParseError(`Failed to decompress gzip .blend: ${e.message}`);
    }
  }
  if (isZstd(uint8)) {
    try {
      return decompressZstd(uint8);
    } catch (e) {
      throw new BlendParseError(`Failed to decompress zstd .blend: ${e.message}`);
    }
  }
  return uint8;
}

function extractBlendFromZip(uint8) {
  let entries;
  try {
    entries = unzipSync(uint8);
  } catch (e) {
    throw new BlendParseError(`Invalid ZIP archive: ${e.message}`);
  }

  const blendNames = Object.keys(entries)
    .filter(
      (n) =>
        n.toLowerCase().endsWith(".blend") &&
        !n.startsWith("__MACOSX/") &&
        !n.includes("/._") &&
        !n.startsWith("._"),
    )
    .sort((a, b) => {
      const depthDelta = a.split("/").length - b.split("/").length;
      if (depthDelta !== 0) return depthDelta;
      const lenDelta = a.length - b.length;
      if (lenDelta !== 0) return lenDelta;
      return a.toLowerCase().localeCompare(b.toLowerCase());
    });

  if (!blendNames.length) {
    throw new BlendParseError("No .blend file found inside the ZIP archive");
  }

  const data = entries[blendNames[0]];
  if (data.length < 12) {
    throw new BlendParseError(`Extracted .blend file is too small (${data.length} bytes)`);
  }
  return new Uint8Array(data);
}

// ---------------------------------------------------------------------------
// Main parse
// ---------------------------------------------------------------------------

function parseBlendBuffer(input) {
  let uint8 = input instanceof Uint8Array ? new Uint8Array(input) : new Uint8Array(input);

  // Decompress if needed
  uint8 = decompressBlend(uint8);

  const view = new DataView(uint8.buffer, uint8.byteOffset, uint8.byteLength);
  const { ptrSize, littleEndian, version, dataStart, bheadLayout } = readHeader(view);

  // Pass 1: find DNA1 block, parse SDNA
  let sdna = null;
  for (const block of iterBlocks(view, ptrSize, littleEndian, dataStart, bheadLayout)) {
    if (block.code === "DNA1") {
      sdna = parseSDNA(view, block.dataOffset, block.size, littleEndian);
      break;
    }
  }
  if (!sdna) throw new BlendParseError("No SDNA found in .blend file");

  // Locate Scene struct
  const sceneIdx = sdna.structByName["Scene"];
  if (sceneIdx === undefined) {
    throw new BlendParseError("Scene struct not found in SDNA");
  }

  // Find 'r' (RenderData) field inside Scene
  const rField = findFieldOffset(sdna, sceneIdx, "r", ptrSize);
  if (!rField) {
    throw new BlendParseError("RenderData field 'r' not found in Scene struct");
  }

  // Locate RenderData struct
  const rdTypeName = sdna.types[rField.typeIdx];
  const rdIdx = sdna.structByName[rdTypeName];
  if (rdIdx === undefined) {
    throw new BlendParseError(`RenderData type '${rdTypeName}' not found in SDNA structs`);
  }

  // Find sfra, efra, frame_step within RenderData
  const sfra = findFieldOffset(sdna, rdIdx, "sfra", ptrSize);
  const efra = findFieldOffset(sdna, rdIdx, "efra", ptrSize);
  const fstep = findFieldOffset(sdna, rdIdx, "frame_step", ptrSize);

  if (!sfra || !efra) {
    throw new BlendParseError("Could not find sfra/efra fields in RenderData");
  }

  // Resolve active scene pointer from FileGlobal.curscene so we match Blender UI.
  let activeSceneOldPtr = null;
  const fileGlobalIdx = sdna.structByName["FileGlobal"];
  if (fileGlobalIdx !== undefined) {
    const curSceneField = findFieldOffset(sdna, fileGlobalIdx, "curscene", ptrSize);
    if (curSceneField) {
      for (const block of iterBlocks(view, ptrSize, littleEndian, dataStart, bheadLayout)) {
        if (block.code !== "GLOB") continue;
        if (block.sdnaIndex >= sdna.structs.length) continue;
        const blockTypeIdx = sdna.structs[block.sdnaIndex].typeIdx;
        if (sdna.types[blockTypeIdx] !== "FileGlobal") continue;
        const ptrAbs = block.dataOffset + curSceneField.offset;
        if (ptrAbs + ptrSize > view.byteLength) continue;
        activeSceneOldPtr =
          ptrSize === 8
            ? view.getBigUint64(ptrAbs, littleEndian)
            : BigInt(view.getUint32(ptrAbs, littleEndian));
        break;
      }
    }
  }

  // Pass 2: find first Scene block and read frame data
  const sceneTypeIdx = sdna.structs[sceneIdx].typeIdx;
  let fallbackResult = null;

  for (const block of iterBlocks(view, ptrSize, littleEndian, dataStart, bheadLayout)) {
    if (block.code.startsWith("SC")) {
      // Verify this block uses the Scene struct
      if (block.sdnaIndex >= sdna.structs.length) {
        continue;
      }
      const blockTypeIdx = sdna.structs[block.sdnaIndex].typeIdx;
      if (blockTypeIdx !== sceneTypeIdx && sdna.types[blockTypeIdx] !== "Scene") {
        continue;
      }

      const rdStart = block.dataOffset + rField.offset;
      const blockEnd = block.dataOffset + block.size;

      const readInt = (off, typeIdx) => {
        const absOff = rdStart + off;
        const sz = sdna.typeLens[typeIdx];
        if (absOff + sz > blockEnd) {
          throw new BlendParseError("Frame field extends beyond block data");
        }
        if (sz === 4) return view.getInt32(absOff, littleEndian);
        if (sz === 2) return view.getInt16(absOff, littleEndian);
        if (sz === 8) return Number(view.getBigInt64(absOff, littleEndian));
        return view.getInt32(absOff, littleEndian);
      };

      // Bounds check
      const sfraEnd = rdStart + sfra.offset + sdna.typeLens[sfra.typeIdx];
      const efraEnd = rdStart + efra.offset + sdna.typeLens[efra.typeIdx];
      if (sfraEnd > blockEnd || efraEnd > blockEnd) continue;

      const frameStart = readInt(sfra.offset, sfra.typeIdx);
      const frameEnd = readInt(efra.offset, efra.typeIdx);

      let frameStep = 1;
      if (fstep) {
        const stepEnd = rdStart + fstep.offset + sdna.typeLens[fstep.typeIdx];
        if (stepEnd <= blockEnd) {
          try {
            frameStep = readInt(fstep.offset, fstep.typeIdx);
          } catch {
            // ignore
          }
        }
      }
      if (frameStep < 1) frameStep = 1;

      const totalFrames =
        frameEnd >= frameStart ? Math.floor((frameEnd - frameStart) / frameStep) + 1 : 0;

      const result = {
        frame_start: frameStart,
        frame_end: frameEnd,
        frame_step: frameStep,
        total_frames: totalFrames,
        blender_version: version,
      };
      if (activeSceneOldPtr !== null && activeSceneOldPtr !== 0n && block.oldPtr === activeSceneOldPtr) {
        return result;
      }
      if (!fallbackResult) {
        fallbackResult = result;
      }
    }
  }

  if (fallbackResult) {
    return fallbackResult;
  }

  throw new BlendParseError("No Scene block found in .blend file");
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Parse a .blend or .zip file and extract frame range metadata.
 *
 * @param {File} file - File object from an <input type="file"> or drag-and-drop
 * @returns {Promise<{frame_start: number, frame_end: number, frame_step: number, total_frames: number, blender_version: number}>}
 * @throws {BlendParseError} on invalid or unparseable files
 */
export async function parseBlendFile(file) {
  const buffer = await file.arrayBuffer();
  const uint8 = new Uint8Array(buffer);

  // ZIP handling
  if (isZip(uint8)) {
    const blendData = extractBlendFromZip(uint8);
    return parseBlendBuffer(blendData);
  }

  // Raw .blend (possibly compressed)
  return parseBlendBuffer(uint8);
}
