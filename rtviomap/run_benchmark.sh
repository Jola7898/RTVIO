#!/usr/bin/env bash
# Reruns the courthouse benchmark with the full stack in place (real
# FlashInfer + CUDA Toolkit 12.9 nvcc for JIT-compiling its kernels).
# Run via: wsl -d Ubuntu -u root -- bash /mnt/c/Users/HP/Desktop/RTVIO/rtviomap/run_benchmark.sh
set -euo pipefail

cd "$HOME/lingbot-map"
source .venv/bin/activate
export PATH="/usr/local/cuda-12.9/bin:$PATH"
# nvcc 12.9 rejects host gcc > 14; Ubuntu 26.04 ships gcc-15. Pin nvcc to
# gcc-14 (installed alongside) via its own supported env-var mechanism so
# FlashInfer's runtime JIT builds pick it up too.
export NVCC_PREPEND_FLAGS="-ccbin g++-14"
# NOTE: deliberately NOT clearing ~/.cache/flashinfer this time -- the first
# successful run already paid the one-time JIT-compile cost for every kernel
# shape this benchmark exercises. This run measures the warm/steady-state
# speed, which is what actually compares to LingBot-Map's claimed ~20fps.

echo "=== sanity checks ==="
which python
python --version
which nvcc
nvcc --version
python -c "import flashinfer; print('flashinfer', flashinfer.__version__)"

echo "=== running courthouse benchmark ==="
python demo.py \
  --model_path /mnt/c/Users/HP/Desktop/RTVIO/rtviomap/checkpoints/lingbot-map.pt \
  --image_folder example/courthouse
