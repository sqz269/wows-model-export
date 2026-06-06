// Software BC6H (DXGI_FORMAT_BC6H_UF16=95 / _SF16=96) block decoder.
//
// WG ships HDR particle colour ramps (e.g. ``particles/ramps/
// fire_yellow_3_HDR.dds``) as BC6H. WebGL has no BC6H sampling without the
// rarely-present EXT_texture_compression_bptc *float* variant, so we decode
// on the CPU (off the main thread, in dds_worker) into a Float32 RGBA buffer
// the main thread wraps in a THREE.DataTexture(..., FloatType).
//
// The block decode is a faithful port of the public-domain (Unlicense / MIT)
// single-header decoder ``bcdec.h`` by Sergii Kudlai (iOrange) —
// https://github.com/iOrange/bcdec — specifically ``bcdec_bc6h_half`` and its
// helpers. bcdec.h is explicitly a drop-in single-header library; this is a
// straight algorithmic translation to TypeScript. The numeric tables
// (per-mode bit counts, the 32 two-region partition shapes, the 3-/4-bit
// interpolation weights) are dictated by the BC6H format itself (see
// https://learn.microsoft.com/windows/win32/direct3d11/bc6h-format) and are
// reproduced exactly — there is no latitude in a hardware format spec.
//
// Deviation from the C source: bcdec.h streams bits out of two 64-bit words
// (``low`` / ``high``). JS bitwise ops are 32-bit, so we instead walk the 16
// block bytes with an LSB-first bit cursor (low byte first, bit 0 first).
// That is numerically identical to the reference's shift-out scheme — each
// ``read_bits(n)`` consumes the next ``n`` least-significant unread bits — but
// sidesteps any 53-bit float / 32-bit-shift precision hazard.

// actual_bits_count[component][modeIndex]; component 0=W (base), 1..3 = dR/dG/dB
// (delta) precision. 14 columns = the 14 valid BC6H modes (internal 0..13).
const ACTUAL_BITS_COUNT: ReadonlyArray<ReadonlyArray<number>> = [
  [10, 7, 11, 11, 11, 9, 8, 8, 8, 6, 10, 11, 12, 16], //  W
  [5, 6, 5, 4, 4, 5, 6, 5, 5, 6, 10, 9, 8, 4], // dR
  [5, 6, 4, 5, 4, 5, 5, 6, 5, 6, 10, 9, 8, 4], // dG
  [5, 6, 4, 4, 5, 5, 5, 5, 6, 6, 10, 9, 8, 4], // dB
];

// The 32 two-region partition shapes. Each entry is a flat 16-element row-major
// 4x4 grid of subset ids (0 or 1). The high bit (0x80) marks a "fix-up" index
// position: subset 0's fix-up is always texel 0; subset 1's fix-up varies. At
// those positions the index is stored with one fewer bit (its MSB is implied 0).
// Bytes are taken verbatim from bcdec.h's bcdec__bc6h_partition_sets.
// prettier-ignore
const PARTITION_SETS: ReadonlyArray<ReadonlyArray<number>> = [
  [128, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 129], //  0
  [128, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 129], //  1
  [128, 1, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 0, 1, 1, 129], //  2
  [128, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 1, 1, 129], //  3
  [128, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 129], //  4
  [128, 0, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 129], //  5
  [128, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 1, 1, 1, 1, 129], //  6
  [128, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 129], //  7
  [128, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 129], //  8
  [128, 0, 1, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 129], //  9
  [128, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1, 1, 129], // 10
  [128, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 129], // 11
  [128, 0, 0, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 129], // 12
  [128, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 129], // 13
  [128, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 129], // 14
  [128, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 129], // 15
  [128, 0, 0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1, 129], // 16
  [128, 1, 129, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0], // 17
  [128, 0, 0, 0, 0, 0, 0, 0, 129, 0, 0, 0, 1, 1, 1, 0], // 18
  [128, 1, 129, 1, 0, 0, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0], // 19
  [128, 0, 129, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0], // 20
  [128, 0, 0, 0, 1, 0, 0, 0, 129, 1, 0, 0, 1, 1, 1, 0], // 21
  [128, 0, 0, 0, 0, 0, 0, 0, 129, 0, 0, 0, 1, 1, 0, 0], // 22
  [128, 1, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 129], // 23
  [128, 0, 129, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0], // 24
  [128, 0, 0, 0, 1, 0, 0, 0, 129, 0, 0, 0, 1, 1, 0, 0], // 25
  [128, 1, 129, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0], // 26
  [128, 0, 129, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 1, 0, 0], // 27
  [128, 0, 0, 1, 0, 1, 1, 1, 129, 1, 1, 0, 1, 0, 0, 0], // 28
  [128, 0, 0, 0, 1, 1, 1, 1, 129, 1, 1, 1, 0, 0, 0, 0], // 29
  [128, 1, 129, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1, 1, 1, 0], // 30
  [128, 0, 129, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0], // 31
];

