# ARG BUILD_FROM
# FROM $BUILD_FROM

# # Install Chromium
# RUN apk add --no-cache \
#   chromium \
#   nss \
#   freetype \
#   harfbuzz \
#   ca-certificates \
#   ttf-freefont

# # Ensure Node.js and npm are installed (for Alpine-based images)
# RUN apk add --no-cache nodejs npm || echo "Node.js already installed"


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
RUN npm ci --unsafe-perm

# Copy the rest of the project files
COPY ha-puppet/ .

# Set Puppeteer to use Alpine Chromium
# ENV PUPPETEER_EXECUTABLE_PATH="/usr/bin/chromium"

# Run the application
CMD ["node", "http.js"]
