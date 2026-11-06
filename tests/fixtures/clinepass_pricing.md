# ClinePass reference pricing (fixture)

Mirrors the shape of the published table, including the variant rows that have
to collapse onto one entry.

## Reference pricing

ClinePass is a flat monthly subscription.

| Model | Input | Output | Cached Read | Cached Write |
| - | - | - | - | - |
| DeepSeek V4.1 Flash (Off-peak) | \$0.15 | \$0.60 | \$0.003 | - |
| DeepSeek V4.1 Flash (Peak) | \$0.30 | \$1.20 | \$0.006 | - |
| GLM 5.3 | \$1.40 | \$4.40 | \$0.26 | - |
| Kimi K3.1 Pro | \$2.00 | \$10.00 | \$0.20 | $2.00 (5min) / $4.00 (1h) |
| MiniMax M3 (≤ 512K tokens) | \$0.30 | \$1.20 | \$0.06 | - |
| MiniMax M3 (> 512K tokens) | \$0.60 | \$2.40 | \$0.12 | - |
| MiMo V2.6 Pro | \$0.435 | \$0.87 | \$0.0036 | - |
| Qwen3.7 Plus (≤ 256K tokens) | \$0.40 | \$1.60 | \$0.04 | \$0.50 |
| Qwen3.7 Plus (> 256K tokens) | \$1.20 | \$4.80 | \$0.12 | \$1.50 |
| Qwen3.8 Max | \$2.00 | \$6.00 | \$0.17 | \$2.50 |

## Usage

Not part of the table.
