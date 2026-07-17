import fs from 'fs';
import { BMPEncoder } from './bmp.js';

function write(path, buf) {
  fs.writeFileSync(path, buf);
  console.log('Wrote', path, buf.length, 'bytes');
}

// Grayscale gradient
{
  const width = 256;
  const height = 64;
  const data = new Uint8Array(width * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      data[y * width + x] = x & 0xff;
    }
  }
  const enc = new BMPEncoder(width, height, 8);
  const buf = enc.encode(data);
  write('./out_gray.bmp', buf);
}

// Binary checkerboard
{
  const width = 128;
  const height = 128;
  const data = new Uint8Array(width * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      data[y * width + x] = ((x >> 4) + (y >> 4)) % 2 ? 0xFF : 0x00;
    }
  }
  const enc = new BMPEncoder(width, height, 1);
  const buf = enc.encode(data);
  write('./out_binary.bmp', buf);
}

// Assertion-based checks, including widths that need row padding
import assert from 'assert';

// 24bpp, width 5 => rowBytes 15, padding 1
{
  const width = 5;
  const height = 2;
  const data = Buffer.alloc(width * height * 3);
  for (let i = 0; i < width * height; i++) {
    data[i * 3] = i; // R
    data[i * 3 + 1] = 100 + i; // G
    data[i * 3 + 2] = 200 + i; // B
  }
  const enc = new BMPEncoder(width, height, 24);
  assert.strictEqual(enc.padding, 1);
  const buf = enc.encode(data);
  assert.strictEqual(buf.length, 54 + height * enc.paddedWidthBytes);
  // BMP rows are bottom-up: first stored row is image row 1, pixel index 5
  // Stored as B,G,R
  assert.strictEqual(buf[54], 205); // B of pixel 5
  assert.strictEqual(buf[55], 105); // G of pixel 5
  assert.strictEqual(buf[56], 5);   // R of pixel 5
  // Padding byte at end of row is 0
  assert.strictEqual(buf[54 + 15], 0);
  // Second stored row is image row 0, pixel index 0
  assert.strictEqual(buf[54 + 16], 200);
  assert.strictEqual(buf[54 + 17], 100);
  assert.strictEqual(buf[54 + 18], 0);
  console.log('24bpp width=5 OK');
}

// 1bpp, width 100 => rowBytes 13, padding 3
{
  const width = 100;
  const height = 3;
  const data = new Uint8Array(width * height);
  data[0] = 0xFF; // top-left white
  data[width * height - 1] = 0xFF; // bottom-right white
  const enc = new BMPEncoder(width, height, 1);
  assert.strictEqual(enc.rowBytes, 13);
  assert.strictEqual(enc.padding, 3);
  assert.strictEqual(enc.paddedWidthBytes, 16);
  const buf = enc.encode(data);
  assert.strictEqual(buf.length, 62 + height * 16);
  // Bottom-up: last stored row = image row 0; its first byte has MSB set
  assert.strictEqual(buf[62 + 2 * 16], 0b10000000);
  // First stored row = image row 2; bit for x=99 lives in byte 12, bit 7-(99%8)=4
  assert.strictEqual(buf[62 + 12], 1 << 4);
  console.log('1bpp width=100 OK');
}

// 8bpp, width 5 => padding 3
{
  const width = 5;
  const height = 2;
  const data = new Uint8Array(width * height);
  for (let i = 0; i < width * height; i++) data[i] = i * 10;
  const enc = new BMPEncoder(width, height, 8);
  assert.strictEqual(enc.padding, 3);
  const buf = enc.encode(data);
  assert.strictEqual(buf.length, 1078 + height * 8);
  // Bottom-up: first stored row = image row 1, first pixel value 50
  assert.strictEqual(buf[1078], 50);
  assert.strictEqual(buf[1078 + 8], 0); // image row 0, pixel 0
  console.log('8bpp width=5 OK');
}

console.log('Done');
