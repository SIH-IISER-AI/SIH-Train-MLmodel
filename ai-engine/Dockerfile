# Use a lightweight Python image
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr (crucial for Docker logs)
ENV PYTHONUNBUFFERED=1

# Set the working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the microservice code
COPY . .

# Run the main loop
CMD ["python", "main.py"]