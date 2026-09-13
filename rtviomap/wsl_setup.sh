#!/usr/bin/env bash
# Run this INSIDE the WSL2 Ubuntu shell, after the wsl --install reboot is
# done and you've set your Linux username/password on first launch.
#
# From Windows, open the "Ubuntu" app (or `wsl` from PowerShell) and run:
#   bash /mnt/c/Users/HP/Desktop/RTVIO/rtviomap/wsl_setup.sh
#
# What this does, and why:
# - Clones lingbot-map fresh into WSL's native ext4 filesystem (~/), not
#   /mnt/c. git/pip/python are much slower over the 9p mount that backs
#   /mnt/c, so the *code* lives natively.
# - Points at the checkpoint we ALREADY downloaded on the Windows side
#   (4.6 GB) via /mnt/c instead of re-downloading it. One sequential 4.6 GB
#   read through the 9p mount at startup is fine; it's git/pip's many small
#   file operations that are slow there, not one big read.
# - Installs the exact torch/cu128 build their README recommends (native
#   Windows already had a newer nightly that worked for demo.py, but
#   FlashInfer's prebuilt wheels are the whole point of this environment,
#   so match their pin exactly here).
set -euo pipefail

WIN_CKPT="/mnt/c/Users/HP/Desktop/RTVIO/rtviomap/checkpoints/lingbot-map.pt"
REPO_DIR="$HOME/lingbot-map"

echo "=== [1/6] apt prerequisites ==="
# NOTE: originally pinned python3.10 here, but whatever Ubuntu release WSL
# installs may not carry it (e.g. Ubuntu 26.04 "Resolute" ships Python 3.14
# only, and dropped 3.10 from the repos entirely). torch==2.8.0+cu128 (the
# whole reason we're pinning versions here) only has wheels up to cp313, so
# instead of fighting apt/PPA availability across Ubuntu releases, get the
# interpreter from `uv` (astral.sh) below -- it downloads a standalone
# CPython build independent of the distro's package repos.
sudo apt-get update -y
sudo apt-get install -y git build-essential curl
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  source "$HOME/.local/bin/env"
fi
uv python install 3.12

echo "=== [2/7] checking GPU is visible inside WSL ==="
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi not found inside WSL. This means the Windows NVIDIA driver"
  echo "doesn't have WSL CUDA support, or is stale. Install/update the driver"
  echo "from https://www.nvidia.com/Download/index.aspx (NOT a separate Linux"
  echo "driver -- WSL2 uses the Windows host driver) and re-run this script."
  exit 1
fi
nvidia-smi

echo "=== [3/7] CUDA Toolkit (nvcc) -- FlashInfer JIT-compiles kernels at first ==="
echo "use, and needs the actual compiler, not just the runtime .so files torch's"
echo "pip wheel bundles. Blackwell (sm_120, e.g. RTX 50-series) specifically"
echo "needs CUDA >= 12.9 toolchain support -- torch's own cu128 pin is fine for"
echo "the runtime libs, nvcc version is independent of that."
if ! command -v nvcc >/dev/null 2>&1; then
  curl -s -o /tmp/cuda-keyring.deb \
    https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
  dpkg -i /tmp/cuda-keyring.deb
  apt-get update -y
  apt-get install -y cuda-toolkit-12-9
fi
export PATH="/usr/local/cuda-12.9/bin:$PATH"
nvcc --version

# nvcc 12.9 refuses to compile with a host GCC newer than 14, but Ubuntu
# 26.04 "Resolute" ships gcc-15 by default -- install gcc-14 from universe
# and pin nvcc to it via NVCC_PREPEND_FLAGS (nvcc's own supported mechanism
# for this, so every nvcc invocation downstream -- including FlashInfer's
# own JIT builds -- picks it up automatically without patching anything).
if ! command -v g++-14 >/dev/null 2>&1; then
  apt-get install -y gcc-14 g++-14
fi
export NVCC_PREPEND_FLAGS="-ccbin g++-14"

echo "=== [4/7] clone lingbot-map (native filesystem, not /mnt/c) ==="
if [ ! -d "$REPO_DIR" ]; then
  git clone https://github.com/Robbyant/lingbot-map.git "$REPO_DIR"
fi
cd "$REPO_DIR"
# matplotlib >=3.9 removed cm.get_cmap(); their pin predates that removal.
# NOTE: cm.colormaps isn't valid either on newer matplotlib (3.11, what uv
# resolves here) -- the registry lives on the top-level `matplotlib` module,
# not the `cm` submodule (see this same file's own line ~747 for the correct
# form already used elsewhere in their code), which needs its own import.
sed -i "s/cm.get_cmap('viridis')/matplotlib.colormaps['viridis']/" \
  lingbot_map/vis/point_cloud_viewer.py
sed -i "/^import matplotlib.cm as cm$/a import matplotlib" \
  lingbot_map/vis/point_cloud_viewer.py

echo "=== [5/7] venv + torch (cu128, pinned to match FlashInfer's prebuilt wheels) ==="
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install -e ".[vis]"

echo "=== [6/7] FlashInfer -- the whole point of doing this in WSL ==="
uv pip install --index-url https://pypi.org/simple flashinfer-python
# Optional: prebuilt JIT cache so the first real inference call isn't also
# paying for kernel compilation. Skip if this 404s; it JIT-compiles on first
# use either way.
# IMPORTANT: pin this to the exact flashinfer-python version just installed.
# An unpinned install grabs whatever jit-cache build is newest, which lags
# flashinfer-python's own releases -- that mismatch makes flashinfer raise at
# import time (RuntimeError: ...does not match...) rather than silently using
# stale kernels, so it must match exactly or be skipped entirely.
FI_VERSION="$(python -c 'import flashinfer; print(flashinfer.__version__)')"
uv pip install "flashinfer-jit-cache==${FI_VERSION}" -f https://flashinfer.ai/whl/cu128/flashinfer-jit-cache/ || \
  echo "flashinfer-jit-cache not available for flashinfer==$FI_VERSION -- fine, first call just JIT-compiles"

echo "=== [7/7] smoke test against the courthouse example, same as the Windows run ==="
echo "Using checkpoint at: $WIN_CKPT"
if [ ! -f "$WIN_CKPT" ]; then
  echo "Checkpoint not found at $WIN_CKPT -- did the path change? Check the"
  echo "Windows side (rtviomap/checkpoints/lingbot-map.pt) or edit WIN_CKPT above."
  exit 1
fi

python demo.py --model_path "$WIN_CKPT" --image_folder example/courthouse

echo ""
echo "=== Done. Compare the 'Inference done in Ns' line against the Windows"
echo "SDPA run (1083.4s for 286 frames, ~0.26 fps). This run should be much"
echo "closer to their claimed ~20 fps if FlashInfer actually engaged -- check"
echo "for a 'flashinfer not available' line near the top of this run's output;"
echo "if that line is gone, FlashInfer is active."
