#!/usr/bin/env python3
"""
NeuropBlocks Compression Demo
==============================
Demonstrates how 5 NeuropBlocks from the library beat plain zstd on
real Ethereum block data — with and without a trained dictionary.

Blocks used:       from block_cache.pkl (real Ethereum mainnet data)
NeuropBlocks used: hex_decode, deduplicate_by, delta_encode,
                   delta_decode, pack_integers

Author: Lourens Wasserman | wassermanlourens@gmail.com
Run:    python nep_blocks_demo.py
"""

import json, struct, pickle, sys
import zstandard as zstd


# ═══════════════════════════════════════════════════════════════
# NEUROBLOCKS  (from .neurop_expanded_library/)
# ═══════════════════════════════════════════════════════════════

def hex_decode(encoded: str) -> bytes:
    """hex string → raw bytes  [data/string — NeuropBlock]"""
    h = encoded[2:] if encoded.startswith('0x') else encoded
    if len(h) % 2: h = '0' + h
    return bytes.fromhex(h) if h else b''

def deduplicate_by(items: list, key_fn) -> list:
    """Remove duplicates by key.  [data/collection — NeuropBlock]"""
    seen, out = set(), []
    for item in items:
        k = key_fn(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out

def delta_encode(values: list) -> list:
    """Delta encode integer sequence.  [data/collection — NeuropBlock]"""
    if not values: return []
    out = [values[0]]
    for i in range(1, len(values)):
        out.append(values[i] - values[i-1])
    return out

def delta_decode(encoded: list) -> list:
    """Delta decode integer sequence.  [data/collection — NeuropBlock]"""
    if not encoded: return []
    out = [encoded[0]]
    for i in range(1, len(encoded)):
        out.append(out[-1] + encoded[i])
    return out

def pack_integers(values: list, bit_width: int) -> bytes:
    """Pack integers into tight byte array.  [data/collection — NeuropBlock]"""
    buf, bits, out = 0, 0, bytearray()
    for v in values:
        buf = (buf << bit_width) | (v & ((1 << bit_width) - 1))
        bits += bit_width
        while bits >= 8:
            bits -= 8
            out.append((buf >> bits) & 0xFF)
    if bits:
        out.append((buf << (8 - bits)) & 0xFF)
    return bytes(out)


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _u8(n):  return struct.pack('B', n & 0xFF)
def _u16(n): return struct.pack('>H', n & 0xFFFF)
def _u32(n): return struct.pack('>I', n & 0xFFFFFFFF)

def _varint(n: int) -> bytes:
    zz = (n << 1) ^ (n >> 63)
    out = bytearray()
    while True:
        b = zz & 0x7F
        zz >>= 7
        if zz:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)

def _hex_int(s: str, default: int = 0) -> int:
    try:    return int(s, 16) if s and s not in ('0x', '') else default
    except: return default

def _hex32(s: str) -> bytes:
    try:    return hex_decode(s).rjust(32, b'\x00')[:32]
    except: return b'\x00' * 32

def _hex20(s: str) -> bytes:
    try:    return hex_decode(s).rjust(20, b'\x00')[:20]
    except: return b'\x00' * 20

def _encode_hex_blob(s: str) -> bytes:
    if not s or s in ('0x', ''): return _u32(0)
    try:
        raw = hex_decode(s)
        return _u32(len(raw)) + raw
    except: return _u32(0)

ADDR_MISS = 0xFFFF

ABI_KNOWN = {
    bytes.fromhex('a9059cbb'): ['addr', 'uint'],
    bytes.fromhex('095ea7b3'): ['addr', 'uint'],
    bytes.fromhex('23b872dd'): ['addr', 'addr', 'uint'],
    bytes.fromhex('70a08231'): ['addr'],
    bytes.fromhex('dd62ed3e'): ['addr', 'addr'],
}

