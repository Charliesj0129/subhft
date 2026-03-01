
import optuna
import subprocess
import sys

def objective(trial):
    # Suggest Hyperparameters
    # Coarse-to-Fine Search Concept
    
    # 1. Trading Bonus (Log Scale)
    # Range: 0.01 to 1.0
    trading_bonus = trial.suggest_float('bonus', 0.01, 1.0, log=True)
    
    # 2. Entropy Coefficient (Log Scale)
    # Range: 0.001 to 0.2
    ent_coef = trial.suggest_float('ent_coef', 0.001, 0.2, log=True)
    
    # 3. Gamma (Discrete/Categorical but effectively float)
    gamma = trial.suggest_categorical('gamma', [0.9, 0.95, 0.99])
    
    # 4. Learning Rate (Log Scale)
    lr = trial.suggest_float('lr', 1e-5, 1e-3, log=True)
    
    run_name = f"optuna_trial_{trial.number}"
    print(f"\n>>> Trial {trial.number}: Bonus={trading_bonus:.4f}, Ent={ent_coef:.4f}, Gamma={gamma}, LR={lr:.6f}")
    
    # Call Training Script
    cmd = [
        "uv", "run", "python3", "research/rl/train_rl_agent.py",
        "--bonus", str(trading_bonus),
        "--ent_coef", str(ent_coef),
        "--gamma", str(gamma),
        "--lr", str(lr),
        "--run_name", run_name
    ]
    
    try:
        # Capture Output
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = result.stdout
        
        # Parse "RESULT: <value>"
        for line in output.split('\n'):
            if line.startswith("RESULT:"):
                score = float(line.split(":")[1].strip())
                return score
                
        # If no result found
        return float('-inf')
        
    except subprocess.CalledProcessError as e:
        print(f"Trial Failed: {e}")
        return float('-inf')

def run_optimization():
    # Create Study
    # Direction: Maximize Reward
    study = optuna.create_study(direction="maximize", study_name="hft_ppo_sweep")
    
    print("Starting Optuna Study (10 Trials for Demo)...")
    study.optimize(objective, n_trials=10)
    
    print("\noptimization Complete.")
    print(f"Best Trial: {study.best_trial.value}")
    print("Best Params:")
    for k, v in study.best_trial.params.items():
        print(f"  {k}: {v}")
        
    # Visualize Importances (requires plotly, optional)
    # optuna.visualization.plot_param_importances(study)

if __name__ == "__main__":
    run_optimization()
