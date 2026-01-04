const supportedBitsPerPixel = [1, 24];

export class BMPEncoder {
  constructor(width, height, bitsPerPixel) {
    this.width = width;
    this.height = height;
    this.bitsPerPixel = bitsPerPixel;
    if (!supportedBitsPerPixel.includes(bitsPerPixel)) {
      throw new Error(`Unsupported bits per pixel. Supported values are: ${supportedBitsPerPixel.join(", ")}`);
    }

    let padding = (this.width * (this.bitsPerPixel / 8)) % 4;
    if (padding > 0) {
      padding = 4 - padding;
    }
    this.padding = padding;
    this.paddedWidthBytes = Math.ceil(this.width * (this.bitsPerPixel / 8)) + padding;
  };

  encode(data) {
    const header = this.createHeader();
    const pixelData = this.createPixelData(data);
    return Buffer.concat([header, pixelData]);
  };

  createHeader() {
    const headerSize = this.bitsPerPixel === 1 ? 62 : 54;
    const fileSize = headerSize + this.height * this.paddedWidthBytes;
    const header = Buffer.alloc(headerSize);
    header.write("BM", 0, 2, "ascii");
    header.writeUInt32LE(fileSize, 2);
    header.writeUInt32LE(0, 6);
    header.writeUInt32LE(headerSize, 10);
    header.writeUInt32LE(40, 14);
    header.writeInt32LE(this.width, 18);
    header.writeInt32LE(this.height, 22); // Negative height for top-down DIB
    header.writeUInt16LE(1, 26); // Number of color planes
    header.writeUInt16LE(this.bitsPerPixel, 28); // Bits per pixel
    header.writeUInt32LE(0, 30); // Compression (none)
    header.writeUInt32LE(this.width * this.height * (this.bitsPerPixel / 8), 34); // Image size
    header.writeInt32LE(0, 38); // Horizontal resolution (pixels per meter)
    header.writeInt32LE(0, 42); // Vertical resolution (pixels per meter)
    header.writeUInt32LE(this.bitsPerPixel === 1 ? 2 : 0, 46); // Number of colors in color palette
    header.writeUInt32LE(this.bitsPerPixel === 1 ? 2 : 0, 50); // Important colors
    if (this.bitsPerPixel === 1) {
      header.writeUInt32LE(0x00000000, 54); // Color palette 0 - black
      header.writeUInt32LE(0x00FFFFFF, 58); // Color palette 1 - white
    }
    return header;
  };

  // Handles bitsPerPixel 1, 24

  createPixelData(imageData) {
    let offset = 0;
    const pixelData = Buffer.alloc(this.height * this.paddedWidthBytes);

    if (this.bitsPerPixel === 1) {
      for (let y = 0; y < this.height; y++) {
        for (let x = 0; x < this.width; x++) {
          const pixel = imageData[y * this.width + x];
          const byteIndex = ((this.height - 1 - y) * this.paddedWidthBytes + Math.floor(x / 8));
          const bitIndex = x % 8;
          const currentByte = pixelData.readUInt8(byteIndex);
          if (pixel == 0xFF) {
            pixelData.writeUInt8(currentByte | (1 << (7 - bitIndex)), byteIndex);
          } else {
            pixelData.writeUInt8(currentByte & ~(1 << (7 - bitIndex)), byteIndex);
          }
        }
        offset += Math.ceil(this.width / 8);
        for (let p = 0; p < this.padding; p++) {
          pixelData.writeUInt8(0, offset++);
        }
      }
    } else if (this.bitsPerPixel === 24) {
      for (let y = this.height - 1; y >= 0; y--) {
        for (let x = 0; x < this.width; x++) {
          const sourceIndex = (y * this.paddedWidthBytes) + (x * 3);
          const r = imageData[sourceIndex];
          const g = imageData[sourceIndex + 1];
          const b = imageData[sourceIndex + 2];
          pixelData.writeUInt8(b, offset++);
          pixelData.writeUInt8(g, offset++);
          pixelData.writeUInt8(r, offset++);
        }
        for (let p = 0; p < this.padding; p++) {
          pixelData.writeUInt8(0, offset++);
        }
      }
    }

    return pixelData;
  }
}