const A_WEIGHT3: ReadonlyArray<number> = [0, 9, 18, 27, 37, 46, 55, 64];
// prettier-ignore
const A_WEIGHT4: ReadonlyArray<number> = [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64];

// LSB-first bit reader over a 16-byte block. Mirrors bcdec.h's low/high
// shift-out: read_bits(n) returns the next n least-significant unread bits.
class BitReader {
  private readonly bytes: Uint8Array;
  private bitPos = 0;

  constructor(bytes: Uint8Array) {
    this.bytes = bytes;
  }

  readBits(numBits: number): number {
    let result = 0;
    for (let i = 0; i < numBits; i++) {
      const p = this.bitPos++;
      const bit = (this.bytes[p >> 3] >> (p & 7)) & 1;
      result |= bit << i;
    }
    return result;
  }

  readBit(): number {
    return this.readBits(1);
  }

  // Reversed bit pull used by BC6H modes 13/14/16 — bcdec__bitstream_read_bits_r.
  readBitsR(numBits: number): number {
    let bits = this.readBits(numBits);
    let result = 0;
    while (numBits--) {
      result = (result << 1) | (bits & 1);
      bits >>= 1;
    }
    return result;
  }
}

// http://graphics.stanford.edu/~seander/bithacks.html#VariableSignExtend
function extendSign(val: number, bits: number): number {
  const shift = 32 - bits;
  return (val << shift) >> shift;
}

function transformInverse(val: number, a0: number, bits: number, isSigned: boolean): number {
  val = (val + a0) & ((1 << bits) - 1);
  if (isSigned) val = extendSign(val, bits);
  return val;
}

// Inverse-quantize an endpoint component to a 16-bit (signed) magnitude.
function unquantize(val: number, bits: number, isSigned: boolean): number {
  let unq: number;
  let s = 0;
  if (!isSigned) {
    if (bits >= 15) {
      unq = val;
    } else if (val === 0) {
      unq = 0;
    } else if (val === (1 << bits) - 1) {
      unq = 0xffff;
    } else {
      unq = ((val << 16) + 0x8000) >> bits;
    }
  } else {
    if (bits >= 16) {
      unq = val;
    } else {
      if (val < 0) {
        s = 1;
        val = -val;
      }
      if (val === 0) {
        unq = 0;
      } else if (val >= (1 << (bits - 1)) - 1) {
        unq = 0x7fff;
      } else {
        unq = ((val << 15) + 0x4000) >> (bits - 1);
      }
      if (s) unq = -unq;
    }
  }
  return unq;
}

function interpolate(a: number, b: number, weights: ReadonlyArray<number>, index: number): number {
  return (a * (64 - weights[index]) + b * weights[index] + 32) >> 6;
}

// Final scale of the interpolated value into the half-float bit pattern.
function finishUnquantize(val: number, isSigned: boolean): number {
  if (!isSigned) {
    return ((val * 31) >> 6) & 0xffff; // scale magnitude by 31/64
  }
  val = val < 0 ? -(((-val) * 31) >> 5) : (val * 31) >> 5; // scale by 31/32
  let s = 0;
  if (val < 0) {
    s = 0x8000;
    val = -val;
  }
  return (s | val) & 0xffff;
}

