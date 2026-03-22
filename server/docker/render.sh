#!/bin/bash
set -e

BLEND_FILE=$(find /input -name "*.blend" -print -quit)

if [ -z "$BLEND_FILE" ]; then
    echo "ERROR: No .blend file found in /input"
    exit 1
fi

echo "=== PC Rent Render Container ==="
echo "Blend file: $BLEND_FILE"

# Detect available GPUs and pick render device
if nvidia-smi > /dev/null 2>&1; then
    echo "GPU detected:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    DEVICE_FLAG="-- --cycles-device OPTIX"
    echo "Rendering with: OPTIX (GPU)"
else
    DEVICE_FLAG=""
    echo "WARNING: No GPU detected, falling back to CPU rendering"
fi

echo "Starting render..."
/opt/blender/blender \
    -b "$BLEND_FILE" \
    -o /output/frame#### \
    -E CYCLES \
    -a \
    $DEVICE_FLAG

echo "Render complete. Output files:"
ls -la /output/
