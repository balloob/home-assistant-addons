const supportedBitsPerPixel = [1, 24];

export class BMPEncoder {
  constructor(width, height, bitsPerPixel) {
    this.width = width;
    this.height = height;
    this.bitsPerPixel = bitsPerPixel;
    if (!supportedBitsPerPixel.includes(bitsPerPixel)) {
      throw new Error(`Unsupported bits per pixel. Supported values are: ${supportedBitsPerPixel.join(", ")}`);
    }
  };

  encode(data) {
    const header = this.createHeader();
    const pixelData = this.createPixelData(data);
    return Buffer.concat([header, pixelData]);
  };

  createHeader() {
    const headerSize = this.bitsPerPixel === 1 ? 62 : 54;
    const fileSize = headerSize + this.width * this.height * (this.bitsPerPixel / 8);
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
    const pixelData = Buffer.alloc(this.width * this.height * (this.bitsPerPixel / 8));
    let offset = 0;

    if (this.bitsPerPixel === 1) {
      for (let y = this.height - 1; y >= 0; y--) {
        for (let x = 0; x < this.width; x++) {
          const pixel = imageData[y * this.width + x];
          const byteIndex = Math.floor(offset / 8);
          const bitIndex = offset % 8;
          if (pixel == 0xFF) {
            pixelData[byteIndex] |= (1 << (7 - bitIndex));
          } else {
            pixelData[byteIndex] &= ~(1 << (7 - bitIndex));
          }
          offset++;
        }
      }
    } else if (this.bitsPerPixel === 24) {
      const padding = 4 - ((this.width * 3) % 4);
      for (let y = this.height - 1; y >= 0; y--) {
        for (let x = 0; x < this.width; x++) {
          const sourceIndex = (y * this.width * 3) + (x * 3);
          const r = imageData[sourceIndex];
          const g = imageData[sourceIndex + 1];
          const b = imageData[sourceIndex + 2];
          pixelData.writeUInt8(b, offset++);
          pixelData.writeUInt8(g, offset++);
          pixelData.writeUInt8(r, offset++);
        }
        for (let p = 0; p < padding; p++) {
          pixelData.writeUInt8(0, offset++);
        }
      }
    }

    return pixelData;
  }
}