// IEEE half (uint16 bit pattern) -> float32. Port of bcdec__half_to_float_quick
// (rygorous half_to_float_fast4). Uses a scratch Float32Array/Uint32Array union.
const _fpU32 = new Uint32Array(1);
const _fpF32 = new Float32Array(_fpU32.buffer);
const _magicU32 = new Uint32Array(1);
const _magicF32 = new Float32Array(_magicU32.buffer);
_magicU32[0] = 113 << 23;
const SHIFTED_EXP = 0x7c00 << 13;

function halfToFloat(half: number): number {
  let u = (half & 0x7fff) << 13; // exponent/mantissa bits
  const exp = SHIFTED_EXP & u; // just the exponent
  u = (u + ((127 - 15) << 23)) >>> 0; // exponent adjust
  _fpU32[0] = u;
  if (exp === SHIFTED_EXP) {
    // Inf/NaN
    _fpU32[0] = (_fpU32[0] + ((128 - 16) << 23)) >>> 0;
  } else if (exp === 0) {
    // Zero/Denormal
    _fpU32[0] = (_fpU32[0] + (1 << 23)) >>> 0;
    _fpF32[0] -= _magicF32[0]; // renormalize
  }
  _fpU32[0] = (_fpU32[0] | ((half & 0x8000) << 16)) >>> 0; // sign bit
  return _fpF32[0];
}

