ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM $BUILD_FROM

# Install Python, ffmpeg, and font
RUN apk add --no-cache \
    python3 \
    py3-pip \
    ffmpeg \
    font-dejavu \
    && pip3 install --no-cache-dir --break-system-packages Pillow

# Copy main script
COPY rootfs/usr/bin/run.py /usr/bin/run.py
RUN chmod +x /usr/bin/run.py

CMD ["python3", "/usr/bin/run.py"]
