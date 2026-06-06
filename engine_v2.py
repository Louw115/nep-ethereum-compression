"""
Neurop Compression Engine — NC-v2
Four-layer compression for Ethereum/blockchain JSON:

1. HEX → BINARY  : "0x..." strings stored as raw bytes (saves ~55% on hashes/addresses)
2. SCHEMA STRIP   : Field layout stored once, only values streamed per record
3. DELTA ENCODE   : Sequential numeric fields (blockNumber, nonce, gasPrice…)
4. ADDRESS TABLE  : Popular addresses (contracts, DEXes) stored as 2-byte index

Output is then gzip-compressed, giving typical 4-7x on blockchain JSON.
"""

import json
import struct
import gzip
import re
from typing import Any

MAGIC    = b"NC2\x02"
SEPARATOR = b"\xff\xfe\xfd\xfc"

TYPE_NULL   = 0x00
TYPE_HEX    = 0x01
TYPE_STR    = 0x02
TYPE_UINT   = 0x03
TYPE_BOOL   = 0x04
TYPE_ARRAY  = 0x05
TYPE_OBJ    = 0x06
TYPE_FLOAT  = 0x08
TYPE_HEXREF = 0x09  # address table reference

HEX_RE = re.compile(r'^0x[0-9a-fA-F]*$')

DELTA_FIELDS = {
    "blockNumber", "nonce", "transactionIndex", "gasPrice",
    "maxFeePerGas", "maxPriorityFeePerGas", "baseFeePerGas",
    "timestamp", "number", "logIndex", "index", "validatorIndex",
    "gasUsed", "cumulativeGasUsed", "gasLimit",
}

DELTA_FLAG = 0xFF
EXT_LEN    = 0xFE


def is_hex(v: str) -> bool:
    return isinstance(v, str) and bool(HEX_RE.match(v))


def hex_to_bin(v: str) -> tuple:
    """
    Convert '0x...' string to (raw_bytes, original_hex_char_count).
    Preserves odd-length info for perfect roundtrip.
    """
    h = v[2:]
    orig_len = len(h)
    if not h:
        return b"", 0
    padded = h if len(h) % 2 == 0 else "0" + h
    return bytes.fromhex(padded), orig_len


def bin_to_hex(raw: bytes, orig_hex_len: int) -> str:
    """Reconstruct exact original hex string from binary + original length."""
    if orig_hex_len == 0:
        return "0x"
    full = raw.hex()                          # always even length
    # Trim leading padding if original was odd
    if len(full) > orig_hex_len:
        full = full[len(full) - orig_hex_len:]
    return "0x" + full


def write_u8(n: int)  -> bytes: return struct.pack("B", n)
def write_u16(n: int) -> bytes: return struct.pack(">H", n)
def write_u32(n: int) -> bytes: return struct.pack(">I", n)
def write_i64(n: int) -> bytes: return struct.pack(">q", n)

def read_u8(d, o):  return struct.unpack("B",  d[o:o+1])[0], o+1
def read_u16(d, o): return struct.unpack(">H", d[o:o+2])[0], o+2
def read_u32(d, o): return struct.unpack(">I", d[o:o+4])[0], o+4
def read_i64(d, o): return struct.unpack(">q", d[o:o+8])[0], o+8


def encode_varint(n: int) -> bytes:
    n = int(n)
    result = []
    while n >= 0x80:
        result.append((n & 0x7F) | 0x80)
        n >>= 7
    result.append(n)
    return bytes(result)


def decode_varint(data: bytes, offset: int) -> tuple:
    result = shift = 0
    while True:
        b = data[offset]; offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, offset


