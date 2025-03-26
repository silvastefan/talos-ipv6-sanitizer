# Dockerfile

FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1  

WORKDIR /app

# Instala dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o script Python principal
COPY main.py .

# Executa o script quando o container iniciar
CMD ["python", "-u", "main.py"]