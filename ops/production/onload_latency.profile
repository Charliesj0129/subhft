# Solarflare OpenOnload Profile for HFT
# Usage: EF_Profile=latency onload ./strategy

# 1. Spinning (Busy Wait)
# Poll the NIC for 100ms before sleeping (essentially always spin in HFT)
EF_POLL_USEC=100000

# Spin CPU for 200us after syscalls
EF_SPIN_USEC=200

# 2. Interrupts
# Disable interrupts? (Polling mode handles this)
# Force interrupt affinity if needed
# EF_IRQ_CORE=...

# 3. Optimizations
# Disable TCP Loopback acceleration if not needed (for purity)
EF_TCP_SERVER_LOOPBACK=0

# Preallocate packet buffers
EF_PACKET_BUFFER_MODE=1

# Enable CTPIO (Cut-Through PIO) for ultra-low latency sends
# (Requires supportive NIC)
EF_CTPIO=1
