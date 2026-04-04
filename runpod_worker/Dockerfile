FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04

# Blender system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget xz-utils libxi6 libxxf86vm1 libxfixes3 \
    libxrender1 libgl1-mesa-glx libxkbcommon0 \
    libsm6 libice6 libxrandr2 libxinerama1 libxcursor1 \
    libegl1-mesa libgles2-mesa \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Bake Blender into the image
RUN wget -q https://download.blender.org/release/Blender5.0/blender-5.0.1-linux-x64.tar.xz \
    && tar xf blender-5.0.1-linux-x64.tar.xz \
    && mv blender-5.0.1-linux-x64 /opt/blender \
    && rm blender-5.0.1-linux-x64.tar.xz

RUN mkdir -p /input /output /scripts

# Render scripts (reuse existing ones from server/docker_linux)
COPY server/docker_linux/render.sh /scripts/render.sh
COPY server/docker_linux/progress_handler.py /scripts/progress_handler.py
COPY server/docker_linux/render_driver.py /scripts/render_driver.py
RUN sed -i 's/\r$//' /scripts/render.sh && chmod +x /scripts/render.sh

# Handler deps
RUN pip3 install --no-cache-dir runpod==1.7.9 requests==2.32.3

COPY runpod_worker/handler.py /handler.py

CMD ["python3", "-u", "/handler.py"]
