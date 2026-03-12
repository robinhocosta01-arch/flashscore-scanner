FROM python:3.11-slim

# Instala Chrome e dependências
RUN apt-get update && apt-get install -y \
    wget curl unzip gnupg \
    chromium chromium-driver \
    --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Diretório de trabalho
WORKDIR /app

# Copia arquivos
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner.py .

# Roda o scanner
CMD ["python", "scanner.py"]
