FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared

COPY simulator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY data/ /app/data/
COPY simulator/ .

CMD ["python", "main.py"]