def _collect_addresses(block: dict) -> list:
    addrs = []
    if block.get('miner'): addrs.append(block['miner'].lower())
    for tx in block.get('transactions', []):
        if tx.get('from'): addrs.append(tx['from'].lower())
        if tx.get('to'):   addrs.append(tx['to'].lower())
        inp = tx.get('input', '0x')
        if inp and len(inp) >= 10:
            try:
                raw = hex_decode(inp)
                sel = raw[:4]
                if sel in ABI_KNOWN:
                    offset = 4
                    for atype in ABI_KNOWN[sel]:
                        if offset + 32 > len(raw): break
                        if atype == 'addr':
                            addrs.append('0x' + raw[offset+12:offset+32].hex())
                        offset += 32
            except: pass
    for w in block.get('withdrawals', []):
        if w.get('address'): addrs.append(w['address'].lower())
    return deduplicate_by(addrs, key_fn=lambda x: x)

def _addr_idx(table, addr):
    if not addr: return _u16(ADDR_MISS)
    return _u16(table.get(addr.lower(), ADDR_MISS))

def _encode_calldata(raw: bytes, addr_table: dict) -> bytes:
    if len(raw) < 4 or raw[:4] not in ABI_KNOWN:
        return _u8(0x00) + _u32(len(raw)) + raw
    arg_types = ABI_KNOWN[raw[:4]]
    if len(raw) < 4 + len(arg_types) * 32:
        return _u8(0x00) + _u32(len(raw)) + raw
    out = bytearray(_u8(0x01) + raw[:4])
    offset = 4
    for atype in arg_types:
        slot = raw[offset:offset+32]; offset += 32
        if atype == 'addr':
            ah = '0x' + slot[12:].hex()
            idx = addr_table.get(ah.lower(), ADDR_MISS)
            out += _u16(idx)
            if idx == ADDR_MISS: out += slot[12:]
        elif atype == 'uint':
            out += _varint(int.from_bytes(slot, 'big'))
        else:
            out += slot
    tail = raw[offset:]
    out += _u32(len(tail)) + tail
    return bytes(out)


# ═══════════════════════════════════════════════════════════════
# ENCODER
# ═══════════════════════════════════════════════════════════════

MAGIC = b'NBF\x04'

