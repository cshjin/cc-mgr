// In-memory FIFO job queue with a fixed concurrency (1 by default).
// Jobs are keyed by id; enqueueing the same id while it is pending or
// running is a no-op and returns false.
import { EventEmitter } from 'node:events';

export class Queue extends EventEmitter {
  constructor(worker, concurrency = 1) {
    super();
    this.worker = worker; // async (job) => {}
    this.concurrency = concurrency;
    this.pending = [];
    this.running = new Set();
  }

  enqueue(id, job) {
    if (this.running.has(id) || this.pending.some((j) => j.id === id)) return false;
    this.pending.push({ id, job });
    this._pump();
    return true;
  }

  _pump() {
    while (this.running.size < this.concurrency && this.pending.length > 0) {
      const { id, job } = this.pending.shift();
      this.running.add(id);
      Promise.resolve()
        .then(() => this.worker(job))
        .catch((err) => {
          if (this.listenerCount('error') > 0) {
            try { this.emit('error', id, err); } catch { /* listener threw — keep pumping */ }
          } else {
            console.error(`queue job ${id} failed:`, err);
          }
        })
        .finally(() => {
          this.running.delete(id);
          this._pump();
        });
    }
  }
}