class NC2Encoder:
    def __init__(self):
        self.schema_map   = {}   # tuple(keys) → schema_id
        self.addr_table   = {}   # hex_str → index  (for 20-byte addresses)
        self.addr_list    = []   # index → hex_str
        self.addr_freq    = {}   # hex_str → count (pre-scan)
        self.delta_state  = {}   # field_name → last int value

    def prescan(self, val: Any):
        """Count address frequencies before encoding to build lookup table."""
        if isinstance(val, str) and is_hex(val) and len(val) == 42:  # 20-byte address
            self.addr_freq[val] = self.addr_freq.get(val, 0) + 1
        elif isinstance(val, dict):
            for v in val.values(): self.prescan(v)
        elif isinstance(val, list):
            for v in val: self.prescan(v)

    def build_addr_table(self, min_freq: int = 2):
        """Populate address table with frequently-seen addresses."""
        popular = sorted(
            [(addr, cnt) for addr, cnt in self.addr_freq.items() if cnt >= min_freq],
            key=lambda x: -x[1]
        )[:65535]
        for i, (addr, _) in enumerate(popular):
            self.addr_table[addr] = i
            self.addr_list.append(addr)

    def encode_value(self, val: Any, field_name: str = "") -> bytes:
        if val is None:
            return write_u8(TYPE_NULL)

        if isinstance(val, bool):
            return write_u8(TYPE_BOOL) + write_u8(int(val))

        if isinstance(val, int):
            vb = encode_varint(val)
            return write_u8(TYPE_UINT) + write_u16(len(vb)) + vb

        if isinstance(val, float):
            return write_u8(TYPE_FLOAT) + struct.pack(">d", val)

        if isinstance(val, str):
            if is_hex(val):
                # Check address table first (20-byte addresses = 42-char hex strings)
                if len(val) == 42 and val in self.addr_table:
                    idx = self.addr_table[val]
                    return write_u8(TYPE_HEXREF) + write_u16(idx)

                raw, orig_hex_len = hex_to_bin(val)

                # Delta encoding for small numeric hex fields
                if field_name in DELTA_FIELDS and orig_hex_len <= 16:
                    int_val = int(val, 16) if val != "0x" else 0
                    prev = self.delta_state.get(field_name, 0)
                    delta = int_val - prev
                    self.delta_state[field_name] = int_val
                    # [TYPE_HEX][DELTA_FLAG][orig_hex_len][delta i64]
                    return (write_u8(TYPE_HEX) + write_u8(DELTA_FLAG) +
                            write_u8(orig_hex_len) + write_i64(delta))

                # Regular hex binary storage
                # [TYPE_HEX][orig_hex_len_byte or EXT_LEN][optional u32][raw_bytes]
                # Use EXT_LEN marker + u32 for anything >= 254 chars
                # (real contract bytecode input fields can be 100KB+)
                if orig_hex_len <= 0xFD:
                    return write_u8(TYPE_HEX) + write_u8(orig_hex_len) + raw
                else:
                    return write_u8(TYPE_HEX) + write_u8(EXT_LEN) + write_u32(orig_hex_len) + raw

            else:
                sb = val.encode("utf-8")
                return write_u8(TYPE_STR) + write_u16(len(sb)) + sb

        if isinstance(val, list):
            parts = [write_u8(TYPE_ARRAY) + write_u16(len(val))]
            for item in val:
                parts.append(self.encode_value(item, field_name))
            return b"".join(parts)

        if isinstance(val, dict):
            keys = list(val.keys())
            schema_key = tuple(keys)
            if schema_key not in self.schema_map:
                self.schema_map[schema_key] = len(self.schema_map)
            schema_id = self.schema_map[schema_key]

            parts = [write_u8(TYPE_OBJ) + write_u16(schema_id) + write_u16(len(keys))]
            for k in keys:
                parts.append(self.encode_value(val[k], k))
            return b"".join(parts)

        return write_u8(TYPE_NULL)

    def get_schema_table(self) -> bytes:
        entries = []
        for schema_tuple, _ in sorted(self.schema_map.items(), key=lambda x: x[1]):
            kj = json.dumps(list(schema_tuple), separators=(",", ":")).encode("utf-8")
            entries.append(write_u32(len(kj)) + kj)
        return write_u32(len(entries)) + b"".join(entries)

    def get_addr_table(self) -> bytes:
        entries = [addr.encode("utf-8") for addr in self.addr_list]
        return write_u32(len(entries)) + b"".join(
            write_u8(len(e)) + e for e in entries
        )


class NC2Decoder:
    def __init__(self, schema_table: dict, addr_table: list):
        self.schema_table = schema_table
        self.addr_table   = addr_table
        self.delta_state  = {}

    def decode_value(self, data: bytes, offset: int, field_name: str = "") -> tuple:
        typ, offset = read_u8(data, offset)

        if typ == TYPE_NULL:
            return None, offset

        if typ == TYPE_BOOL:
            v, offset = read_u8(data, offset)
            return bool(v), offset

        if typ == TYPE_UINT:
            length, offset = read_u16(data, offset)
            val, _ = decode_varint(data[offset:offset+length], 0)
            return val, offset + length

        if typ == TYPE_FLOAT:
            val = struct.unpack(">d", data[offset:offset+8])[0]
            return val, offset + 8

        if typ == TYPE_HEXREF:
            idx, offset = read_u16(data, offset)
            return self.addr_table[idx], offset

        if typ == TYPE_HEX:
            len_byte, offset = read_u8(data, offset)

            if len_byte == DELTA_FLAG:
                orig_hex_len, offset = read_u8(data, offset)
                delta, offset = read_i64(data, offset)
                prev = self.delta_state.get(field_name, 0)
                int_val = prev + delta
                self.delta_state[field_name] = int_val
                if orig_hex_len == 0:
                    return "0x", offset
                return "0x" + format(int_val, f'0{orig_hex_len}x'), offset

            if len_byte == EXT_LEN:
                orig_hex_len, offset = read_u32(data, offset)
            else:
                orig_hex_len = len_byte

            byte_len = (orig_hex_len + 1) // 2
            raw = data[offset:offset + byte_len]
            return bin_to_hex(raw, orig_hex_len), offset + byte_len

        if typ == TYPE_STR:
            length, offset = read_u16(data, offset)
            return data[offset:offset+length].decode("utf-8"), offset + length

        if typ == TYPE_ARRAY:
            count, offset = read_u16(data, offset)
            items = []
            for _ in range(count):
                item, offset = self.decode_value(data, offset, field_name)
                items.append(item)
            return items, offset

        if typ == TYPE_OBJ:
            schema_id, offset = read_u16(data, offset)
            key_count, offset = read_u16(data, offset)
            keys = self.schema_table.get(schema_id, [f"f{i}" for i in range(key_count)])
            obj = {}
            for k in keys:
                v, offset = self.decode_value(data, offset, k)
                obj[k] = v
            return obj, offset

        return None, offset


