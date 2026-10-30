# Lookout Hub: detection + alerts + the app API, with go2rtc and the person model built in.
# One container; everything it keeps lives in /data.
FROM python:3.12-slim
ARG TARGETARCH
ARG GO2RTC_VERSION=1.9.14
ARG LOOKOUT_VERSION=dev
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH:-amd64}" \
 && chmod +x /usr/local/bin/go2rtc
WORKDIR /app
COPY requirements.txt .
# ONNX Runtime with OpenVINO on x86 (about 2x faster on Intel CPUs, uses Intel GPUs it has
# drivers for); the plain build elsewhere
RUN pip install --no-cache-dir -r requirements.txt  && if [ "${TARGETARCH:-amd64}" = "amd64" ]; then pip install --no-cache-dir onnxruntime-openvino openvino;     else pip install --no-cache-dir onnxruntime; fi
# Person models: RF-DETR nano/small/medium (Apache-2.0, exported by tools/export_rfdetr.py); the
# hub picks one per machine. Downloaded from the project's release and checked against models.sha256.
ARG MODELS_URL=https://github.com/RogerSquare/lookout/releases/download/models-v1
COPY models.sha256 /models/
RUN cd /models && for f in $(awk '{print $2}' models.sha256); do curl -fsSL --retry 3 -o "$f" "$MODELS_URL/$f"; done \
 && sha256sum -c models.sha256
COPY hub.py store.py streams.py detect.py frigate.py auth.py pairing.py bench.py ./
COPY web ./web
ENV PYTHONUNBUFFERED=1 LOOKOUT_DATA=/data LOOKOUT_VERSION=${LOOKOUT_VERSION}
VOLUME /data
EXPOSE 8765 8766 8555
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('LOOKOUT_PORT', '8765') + '/healthz', timeout=3)"
CMD ["python", "hub.py"]
