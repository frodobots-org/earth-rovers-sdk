#!/usr/bin/env bash
# Download the SAM-TP checkpoint that GENIE-SAMTP was trained with.
#
# Source: https://drive.google.com/drive/folders/190yHH-TcfQVoByZeB1809sPIR62CsBD1
# Target: autonav-urban/third_party/sam2_ckpt/checkpoint_2.pt
#
# The checkpoint is ~50 MB and is NOT tracked in git.
# You need `gdown` installed: `pip install gdown`

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTONAV_URBAN_ROOT="$(cd "${HERE}/.." && pwd)"
CKPT_DIR="${AUTONAV_URBAN_ROOT}/third_party/sam2_ckpt"
CKPT_PATH="${CKPT_DIR}/checkpoint_2.pt"

mkdir -p "${CKPT_DIR}"

if [[ -f "${CKPT_PATH}" ]]; then
    echo "Checkpoint already present: ${CKPT_PATH}"
    exit 0
fi

if ! command -v gdown >/dev/null 2>&1; then
    echo "ERROR: gdown is not installed. Run: pip install gdown" >&2
    exit 1
fi

echo "Downloading SAM-TP checkpoint to ${CKPT_PATH} ..."
echo "If this fails, download manually from:"
echo "  https://drive.google.com/drive/folders/190yHH-TcfQVoByZeB1809sPIR62CsBD1"
echo "and place the file at the path above."

# The public Google Drive folder contains multiple files; the operator
# should confirm the exact file ID for checkpoint_2.pt before use.
# Pass -O to force output path.
FOLDER_URL="https://drive.google.com/drive/folders/190yHH-TcfQVoByZeB1809sPIR62CsBD1"
gdown --folder "${FOLDER_URL}" -O "${CKPT_DIR}"

if [[ ! -f "${CKPT_PATH}" ]]; then
    echo "ERROR: download completed but checkpoint_2.pt was not found in ${CKPT_DIR}" >&2
    echo "Inspect the folder contents and rename/move the checkpoint file manually." >&2
    ls -la "${CKPT_DIR}" >&2 || true
    exit 1
fi

echo "Done. Checkpoint at: ${CKPT_PATH}"
