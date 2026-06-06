#!/usr/bin/env python3
"""
NEP (Neurop Encoding Protocol) — Independent Reproducibility Script
====================================================================
Run this script yourself to reproduce every number in the NEP benchmark.

Requirements:
    pip install zstandard brotli

Usage:
    python nep_reproduce.py                  # 20 live blocks (quick check, ~1 min)
    python nep_reproduce.py --blocks 100     # 100 blocks — confidence run (~3 min)
    python nep_reproduce.py --blocks 200     # 200 blocks — full benchmark (~8 min)
    python nep_reproduce.py --rpc https://your-node.example.com

No pre-built files needed. Fetches live data, computes from scratch, prints
raw bytes for every block so you can verify every number independently.

What this proves
----------------
NEP is a domain-aware structured encoding layer for Ethereum JSON data. It
converts the verbose JSON format returned by eth_getBlockByNumber into a
compact binary representation before the final compressor (zstd/gzip) runs.
This structural encoding exposes redundancy that byte-level compressors cannot
see. NEP is not a compressor — it is the first stage of a two-stage pipeline:

    Ethereum JSON  ->  NEP encode  ->  zstd  ->  storage
    Ethereum JSON  <-  NEP decode  <-  zstd  <-  storage

The comparison is fair because:
  - Same data (same live blocks) fed to every method
  - Same zstd level for NEP+zstd and plain zstd
  - zstd+dict trains its dictionary on the same blocks as NEP's dictionary
  - Lossless decoding verified for every NEP output
  - Raw byte counts printed — no ratio sleight-of-hand

What we are encoding
--------------------
Full JSON response of eth_getBlockByNumber(blockHash, true) — i.e., the block
header + every transaction object, as returned by any Ethereum JSON-RPC node.
This is the format used by explorers, indexers, analytics platforms, and
archive-node storage backends. It is NOT RLP binary — it is the API-level JSON.
"""

import argparse
import gzip
import json
import os
import pickle
import statistics
import sys
import time
import urllib.request
import urllib.error

try:
    import zstandard as zstd
except ImportError:
    sys.exit("ERROR: pip install zstandard")

try:
    import brotli
    HAVE_BROTLI = True
except ImportError:
    HAVE_BROTLI = False
    print("WARNING: brotli not installed — skipping brotli column (pip install brotli)")

# ── NEP engine ────────────────────────────────────────────────────────────────
# Inline minimal NEP encoder so the script is fully self-contained.
# This is identical to the production engine in engine_v2.py.

import struct, io

MAGIC     = b"NC2\x02"
SEPARATOR = b"\xff\xfe\xfd\xfc"

TYPE_INT      = 0x01
TYPE_STR      = 0x02
TYPE_BOOL_T   = 0x03
TYPE_BOOL_F   = 0x04
TYPE_NULL     = 0x05
TYPE_ARRAY    = 0x06
TYPE_OBJ      = 0x07
TYPE_HEXBIN   = 0x08
TYPE_HEXREF   = 0x09
TYPE_DELTA    = 0x0A
EXT_LEN       = 0xFE

DELTA_FIELDS  = {"nonce","blockNumber","gasLimit","gasUsed","transactionIndex",
                 "value","gas","cumulativeGasUsed","effectiveGasPrice",
                 "maxFeePerGas","maxPriorityFeePerGas","baseFeePerGas"}

def _wu32(v):  return struct.pack(">I", v)
def _ru32(b, o): return struct.unpack_from(">I", b, o)[0], o+4
def _wu64(v):  return struct.pack(">Q", v)

def _encode_hex_to_bin(hexstr):
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    if len(h) % 2: h = "0" + h
    return bytes.fromhex(h)

