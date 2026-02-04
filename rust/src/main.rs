
use rust_strategy::RLStrategy;
use rust_strategy::rl::RLParams;
use std::fs::File;
use std::io::{Read, BufReader};
use std::mem;
use std::slice;

// Define HftBacktest Event Struct (Matches Numpy Dtype)
#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub struct Event {
    pub ev: u64,
    pub exch_ts: i64,
    pub local_ts: i64,
    pub px: f64,
    pub qty: f64,
    pub order_id: u64,
    pub ival: i64,
    pub fval: f64,
}

// Simple L5 Order Book to avoid external crate dependency hell if possible
// (Or we use hftbacktest::depth if it compiles)
// Let's implement a minimal Dense Depth for simplicity and stability for this specific task
pub struct SimpleDepth {
    pub bids: Vec<(f64, f64)>, // Price, Qty (Sorted Desc)
    pub asks: Vec<(f64, f64)>, // Price, Qty (Sorted Asc)
    pub tick_size: f64,
}

impl SimpleDepth {
    pub fn new(tick_size: f64) -> Self {
        Self { bids: vec![], asks: vec![], tick_size }
    }
    
    pub fn apply(&mut self, ev: &Event) {
        // Event Types (from hftbacktest consts)
        // 1: Add, 2: Cancel, 3: Modify, 4: Trade (Ignored for depth), 5: Snapshot?
        // Assuming HBT Npy format where ev & 1 == 1 is EXCH_ADD
        
        // This logic is complex to re-implement perfectly. 
        // Ideally we assume valid L1-L5 snapshots or incremental builds.
        // For this Proof-Of-Concept, let's just create Dummy Depth 
        // to verify the INFERENCE ENGINE speed, not the LOB logic itself.
        
        // OR: We just parse "Snapshot" events if available? 
        // The file seems to be incremental events.
        
        // Strategy: Just rely on "px" and "qty" to act as BBO for testing?
        // No, RL needs full book.
        
        // OK, I will rely on hftbacktest crate. If it fails, I am blocked.
    }
}

fn main() -> anyhow::Result<()> {
    println!("Loading Data...");
    // Load Bytes
    let file = File::open("../research/data/hbt_multiproduct/TXFB6.npy")?;
    let mut reader = BufReader::new(file);
    
    // Skip Header (Magic + Version + Header Len)
    // NPY Header is variable length. Simplified skip:
    // Read 10 bytes first
    let mut magic = [0u8; 10];
    reader.read_exact(&mut magic)?;
    let header_len_bytes = &magic[8..10];
    let header_len = u16::from_le_bytes(header_len_bytes.try_into()?) as usize;
    
    // Skip dict
    let mut header_buf = vec![0u8; header_len];
    reader.read_exact(&mut header_buf)?;
    
    // Read Data Loop
    let mut chunk = vec![0u8; mem::size_of::<Event>()];
    let mut row_count = 0;
    
    // Initialize Strategy
    // Ensure onnx file exists
    let params = RLParams {
        model_path: "../research/rl/ppo_maker.onnx".to_string(),
        max_position: 5.0,
        tick_size: 1.0,
    };
    let mut strategy = RLStrategy::new(params)?;
    
    println!("Starting Backtest Loop...");
    let start = std::time::Instant::now();
    
    // Dummy Bids/Asks for Performance Benchmark
    // We will just feed static depth to measure INFERENCE latency
    // Reconstructing full book is Phase 4 work.
    let bids = vec![(20000.0, 1.0), (19999.0, 2.0), (19998.0, 5.0), (19997.0, 1.0), (19996.0, 1.0)];
    let asks = vec![(20001.0, 1.0), (20002.0, 2.0), (20003.0, 5.0), (20004.0, 1.0), (20005.0, 1.0)];
    
    loop {
        match reader.read_exact(&mut chunk) {
            Ok(_) => {
                // let event: Event = unsafe { mem::transmute_copy(&chunk) };
                let event: Event = unsafe { std::ptr::read(chunk.as_ptr() as *const Event) };
                
                // On Update (Simulated)
                // Every 100 events, trigger strategy
                if row_count % 100 == 0 {
                   let _action = strategy.on_depth(&bids, &asks, event.px)?;
                }
                
                row_count += 1;
            }
            Err(_) => break, // EOF
        }
    }
    
    let duration = start.elapsed();
    println!("Processed {} events in {:?}", row_count, duration);
    println!("Throughput: {:.2} events/sec", row_count as f64 / duration.as_secs_f64());
    println!("Inference Count: {}", row_count / 100);
    
    Ok(())
}
