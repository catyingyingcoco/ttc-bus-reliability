FROM python:3.12-slim

# keep Python output unbuffered so logs show up immediately
ENV PYTHONUNBUFFERED=1 \
    TZ=America/Toronto

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

CMD ["python", "src/poller.py"]