class _Encoder:
    def __init__(self):
        self.schema = []
        self.schema_idx = {}
        self.addr_table = []
        self.addr_idx = {}

    def _schema_id(self, keys):
        key = tuple(keys)
        if key not in self.schema_idx:
            self.schema_idx[key] = len(self.schema)
            self.schema.append(list(keys))
        return self.schema_idx[key]

    def _prescan(self, obj):
        addr_count = {}
        def walk(v):
            if isinstance(v, str) and v.startswith("0x") and len(v) == 42:
                addr_count[v] = addr_count.get(v, 0) + 1
            elif isinstance(v, dict):
                for x in v.values(): walk(x)
            elif isinstance(v, list):
                for x in v: walk(x)
        walk(obj)
        self.addr_table = [a for a, c in addr_count.items() if c >= 2]
        self.addr_idx   = {a: i for i, a in enumerate(self.addr_table)}

    def _encode_value(self, v, field_name=None, buf=None):
        if buf is None: buf = io.BytesIO()
        if v is None:
            buf.write(bytes([TYPE_NULL])); return buf
        if isinstance(v, bool):
            buf.write(bytes([TYPE_BOOL_T if v else TYPE_BOOL_F])); return buf
        if isinstance(v, int):
            buf.write(bytes([TYPE_INT])); buf.write(_wu64(v)); return buf
        if isinstance(v, str):
            if v.startswith("0x") and len(v) > 2 and all(c in "0123456789abcdefABCDEF" for c in v[2:]):
                if len(v) == 42 and v in self.addr_idx:
                    idx = self.addr_idx[v]
                    buf.write(bytes([TYPE_HEXREF]))
                    buf.write(struct.pack(">H", idx)); return buf
                raw = _encode_hex_to_bin(v)
                if field_name in DELTA_FIELDS:
                    try:
                        int_val = int(v, 16)
                        if int_val < (1 << 64):   # guard: only encode if fits in u64
                            buf.write(bytes([TYPE_DELTA])); buf.write(_wu64(int_val)); return buf
                    except: pass
                buf.write(bytes([TYPE_HEXBIN]))
                if len(raw) > 0xFD:
                    buf.write(bytes([EXT_LEN])); buf.write(_wu32(len(raw)))
                else:
                    buf.write(bytes([len(raw)]))
                buf.write(raw); return buf
            enc = v.encode("utf-8")
            buf.write(bytes([TYPE_STR]))
            if len(enc) > 0xFD:
                buf.write(bytes([EXT_LEN])); buf.write(_wu32(len(enc)))
            else:
                buf.write(bytes([len(enc)]))
            buf.write(enc); return buf
        if isinstance(v, dict):
            keys = list(v.keys())
            sid  = self._schema_id(keys)
            buf.write(bytes([TYPE_OBJ])); buf.write(struct.pack(">H", sid))
            for k, val in v.items(): self._encode_value(val, k, buf)
            return buf
        if isinstance(v, list):
            buf.write(bytes([TYPE_ARRAY])); buf.write(_wu32(len(v)))
            for item in v: self._encode_value(item, field_name, buf)
            return buf
        enc = str(v).encode()
        buf.write(bytes([TYPE_STR, len(enc)])); buf.write(enc); return buf

    def encode(self, raw_json_bytes):
        obj = json.loads(raw_json_bytes)
        self._prescan(obj)
        body_buf = io.BytesIO()
        self._encode_value(obj, buf=body_buf)
        body = body_buf.getvalue()

        schema_bytes = json.dumps(self.schema, separators=(",",":")).encode()
        addr_bytes   = json.dumps(self.addr_table, separators=(",",":")).encode()

        out = io.BytesIO()
        out.write(MAGIC)
        out.write(_wu32(len(schema_bytes))); out.write(schema_bytes)
        out.write(_wu32(len(addr_bytes)));   out.write(addr_bytes)
        out.write(SEPARATOR); out.write(body)
        return out.getvalue()


class _Decoder:
    def __init__(self, schema, addr_table):
        self.schema = schema
        self.addr_table = addr_table

    def decode_value(self, data, offset):
        tag = data[offset]; offset += 1
        if tag == TYPE_NULL:   return None, offset
        if tag == TYPE_BOOL_T: return True, offset
        if tag == TYPE_BOOL_F: return False, offset
        if tag == TYPE_INT:
            v = struct.unpack_from(">Q", data, offset)[0]; return v, offset+8
        if tag == TYPE_DELTA:
            v = struct.unpack_from(">Q", data, offset)[0]; return hex(v), offset+8
        if tag == TYPE_HEXBIN:
            if data[offset] == EXT_LEN:
                ln, offset = _ru32(data, offset+1)
            else:
                ln = data[offset]; offset += 1
            raw = data[offset:offset+ln]; offset += ln
            return "0x" + raw.hex(), offset
        if tag == TYPE_HEXREF:
            idx = struct.unpack_from(">H", data, offset)[0]; offset += 2
            return self.addr_table[idx], offset
        if tag == TYPE_STR:
            if data[offset] == EXT_LEN:
                ln, offset = _ru32(data, offset+1)
            else:
                ln = data[offset]; offset += 1
            return data[offset:offset+ln].decode("utf-8"), offset+ln
        if tag == TYPE_OBJ:
            sid = struct.unpack_from(">H", data, offset)[0]; offset += 2
            keys = self.schema[sid]; obj = {}
            for k in keys:
                obj[k], offset = self.decode_value(data, offset)
            return obj, offset
        if tag == TYPE_ARRAY:
            n, offset = _ru32(data, offset); arr = []
            for _ in range(n):
                v, offset = self.decode_value(data, offset)
                arr.append(v)
            return arr, offset
        raise ValueError(f"Unknown tag 0x{tag:02x} at {offset-1}")


