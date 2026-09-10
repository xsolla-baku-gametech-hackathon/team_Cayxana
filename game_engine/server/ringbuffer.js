// Fixed-capacity ring buffer. push() is O(1); toArray() returns oldest → newest.
export class RingBuffer {
  constructor(capacity) {
    if (!Number.isInteger(capacity) || capacity <= 0) {
      throw new RangeError(`RingBuffer capacity must be a positive integer, got ${capacity}`);
    }
    this.capacity = capacity;
    this.buf = new Array(capacity);
    this.start = 0;
    this.length = 0;
  }

  push(item) {
    const idx = (this.start + this.length) % this.capacity;
    this.buf[idx] = item;
    if (this.length < this.capacity) this.length++;
    else this.start = (this.start + 1) % this.capacity; // overwrote the oldest
  }

  toArray() {
    const out = new Array(this.length);
    for (let i = 0; i < this.length; i++) out[i] = this.buf[(this.start + i) % this.capacity];
    return out;
  }
}
