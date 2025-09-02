FROM python:3.13-slim

# Устанавливаем uv и ffmpeg
RUN pip install uv && \
    apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*


WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache .

COPY .env .
COPY main.py .



CMD ["python", "main.py"]
