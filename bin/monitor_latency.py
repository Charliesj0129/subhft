#!/usr/bin/env python3
import time
import argparse
from bcc import BPF

# Ops Tool: eBPF Latency Monitor
# Usage: sudo python3 ops/monitor_latency.py --symbol "elapse" --lib ".../libhftbacktest.so"

description = """
Trace latency of a specific symbol/function using eBPF uprobes.
Useful for measuring Numba/Rust native extension performance in production.
"""

parser = argparse.ArgumentParser(description=description)
parser.add_argument("--pid", type=int, help="PID to trace (optional)", default=-1)
parser.add_argument("--lib", type=str, help="Library path to attach probe (e.g., .so or binary)")
parser.add_argument("--symbol", type=str, help="Symbol name to trace")
parser.add_argument("--min-ns", type=int, default=1000, help="Minimum execution time in ns to report")

args = parser.parse_args()

if not args.lib or not args.symbol:
    print("Error: --lib and --symbol are required.")
    exit(1)

# BPF Program
# Captures entry timestamp and calculates delta on exit.
bpf_text = """
#include <uapi/linux/ptrace.h>

struct key_t {
    u32 pid;
    u64 start_ts;
};

BPF_HASH(start_map, u32, u64);
BPF_HISTOGRAM(dist);

int probe_entry(struct pt_regs *ctx) {
    u64 ts = bpf_ktime_get_ns();
    u32 pid = bpf_get_current_pid_tgid();
    start_map.update(&pid, &ts);
    return 0;
}

int probe_return(struct pt_regs *ctx) {
    u64 *tsp, delta;
    u32 pid = bpf_get_current_pid_tgid();
    
    tsp = start_map.lookup(&pid);
    if (tsp != 0) {
        delta = bpf_ktime_get_ns() - *tsp;
        start_map.delete(&pid);
        
        if (delta > MIN_NS) {
            dist.increment(bpf_log2l(delta));
            // bpf_trace_printk("Lat: %d ns\\n", delta);
        }
    }
    return 0;
}
"""

bpf_text = bpf_text.replace("MIN_NS", str(args.min_ns))

print(f"Attaching BPF probe to {args.lib}:{args.symbol}...")
b = BPF(text=bpf_text)
b.attach_uprobe(name=args.lib, sym=args.symbol, fn_name="probe_entry", pid=args.pid)
b.attach_uretprobe(name=args.lib, sym=args.symbol, fn_name="probe_return", pid=args.pid)

print("Tracing... Hit Ctrl-C to end.")

try:
    while True:
        time.sleep(1)
        b["dist"].print_log2_hist("latency (ns)")
        print("-" * 20)
except KeyboardInterrupt:
    print("Detaching...")
