import bpy

# Enable required add-on (may not exist in Blender 5.0 — skip if missing)
try:
    bpy.ops.preferences.addon_enable(module='copy_global_transform')
except Exception as e:
    print(f"Skipping copy_global_transform: {e}")

# Switch to GPU rendering
prefs = bpy.context.preferences
cprefs = prefs.addons['cycles'].preferences

# Use OptiX for NVIDIA RTX (fastest); fallback to CUDA if needed
cprefs.compute_device_type = 'OPTIX'
cprefs.get_devices()

for device in cprefs.devices:
    device.use = True  # enable all available devices

bpy.context.scene.cycles.device = 'GPU'
print("Render device set to:", bpy.context.scene.cycles.device)
print("Active devices:", [d.name for d in cprefs.devices if d.use])
