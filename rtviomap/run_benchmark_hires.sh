#!/usr/bin/env bash
# One-off experiment (not the canonical benchmark -- see run_benchmark.sh for
# that): measure the throughput cost of --image_size above the demo's
# default 518, since PS-17 specifies 1080p/4K input and the ~7fps number in
# session.md sec 4 was measured at 518x294. This does NOT answer the
# accuracy question (no ground truth exists for this scene either way -- see
# rtvio/docs/STREAMING.md's note that ground-truth scoring tooling was
# removed) -- it only answers "what does higher resolution cost in fps",
# which is a real input to the same budget math regardless.
set -euo pipefail

cd "$HOME/lingbot-map"
source .venv/bin/activate
export PATH="/usr/local/cuda-12.9/bin:$PATH"
export NVCC_PREPEND_FLAGS="-ccbin g++-14"

SIZE="${1:-1036}"   # must be divisible by patch_size=14; 1036 = 2x default 518
echo "=== running courthouse benchmark at --image_size $SIZE (default is 518) ==="
python demo.py \
  --model_path /mnt/c/Users/HP/Desktop/RTVIO/rtviomap/checkpoints/lingbot-map.pt \
  --image_folder example/courthouse \
  --image_size "$SIZE"
