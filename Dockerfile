# Use a base image with Python
FROM --platform=linux/arm64/v8 python:3.9

# Define environment variables
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Install necessary packages and Chromium
RUN apt-get update \
  && apt-get install -y wget gnupg \
  && apt-get install -y chromium fonts-ipafont-gothic fonts-wqy-zenhei fonts-thai-tlwg fonts-khmeros fonts-kacst fonts-freefont-ttf libxss1 \
  --no-install-recommends \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy the requirements and source code files
COPY requirements.txt .
COPY . .

# Create a virtual environment and install Python dependencies
RUN python3 -m venv venv && \
  . venv/bin/activate && \
  pip install --no-cache-dir -r requirements.txt

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/venv/bin:$PATH"

# Expose the application port
EXPOSE 8000

# Command to run the application
CMD ["python3", "-m", "hypercorn", "main:app", "--bind", "0.0.0.0:8000"]
