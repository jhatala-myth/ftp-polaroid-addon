ARG BUILD_FROM
FROM $BUILD_FROM

# Install ffmpeg and Pillow dependencies
RUN apk add --no-cache \
    ffmpeg \
    python3 \
    py3-pip \
    font-dejavu \
    && pip3 install --no-cache-dir Pillow

# Copy main script
COPY rootfs/usr/bin/run.py /usr/bin/run.py
RUN chmod +x /usr/bin/run.py

CMD ["python3", "/usr/bin/run.py"]
