import sys
import os
import argparse
import glob

# Ensure hftbacktest is in path
HFTBACKTEST_PATH = "/home/charlie/hft_platform/external_repos/hftbacktest_fresh/py-hftbacktest"
if HFTBACKTEST_PATH not in sys.path:
    sys.path.append(HFTBACKTEST_PATH)

from hftbacktest.data.utils import binancefutures

def ingest(args):
    source = args.source
    if source != 'binance-futures':
        print(f"Error: Only 'binance-futures' is currently supported. Got {source}")
        return

    # Input pattern
    # User provides a file pattern
    files = glob.glob(args.input_pattern)
    if not files:
        print(f"No files found matching {args.input_pattern}")
        return

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    for f in files:
        basename = os.path.basename(f)
        # Assuming format like "BTCUSDT-2023-01-01.zip" or similar
        # Output should be .npz
        output_name = basename.replace(".zip", "").replace(".csv", "").replace(".txt", "") + ".npz"
        output_path = os.path.join(args.output_dir, output_name)
        
        print(f"Converting {f} -> {output_path}")
        try:
            binancefutures.convert(
                input_filename=f,
                output_filename=output_path,
                opt='mt' # Default to including everything
            )
        except Exception as e:
            print(f"Failed to convert {f}: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest market data for hftbacktest")
    parser.add_argument("--source", default="binance-futures", help="Data source (e.g. binance-futures)")
    parser.add_argument("--input-pattern", required=True, help="Glob pattern for input files (e.g. 'data/*.zip')")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    
    args = parser.parse_args()
    ingest(args)
