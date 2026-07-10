"""
Proof-of-Work miner: finds a nonce such that
sha256(f"{token}:{nonce}") has at least `difficulty` leading zero bits.

Usage:
    python pow_miner.py

Edit TOKEN and DIFFICULTY below to match your assignment.
Uses all available CPU cores via multiprocessing for speed.
"""

import hashlib
import multiprocessing as mp
import time

TOKEN = "12b34582decb6c02"   # <-- your token
DIFFICULTY = 27              # <-- your required leading zero bits


def search_range(args):
    token, difficulty, start_nonce, stride, stop_event, result_queue = args
    threshold = 1 << (256 - difficulty)
    nonce = start_nonce
    checked = 0
    while not stop_event.is_set():
        h = hashlib.sha256(f"{token}:{nonce}".encode()).digest()
        val = int.from_bytes(h, "big")
        if val < threshold:
            result_queue.put(nonce)
            stop_event.set()
            return
        nonce += stride
        checked += 1
        # periodically check stop_event without too much overhead
        if checked % 200_000 == 0 and stop_event.is_set():
            return


def main():
    num_workers = mp.cpu_count()
    print(f"Using {num_workers} worker processes")
    print(f"Token: {TOKEN}, Difficulty: {DIFFICULTY} bits")
    print(f"Expected work: ~2^{DIFFICULTY} = {2**DIFFICULTY:,} hashes\n")

    manager = mp.Manager()
    stop_event = manager.Event()
    result_queue = manager.Queue()

    # Each worker searches nonce = worker_id, worker_id + num_workers, worker_id + 2*num_workers, ...
    tasks = [
        (TOKEN, DIFFICULTY, worker_id, num_workers, stop_event, result_queue)
        for worker_id in range(num_workers)
    ]

    start = time.time()
    with mp.Pool(num_workers) as pool:
        pool.map_async(search_range, tasks)
        # Wait for a result
        nonce = result_queue.get()  # blocks until a worker finds one
        stop_event.set()
        pool.terminate()
        pool.join()

    elapsed = time.time() - start
    h = hashlib.sha256(f"{TOKEN}:{nonce}".encode()).hexdigest()
    print(f"\nFOUND nonce = {nonce}")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"sha256(\"{TOKEN}:{nonce}\") = {h}")

    # Verify leading zero bits
    digest = bytes.fromhex(h)
    bits = "".join(f"{byte:08b}" for byte in digest)
    leading_zeros = len(bits) - len(bits.lstrip("0"))
    print(f"Leading zero bits: {leading_zeros} (required: {DIFFICULTY})")


if __name__ == "__main__":
    main()