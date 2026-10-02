#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."
source .venv/bin/activate

export LD_LIBRARY_PATH="/usr/lib/wsl/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cusolver/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cudnn/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cublas/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cufft/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cusparse/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cuda_runtime/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cuda_cupti/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/nccl/lib:${LD_LIBRARY_PATH:-}"

echo "GPU env loaded (.venv + CUDA libs for TensorFlow)."