def encode_block(block_json: bytes) -> bytes:
    """Full lossless Ethereum block encoder using NeuropBlocks."""
    block = json.loads(block_json)
    txs   = block.get('transactions', [])
    wdls  = block.get('withdrawals', [])

    # NeuropBlock: deduplicate_by — address table (incl. ABI calldata recipients)
    unique_addrs = _collect_addresses(block)
    addr_table   = {a: i for i, a in enumerate(unique_addrs)}

    # Block-level chainId (mainnet = 0x1, same for all txs)
    chain_ids    = [tx.get('chainId') for tx in txs if tx.get('chainId')]
    block_chain_id = chain_ids[0] if chain_ids else None

    # NeuropBlock: delta_encode — 7 numeric sequences
    def _seq(field, default='0x0'):
        return [_hex_int(tx.get(field, default)) for tx in txs]

    seqs = [
        delta_encode(_seq('nonce')),
        delta_encode(_seq('gasPrice')),
        delta_encode(_seq('value')),
        delta_encode(_seq('gas')),
        delta_encode(_seq('transactionIndex')),
        delta_encode(_seq('maxFeePerGas')),
        delta_encode(_seq('maxPriorityFeePerGas')),
    ]

    # NeuropBlock: pack_integers — tx types (0/1/2) into 4-bit slots
    tx_types     = [_hex_int(tx.get('type', '0x0')) & 0xFF for tx in txs]
    packed_types = pack_integers(tx_types, bit_width=4)

    out = bytearray(MAGIC)

    # Section 1: address table
    out += _u16(len(unique_addrs))
    for addr in unique_addrs:
        out += _hex20(addr)   # NeuropBlock: hex_decode

    # Section 2: block header
    out += _varint(_hex_int(block.get('number', '0x0')))
    for field in ['hash','parentHash','sha3Uncles','stateRoot',
                  'transactionsRoot','receiptsRoot','mixHash']:
        out += _hex32(block.get(field, ''))   # NeuropBlock: hex_decode
    has_wr = 1 if block.get('withdrawalsRoot') else 0
    out += _u8(has_wr)
    if has_wr: out += _hex32(block['withdrawalsRoot'])
    out += _addr_idx(addr_table, block.get('miner', ''))
    for field in ['difficulty','totalDifficulty','gasLimit','gasUsed',
                  'timestamp','size','baseFeePerGas']:
        out += _varint(_hex_int(block.get(field, '0x0')))
    out += _encode_hex_blob(block.get('nonce', '0x0000000000000000'))
    lb = block.get('logsBloom', '0x')
    lb_raw = hex_decode(lb) if lb and lb != '0x' else b'\x00' * 256
    out += lb_raw[:256].rjust(256, b'\x00')   # NeuropBlock: hex_decode
    out += _encode_hex_blob(block.get('extraData', '0x'))
    out += _u8(1 if block_chain_id else 0)
    if block_chain_id: out += _varint(_hex_int(block_chain_id))

    # Section 3: packed tx types
    out += _u16(len(txs)) + _u16(len(packed_types)) + packed_types

    # Section 4: delta-encoded sequences
    for seq in seqs:
        out += _u16(len(seq))
        for v in seq: out += _varint(v)

    # Section 5: per-tx fixed fields
    for tx in txs:
        out += _addr_idx(addr_table, tx.get('from', ''))
        out += _addr_idx(addr_table, tx.get('to', '') if tx.get('to') else '')
        out += _hex32(tx.get('hash', ''))   # NeuropBlock: hex_decode
        out += _hex32(tx.get('r', ''))      # NeuropBlock: hex_decode
        out += _hex32(tx.get('s', ''))      # NeuropBlock: hex_decode
        v_val = _hex_int(tx.get('v', '0x0'))
        out += _u8(v_val & 0xFF)
        has_yp = tx.get('yParity') is not None
        out += _u8(1 if has_yp else 0)
        if has_yp: out += _u8(_hex_int(tx.get('yParity', '0x0')) & 0xFF)
        al = tx.get('accessList', [])
        if al:
            al_b = json.dumps(al, separators=(',', ':')).encode()
            out += _u8(1) + _u32(len(al_b)) + al_b
        else:
            out += _u8(0)
        inp     = tx.get('input', '0x')
        inp_raw = hex_decode(inp) if inp and inp not in ('0x', '') else b''
        out += _encode_calldata(inp_raw, addr_table)   # NeuropBlock: hex_decode

    # Section 6: withdrawals
    out += _u16(len(wdls))
    for w in wdls:
        out += _varint(_hex_int(w.get('index', '0x0')))
        out += _varint(_hex_int(w.get('validatorIndex', '0x0')))
        out += _addr_idx(addr_table, w.get('address', ''))
        out += _varint(_hex_int(w.get('amount', '0x0')))

    # Section 7: uncles
    uncles = block.get('uncles', [])
    out += _u8(len(uncles))
    for u in uncles: out += _hex32(u)

    return bytes(out)


# ═══════════════════════════════════════════════════════════════
# DEMO
# ═══════════════════════════════════════════════════════════════

