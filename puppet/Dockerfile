FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y \
    chromium \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy package.json and package-lock.json first
COPY ha-puppet/package*.json ./

# Skip Puppeteer's Chrome download since we use system Chromium
ENV PUPPETEER_SKIP_DOWNLOAD=true

# Ensure a clean state before installing
RUN npm ci --omit=dev

# Copy the rest of the project files
COPY ha-puppet/ .

# Run the application
CMD ["node", "http.js"]