def parse_schema_table(raw: bytes) -> dict:
    offset = 0
    count, offset = read_u32(raw, offset)
    schemas = {}
    for i in range(count):
        length, offset = read_u32(raw, offset)
        keys = json.loads(raw[offset:offset+length].decode("utf-8"))
        schemas[i] = keys
        offset += length
    return schemas


def parse_addr_table(raw: bytes) -> list:
    offset = 0
    count, offset = read_u32(raw, offset)
    addrs = []
    for _ in range(count):
        length, offset = read_u8(raw, offset)
        addr = raw[offset:offset+length].decode("utf-8")
        addrs.append(addr)
        offset += length
    return addrs


class NeuropCompressorV2:
    """
    NC-v2: Semantic binary compression for Ethereum/blockchain JSON.
    Pipeline: JSON parse → prescan → encode (binary+delta+addr_table) → final_compressor

    final_compressor: "gzip" (default) or "zstd"
    Using zstd as the final pass gives maximum ratio because NC-v2's binary
    output is already far denser than raw JSON text — zstd then compresses
    the remaining structural patterns more efficiently than gzip can.
    """

    def __init__(self, final: str = "gzip"):
        self.final = final
        self._zstd_cctx = None
        self._zstd_dctx = None
        if final == "zstd":
            try:
                import zstandard as _zstd
                self._zstd_cctx = _zstd.ZstdCompressor(level=22)
                self._zstd_dctx = _zstd.ZstdDecompressor()
            except ImportError:
                self.final = "gzip"

    def _final_compress(self, data: bytes) -> bytes:
        if self.final == "zstd" and self._zstd_cctx:
            return b"Z" + self._zstd_cctx.compress(data)
        return b"G" + gzip.compress(data, compresslevel=9)

    def _final_decompress(self, data: bytes) -> bytes:
        tag, payload = data[:1], data[1:]
        if tag == b"Z" and self._zstd_dctx:
            return self._zstd_dctx.decompress(payload)
        return gzip.decompress(payload)

    def _encode_raw(self, data: bytes) -> bytes:
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return b"\x00" + data

        encoder = NC2Encoder()
        encoder.prescan(parsed)
        encoder.build_addr_table(min_freq=2)

        body         = encoder.encode_value(parsed)
        schema_bytes = encoder.get_schema_table()
        addr_bytes   = encoder.get_addr_table()

        return (MAGIC
                + write_u32(len(schema_bytes)) + schema_bytes
                + write_u32(len(addr_bytes))   + addr_bytes
                + SEPARATOR + body)

    def compress(self, data: bytes) -> bytes:
        return self._final_compress(self._encode_raw(data))

    def decompress(self, data: bytes) -> bytes:
        raw = self._final_decompress(data)

        if raw[:1] == b"\x00":
            return raw[1:]

        if not raw.startswith(MAGIC):
            raise ValueError("Not a valid NC-v2 stream")

        offset = len(MAGIC)

        schema_len, offset = read_u32(raw, offset)
        schema_raw = raw[offset:offset+schema_len]
        offset += schema_len

        addr_len, offset = read_u32(raw, offset)
        addr_raw = raw[offset:offset+addr_len]
        offset += addr_len

        sep_pos = raw.find(SEPARATOR, offset)
        if sep_pos == -1:
            raise ValueError("Corrupted NC-v2 stream: missing separator")

        body = raw[sep_pos + len(SEPARATOR):]

        schema_table = parse_schema_table(schema_raw)
        addr_table   = parse_addr_table(addr_raw)

        decoder = NC2Decoder(schema_table, addr_table)
        value, _ = decoder.decode_value(body, 0)

        return json.dumps(value, separators=(",", ":")).encode("utf-8")
