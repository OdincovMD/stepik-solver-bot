#!/bin/sh
set -eu
LLM_DATA_DIR=${LLM_DATA_DIR:-/home/admin/.local/share/raspberry-llm}
LLM_MODEL_FILE=${LLM_MODEL_FILE:-Qwen3-4B-Instruct-2507-Q4_K_M.gguf}
if [ "${LLM_REPACK:-1}" = "0" ]; then
  LLM_REPACK_FLAG=--no-repack
else
  LLM_REPACK_FLAG=--repack
fi
exec "$LLM_DATA_DIR/runtime/llama-b12359/llama-server" \
  --model "$LLM_DATA_DIR/models/$LLM_MODEL_FILE" \
  --alias raspberry-llm \
  --host 127.0.0.1 --port 11434 \
  --ctx-size 2048 --parallel 1 \
  --threads 3 --threads-batch 3 --threads-http 2 \
  --batch-size 128 --ubatch-size 128 \
  --n-gpu-layers 0 "$LLM_REPACK_FLAG" --no-warmup --cache-ram 0 \
  --jinja --reasoning off --cors-origins localhost \
  --reasoning-budget 0 --n-predict 256 \
  --temp 0.6 --top-p 0.95 --top-k 20 \
  --timeout 180 --metrics