// Decode one 16-byte BC6H block into 16 RGB texels (half-float bit patterns).
// `out` is a length-48 Int32Array (r,g,b per texel, row-major) of uint16 halves.
function decodeBc6hBlock(block: Uint8Array, isSigned: boolean, out: Int32Array): void {
  const bs = new BitReader(block);
  const r = [0, 0, 0, 0];
  const g = [0, 0, 0, 0];
  const b = [0, 0, 0, 0];
  let partition = 0;

  let mode = bs.readBits(2);
  if (mode > 1) {
    mode |= bs.readBits(3) << 2;
  }

  // Translate the raw 2-/5-bit mode field into the internal 0..13 index and
  // pull the mode-specific endpoint bit layout. Layout (bit assignments and
  // order) is taken verbatim from bcdec.h's switch(mode).
  switch (mode) {
    case 0b00: {
      // mode 1: 10.555 x3
      g[2] |= bs.readBit() << 4;
      b[2] |= bs.readBit() << 4;
      b[3] |= bs.readBit() << 4;
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(5);
      g[3] |= bs.readBit() << 4;
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(5);
      b[3] |= bs.readBit();
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(5);
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(5);
      b[3] |= bs.readBit() << 2;
      r[3] |= bs.readBits(5);
      b[3] |= bs.readBit() << 3;
      partition = bs.readBits(5);
      mode = 0;
      break;
    }
    case 0b01: {
      // mode 2: 7.666 x3
      g[2] |= bs.readBit() << 5;
      g[3] |= bs.readBit() << 4;
      g[3] |= bs.readBit() << 5;
      r[0] |= bs.readBits(7);
      b[3] |= bs.readBit();
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBit() << 4;
      g[0] |= bs.readBits(7);
      b[2] |= bs.readBit() << 5;
      b[3] |= bs.readBit() << 2;
      g[2] |= bs.readBit() << 4;
      b[0] |= bs.readBits(7);
      b[3] |= bs.readBit() << 3;
      b[3] |= bs.readBit() << 5;
      b[3] |= bs.readBit() << 4;
      r[1] |= bs.readBits(6);
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(6);
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(6);
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(6);
      r[3] |= bs.readBits(6);
      partition = bs.readBits(5);
      mode = 1;
      break;
    }
    case 0b00010: {
      // mode 3: 11.555, 11.444, 11.444
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(5);
      r[0] |= bs.readBit() << 10;
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(4);
      g[0] |= bs.readBit() << 10;
      b[3] |= bs.readBit();
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(4);
      b[0] |= bs.readBit() << 10;
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(5);
      b[3] |= bs.readBit() << 2;
      r[3] |= bs.readBits(5);
      b[3] |= bs.readBit() << 3;
      partition = bs.readBits(5);
      mode = 2;
      break;
    }
    case 0b00110: {
      // mode 4: 11.444, 11.555, 11.444
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(4);
      r[0] |= bs.readBit() << 10;
      g[3] |= bs.readBit() << 4;
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(5);
      g[0] |= bs.readBit() << 10;
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(4);
      b[0] |= bs.readBit() << 10;
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(4);
      b[3] |= bs.readBit();
      b[3] |= bs.readBit() << 2;
      r[3] |= bs.readBits(4);
      g[2] |= bs.readBit() << 4;
      b[3] |= bs.readBit() << 3;
      partition = bs.readBits(5);
      mode = 3;
      break;
    }
    case 0b01010: {
      // mode 5: 11.444, 11.444, 11.555
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(4);
      r[0] |= bs.readBit() << 10;
      b[2] |= bs.readBit() << 4;
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(4);
      g[0] |= bs.readBit() << 10;
      b[3] |= bs.readBit();
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(5);
      b[0] |= bs.readBit() << 10;
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(4);
      b[3] |= bs.readBit() << 1;
      b[3] |= bs.readBit() << 2;
      r[3] |= bs.readBits(4);
      b[3] |= bs.readBit() << 4;
      b[3] |= bs.readBit() << 3;
      partition = bs.readBits(5);
      mode = 4;
      break;
    }
    case 0b01110: {
      // mode 6: 9.555 x3
      r[0] |= bs.readBits(9);
      b[2] |= bs.readBit() << 4;
      g[0] |= bs.readBits(9);
      g[2] |= bs.readBit() << 4;
      b[0] |= bs.readBits(9);
      b[3] |= bs.readBit() << 4;
      r[1] |= bs.readBits(5);
      g[3] |= bs.readBit() << 4;
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(5);
      b[3] |= bs.readBit();
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(5);
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(5);
      b[3] |= bs.readBit() << 2;
      r[3] |= bs.readBits(5);
      b[3] |= bs.readBit() << 3;
      partition = bs.readBits(5);
      mode = 5;
      break;
    }
    case 0b10010: {
      // mode 7: 8.666, 8.555, 8.555
      r[0] |= bs.readBits(8);
      g[3] |= bs.readBit() << 4;
      b[2] |= bs.readBit() << 4;
      g[0] |= bs.readBits(8);
      b[3] |= bs.readBit() << 2;
      g[2] |= bs.readBit() << 4;
      b[0] |= bs.readBits(8);
      b[3] |= bs.readBit() << 3;
      b[3] |= bs.readBit() << 4;
      r[1] |= bs.readBits(6);
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(5);
      b[3] |= bs.readBit();
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(5);
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(6);
      r[3] |= bs.readBits(6);
      partition = bs.readBits(5);
      mode = 6;
      break;
    }
    case 0b10110: {
      // mode 8: 8.555, 8.666, 8.555
      r[0] |= bs.readBits(8);
      b[3] |= bs.readBit();
      b[2] |= bs.readBit() << 4;
      g[0] |= bs.readBits(8);
      g[2] |= bs.readBit() << 5;
      g[2] |= bs.readBit() << 4;
      b[0] |= bs.readBits(8);
      g[3] |= bs.readBit() << 5;
      b[3] |= bs.readBit() << 4;
      r[1] |= bs.readBits(5);
      g[3] |= bs.readBit() << 4;
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(6);
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(5);
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(5);
      b[3] |= bs.readBit() << 2;
      r[3] |= bs.readBits(5);
      b[3] |= bs.readBit() << 3;
      partition = bs.readBits(5);
      mode = 7;
      break;
    }
    case 0b11010: {
      // mode 9: 8.555, 8.555, 8.666
      r[0] |= bs.readBits(8);
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBit() << 4;
      g[0] |= bs.readBits(8);
      b[2] |= bs.readBit() << 5;
      g[2] |= bs.readBit() << 4;
      b[0] |= bs.readBits(8);
      b[3] |= bs.readBit() << 5;
      b[3] |= bs.readBit() << 4;
      r[1] |= bs.readBits(5);
      g[3] |= bs.readBit() << 4;
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(5);
      b[3] |= bs.readBit();
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(6);
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(5);
      b[3] |= bs.readBit() << 2;
      r[3] |= bs.readBits(5);
      b[3] |= bs.readBit() << 3;
      partition = bs.readBits(5);
      mode = 8;
      break;
    }
    case 0b11110: {
      // mode 10: 6.666 x3
      r[0] |= bs.readBits(6);
      g[3] |= bs.readBit() << 4;
      b[3] |= bs.readBit();
      b[3] |= bs.readBit() << 1;
      b[2] |= bs.readBit() << 4;
      g[0] |= bs.readBits(6);
      g[2] |= bs.readBit() << 5;
      b[2] |= bs.readBit() << 5;
      b[3] |= bs.readBit() << 2;
      g[2] |= bs.readBit() << 4;
      b[0] |= bs.readBits(6);
      g[3] |= bs.readBit() << 5;
      b[3] |= bs.readBit() << 3;
      b[3] |= bs.readBit() << 5;
      b[3] |= bs.readBit() << 4;
      r[1] |= bs.readBits(6);
      g[2] |= bs.readBits(4);
      g[1] |= bs.readBits(6);
      g[3] |= bs.readBits(4);
      b[1] |= bs.readBits(6);
      b[2] |= bs.readBits(4);
      r[2] |= bs.readBits(6);
      r[3] |= bs.readBits(6);
      partition = bs.readBits(5);
      mode = 9;
      break;
    }
    case 0b00011: {
      // mode 11: 10.10 x3 (no delta, explicit endpoints)
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(10);
      g[1] |= bs.readBits(10);
      b[1] |= bs.readBits(10);
      mode = 10;
      break;
    }
    case 0b00111: {
      // mode 12: 11.9 x3
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(9);
      r[0] |= bs.readBit() << 10;
      g[1] |= bs.readBits(9);
      g[0] |= bs.readBit() << 10;
      b[1] |= bs.readBits(9);
      b[0] |= bs.readBit() << 10;
      mode = 11;
      break;
    }
    case 0b01011: {
      // mode 13: 12.8 x3
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(8);
      r[0] |= bs.readBitsR(2) << 10;
      g[1] |= bs.readBits(8);
      g[0] |= bs.readBitsR(2) << 10;
      b[1] |= bs.readBits(8);
      b[0] |= bs.readBitsR(2) << 10;
      mode = 12;
      break;
    }
    case 0b01111: {
      // mode 14: 16.4 x3
      r[0] |= bs.readBits(10);
      g[0] |= bs.readBits(10);
      b[0] |= bs.readBits(10);
      r[1] |= bs.readBits(4);
      r[0] |= bs.readBitsR(6) << 10;
      g[1] |= bs.readBits(4);
      g[0] |= bs.readBitsR(6) << 10;
      b[1] |= bs.readBits(4);
      b[0] |= bs.readBitsR(6) << 10;
      mode = 13;
      break;
    }
    default: {
      // Reserved modes (10011/10111/11011/11111) decode to all-zero RGB.
      for (let p = 0; p < 16; p++) {
        out[p * 3 + 0] = 0;
        out[p * 3 + 1] = 0;
        out[p * 3 + 2] = 0;
      }
      return;
    }
  }

  const numPartitions = mode >= 10 ? 0 : 1;
  const actualBits0 = ACTUAL_BITS_COUNT[0][mode];

  if (isSigned) {
    r[0] = extendSign(r[0], actualBits0);
    g[0] = extendSign(g[0], actualBits0);
    b[0] = extendSign(b[0], actualBits0);
  }

  // Modes 10/11 (internal) store endpoints explicitly with no delta; everything
  // else delta-codes endpoints 1.. against endpoint 0. Sign-extend the deltas
  // (always for delta modes; for the explicit modes only when signed).
  const epCount = (numPartitions + 1) * 2;
  if ((mode !== 9 && mode !== 10) || isSigned) {
    for (let i = 1; i < epCount; i++) {
      r[i] = extendSign(r[i], ACTUAL_BITS_COUNT[1][mode]);
      g[i] = extendSign(g[i], ACTUAL_BITS_COUNT[2][mode]);
      b[i] = extendSign(b[i], ACTUAL_BITS_COUNT[3][mode]);
    }
  }

  if (mode !== 9 && mode !== 10) {
    for (let i = 1; i < epCount; i++) {
      r[i] = transformInverse(r[i], r[0], actualBits0, isSigned);
      g[i] = transformInverse(g[i], g[0], actualBits0, isSigned);
      b[i] = transformInverse(b[i], b[0], actualBits0, isSigned);
    }
  }

  for (let i = 0; i < epCount; i++) {
    r[i] = unquantize(r[i], actualBits0, isSigned);
    g[i] = unquantize(g[i], actualBits0, isSigned);
    b[i] = unquantize(b[i], actualBits0, isSigned);
  }

  const weights = mode >= 10 ? A_WEIGHT4 : A_WEIGHT3;
  for (let i = 0; i < 4; i++) {
    for (let j = 0; j < 4; j++) {
      const texel = i * 4 + j;
      let partitionSet: number;
      if (mode >= 10) {
        partitionSet = i | j ? 0 : 128;
      } else {
        partitionSet = PARTITION_SETS[partition][texel];
      }

      let indexBits = mode >= 10 ? 4 : 3;
      // Fix-up index positions store one fewer bit (implied-0 MSB).
      if (partitionSet & 0x80) indexBits--;
      partitionSet &= 0x01;

      const index = bs.readBits(indexBits);
      const epI = partitionSet * 2;

      out[texel * 3 + 0] = finishUnquantize(
        interpolate(r[epI], r[epI + 1], weights, index),
        isSigned,
      );
      out[texel * 3 + 1] = finishUnquantize(
        interpolate(g[epI], g[epI + 1], weights, index),
        isSigned,
      );
      out[texel * 3 + 2] = finishUnquantize(
        interpolate(b[epI], b[epI + 1], weights, index),
        isSigned,
      );
    }
  }
}

