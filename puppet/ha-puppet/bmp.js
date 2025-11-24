const supportedBitsPerPixel = [1, 2, 4, 24];

export class BMPEncoder {
  constructor(width, height, bitsPerPixel, palette = null) {
    this.width = width;
    this.height = height;
    this.bitsPerPixel = bitsPerPixel;
    this.palette = palette;
    
    if (!supportedBitsPerPixel.includes(bitsPerPixel)) {
      throw new Error(`Unsupported bits per pixel. Supported values are: ${supportedBitsPerPixel.join(", ")}`);
    }

    // Validate palette for indexed color modes
    if (bitsPerPixel < 24) {
      const maxColors = 1 << bitsPerPixel; // 2^bitsPerPixel
      if (!palette || palette.length === 0) {
        throw new Error(`Palette is required for ${bitsPerPixel}-bit BMP`);
      }
      if (palette.length > maxColors) {
        throw new Error(`Palette has ${palette.length} colors but ${bitsPerPixel}-bit BMP supports maximum ${maxColors} colors`);
      }
    }

    // Calculate row size in bytes (rounded up) and padding
    const rowBytes = Math.ceil(this.width * this.bitsPerPixel / 8);
    const padding = (4 - (rowBytes % 4)) % 4;
    this.padding = padding;
    this.paddedWidthBytes = rowBytes + padding;
  };

  encode(data) {
    const header = this.createHeader();
    const pixelData = this.createPixelData(data);
    return Buffer.concat([header, pixelData]);
  };

  createHeader() {
    // Header size includes color palette for indexed color modes
    const numColors = this.bitsPerPixel < 24 ? (1 << this.bitsPerPixel) : 0;
    const paletteSize = numColors * 4; // 4 bytes per color (BGRA)
    const headerSize = 54 + paletteSize;
    const fileSize = headerSize + this.height * this.paddedWidthBytes;
    const header = Buffer.alloc(headerSize);
    
    // BMP file header
    header.write("BM", 0, 2, "ascii");
    header.writeUInt32LE(fileSize, 2);
    header.writeUInt32LE(0, 6); // Reserved
    header.writeUInt32LE(headerSize, 10); // Offset to pixel data
    
    // DIB header (BITMAPINFOHEADER)
    header.writeUInt32LE(40, 14); // DIB header size
    header.writeInt32LE(this.width, 18);
    header.writeInt32LE(this.height, 22); // Positive height for bottom-up DIB
    header.writeUInt16LE(1, 26); // Number of color planes
    header.writeUInt16LE(this.bitsPerPixel, 28); // Bits per pixel
    header.writeUInt32LE(0, 30); // Compression (none)
    header.writeUInt32LE(this.height * this.paddedWidthBytes, 34); // Image size
    header.writeInt32LE(0, 38); // Horizontal resolution (pixels per meter)
    header.writeInt32LE(0, 42); // Vertical resolution (pixels per meter)
    header.writeUInt32LE(numColors, 46); // Number of colors in color palette
    header.writeUInt32LE(numColors, 50); // Important colors
    
    // Write color palette for indexed color modes
    if (this.bitsPerPixel < 24 && this.palette) {
      let paletteOffset = 54;
      for (let i = 0; i < numColors; i++) {
        if (i < this.palette.length) {
          // Parse hex color
          const { r, g, b } = this.parseHexColor(this.palette[i]);
          // BMP palette is in BGRA order
          header.writeUInt8(b, paletteOffset++);
          header.writeUInt8(g, paletteOffset++);
          header.writeUInt8(r, paletteOffset++);
          header.writeUInt8(0, paletteOffset++); // Alpha (reserved)
        } else {
          // Fill remaining palette entries with black
          header.writeUInt32LE(0x00000000, paletteOffset);
          paletteOffset += 4;
        }
      }
    }
    
    return header;
  };

  // Handles bitsPerPixel 1, 2, 4, 24

