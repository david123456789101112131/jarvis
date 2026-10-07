'use strict';
/* Items kept in a JSON file. Writes go to a temp file first, then replace the real one. */
const fs = require('node:fs');
const path = require('node:path');

class Store {
  constructor(file) {
    this.file = file;
    this.data = { nextId: 1, items: [] };
    if (file && fs.existsSync(file)) {
      try {
        const loaded = JSON.parse(fs.readFileSync(file, 'utf8'));
        if (loaded && Array.isArray(loaded.items)) this.data = loaded;
      } catch (err) {
        /* a corrupt file starts an empty store; the old file is kept until the next write */
      }
    }
  }

  save() {
    if (!this.file) return;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = this.file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2));
    fs.renameSync(tmp, this.file);
  }

  list() { return this.data.items.map((item) => ({ ...item })); }

  get(id) {
    const item = this.data.items.find((i) => i.id === id);
    return item ? { ...item } : null;
  }

  create(fields) {
    const now = new Date().toISOString();
    const item = { ...fields, id: this.data.nextId++, createdAt: now, updatedAt: now };
    this.data.items.push(item);
    this.save();
    return { ...item };
  }

  update(id, changes) {
    const item = this.data.items.find((i) => i.id === id);
    if (!item) return null;
    Object.assign(item, changes, { updatedAt: new Date().toISOString() });
    this.save();
    return { ...item };
  }

  remove(id) {
    const before = this.data.items.length;
    this.data.items = this.data.items.filter((i) => i.id !== id);
    if (this.data.items.length === before) return false;
    this.save();
    return true;
  }
}

module.exports = { Store };