/**
 * Decode a full BC6H mip (4x4 blocks, 16 bytes/block) into a Float32Array of
 * RGBA pixels in row-major order. R/G/B carry the decoded HDR value as float32;
 * A is forced to 1.0 (BC6H has no alpha). `signed` selects UF16 (false, the
 * live fire-ramp case) vs SF16 (true).
 *
 * `blockData` must hold exactly ``ceil(w/4)*ceil(h/4)*16`` bytes.
 */
export function decodeBc6hToRgbaFloat(
  blockData: Uint8Array,
  width: number,
  height: number,
  signed: boolean,
): Float32Array {
  const out = new Float32Array(width * height * 4);
  const blockW = Math.max(1, Math.ceil(width / 4));
  const blockH = Math.max(1, Math.ceil(height / 4));
  const halves = new Int32Array(48); // 16 texels * 3 (uint16 half bit patterns)

  for (let by = 0; by < blockH; by++) {
    for (let bx = 0; bx < blockW; bx++) {
      const blockIdx = by * blockW + bx;
      const off = blockIdx * 16;
      const block = blockData.subarray(off, off + 16);
      decodeBc6hBlock(block, signed, halves);

      for (let py = 0; py < 4; py++) {
        const y = by * 4 + py;
        if (y >= height) continue;
        for (let px = 0; px < 4; px++) {
          const x = bx * 4 + px;
          if (x >= width) continue;
          const texel = py * 4 + px;
          const oi = (y * width + x) * 4;
          out[oi + 0] = halfToFloat(halves[texel * 3 + 0]);
          out[oi + 1] = halfToFloat(halves[texel * 3 + 1]);
          out[oi + 2] = halfToFloat(halves[texel * 3 + 2]);
          out[oi + 3] = 1.0;
        }
      }
    }
  }
  return out;
}