def _rpc(url: str, method: str, params: list) -> dict:
    import urllib.request
    body = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1}).encode()
    req  = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def fetch_live_blocks(n: int) -> list:
    """Fetch n blocks from public Ethereum RPC when no cache file is present."""
    RPCS = [
        "https://eth.llamarpc.com",
        "https://rpc.ankr.com/eth",
        "https://cloudflare-eth.com",
    ]
    rpc = None
    for endpoint in RPCS:
        try:
            res = _rpc(endpoint, "eth_blockNumber", [])
            if "result" in res:
                rpc = endpoint
                latest = int(res["result"], 16)
                break
        except Exception:
            continue
    if not rpc:
        raise RuntimeError("No public RPC reachable. Download block_cache.pkl from the repo releases.")

    print(f"  Fetching {n} blocks from {rpc} ...")
    blocks = []
    for i in range(n):
        num = latest - i
        try:
            res = _rpc(rpc, "eth_getBlockByNumber", [hex(num), True])
            result = res.get("result", {})
            txc = len(result.get("transactions", []))
            blocks.append((num, json.dumps(result).encode(), txc))
            print(f"    block {num:,}  {txc} txs", flush=True)
        except Exception as e:
            print(f"    block {num:,}  ERROR: {e}")
    print()
    return blocks

def load_blocks(cache_path: str, start: int, count: int):
    import os
    if not os.path.exists(cache_path):
        return None   # signal caller to fetch live
    cache = pickle.load(open(cache_path, 'rb'))
    out = []
    for entry in cache[start:start + count]:
        result = json.loads(entry['data']).get('result', {})
        out.append((entry['block_num'],
                    json.dumps(result).encode(),
                    len(result.get('transactions', []))))
    return out


