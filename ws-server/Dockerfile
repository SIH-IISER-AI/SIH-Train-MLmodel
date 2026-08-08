FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY ws-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ws-server/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]