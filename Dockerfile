FROM python:3.11-slim

# Install fonts for Vietnamese support in matplotlib
RUN apt-get update && apt-get install -y \
    fonts-dejavu-core \
    fonts-noto \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure config directory exists
RUN mkdir -p config

CMD ["python", "bot.py"]
