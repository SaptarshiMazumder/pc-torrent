import subprocess
import sys
import os

# --- Configure these ---
BLENDER_PATH = r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"
BLEND_FILE   = r"C:\Users\googler\Downloads\blender-4.2-splash\gold-splash_screen.blend"
OUTPUT_DIR   = r"C:\Users\googler\OneDrive\Desktop\Projects\TestRenderBlender\output"
OUTPUT_NAME  = "frame_####"   # #### = frame number padding
FRAME_START  = 1
FRAME_END    = 4
SETUP_SCRIPT = os.path.join(os.path.dirname(__file__), "blender_setup.py")
# -----------------------

os.makedirs(OUTPUT_DIR, exist_ok=True)

output_path = os.path.join(OUTPUT_DIR, OUTPUT_NAME)

cmd = [
    BLENDER_PATH,
    "--background",
    BLEND_FILE,
    "--python", SETUP_SCRIPT,
    "--render-output", output_path,
    "--frame-start", str(FRAME_START),
    "--frame-end",   str(FRAME_END),
    "--render-anim",
]

print("Running:", " ".join(cmd))
result = subprocess.run(cmd, check=False)
sys.exit(result.returncode)
