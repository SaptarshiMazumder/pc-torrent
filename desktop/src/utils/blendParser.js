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

// How much of a .blend to read for analysis. SDNA + scene blocks are always
// near the start, so 32 MB is enough for any file regardless of total size.
export const MAX_CLIENT_PARSE_BYTES = 32 * 1024 * 1024; // 32 MB
const MAX_ZIP_FULL_FALLBACK_BYTES = 512 * 1024 * 1024; // 512 MB

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

function normalizeFieldName(rawName) {
  let clean = rawName.replace(/^\*+/, "");
  const bracket = clean.indexOf("[");
  if (bracket !== -1) clean = clean.substring(0, bracket);
  if (clean.startsWith("(") && clean.includes(")")) {
    clean = clean.split(")")[0].replace(/^\(/, "").replace(/^\*+/, "");
  }
  return clean;
}

function findFieldInfo(sdna, structIdx, fieldName, ptrSize) {
  const { fields } = sdna.structs[structIdx];
  let offset = 0;

  for (const { typeIdx, nameIdx } of fields) {
    const rawName = sdna.names[nameIdx];
    const clean = normalizeFieldName(rawName);
    const size = fieldByteSize(sdna, typeIdx, nameIdx, ptrSize);

    if (clean === fieldName) {
      return { offset, typeIdx, size, rawName };
    }

    offset += size;
  }

  return null;
}

function findFieldOffset(sdna, structIdx, fieldName, ptrSize) {
  const info = findFieldInfo(sdna, structIdx, fieldName, ptrSize);
  if (!info) return null;
  return { offset: info.offset, typeIdx: info.typeIdx };
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

function readFixedString(view, offset, maxBytes) {
  const end = Math.min(view.byteLength, offset + maxBytes);
  let out = "";
  for (let i = offset; i < end; i++) {
    const b = view.getUint8(i);
    if (b === 0) break;
    out += String.fromCharCode(b);
  }
  return out;
}

function readIntegerBySize(view, offset, size, littleEndian, signed = true) {
  if (offset < 0 || offset + size > view.byteLength) {
    return null;
  }
  if (size === 1) return signed ? view.getInt8(offset) : view.getUint8(offset);
  if (size === 2) return signed ? view.getInt16(offset, littleEndian) : view.getUint16(offset, littleEndian);
  if (size === 4) return signed ? view.getInt32(offset, littleEndian) : view.getUint32(offset, littleEndian);
  if (size === 8) {
    const v = signed ? view.getBigInt64(offset, littleEndian) : view.getBigUint64(offset, littleEndian);
    return Number(v);
  }
  return null;
}

function readFloatBySize(view, offset, size, littleEndian) {
  if (offset < 0 || offset + size > view.byteLength) return null;
  if (size === 4) return view.getFloat32(offset, littleEndian);
  if (size === 8) return view.getFloat64(offset, littleEndian);
  return null;
}

function readPointer(view, offset, ptrSize, littleEndian) {
  if (offset < 0 || offset + ptrSize > view.byteLength) return null;
  if (ptrSize === 8) {
    return view.getBigUint64(offset, littleEndian);
  }
  return BigInt(view.getUint32(offset, littleEndian));
}

function normalizeBlenderIdName(name) {
  if (!name) return "";
  if (name.length >= 3 && /^[A-Za-z]{2}/.test(name)) {
    return name.slice(2);
  }
  return name;
}

function findFirstField(sdna, structIdx, ptrSize, candidates) {
  for (const candidate of candidates) {
    const info = findFieldInfo(sdna, structIdx, candidate, ptrSize);
    if (info) return info;
  }
  return null;
}

function buildBlockCatalog(view, ptrSize, littleEndian, dataStart, bheadLayout, sdna) {
  const blocks = [];
  const byPtr = new Map();
  const byType = new Map();

  for (const block of iterBlocks(view, ptrSize, littleEndian, dataStart, bheadLayout)) {
    let typeName = null;
    if (block.sdnaIndex >= 0 && block.sdnaIndex < sdna.structs.length) {
      const structDef = sdna.structs[block.sdnaIndex];
      typeName = sdna.types[structDef.typeIdx];
    }
    const enriched = { ...block, typeName };
    blocks.push(enriched);
    byPtr.set(block.oldPtr.toString(), enriched);
    if (typeName) {
      if (!byType.has(typeName)) byType.set(typeName, []);
      byType.get(typeName).push(enriched);
    }
  }

  return { blocks, byPtr, byType };
}

function readIdNameFromBlock(view, block, sdna, ptrSize) {
  if (!block || block.sdnaIndex < 0 || block.sdnaIndex >= sdna.structs.length) return "";
  const structIdx = block.sdnaIndex;
  const idField = findFieldInfo(sdna, structIdx, "id", ptrSize);
  if (!idField) return "";
  const idStructName = sdna.types[idField.typeIdx];
  const idStructIdx = sdna.structByName[idStructName];
  if (idStructIdx === undefined) return "";
  const nameField = findFieldInfo(sdna, idStructIdx, "name", ptrSize);
  if (!nameField) return "";
  const abs = block.dataOffset + idField.offset + nameField.offset;
  const raw = readFixedString(view, abs, nameField.size || 66);
  return normalizeBlenderIdName(raw);
}

function listFromListBase(
  view,
  ownerBlock,
  listField,
  listBaseInfo,
  ptrSize,
  littleEndian,
  byPtr,
  sdna,
  expectedTypeName = null,
) {
  if (!ownerBlock || !listField || !listBaseInfo) return [];
  const firstField = findFieldInfo(sdna, listBaseInfo, "first", ptrSize);
  if (!firstField) return [];

  const firstAbs = ownerBlock.dataOffset + listField.offset + firstField.offset;
  const firstPtr = readPointer(view, firstAbs, ptrSize, littleEndian);
  if (!firstPtr || firstPtr === 0n) return [];

  const out = [];
  const seen = new Set();
  let ptr = firstPtr;
  while (ptr && ptr !== 0n) {
    const key = ptr.toString();
    if (seen.has(key)) break;
    seen.add(key);

    const node = byPtr.get(key);
    if (!node) break;
    if (!expectedTypeName || node.typeName === expectedTypeName) {
      out.push(node);
    }

    if (node.sdnaIndex < 0 || node.sdnaIndex >= sdna.structs.length) break;
    const nextField = findFieldInfo(sdna, node.sdnaIndex, "next", ptrSize);
    if (!nextField) break;
    const nextAbs = node.dataOffset + nextField.offset;
    const nextPtr = readPointer(view, nextAbs, ptrSize, littleEndian);
    if (!nextPtr || nextPtr === 0n) break;
    ptr = nextPtr;
  }

  return out;
}

function mapImageFormatCodeToName(code) {
  const map = {
    0: "TGA",
    1: "IRIS",
    2: "HAMX",
    3: "FTYPE",
    4: "JPEG90",
    5: "MOVIE",
    6: "IRIZ",
    7: "RAWTGA",
    8: "AVIRAW",
    9: "AVIJPEG",
    10: "PNG",
    11: "BMP",
    12: "HDR",
    13: "TIFF",
    14: "OPEN_EXR",
    15: "FFMPEG",
    16: "FRAMESERVER",
    17: "CINEON",
    18: "DPX",
    19: "MULTILAYER",
    20: "DDS",
    21: "JP2",
    22: "OPEN_EXR_MULTILAYER",
    23: "WEBP",
  };
  return map[code] || null;
}

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

  const catalog = buildBlockCatalog(view, ptrSize, littleEndian, dataStart, bheadLayout, sdna);

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
  const sfra = findFieldInfo(sdna, rdIdx, "sfra", ptrSize);
  const efra = findFieldInfo(sdna, rdIdx, "efra", ptrSize);
  const fstep = findFieldInfo(sdna, rdIdx, "frame_step", ptrSize);

  if (!sfra || !efra) {
    throw new BlendParseError("Could not find sfra/efra fields in RenderData");
  }

  const fpsField = findFirstField(sdna, rdIdx, ptrSize, ["frs_sec", "fps"]);
  const fpsBaseField = findFirstField(sdna, rdIdx, ptrSize, ["frs_sec_base", "fps_base"]);
  const mapOldField = findFirstField(sdna, rdIdx, ptrSize, ["framapto", "frame_map_old"]);
  const mapNewField = findFirstField(sdna, rdIdx, ptrSize, ["images", "frame_map_new", "framelen"]);
  const xschField = findFirstField(sdna, rdIdx, ptrSize, ["xsch"]);
  const yschField = findFirstField(sdna, rdIdx, ptrSize, ["ysch"]);
  const sizeField = findFirstField(sdna, rdIdx, ptrSize, ["size", "resolution_percentage"]);
  const engineField = findFirstField(sdna, rdIdx, ptrSize, ["engine"]);
  const picField = findFirstField(sdna, rdIdx, ptrSize, ["pic"]);
  const imFormatField = findFirstField(sdna, rdIdx, ptrSize, ["im_format"]);

  const listBaseIdx = sdna.structByName["ListBase"];
  const sceneMarkersField = findFirstField(sdna, sceneIdx, ptrSize, ["markers"]);
  const sceneViewLayersField = findFirstField(sdna, sceneIdx, ptrSize, ["view_layers"]);
  const sceneCameraField = findFirstField(sdna, sceneIdx, ptrSize, ["camera"]);
  const sceneCyclesField = findFirstField(sdna, sceneIdx, ptrSize, ["cycles"]);

  const viewLayerIdx = sdna.structByName["ViewLayer"];
  const viewLayerNameField = viewLayerIdx !== undefined
    ? findFirstField(sdna, viewLayerIdx, ptrSize, ["name"])
    : null;

  const markerIdx = sdna.structByName["TimeMarker"];
  const markerFrameField = markerIdx !== undefined
    ? findFirstField(sdna, markerIdx, ptrSize, ["frame"])
    : null;
  const markerCameraField = markerIdx !== undefined
    ? findFirstField(sdna, markerIdx, ptrSize, ["camera"])
    : null;

  const objectIdx = sdna.structByName["Object"];
  const objectDataField = objectIdx !== undefined
    ? findFirstField(sdna, objectIdx, ptrSize, ["data"])
    : null;

  const unsupportedFields = [];

  // Build object -> camera map for human camera names.
  const objectNameByPtr = new Map();
  const cameraObjectNames = [];
  if (objectIdx !== undefined && objectDataField) {
    const objectBlocks = catalog.byType.get("Object") || [];
    for (const objBlock of objectBlocks) {
      const objectName = readIdNameFromBlock(view, objBlock, sdna, ptrSize) || "Camera";
      objectNameByPtr.set(objBlock.oldPtr.toString(), objectName);
      const dataPtr = readPointer(
        view,
        objBlock.dataOffset + objectDataField.offset,
        ptrSize,
        littleEndian
      );
      if (!dataPtr || dataPtr === 0n) continue;
      const dataBlock = catalog.byPtr.get(dataPtr.toString());
      if (dataBlock && dataBlock.typeName === "Camera") {
        cameraObjectNames.push(objectName);
      }
    }
  } else {
    unsupportedFields.push("cameras");
  }

  // Resolve active scene pointer from FileGlobal.curscene so we match Blender UI.
  let activeSceneOldPtr = null;
  const fileGlobalIdx = sdna.structByName["FileGlobal"];
  if (fileGlobalIdx !== undefined) {
    const curSceneField = findFieldInfo(sdna, fileGlobalIdx, "curscene", ptrSize);
    if (curSceneField) {
      for (const block of catalog.blocks) {
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

  // Pass 2: collect scene metadata
  const sceneTypeIdx = sdna.structs[sceneIdx].typeIdx;
  const scenes = [];
  const sceneBlocks = catalog.byType.get("Scene") || [];

  for (const block of sceneBlocks) {
    if (block.sdnaIndex >= sdna.structs.length) continue;
    const blockTypeIdx = sdna.structs[block.sdnaIndex].typeIdx;
    if (blockTypeIdx !== sceneTypeIdx && sdna.types[blockTypeIdx] !== "Scene") continue;

    const rdStart = block.dataOffset + rField.offset;
    const blockEnd = block.dataOffset + block.size;
    const readIntField = (fieldInfo, signed = true) => {
      if (!fieldInfo) return null;
      const absOff = rdStart + fieldInfo.offset;
      if (absOff + fieldInfo.size > blockEnd) return null;
      return readIntegerBySize(view, absOff, fieldInfo.size, littleEndian, signed);
    };

    const frameStart = readIntField(sfra);
    const frameEnd = readIntField(efra);
    let frameStep = readIntField(fstep);
    if (!Number.isInteger(frameStart) || !Number.isInteger(frameEnd)) {
      continue;
    }
    if (!Number.isInteger(frameStep) || frameStep < 1) frameStep = 1;
    const totalFrames = frameEnd >= frameStart ? Math.floor((frameEnd - frameStart) / frameStep) + 1 : 0;

    const fps = readIntField(fpsField, false);
    const fpsBase = fpsBaseField
      ? readFloatBySize(view, rdStart + fpsBaseField.offset, fpsBaseField.size, littleEndian)
      : null;
    const frameMapOld = readIntField(mapOldField, false);
    const frameMapNew = readIntField(mapNewField, false);
    const resolutionX = readIntField(xschField, false);
    const resolutionY = readIntField(yschField, false);
    const resolutionPctRaw = readIntField(sizeField, false);
    const resolutionPercentage = Number.isFinite(resolutionPctRaw) ? resolutionPctRaw : 100;
    const engine = engineField
      ? readFixedString(view, rdStart + engineField.offset, engineField.size || 32)
      : "";
    const outputPathPattern = picField
      ? readFixedString(view, rdStart + picField.offset, picField.size || 1024)
      : "";

    let output = {
      path_pattern: outputPathPattern || null,
      file_format: null,
      color_mode: null,
      color_depth: null,
      compression: null,
      quality: null,
      exr_codec: null,
    };
    if (imFormatField) {
      const imStructName = sdna.types[imFormatField.typeIdx];
      const imStructIdx = sdna.structByName[imStructName];
      if (imStructIdx !== undefined) {
        const imStart = rdStart + imFormatField.offset;
        const imFmtField = findFirstField(sdna, imStructIdx, ptrSize, ["imtype", "file_format"]);
        const planesField = findFirstField(sdna, imStructIdx, ptrSize, ["planes", "color_mode"]);
        const depthField = findFirstField(sdna, imStructIdx, ptrSize, ["depth", "color_depth"]);
        const qualityField = findFirstField(sdna, imStructIdx, ptrSize, ["quality"]);
        const compressField = findFirstField(sdna, imStructIdx, ptrSize, ["compress", "compression"]);
        const exrCodecField = findFirstField(sdna, imStructIdx, ptrSize, ["exr_codec"]);

        if (imFmtField) {
          const code = readIntegerBySize(
            view,
            imStart + imFmtField.offset,
            imFmtField.size,
            littleEndian,
            false
          );
          output.file_format = mapImageFormatCodeToName(code) || (code != null ? String(code) : null);
        }
        if (planesField) {
          const v = readIntegerBySize(view, imStart + planesField.offset, planesField.size, littleEndian, false);
          output.color_mode = v != null ? String(v) : null;
        }
        if (depthField) {
          const v = readIntegerBySize(view, imStart + depthField.offset, depthField.size, littleEndian, false);
          output.color_depth = v != null ? String(v) : null;
        }
        if (qualityField) {
          output.quality = readIntegerBySize(
            view, imStart + qualityField.offset, qualityField.size, littleEndian, false
          );
        }
        if (compressField) {
          output.compression = readIntegerBySize(
            view, imStart + compressField.offset, compressField.size, littleEndian, false
          );
        }
        if (exrCodecField) {
          const v = readIntegerBySize(
            view, imStart + exrCodecField.offset, exrCodecField.size, littleEndian, false
          );
          output.exr_codec = v != null ? String(v) : null;
        }
      } else {
        unsupportedFields.push("output.image_format");
      }
    } else {
      unsupportedFields.push("output.image_format");
    }

    let cycles = {
      samples: null,
      adaptive_sampling: null,
      denoise: null,
    };

    if (sceneCyclesField) {
      let cyclesStructIdx = sdna.structByName[sdna.types[sceneCyclesField.typeIdx]];
      let cyclesDataOffset = block.dataOffset + sceneCyclesField.offset;

      const isPointerLike =
        sceneCyclesField.rawName.startsWith("*") || sceneCyclesField.size === ptrSize;

      if (isPointerLike) {
        const cyclesPtr = readPointer(
          view,
          block.dataOffset + sceneCyclesField.offset,
          ptrSize,
          littleEndian
        );
        if (cyclesPtr && cyclesPtr !== 0n) {
          const cyclesBlock = catalog.byPtr.get(cyclesPtr.toString());
          if (cyclesBlock && cyclesBlock.sdnaIndex >= 0 && cyclesBlock.sdnaIndex < sdna.structs.length) {
            cyclesStructIdx = cyclesBlock.sdnaIndex;
            cyclesDataOffset = cyclesBlock.dataOffset;
          }
        }
      }

      if (cyclesStructIdx !== undefined && cyclesStructIdx !== null) {
        const samplesField = findFirstField(sdna, cyclesStructIdx, ptrSize, ["samples", "aa_samples"]);
        const adaptiveField = findFirstField(sdna, cyclesStructIdx, ptrSize, [
          "use_adaptive_sampling",
          "use_adaptive_sample",
        ]);
        const denoiseField = findFirstField(sdna, cyclesStructIdx, ptrSize, [
          "use_denoising",
          "use_preview_denoising",
          "use_denoise",
        ]);

        if (samplesField) {
          cycles.samples = readIntegerBySize(
            view,
            cyclesDataOffset + samplesField.offset,
            samplesField.size,
            littleEndian,
            false
          );
        }
        if (adaptiveField) {
          const v = readIntegerBySize(
            view,
            cyclesDataOffset + adaptiveField.offset,
            adaptiveField.size,
            littleEndian,
            false
          );
          cycles.adaptive_sampling = v != null ? Boolean(v) : null;
        }
        if (denoiseField) {
          const v = readIntegerBySize(
            view,
            cyclesDataOffset + denoiseField.offset,
            denoiseField.size,
            littleEndian,
            false
          );
          cycles.denoise = v != null ? Boolean(v) : null;
        }
      }
    }

    let activeCameraName = null;
    const cameraNames = [];
    if (sceneCameraField) {
      const camPtr = readPointer(view, block.dataOffset + sceneCameraField.offset, ptrSize, littleEndian);
      if (camPtr && camPtr !== 0n) {
        activeCameraName = objectNameByPtr.get(camPtr.toString()) || null;
        if (activeCameraName) cameraNames.push(activeCameraName);
      }
    } else {
      unsupportedFields.push("scene.camera");
    }

    const viewLayers = [];
    if (sceneViewLayersField && listBaseIdx !== undefined && viewLayerNameField) {
      const layerBlocks = listFromListBase(
        view,
        block,
        sceneViewLayersField,
        listBaseIdx,
        ptrSize,
        littleEndian,
        catalog.byPtr,
        sdna,
        "ViewLayer"
      );
      for (const layerBlock of layerBlocks) {
        const name = readFixedString(
          view,
          layerBlock.dataOffset + viewLayerNameField.offset,
          viewLayerNameField.size || 64
        );
        if (name) viewLayers.push(name);
      }
    } else {
      unsupportedFields.push("scene.view_layers");
    }

    const cameraCuts = [];
    if (sceneMarkersField && listBaseIdx !== undefined && markerIdx !== undefined && markerFrameField) {
      const markerBlocks = listFromListBase(
        view,
        block,
        sceneMarkersField,
        listBaseIdx,
        ptrSize,
        littleEndian,
        catalog.byPtr,
        sdna,
        "TimeMarker"
      );
      for (const markerBlock of markerBlocks) {
        const frame = readIntegerBySize(
          view,
          markerBlock.dataOffset + markerFrameField.offset,
          markerFrameField.size,
          littleEndian,
          true
        );
        let cameraName = null;
        if (markerCameraField) {
          const camPtr = readPointer(
            view,
            markerBlock.dataOffset + markerCameraField.offset,
            ptrSize,
            littleEndian
          );
          if (camPtr && camPtr !== 0n) {
            cameraName = objectNameByPtr.get(camPtr.toString()) || null;
            if (!cameraName) {
              const camObjBlock = catalog.byPtr.get(camPtr.toString());
              if (camObjBlock) {
                cameraName = readIdNameFromBlock(view, camObjBlock, sdna, ptrSize) || null;
              }
            }
          }
        }
        if (Number.isInteger(frame)) {
          cameraCuts.push({ frame, camera_name: cameraName });
          if (cameraName) cameraNames.push(cameraName);
        }
      }
      cameraCuts.sort((a, b) => a.frame - b.frame);
    } else {
      unsupportedFields.push("scene.camera_cuts");
    }

    for (const globalCameraName of cameraObjectNames) {
      cameraNames.push(globalCameraName);
    }

    const uniqueCameraNames = Array.from(new Set(cameraNames.filter(Boolean)));
    if (activeCameraName && uniqueCameraNames.includes(activeCameraName)) {
      uniqueCameraNames.splice(uniqueCameraNames.indexOf(activeCameraName), 1);
      uniqueCameraNames.unshift(activeCameraName);
    }

    const sceneName = readIdNameFromBlock(view, block, sdna, ptrSize) || `Scene_${scenes.length + 1}`;
    scenes.push({
      name: sceneName,
      is_active: activeSceneOldPtr !== null && activeSceneOldPtr !== 0n
        ? block.oldPtr === activeSceneOldPtr
        : scenes.length === 0,
      frame_start: frameStart,
      frame_end: frameEnd,
      frame_step: frameStep,
      total_frames: totalFrames,
      fps: fps,
      fps_base: fpsBase,
      frame_map_old: frameMapOld,
      frame_map_new: frameMapNew,
      engine: engine || null,
      resolution_x: resolutionX,
      resolution_y: resolutionY,
      resolution_percentage: resolutionPercentage,
      output,
      cycles,
      active_camera: activeCameraName,
      cameras: uniqueCameraNames,
      view_layers: Array.from(new Set(viewLayers)),
      camera_cuts: cameraCuts,
    });
  }

  if (scenes.length === 0) {
    throw new BlendParseError("No Scene block found in .blend file");
  }

  const activeScene = scenes.find((scene) => scene.is_active) || scenes[0];
  if (!activeScene) {
    throw new BlendParseError("No active Scene could be determined");
  }

  return {
    frame_start: activeScene.frame_start,
    frame_end: activeScene.frame_end,
    frame_step: activeScene.frame_step,
    total_frames: activeScene.total_frames,
    blender_version: version,
    active_scene: activeScene.name,
    cameras: activeScene.cameras || [],
    camera_cuts: activeScene.camera_cuts || [],
    view_layers: activeScene.view_layers || [],
    timeline_defaults: {
      frame_start: activeScene.frame_start,
      frame_end: activeScene.frame_end,
      frame_step: activeScene.frame_step,
      fps: activeScene.fps,
      frame_map_old: activeScene.frame_map_old,
      frame_map_new: activeScene.frame_map_new,
    },
    output_defaults: activeScene.output || null,
    render_defaults: {
      engine: activeScene.engine || null,
      resolution_x: activeScene.resolution_x,
      resolution_y: activeScene.resolution_y,
      resolution_percentage: activeScene.resolution_percentage,
      cycles_samples: activeScene.cycles?.samples ?? null,
      cycles_adaptive_sampling: activeScene.cycles?.adaptive_sampling ?? null,
      cycles_denoise: activeScene.cycles?.denoise ?? null,
    },
    scenes,
    unsupported_fields: Array.from(new Set(unsupportedFields)),
  };
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
export function parseBlendBytes(input) {
  const uint8 = input instanceof Uint8Array ? new Uint8Array(input) : new Uint8Array(input);
  if (isZip(uint8)) {
    const blendData = extractBlendFromZip(uint8);
    return parseBlendBuffer(blendData);
  }
  return parseBlendBuffer(uint8);
}

export async function parseBlendFile(file, options = {}) {
  const maxBytes =
    Number.isFinite(options?.maxBytes) && options.maxBytes > 0
      ? options.maxBytes
      : Number.POSITIVE_INFINITY;
  // Legacy default is full-file parse.
  // If maxBytes is passed explicitly, parsing is bounded to the first chunk.
  const declaredSize = Number(file?.size);
  const needsSlice = Number.isFinite(declaredSize) && declaredSize > maxBytes;
  const slice = needsSlice ? file.slice(0, maxBytes) : file;

  const buffer = await slice.arrayBuffer();
  const uint8 = new Uint8Array(buffer);

  // ZIP handling — if the .blend is inside a zip and we sliced, the embedded
  // .blend might be truncated. Retry with full read only for reasonably sized ZIPs.
  if (isZip(uint8)) {
    try {
      const blendData = extractBlendFromZip(uint8);
      return parseBlendBuffer(blendData);
    } catch (e) {
      const canFullRetry =
        needsSlice &&
        Number.isFinite(declaredSize) &&
        declaredSize <= MAX_ZIP_FULL_FALLBACK_BYTES;
      if (canFullRetry) {
        const fullBuffer = await file.arrayBuffer();
        const fullUint8 = new Uint8Array(fullBuffer);
        const blendData = extractBlendFromZip(fullUint8);
        return parseBlendBuffer(blendData);
      }
      if (needsSlice) {
        throw new BlendParseError(
          "Could not analyze ZIP from the initial chunk. Upload can continue; server-side parsing will run after upload."
        );
      }
      throw e;
    }
  }

  // Raw .blend (possibly gzip/zstd compressed)
  return parseBlendBuffer(uint8);
}
