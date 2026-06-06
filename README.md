# NEP — Neurop Encoding Protocol
**A structured encoding layer for Ethereum JSON-RPC block data that improves zstd compression by 12–21%.**
NEP sits in front of zstd. It converts verbose Ethereum JSON into a compact binary format before the compressor runs. Not a compressor — a transform.
---
## Results
Tested on real Ethereum mainnet blocks fetched live from a public RPC endpoint. No pre-built datasets.
| Method | Mean ratio | vs zstd | Win rate | Lossless |
|---|---|---|---|---|
| gzip-9 | 4.97x | −13% | 0/200 | N/A |
| brotli-11 | 5.34x | −6% | 0/200 | N/A |
| zstd-9 | 5.71x | baseline | 0/200 | N/A |
| zstd-9 + dict | 5.37x | −6% | 0/200 | N/A |
| NEP + gzip-9 | 5.84x | +2% | 0/200 | 200/200 ✓ |
| NEP + zstd-9 | 6.18x | +8% | 2/200 | 200/200 ✓ |
| **NEP + zstd + dict** | **6.38x** | **+11.7%** | **200/200** | **200/200 ✓** |
**NEP beats plain zstd on every single block. Every output verified lossless.**
Independent reproducibility runs (consecutive recent blocks) show +16% to +21% — see [NEP_BENCHMARK.md](NEP_BENCHMARK.md) for the full three-tier results and explanation of the range.
---
## NeuropBlocks Demo — composable primitives outperform the full engine
`nep_blocks_demo.py` rebuilds the NEP pipeline using 5 composable primitives from the NeuropBlocks library. **No custom encoder — just blocks composed together.**
Run it yourself (requires only `zstandard`):
```bash
pip install zstandard
python nep_blocks_demo.py

Results on 20 real mainnet blocks (4,990 transactions), trained on 480 separate blocks:

Method	vs zstd-22	Blocks won
Plain zstd-22	baseline	—
NeuropBlocks + zstd-22	+3.91%	19/20
NeuropBlocks + dict + zstd-22	+5.14% (incl. 112KB dict)	20/20
Output verified on Windows 10, Python 3.11:

Plain zstd-22 (baseline):              1,510,287 bytes
NeuropBlocks + zstd-22:                1,451,193 bytes  (+3.91%)
NeuropBlocks + dict + zstd-22:         1,318,018 bytes  (+12.73% raw)
NeuropBlocks + dict (with overhead):   1,432,706 bytes  (+5.14%)

The 5 NeuropBlocks doing real work:

Block	Domain	What it does
hex_decode	data/string	All hex fields → raw bytes (hashes, calldata, bloom)
deduplicate_by	data/collection	Address lookup table — from/to/miner + ABI calldata recipients
delta_encode	data/collection	7 numeric sequences: nonce, gasPrice, value, gas, txIndex, maxFeePerGas, priorityFee
delta_decode	data/collection	Lossless verification
pack_integers	data/collection	Tx type bits packed into 4-bit slots
The dictionary amortises in ~17 blocks. In a production stream (Alchemy, QuickNode) it is effectively free.

Reproduce it yourself
No API key. No pre-built files. Fetches live mainnet data.

# Install
pip install zstandard brotli        # Mac / Linux
py -m pip install zstandard brotli  # Windows
# Run interactive demo
python nep_reproduce.py   # Mac / Linux
py nep_reproduce.py       # Windows

The script runs in three stages:

Stage	Blocks	Time	Held-out test
1 — Quick demo	20	~1 min	12 blocks
2 — Confidence	100	~3 min	60 blocks
3 — Full benchmark	200	~8 min	120 blocks
Each stage prints a full per-block table (raw bytes × ratio × lossless flag) and saves a JSON results file. The cache builds progressively — Stage 2 only fetches the delta blocks.

How it works
Ethereum JSON  →  NEP encode  →  zstd  →  storage
Ethereum JSON  ←  NEP decode  ←  zstd  ←  storage

NEP is a fully deterministic 4-stage transform — not a learned model, not a black box. Every byte is reversible by spec.

Stage	What it does	Why it helps
1. Hex → binary	"0x1a2b3c" → raw bytes	Cuts every hex value to 50% of its JSON size
2. Schema stripping	JSON keys replaced with 2-byte IDs	Removes ~30% of block JSON (key names repeated per transaction)
3. Address deduplication	Hot addresses (routers, USDC, WETH) stored once, referenced by 2-byte index	42-char address → 2 bytes on every repeat
4. Delta encoding	Numeric fields stored as raw integers	Better run-length and LZ77 pattern matching
After these 4 stages, the binary blob is ~46% of the original JSON size — before any compressor runs.

Scale
At Ethereum's current chain size (~800 TB):

Stored size
Uncompressed	800 TB
zstd-9	~141 TB
NEP + zstd + dict	~127 TB
NEP saves ~14 TB more than zstd on the full chain. At $0.02/GB object storage, that is ~$280K in storage cost alone — before bandwidth savings on RPC responses. The chain grows ~80 GB/day.

Files
File	What it is
nep_reproduce.py	Self-contained benchmark script — run this
nep_blocks_demo.py	NeuropBlocks primitive composition demo
engine_v2.py	NEP encoder / decoder
NEP_BENCHMARK.md	Full methodology, three-tier results, design notes
NEP_ONE_PAGER.md	Non-technical summary
large_scale_results.json	Raw results from the original 200-block run
What NEP is / is not
NEP is	NEP is not
A structured encoding protocol for Ethereum JSON	A universal compressor
A drop-in pipeline step before zstd	A replacement for zstd
Tuned for eth_getBlockByNumber responses	Useful for arbitrary data
Lossless and independently verifiable	Based on approximations
Effective on JSON API format	Tested on RLP binary
Contact
Built by Lourens Wasserman. For licensing, acquisition, or technical discussion: open an issue or reach out via the profile.
