# NEP — Neurop Encoding Protocol
## A structured encoding layer for Ethereum JSON data

---

### The problem in one sentence

Ethereum JSON-RPC responses are verbose by design — hex strings, repeated field names across every transaction, 20-byte addresses printed as 42-character strings. General-purpose compressors like zstd see this as arbitrary text. They don't know that `"0x1a2b3c4d"` is binary data, or that `"from"`, `"to"`, `"gasPrice"` appear identically in every single transaction object.

---

### What NEP does

NEP is a domain-aware encoding layer that sits **in front of zstd**. It converts Ethereum block JSON into a compact binary format before the compressor runs. NEP is not a compressor — it is the structure-aware first stage of a two-stage pipeline.

```
Ethereum JSON  →  NEP encode  →  zstd  →  storage
Ethereum JSON  ←  NEP decode  ←  zstd  ←  storage
```

**4 stages:**

| Stage | What it does | Example |
|---|---|---|
| Hex → binary | Converts hex strings to raw bytes | `"0x1a2b"` (6 bytes) → `\x1a\x2b` (2 bytes) |
| Schema stripping | Replaces repeated JSON keys with 2-byte IDs | `"transactionIndex"` × 300 txns → `\x00\x04` × 300 |
| Address deduplication | Hot addresses stored once, referenced by index | USDC contract: 42 chars → 2 bytes per occurrence |
| Delta encoding | Numeric fields stored as integers | `"nonce":"0x1a"` → `\x00\x00...\x1a` (8 bytes, compresses better) |

**Result:** NEP binary is ~46% of the original JSON size — before any compressor runs.

---

### The numbers

Tested on **200 real Ethereum mainnet blocks** (never seen during training). 92MB of test data.

| Method | Mean ratio | Wins | Space saved |
|---|---|---|---|
| gzip-9 | 4.97x | 0 / 200 | 79.7% |
| brotli-11 | 5.34x | 0 / 200 | 81.3% |
| zstd-9 | 5.71x | 0 / 200 | 82.5% |
| zstd + dict | 5.37x | 0 / 200 | 81.4% |
| NEP + gzip | 5.84x | 0 / 200 | 82.9% |
| NEP + zstd | 6.18x | 2 / 200 | 83.8% |
| **NEP + zstd + dict** | **6.38x** | **198 / 200** | **84.3%** |

**NEP beats plain zstd on 200/200 blocks.**  
**Mean improvement: +11.7% over zstd alone.**  
**Every NEP output verified lossless.**

---

### Why the gain makes sense

NEP doesn't do anything clever with entropy. It simply removes structure that zstd cannot see:

- A 42-character hex address repeated 80 times across a block → zstd can compress this, but NEP makes it a 2-byte index repeated 80 times. zstd compresses that much better.
- `"transactionIndex","blockNumber","gasPrice"` repeated in 300 transaction objects → NEP makes this a 2-byte schema ID. Zero bytes wasted on key names.
- `"0x5d21dba000"` as a hex string → NEP stores it as 5 raw bytes in a sequence of similar values. Better run-length and LZ77 matching for zstd.

The 11% is structural, not statistical. It's reproducible because the structure of Ethereum blocks is reproducible.

---

### Scale projection

Ethereum's chain currently stores approximately **800 TB** of block data.

| | Stored size |
|---|---|
| Raw JSON (uncompressed) | 800 TB |
| zstd alone | ~141 TB |
| **NEP + zstd** | **~127 TB** |

**NEP saves ~14TB more than zstd.** At current cloud storage prices ($0.02/GB), that is approximately **$280,000** in storage cost alone — before accounting for bandwidth savings on RPC responses.

The Ethereum chain grows ~80GB/day. The savings compound.

---

### Reproduce it yourself in 60 seconds

No pre-built files. No API key. Fetches live mainnet data.

```bash
# Mac / Linux
pip install zstandard brotli
python nep_reproduce.py --blocks 20

# Windows
py -m pip install zstandard brotli
py nep_reproduce.py --blocks 20
```

Output: a table of raw byte counts for every block, every method, with lossless verification. Every ratio in this document is derived from that same script.

---

### What it is / what it is not

| NEP is | NEP is not |
|---|---|
| A structured encoding protocol for Ethereum JSON | A universal compressor |
| A drop-in pipeline step before zstd | A replacement for zstd |
| Tuned for eth_getBlockByNumber responses | Useful for arbitrary data |
| Lossless and independently verifiable | Based on approximations |
| Effective on JSON API format (not RLP) | Tested on RLP binary format |

---

### Integration

NEP is a Python library today. Production integration path:

```
eth_getBlockByNumber response
        ↓
nep.encode(json_bytes)      # ~5ms per block
        ↓
zstd.compress(nep_binary)   # same as before, smaller input
        ↓
storage
```

Decoding is the reverse. No external state required beyond the optional zstd dictionary (112KB, trained on a few hundred blocks).

Target languages for production: Python (current), C extension, Rust, Go.

---

### Contact

Available for a technical call, a pilot on your own dataset, or to share the full benchmark methodology.

**Lourens Wasserman** — Neurop Encoding Protocol  
*Repository and reproduce script: github.com/Louw115/nep-ethereum-compression*
