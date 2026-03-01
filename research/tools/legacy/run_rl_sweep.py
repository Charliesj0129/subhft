
import subprocess
import itertools
import time

def run_sweep():
    # 1. Define Parameter Grid
    # Focused around "Taming the Churning Agent"
    
    # Bonuses: 1.0 (Baseline), 0.1 (Reduced), 0.01 (Minimal)
    bonuses = [1.0, 0.1, 0.01] 
    
    # Entropies: 0.2 (Baseline), 0.05 (More Exploitation), 0.01 (Standard)
    entropies = [0.2, 0.05, 0.01]
    
    # Combinations
    configs = list(itertools.product(bonuses, entropies))
    
    print(f"Starting Sweep: {len(configs)} Configurations...")
    
    results = []
    
    for bonus, ent in configs:
        run_name = f"model_bonus{bonus}_ent{ent}"
        print(f"\n>>> Running: Bonus={bonus}, Ent={ent} -> {run_name}")
        
        start = time.time()
        
        # Call training script
        cmd = [
            "uv", "run", "python3", "research/rl/train_rl_agent.py",
            "--bonus", str(bonus),
            "--ent_coef", str(ent),
            "--run_name", run_name
        ]
        
        try:
            subprocess.run(cmd, check=True)
            duration = time.time() - start
            print(f"Completed in {duration:.1f}s")
            results.append((run_name, "Success"))
            
            # Optional: Run quick Eval here?
            # For now, just Training Sweep.
            
        except subprocess.CalledProcessError as e:
            print(f"Failed: {e}")
            results.append((run_name, "Failed"))
            
    print("\nSweep Complete.")
    for r in results:
        print(r)

if __name__ == "__main__":
    run_sweep()
