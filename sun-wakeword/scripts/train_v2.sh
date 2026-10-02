#!/usr/bin/env bash
# Retrain Sun wake-word model with new positives (Russian EL) + Russian hard negatives.
set -euo pipefail
cd "$(dirname "$0")/.."

source .venv/bin/activate

# TensorFlow 2.21 in this venv needs CUDA 12 user-space libs from pip packages
# plus WSL CUDA bridge libs to discover the host NVIDIA driver.
export LD_LIBRARY_PATH="/usr/lib/wsl/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cusolver/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cudnn/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cublas/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cufft/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cusparse/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cuda_runtime/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cuda_cupti/lib:$(pwd)/.venv/lib/python3.10/site-packages/nvidia/nccl/lib:${LD_LIBRARY_PATH:-}"
export XLA_FLAGS="--xla_gpu_cuda_data_dir=$(pwd)/.venv/lib/python3.10/site-packages/nvidia/cuda_nvcc ${XLA_FLAGS:-}"

# Modes:
#   TRAIN_MODE=resume (default): continue in existing train_dir (restore_checkpoint=1)
#   TRAIN_MODE=fresh: create new train_dir with timestamp suffix (restore_checkpoint=0)
TRAIN_MODE="${TRAIN_MODE:-resume}"
BASE_CONFIG="$(pwd)/training_parameters_v2.yaml"
RUN_CONFIG="${BASE_CONFIG}"
RESTORE_CHECKPOINT=1

if [[ "${TRAIN_MODE}" == "fresh" ]]; then
  RESTORE_CHECKPOINT=0
  NEW_DIR="$(pwd)/trained_models/sun_v2_$(date +%Y%m%d_%H%M%S)"
  RUN_CONFIG="$(pwd)/training_parameters_v2.run.yaml"
  python - <<PY
import yaml
cfg = yaml.safe_load(open("${BASE_CONFIG}", "r", encoding="utf-8"))
cfg["train_dir"] = "${NEW_DIR}"
with open("${RUN_CONFIG}", "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
print("Using fresh train_dir:", cfg["train_dir"])
PY
else
  echo "Using resume mode in train_dir from ${BASE_CONFIG}"
fi

python -m microwakeword.model_train_eval \
  --training_config="${RUN_CONFIG}" \
  --train 1 \
  --restore_checkpoint "${RESTORE_CHECKPOINT}" \
  --test_tf_nonstreaming 0 \
  --test_tflite_nonstreaming 0 \
  --test_tflite_nonstreaming_quantized 0 \
  --test_tflite_streaming 0 \
  --test_tflite_streaming_quantized 1 \
  --use_weights "best_weights" \
  mixednet \
  --pointwise_filters "64,64,64,64" \
  --repeat_in_block "1,1,1,1" \
  --mixconv_kernel_sizes "[5],[7,11],[9,15],[23]" \
  --residual_connection "0,0,0,0" \
  --first_conv_filters 32 \
  --first_conv_kernel_size 5 \
  --stride 3 2>&1 | tee "$(pwd)/train_v2.log"
