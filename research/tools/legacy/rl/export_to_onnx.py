
import torch
import numpy as np
import onnx
import onnxruntime as ort
from stable_baselines3 import PPO
import os

MODEL_PATH = "research/rl/ppo_cycle10_v2"
ONNX_PATH = "research/rl/model_v2.onnx"

class OnnxablePolicy(torch.nn.Module):
    def __init__(self, extractor, action_net, value_net):
        super(OnnxablePolicy, self).__init__()
        self.extractor = extractor
        self.action_net = action_net
        self.value_net = value_net

    def forward(self, observation):
        # NOTE: PPO's MlpPolicy uses a feature extractor then independent action/value nets
        # We only need the actor (action_net) for inference
        features = self.extractor(observation)
        action_logits = self.action_net(features)
        return action_logits

def export_model():
    print(f"Loading model from {MODEL_PATH}...")
    if not os.path.exists(MODEL_PATH + ".zip"):
        print(f"Error: Model not found at {MODEL_PATH}.zip")
        return

    model = PPO.load(MODEL_PATH, device="cpu")
    
    # Create wrapper for export
    # We only export the Actor (Policy) part
    onnx_policy = OnnxablePolicy(
        model.policy.mlp_extractor.policy_net,
        model.policy.action_net,
        model.policy.value_net
    )
    
    # Artificial Observation (1, 11) -> 10 Features + 1 Inventory
    # Matches HftEnv observation space
    dummy_input = torch.randn(1, 11) 

    print(f"Exporting to {ONNX_PATH}...")
    torch.onnx.export(
        onnx_policy,
        dummy_input,
        ONNX_PATH,
        opset_version=11,
        input_names=["input"],
        output_names=["action_logits"],
        dynamic_axes={"input": {0: "batch_size"}, "action_logits": {0: "batch_size"}}
    )
    
    print("Export Complete. Verifying...")
    verify_export(model, onnx_policy)

def verify_export(sb3_model, torch_model):
    # Test Data
    obs_np = np.random.randn(1, 11).astype(np.float32)
    obs_tensor = torch.as_tensor(obs_np)
    
    # 1. SB3 Prediction
    with torch.no_grad():
        # SB3 predict returns (action, state)
        # We want the distribution logits for direct comparison if possible
        # Or just the raw output of our wrapped network
        pytorch_out = torch_model(obs_tensor).numpy()

    # 2. ONNX Runtime Prediction
    ort_session = ort.InferenceSession(ONNX_PATH)
    ort_inputs = {ort_session.get_inputs()[0].name: obs_np}
    ort_outs = ort_session.run(None, ort_inputs)
    onnx_out = ort_outs[0]
    
    # Compare
    print(f"PyTorch Output: {pytorch_out[0][:5]}...")
    print(f"ONNX Output:    {onnx_out[0][:5]}...")
    
    diff = np.max(np.abs(pytorch_out - onnx_out))
    print(f"Max Difference: {diff}")
    
    if diff < 1e-5:
        print("SUCCESS: Model Verified (Match).")
    else:
        print("WARNING: Mismatch detected!")

if __name__ == "__main__":
    export_model()