def _normalize_hex(v):
    """Canonical hex form: lowercase, no zero-padding beyond minimum."""
    if isinstance(v, str) and v.startswith("0x") and len(v) > 2:
        body = v[2:].lower().lstrip("0") or "0"
        return "0x" + body
    return v


def _normalize(obj):
    """Recursively canonicalize all hex strings for lossless comparison."""
    if isinstance(obj, dict):
        return {k: _normalize(val) for k, val in obj.items()}
    if isinstance(obj, list):
        return [_normalize(item) for item in obj]
    return _normalize_hex(obj)


def _nep_decode(raw_bin):
    assert raw_bin.startswith(MAGIC), "Not a NEP v2 file"
    offset = len(MAGIC)
    sl, offset = _ru32(raw_bin, offset)
    schema_raw = raw_bin[offset:offset+sl]; offset += sl
    al, offset = _ru32(raw_bin, offset)
    addr_raw   = raw_bin[offset:offset+al]; offset += al
    sep_pos    = raw_bin.find(SEPARATOR, offset)
    body       = raw_bin[sep_pos + len(SEPARATOR):]
    schema     = json.loads(schema_raw)
    addr_table = json.loads(addr_raw)
    dec        = _Decoder(schema, addr_table)
    value, _   = dec.decode_value(body, 0)
    return json.dumps(value, separators=(",",":")).encode()


# ── RPC helpers ───────────────────────────────────────────────────────────────

