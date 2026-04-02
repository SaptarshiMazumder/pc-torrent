#!/bin/bash
set -euo pipefail

BLENDER_BIN="${BLENDER_BIN:-/opt/blender/blender}"
INPUT_DIR="${INPUT_DIR:-/input}"
OUTPUT_DIR="${OUTPUT_DIR:-/output}"
PROGRESS_SCRIPT="${PROGRESS_SCRIPT:-/progress_handler.py}"
RENDER_DRIVER_SCRIPT="${RENDER_DRIVER_SCRIPT:-/render_driver.py}"
DEVICE_POLICY="${DEVICE_POLICY:-AUTO}"
FRAME_STEP="${FRAME_STEP:-1}"
BLEND_FILE="${BLEND_FILE:-}"

DEVICE_POLICY="$(echo "$DEVICE_POLICY" | tr '[:lower:]' '[:upper:]')"
if ! [[ "$FRAME_STEP" =~ ^[0-9]+$ ]] || [ "$FRAME_STEP" -lt 1 ]; then
    FRAME_STEP=1
fi
export OUTPUT_DIR INPUT_DIR FRAME_STEP DEVICE_POLICY

if [ -z "$BLEND_FILE" ]; then
    BLEND_FILE=$(find "$INPUT_DIR" -name "*.blend" -print -quit)
fi
if [ -z "$BLEND_FILE" ] || [ ! -f "$BLEND_FILE" ]; then
    echo "ERROR: No .blend file found in $INPUT_DIR"
    exit 1
fi
if [ ! -x "$BLENDER_BIN" ]; then
    echo "ERROR: Blender binary is not executable: $BLENDER_BIN"
    exit 1
fi
if [ ! -f "$PROGRESS_SCRIPT" ]; then
    echo "ERROR: Missing progress script: $PROGRESS_SCRIPT"
    exit 1
fi
if [ ! -f "$RENDER_DRIVER_SCRIPT" ]; then
    echo "ERROR: Missing render driver script: $RENDER_DRIVER_SCRIPT"
    exit 1
fi

echo "=== PC Rent Render ==="
echo "Blend file: $BLEND_FILE"
echo "Device policy: $DEVICE_POLICY"
if [ -n "${FRAME_START:-}" ] && [ -n "${FRAME_END:-}" ]; then
    echo "Frame range: ${FRAME_START} - ${FRAME_END} (step ${FRAME_STEP})"
else
    echo "Frame range: scene defaults (step ${FRAME_STEP})"
fi

run_render() {
    local device="$1"
    local label="$2"
    local log_file="$3"
    local -a cmd=(
        "$BLENDER_BIN"
        -b "$BLEND_FILE"
        -P "$PROGRESS_SCRIPT"
        -P "$RENDER_DRIVER_SCRIPT"
    )

    if [ -n "$device" ]; then
        cmd+=(-- --cycles-device "$device")
    fi

    echo "Rendering with: $label"
    set +e
    "${cmd[@]}" 2>&1 | tee "$log_file"
    local render_exit=${PIPESTATUS[0]}
    set -e
    return "$render_exit"
}

log_contains_device_unavailable() {
    local log_file="$1"
    grep -q "Found no Cycles device of the specified type" "$log_file" || \
    grep -q "Requested Cycles device not available" "$log_file"
}

attempt_render() {
    local log_file
    log_file=$(mktemp)
    local last_exit=0

    case "$DEVICE_POLICY" in
        AUTO)
            if nvidia-smi > /dev/null 2>&1; then
                echo "GPU detected:"
                nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true

                for device in CUDA OPTIX; do
                    if run_render "$device" "$device (GPU)" "$log_file"; then
                        rm -f "$log_file"
                        return 0
                    fi
                    last_exit=$?
                    if log_contains_device_unavailable "$log_file"; then
                        echo "Cycles device '$device' unavailable, trying next fallback."
                        : > "$log_file"
                        continue
                    fi
                    rm -f "$log_file"
                    return "$last_exit"
                done

                echo "GPU was detected, but no compatible Cycles GPU device was available. Falling back to CPU."
            else
                echo "WARNING: GPU not accessible. Falling back to CPU."
            fi

            if run_render "" "CPU" "$log_file"; then
                rm -f "$log_file"
                return 0
            fi
            last_exit=$?
            rm -f "$log_file"
            return "$last_exit"
            ;;
        CPU)
            if run_render "" "CPU (strict)" "$log_file"; then
                rm -f "$log_file"
                return 0
            fi
            last_exit=$?
            rm -f "$log_file"
            return "$last_exit"
            ;;
        OPTIX|CUDA)
            if ! nvidia-smi > /dev/null 2>&1; then
                echo "ERROR: Requested device '$DEVICE_POLICY', but GPU is not accessible."
                rm -f "$log_file"
                return 2
            fi
            if run_render "$DEVICE_POLICY" "$DEVICE_POLICY (GPU, strict)" "$log_file"; then
                rm -f "$log_file"
                return 0
            fi
            last_exit=$?
            if log_contains_device_unavailable "$log_file"; then
                echo "ERROR: Requested Cycles device '$DEVICE_POLICY' is unavailable on this worker."
                rm -f "$log_file"
                return 3
            fi
            rm -f "$log_file"
            return "$last_exit"
            ;;
        *)
            echo "ERROR: Unsupported DEVICE_POLICY '$DEVICE_POLICY'"
            rm -f "$log_file"
            return 2
            ;;
    esac
}

attempt_render

echo "Render complete. Output files:"
ls -la "$OUTPUT_DIR"
