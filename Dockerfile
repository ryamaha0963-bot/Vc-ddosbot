FROM python:3.11-slim

# Working directory set karo
WORKDIR /app

# System dependencies install karo (GitHub Actions ke liye)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies install karo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Saari files copy karo
COPY . .

# Spider binary ko executable banao (agar exist karti hai)
RUN chmod +x spider 2>/dev/null || true

# GitHub workflow directory create karo
RUN mkdir -p .github/workflows

# Bot start karo
CMD ["python", "main.py"]
