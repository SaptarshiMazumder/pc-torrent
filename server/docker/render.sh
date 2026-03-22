#!/bin/bash
set -euo pipefail

BLEND_FILE="${BLEND_FILE:-}"
if [ -z "$BLEND_FILE" ]; then
    BLEND_FILE=$(find /input -name "*.blend" -print -quit)
fi

if [ -z "$BLEND_FILE" ] || [ ! -f "$BLEND_FILE" ]; then
    echo "ERROR: No .blend file found in /input"
    exit 1
fi

echo "=== PC Rent Render Container ==="
echo "Blend file: $BLEND_FILE"

run_render() {
    local device="$1"
    local label="$2"
    local log_file="$3"
    local -a cmd=(
        /opt/blender/blender
        -b "$BLEND_FILE"
        -o /output/frame####
        -E CYCLES
        -a
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

attempt_render_with_fallbacks() {
    local log_file
    log_file=$(mktemp)
    local last_exit=0

    if nvidia-smi > /dev/null 2>&1; then
        echo "GPU detected:"
        nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

        for device in OPTIX CUDA; do
            echo "Starting render..."
            if run_render "$device" "$device (GPU)" "$log_file"; then
                rm -f "$log_file"
                return 0
            else
                last_exit=$?
            fi

            if grep -q "Found no Cycles device of the specified type" "$log_file"; then
                echo "Cycles device '$device' is not available in Blender. Trying next fallback..."
                : > "$log_file"
                continue
            fi

            rm -f "$log_file"
            return "$last_exit"
        done

        echo "GPU detected, but Blender could not use OPTIX or CUDA. Falling back to CPU rendering."
    else
        echo "WARNING: No GPU detected, falling back to CPU rendering"
    fi

    echo "Starting render..."
    if run_render "" "CPU" "$log_file"; then
        rm -f "$log_file"
        return 0
    else
        last_exit=$?
    fi

    rm -f "$log_file"
    return "$last_exit"
}

attempt_render_with_fallbacks

echo "Render complete. Output files:"
ls -la /output/