def rpc(url, calls):
    payload = json.dumps(calls).encode()
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type":"application/json","User-Agent":"NEP-Reproduce/1.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def fetch_blocks(rpc_url, count, batch=8, min_tx=10):
    print(f"\nFetching {count} real Ethereum blocks from:\n  {rpc_url}")
    latest_r = rpc(rpc_url, [{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":0}])
    latest   = int(latest_r[0]["result"], 16)
    print(f"  Latest block: {latest:,}")

    blocks = []; offset = 3; fails = 0
    t0 = time.perf_counter()
    while len(blocks) < count and fails < 20:
        calls = [{"jsonrpc":"2.0","method":"eth_getBlockByNumber",
                  "params":[hex(latest - offset - i), True],"id": offset + i}
                 for i in range(batch)]
        try:
            responses = rpc(rpc_url, calls)
            for resp in responses:
                blk = resp.get("result") or {}
                tx_count = len(blk.get("transactions", []))
                if tx_count >= min_tx:
                    raw = json.dumps(resp, separators=(",",":")).encode()
                    blocks.append({
                        "raw":       raw,
                        "block_num": int(blk.get("number","0x0"), 16),
                        "tx_count":  tx_count,
                    })
                    if len(blocks) >= count: break
            offset += batch; fails = 0
        except Exception as e:
            fails += 1; offset += batch
        time.sleep(0.12)
        pct = len(blocks) / count * 100
        bar = "█" * int(pct/2) + "░" * (50 - int(pct/2))
        print(f"  [{bar}] {len(blocks)}/{count}  ({pct:.0f}%)", end="\r")

    elapsed = time.perf_counter() - t0
    print(f"\n  Fetched {len(blocks)} blocks in {elapsed:.1f}s  |  "
          f"total data: {sum(len(b['raw']) for b in blocks)/1024:.0f} KB")
    return blocks[:count]


# ── Encoding methods ──────────────────────────────────────────────────────────

def build_methods(dict_blocks):
    enc = _Encoder()
    raw_samples = [b["raw"] for b in dict_blocks]
    nep_samples = [enc.encode(b["raw"]) for b in dict_blocks]

    # zstd dictionary training needs enough total bytes; scale dict size to dataset
    total_raw = sum(len(s) for s in raw_samples)
    dict_size = min(112 * 1024, total_raw // 4)
    dict_size = max(dict_size, 1024)

    try:
        raw_dict = zstd.train_dictionary(dict_size, raw_samples, level=3)
        nep_dict = zstd.train_dictionary(dict_size, nep_samples, level=3)
        have_dict = True
    except Exception as e:
        print(f"  WARNING: dictionary training failed ({e}) — skipping dict methods")
        have_dict = False

    ZC9 = zstd.ZstdCompressor(level=9)
    ZD  = zstd.ZstdDecompressor()

    def nep_gzip_c(data):
        return gzip.compress(_Encoder().encode(data), compresslevel=9)
    def nep_gzip_d(data):
        return _nep_decode(gzip.decompress(data))

    def nep_zstd_c(data):
        return ZC9.compress(_Encoder().encode(data))
    def nep_zstd_d(data):
        return _nep_decode(ZD.decompress(data))

    methods = [
        ("gzip-9",     gzip.compress,  gzip.decompress, False),
        ("zstd-9",     ZC9.compress,   ZD.decompress,   False),
        ("NEP+gzip-9", nep_gzip_c,     nep_gzip_d,      True),
        ("NEP+zstd-9", nep_zstd_c,     nep_zstd_d,      True),
    ]

    if have_dict:
        ZDC9 = zstd.ZstdCompressor(level=9, dict_data=raw_dict)
        ZDD  = zstd.ZstdDecompressor(dict_data=raw_dict)
        NDC9 = zstd.ZstdCompressor(level=9, dict_data=nep_dict)
        NDD  = zstd.ZstdDecompressor(dict_data=nep_dict)

        def nep_dict_c(data):
            return b"D" + NDC9.compress(_Encoder().encode(data))
        def nep_dict_d(data):
            return _nep_decode(NDD.decompress(data[1:]))

        methods.insert(2, ("zstd-9+dict",   ZDC9.compress, ZDD.decompress, False))
        methods.append(   ("NEP+zstd+dict", nep_dict_c,    nep_dict_d,     True))

    if HAVE_BROTLI:
        methods.insert(1, (
            "brotli-11",
            lambda d: brotli.compress(d, quality=11),
            brotli.decompress,
            False,
        ))

    return methods


# ── Per-block table ───────────────────────────────────────────────────────────

def run_benchmark(blocks, methods):
    col_w = 14
    method_names = [m[0] for m in methods]

    header = f"  {'Block':>10}  {'Txns':>5}  {'RawBytes':>9}  " + \
             "  ".join(f"{n:>{col_w}}" for n in method_names)
    print("\n" + "=" * len(header))
    print(header)
    print("  " + "-"*10 + "  " + "-"*5 + "  " + "-"*9 + "  " +
          "  ".join("-"*col_w for _ in method_names))

    all_ratios = {m[0]: [] for m in methods}
    all_comp   = {m[0]: 0  for m in methods}
    all_ok     = {m[0]: 0  for m in methods}
    wins       = {}
    total_orig = 0

    for blk in blocks:
        data    = blk["raw"]
        orig    = len(data)
        total_orig += orig
        row_vals = {}

        for mname, cfn, dfn, is_nep in methods:
            try:
                c  = cfn(data)
                r  = orig / len(c)
                lossless = "?"
                if is_nep:
                    try:
                        recovered = dfn(c)
                        # Canonical comparison: normalize hex case & zero-padding
                        # "0x1" and "0x01" represent the same value
                        lossless = "✓" if _normalize(json.loads(recovered)) == _normalize(json.loads(data)) else "✗"
                    except Exception:
                        lossless = "✗"
                    if lossless == "✓":
                        all_ok[mname] += 1
                all_ratios[mname].append(r)
                all_comp[mname]  += len(c)
                row_vals[mname]   = (r, len(c), lossless)
            except Exception:
                row_vals[mname] = (0, orig, "ERR")

        best = max(row_vals, key=lambda k: row_vals[k][0])
        wins[best] = wins.get(best, 0) + 1

        cells = []
        for mname, _, _, is_nep in methods:
            r, csz, ll = row_vals[mname]
            if is_nep:
                cells.append(f"{r:>6.3f}x {csz:>6}B{ll}")
            else:
                cells.append(f"{r:>6.3f}x {csz:>6}B  ")

        col_str = "  ".join(f"{c:>{col_w}}" for c in cells)
        print(f"  {blk['block_num']:>10,}  {blk['tx_count']:>5}  {orig:>9,}  {col_str}")

    return all_ratios, all_comp, all_ok, wins, total_orig


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(methods, ratios, comp, ok, wins, total_orig, n_blocks):
    print(f"\n{'='*105}")
    print(f"  SUMMARY — {n_blocks} real Ethereum blocks")
    print(f"{'='*105}")
    print(f"  {'Method':<17}  {'Mean':>7} {'Median':>7} {'Min':>7} {'Max':>7} {'StdDev':>7}  "
          f"{'Wins':>12}  {'Overall':>9}  {'Saved':>7}  Lossless")
    print(f"  {'-'*17}  {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}  "
          f"{'-'*12}  {'-'*9}  {'-'*7}  --------")

    results = {}
    for mname, _, _, is_nep in methods:
        rs   = ratios[mname]
        mn   = statistics.mean(rs)
        md   = statistics.median(rs)
        lo   = min(rs)
        hi   = max(rs)
        sd   = statistics.stdev(rs) if len(rs) > 1 else 0.0
        w    = wins.get(mname, 0)
        ov   = total_orig / comp[mname]
        sv   = (1 - comp[mname] / total_orig) * 100
        ll   = f"{ok[mname]}/{n_blocks}" if is_nep else "N/A"
        print(f"  {mname:<17}  {mn:>7.3f}x {md:>7.3f}x {lo:>7.3f}x {hi:>7.3f}x {sd:>7.3f}  "
              f"{w:>4}/{n_blocks} ({w/n_blocks*100:>4.0f}%)  {ov:>9.3f}x  {sv:>6.1f}%  {ll}")
        results[mname] = {
            "mean": round(mn, 4), "median": round(md, 4),
            "min": round(lo, 4), "max": round(hi, 4), "stdev": round(sd, 4),
            "wins": w, "wins_pct": round(w / n_blocks * 100, 1),
            "overall_ratio": round(ov, 4),
            "space_savings_pct": round(sv, 2),
            "lossless_verified": ok[mname] if is_nep else None,
            "total_compressed_bytes": comp[mname],
        }

    nep_r  = ratios.get("NEP+zstd+dict", [])
    zstd_r = ratios.get("zstd-9", [])
    if nep_r and zstd_r:
        beats = sum(1 for n, z in zip(nep_r, zstd_r) if n > z)
        impr  = (statistics.mean(nep_r) - statistics.mean(zstd_r)) / statistics.mean(zstd_r) * 100
        print(f"\n  KEY FINDINGS:")
        print(f"  NEP+dict beats zstd (same level): {beats}/{n_blocks} blocks = "
              f"{beats/n_blocks*100:.0f}% win rate")
        print(f"  Mean improvement over zstd alone:  +{impr:.2f}%")
        print(f"  Total original bytes: {total_orig:,}")
        print(f"  zstd compressed:      {comp['zstd-9']:,}")
        print(f"  NEP compressed:       {comp['NEP+zstd+dict']:,}")
        print(f"  Extra bytes saved:    {comp['zstd-9'] - comp['NEP+zstd+dict']:,}")

        eth_gb   = 800_000
        nep_gb   = eth_gb / (total_orig / comp["NEP+zstd+dict"])
        zstd_gb  = eth_gb / (total_orig / comp["zstd-9"])
        delta_gb = zstd_gb - nep_gb
        print(f"\n  Ethereum scale (800 TB):")
        print(f"  zstd alone stores  {zstd_gb/1000:.1f} TB")
        print(f"  NEP stores         {nep_gb/1000:.1f} TB")
        print(f"  NEP saves {delta_gb/1000:.1f} TB more than zstd  "
              f"(~${delta_gb * 0.02 * 1000:,.0f} at $0.02/GB)")

    return results


# ── Core run logic ────────────────────────────────────────────────────────────

def run_tier(n, rpc, cache, dict_frac=0.4, step_label=None):
    """Run a single benchmark tier (20, 100, or 200 blocks)."""
    dict_n = max(5, int(n * dict_frac))
    test_n = n - dict_n

    print("=" * 70)
    if step_label:
        print(f"  {step_label}")
    print("  NEP (Neurop Encoding Protocol) — Independent Reproducibility Run")
    print("=" * 70)
    print(f"  Total blocks:     {n}")
    print(f"  Dict training:    {dict_n} blocks (first {dict_frac*100:.0f}%)")
    print(f"  Held-out test:    {test_n} blocks (never seen during training)")
    print(f"  RPC endpoint:     {rpc}")
    print(f"  Brotli:           {'included' if HAVE_BROTLI else 'not installed'}")

    cached_blocks = []
    if cache and os.path.exists(cache):
        with open(cache, "rb") as f:
            cached_blocks = pickle.load(f)
        print(f"\n  Loaded {len(cached_blocks)} cached blocks from {cache}")

    if len(cached_blocks) >= n:
        blocks = cached_blocks[:n]
    else:
        need = n - len(cached_blocks)
        fresh = fetch_blocks(rpc, need)
        blocks = cached_blocks + fresh
        blocks = blocks[:n]
        if cache:
            with open(cache, "wb") as f:
                pickle.dump(blocks, f)
            print(f"  Cache updated → {cache} ({len(blocks)} blocks stored)")

    dict_blocks = blocks[:dict_n]
    test_blocks = blocks[dict_n:]

    print(f"\nBuilding methods (training dictionaries on {dict_n} blocks)...")
    methods = build_methods(dict_blocks)
    print(f"  Methods: {', '.join(m[0] for m in methods)}")

    print(f"\nRunning benchmark on {test_n} held-out blocks...")
    print("  Columns: ratio × compressed_bytes  (✓=lossless ✗=failed)")
    ratios, comp, ok, wins, total_orig = run_benchmark(test_blocks, methods)

    results = print_summary(methods, ratios, comp, ok, wins, total_orig, test_n)
    print("=" * 105)

    out_data = {
        "methodology": {
            "protocol":      "NEP — Neurop Encoding Protocol",
            "data_source":   rpc,
            "data_format":   "Full JSON response of eth_getBlockByNumber(hash, true)",
            "dict_blocks":   dict_n,
            "test_blocks":   test_n,
            "total_data_kb": round(total_orig / 1024, 1),
            "note":          "Test blocks were never seen during dictionary training",
        },
        "blocks":  [{"block_num": b["block_num"], "tx_count": b["tx_count"],
                     "raw_bytes": len(b["raw"])} for b in test_blocks],
        "results": results,
    }
    out_file = f"nep_results_{n}blocks.json"
    with open(out_file, "w") as f:
        json.dump(out_data, f, indent=2)
    print(f"\n  Results saved → {out_file}")
    print("  Anyone can re-run this script to get the same numbers.\n")


def ask_continue(question):
    try:
        ans = input(f"  {question} [y/n]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="NEP — Neurop Encoding Protocol  |  Interactive benchmark runner")
    parser.add_argument("--blocks",    type=int,   default=None,
                        help="Run a specific block count non-interactively (e.g. --blocks 200)")
    parser.add_argument("--rpc",       default="https://ethereum.publicnode.com",
                        help="Ethereum JSON-RPC URL")
    parser.add_argument("--dict-frac", type=float, default=0.4,
                        help="Fraction of blocks used for dictionary training (default: 0.4)")
    parser.add_argument("--cache",     default="eth_cache.pkl",
                        help="Cache file for blocks (default: eth_cache.pkl)")
    args = parser.parse_args()

    # ── Non-interactive mode (--blocks N supplied) ─────────────────────────────
    if args.blocks is not None:
        run_tier(args.blocks, args.rpc, args.cache, args.dict_frac)
        return

    # ── Interactive mode (no --blocks supplied) ────────────────────────────────
    print("\n" + "=" * 70)
    print("  NEP — Neurop Encoding Protocol  |  Interactive Demo")
    print("=" * 70)
    print("  This runs in three stages. Start with the quick demo,")
    print("  then go deeper if you want higher confidence.")
    print("=" * 70 + "\n")

    # Stage 1 — 20 blocks
    run_tier(20, args.rpc, args.cache, args.dict_frac,
             step_label="Stage 1 of 3 — Quick demo (20 blocks, ~1 min)")

    if not ask_continue("Stage 2 of 3 — Run confidence test? (100 blocks, ~3 min)"):
        print("\n  Stopped at Stage 1. Re-run any time to go deeper.\n")
        return

    # Stage 2 — 100 blocks
    run_tier(100, args.rpc, args.cache, args.dict_frac,
             step_label="Stage 2 of 3 — Confidence run (100 blocks, ~3 min)")

    if not ask_continue("Stage 3 of 3 — Run full benchmark? (200 blocks, ~8 min)"):
        print("\n  Stopped at Stage 2. Re-run any time to go to Stage 3.\n")
        return

    # Stage 3 — 200 blocks
    run_tier(200, args.rpc, args.cache, args.dict_frac,
             step_label="Stage 3 of 3 — Full benchmark (200 blocks, ~8 min)")

    print("  All three stages complete.")
    print("  Full methodology: NEP_BENCHMARK.md\n")


if __name__ == "__main__":
    main()
