FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-payment.txt requirements-trtc.txt ./
RUN pip install --no-cache-dir -r requirements-payment.txt -r requirements-trtc.txt
RUN useradd --uid 10001 --create-home dcta && mkdir /data && chown dcta:dcta /data
COPY dcta ./dcta
USER dcta
ENV BIND_ADDRESS=0.0.0.0 PORT=8780 DCTA_DATA_DIR=/data
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8780') + '/healthz', timeout=2)"
CMD ["python", "-m", "dcta.public_server"]

