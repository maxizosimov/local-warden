FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY warden ./warden

RUN useradd --create-home warden && mkdir /data && chown warden /data
USER warden

ENV WARDEN_HOST=0.0.0.0 WARDEN_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8787
CMD ["python", "-m", "warden.server"]