  createPixelData(imageData) {
    const pixelData = Buffer.alloc(this.height * this.paddedWidthBytes);

    if (this.bitsPerPixel === 1) {
      // 1-bit: monochrome (2 colors)
      for (let y = 0; y < this.height; y++) {
        for (let x = 0; x < this.width; x++) {
          const sourceIndex = (y * this.width + x) * 3;
          const paletteIndex = this.findPaletteIndex(imageData[sourceIndex], imageData[sourceIndex + 1], imageData[sourceIndex + 2]);
          const byteIndex = ((this.height - 1 - y) * this.paddedWidthBytes + Math.floor(x / 8));
          const bitIndex = 7 - (x % 8);
          const currentByte = pixelData.readUInt8(byteIndex);
          pixelData.writeUInt8(currentByte | (paletteIndex << bitIndex), byteIndex);
        }
      }
    } else if (this.bitsPerPixel === 2) {
      // 2-bit: 4 colors, 4 pixels per byte
      for (let y = 0; y < this.height; y++) {
        let byteOffset = (this.height - 1 - y) * this.paddedWidthBytes;
        for (let x = 0; x < this.width; x++) {
          const sourceIndex = (y * this.width + x) * 3;
          const paletteIndex = this.findPaletteIndex(imageData[sourceIndex], imageData[sourceIndex + 1], imageData[sourceIndex + 2]);
          const pixelInByte = x % 4;
          const byteIndex = byteOffset + Math.floor(x / 4);
          const bitShift = (3 - pixelInByte) * 2; // 6, 4, 2, 0
          const currentByte = pixelData.readUInt8(byteIndex);
          pixelData.writeUInt8(currentByte | (paletteIndex << bitShift), byteIndex);
        }
      }
    } else if (this.bitsPerPixel === 4) {
      // 4-bit: 16 colors, 2 pixels per byte
      for (let y = 0; y < this.height; y++) {
        let byteOffset = (this.height - 1 - y) * this.paddedWidthBytes;
        for (let x = 0; x < this.width; x++) {
          const sourceIndex = (y * this.width + x) * 3;
          const paletteIndex = this.findPaletteIndex(imageData[sourceIndex], imageData[sourceIndex + 1], imageData[sourceIndex + 2]);
          const pixelInByte = x % 2;
          const byteIndex = byteOffset + Math.floor(x / 2);
          const bitShift = (1 - pixelInByte) * 4; // 4 or 0
          const currentByte = pixelData.readUInt8(byteIndex);
          pixelData.writeUInt8(currentByte | (paletteIndex << bitShift), byteIndex);
        }
      }
    } else if (this.bitsPerPixel === 24) {
      // 24-bit: true color RGB
      // BMP is bottom-up, so we write rows from bottom to top
      let offset = 0;
      for (let bmpRow = 0; bmpRow < this.height; bmpRow++) {
        // Source row is flipped (top-down in input, bottom-up in BMP)
        const sourceRow = this.height - 1 - bmpRow;
        for (let x = 0; x < this.width; x++) {
          const sourceIndex = (sourceRow * this.width + x) * 3;
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

  // Parse a hex color string to RGB components
  parseHexColor(hexColor) {
    const r = parseInt(hexColor.slice(1, 3), 16);
    const g = parseInt(hexColor.slice(3, 5), 16);
    const b = parseInt(hexColor.slice(5, 7), 16);
    return { r, g, b };
  }

  // Find the closest palette index for an RGB color
  findPaletteIndex(r, g, b) {
    if (!this.palette || this.palette.length === 0) {
      return 0;
    }

    let minDistance = Infinity;
    let closestIndex = 0;

    for (let i = 0; i < this.palette.length; i++) {
      const { r: pr, g: pg, b: pb } = this.parseHexColor(this.palette[i]);
      
      // Use squared Euclidean distance (no need for sqrt when comparing)
      const distance = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2;

      if (distance < minDistance) {
        minDistance = distance;
        closestIndex = i;
      }
    }

    return closestIndex;
  }
}
