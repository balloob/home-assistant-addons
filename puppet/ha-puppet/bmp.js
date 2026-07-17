const supportedBitsPerPixel = [1, 8, 24];

export class BMPEncoder {
  constructor(width, height, bitsPerPixel) {
    this.width = width;
    this.height = height;
    this.bitsPerPixel = bitsPerPixel;
    if (!supportedBitsPerPixel.includes(bitsPerPixel)) {
      throw new Error(`Unsupported bits per pixel. Supported values are: ${supportedBitsPerPixel.join(", ")}`);
    }

    // Bytes of actual pixel data per row (whole bytes, so 1bpp rows round up)
    this.rowBytes = Math.ceil((this.width * this.bitsPerPixel) / 8);
    // BMP rows are padded to a multiple of 4 bytes
    let padding = this.rowBytes % 4;
    if (padding > 0) {
      padding = 4 - padding;
    }
    this.padding = padding;
    this.paddedWidthBytes = this.rowBytes + padding;
  };

  encode(data) {
    const header = this.createHeader();
    const pixelData = this.createPixelData(data);
    return Buffer.concat([header, pixelData]);
  };

  createHeader() {
    let headerSize;
    let colorsInPalette = 0;
    if (this.bitsPerPixel === 1) {
      headerSize = 62;
      colorsInPalette = 2;
    } else if (this.bitsPerPixel === 8) {
      headerSize = 1078;
      colorsInPalette = 256;
    } else {
      headerSize = 54;
      colorsInPalette = 0;
    }

    const fileSize = headerSize + this.height * this.paddedWidthBytes;
    const header = Buffer.alloc(headerSize);
    header.write("BM", 0, 2, "ascii");
    header.writeUInt32LE(fileSize, 2);
    header.writeUInt32LE(0, 6);
    header.writeUInt32LE(headerSize, 10);
    header.writeUInt32LE(40, 14);
    header.writeInt32LE(this.width, 18);
    header.writeInt32LE(this.height, 22); // Positive height: bottom-up DIB
    header.writeUInt16LE(1, 26); // Number of color planes
    header.writeUInt16LE(this.bitsPerPixel, 28); // Bits per pixel
    header.writeUInt32LE(0, 30); // Compression (none)
    header.writeUInt32LE(this.height * this.paddedWidthBytes, 34); // Image size (with padding)
    header.writeInt32LE(0, 38); // Horizontal resolution (pixels per meter)
    header.writeInt32LE(0, 42); // Vertical resolution (pixels per meter)
    // Number of colors in palette (2 for 1bpp, 256 for 8bpp)
    header.writeUInt32LE(colorsInPalette, 46);
    header.writeUInt32LE(colorsInPalette, 50);

    // Write color palette for indexed formats
    if (this.bitsPerPixel === 1) {
      // two entries: black and white (B,G,R,0)
      header.writeUInt8(0x00, 54); // blue
      header.writeUInt8(0x00, 55); // green
      header.writeUInt8(0x00, 56); // red
      header.writeUInt8(0x00, 57); // reserved
      header.writeUInt8(0xFF, 58); // blue
      header.writeUInt8(0xFF, 59); // green
      header.writeUInt8(0xFF, 60); // red
      header.writeUInt8(0x00, 61); // reserved
    } else if (this.bitsPerPixel === 8) {
      // 256 grayscale palette entries starting at offset 54, each 4 bytes (B,G,R,0)
      const paletteBase = 54;
      for (let i = 0; i < 256; i++) {
        const off = paletteBase + i * 4;
        header.writeUInt8(i, off + 0); // blue
        header.writeUInt8(i, off + 1); // green
        header.writeUInt8(i, off + 2); // red
        header.writeUInt8(0x00, off + 3); // reserved
      }
    }
    return header;
  };

  // Handles bitsPerPixel 1, 24

  createPixelData(imageData) {
    let offset = 0;
    // Buffer.alloc zero-fills, so row padding bytes are already 0
    const pixelData = Buffer.alloc(this.height * this.paddedWidthBytes);

    if (this.bitsPerPixel === 1) {
      for (let y = 0; y < this.height; y++) {
        const rowOffset = (this.height - 1 - y) * this.paddedWidthBytes;
        for (let x = 0; x < this.width; x++) {
          if (imageData[y * this.width + x] === 0xFF) {
            const byteIndex = rowOffset + Math.floor(x / 8);
            pixelData.writeUInt8(
              pixelData.readUInt8(byteIndex) | (1 << (7 - (x % 8))),
              byteIndex,
            );
          }
        }
      }
    } else if (this.bitsPerPixel === 24) {
      // Source is raw RGB with a stride of width * 3 (no padding)
      for (let y = this.height - 1; y >= 0; y--) {
        for (let x = 0; x < this.width; x++) {
          const sourceIndex = (y * this.width + x) * 3;
          const r = imageData[sourceIndex];
          const g = imageData[sourceIndex + 1];
          const b = imageData[sourceIndex + 2];
          pixelData.writeUInt8(b, offset++);
          pixelData.writeUInt8(g, offset++);
          pixelData.writeUInt8(r, offset++);
        }
        offset += this.padding;
      }
    } else if (this.bitsPerPixel === 8) {
      for (let y = this.height - 1; y >= 0; y--) {
        for (let x = 0; x < this.width; x++) {
          // Expect single-channel grayscale (0-255)
          pixelData.writeUInt8(imageData[y * this.width + x], offset++);
        }
        offset += this.padding;
      }
    }

    return pixelData;
  }
}