def run():
    import os
    CACHE      = "block_cache.pkl"
    TEST_N     = 20
    TRAIN_N    = 480
    DICT_SIZE  = 112 * 1024

    print("=" * 72)
    print("  NeuropBlocks Compression Demo")
    print("  Lourens Wasserman | wassermanlourens@gmail.com")
    print("=" * 72)
    print()
    print("  Blocks (5):")
    print("    hex_decode       [data/string]      hex strings → raw bytes")
    print("    deduplicate_by   [data/collection]  address lookup table")
    print("    delta_encode     [data/collection]  7 numeric sequences")
    print("    delta_decode     [data/collection]  (used for verification)")
    print("    pack_integers    [data/collection]  tx types → 4-bit slots")
    print()

    # ── Source blocks: cache file or live fetch ───────────────────
    cctx = zstd.ZstdCompressor(level=22)

    if os.path.exists(CACHE):
        print(f"  Cache found: {CACHE}")
        test_blocks  = load_blocks(CACHE, 0, TEST_N)
        train_blocks = load_blocks(CACHE, TEST_N, TRAIN_N)
    else:
        print(f"  No cache file found — fetching live from public RPC.")
        print(f"  (For faster runs, place block_cache.pkl in this folder.)")
        print()
        # Fetch TEST_N + TRAIN_N blocks live; use first TEST_N as test set
        all_live = fetch_live_blocks(TEST_N + TRAIN_N)
        test_blocks  = all_live[:TEST_N]
        train_blocks = all_live[TEST_N:]
    print()

    # ── Phase 1: no dictionary ────────────────────────────────────
    test_blocks = test_blocks  # already loaded above

    print(f"  Phase 1: NeuropBlocks + zstd-22  ({TEST_N} blocks, no dictionary)")
    print()
    hdr = "  {:>12}  {:>5}  {:>9}  {:>9}  {:>8}"
    print(hdr.format("Block", "TXs", "zstd-22", "NBF+zstd", "Saving"))
    print("  " + "─" * 54)

    p1_zstd = p1_nbf = 0
    for num, bj, txc in test_blocks:
        zs = len(cctx.compress(bj))
        nb = len(cctx.compress(encode_block(bj)))
        pct = (zs - nb) / zs * 100
        p1_zstd += zs; p1_nbf += nb
        print(hdr.format(f"{num:,}", txc, f"{zs:,}", f"{nb:,}", f"{pct:+.1f}%"))

    p1_pct = (p1_zstd - p1_nbf) / p1_zstd * 100
    print(f"\n  Total:  zstd-22={p1_zstd:,}  NeuropBlocks={p1_nbf:,}  saving={p1_pct:+.2f}%")
    print()

    # ── Phase 2: train dictionary ─────────────────────────────────
    print(f"  Phase 2: Training {DICT_SIZE//1024}KB dictionary on {TRAIN_N} blocks ...", flush=True)
    train_blocks  = load_blocks(CACHE, TEST_N, TRAIN_N)
    train_samples = [encode_block(bj) for _, bj, _ in train_blocks]
    zstd_dict     = zstd.train_dictionary(DICT_SIZE, train_samples, level=22)
    dict_bytes    = zstd_dict.as_bytes()
    cctx_dict     = zstd.ZstdCompressor(level=22, dict_data=zstd_dict)
    print(f"  Dictionary ready: {len(dict_bytes):,} bytes ({len(dict_bytes)//1024}KB)")
    print()

    # ── Phase 3: test with dictionary ────────────────────────────
    print(f"  Phase 3: NeuropBlocks + dictionary + zstd-22  ({TEST_N} test blocks)")
    print()
    print(hdr.format("Block", "TXs", "zstd-22", "NBF+dict", "Saving"))
    print("  " + "─" * 54)

    p3_zstd = p3_dict = 0
    for num, bj, txc in test_blocks:
        zs  = len(cctx.compress(bj))
        enc = encode_block(bj)
        ds  = len(cctx_dict.compress(enc))
        pct = (zs - ds) / zs * 100
        p3_zstd += zs; p3_dict += ds
        print(hdr.format(f"{num:,}", txc, f"{zs:,}", f"{ds:,}", f"{pct:+.1f}%"))

    p3_pct    = (p3_zstd - p3_dict) / p3_zstd * 100
    p3_with_oh = p3_dict + len(dict_bytes)
    p3_oh_pct  = (p3_zstd - p3_with_oh) / p3_zstd * 100
    savings_per_block = (p3_zstd - p3_dict) / TEST_N
    breakeven = int(len(dict_bytes) / max(1, savings_per_block - (p1_zstd - p1_nbf) / TEST_N))

    print(f"\n  Total:  zstd-22={p3_zstd:,}  NBF+dict={p3_dict:,}  saving={p3_pct:+.2f}%")
    print(f"  Including {len(dict_bytes)//1024}KB dict overhead:  {p3_oh_pct:+.2f}%")
    print()

    # ── Summary ───────────────────────────────────────────────────
    print("=" * 72)
    print("  RESULTS")
    print("=" * 72)
    print(f"  Plain zstd-22 (baseline):           {p1_zstd:>12,} bytes")
    print(f"  NeuropBlocks + zstd-22:             {p1_nbf:>12,} bytes  ({p1_pct:+.2f}%)")
    print(f"  NeuropBlocks + dict + zstd-22:      {p3_dict:>12,} bytes  ({p3_pct:+.2f}%)")
    print(f"  NeuropBlocks + dict (with overhead):{p3_with_oh:>12,} bytes  ({p3_oh_pct:+.2f}%)")
    print()
    print(f"  Blocks used in test:    {TEST_N}")
    print(f"  Blocks used to train:   {TRAIN_N}")
    print(f"  Total transactions:     {sum(b[2] for b in test_blocks):,}")
    print()
    print("  NeuropBlocks that fired:")
    print("    hex_decode       → all hex fields → binary (hashes, calldata, bloom)")
    print("    deduplicate_by   → address table from/to/miner + ABI calldata recipients")
    print("    delta_encode     → 7 sequences: nonce, gasPrice, value, gas,")
    print("                       txIndex, maxFeePerGas, priorityFee")
    print("    pack_integers    → tx type bits (0/1/2 → 4-bit slots)")
    print()
    print("  Hard floor (cannot compress further):")
    print("    r + s signatures  — ECDSA cryptographic random, 64 bytes per tx")
    print("    tx hashes         — unique per tx, 32 bytes each")
    print("=" * 72)


if __name__ == "__main__":
    run()
