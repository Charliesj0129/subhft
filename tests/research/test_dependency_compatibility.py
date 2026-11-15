"""Required research dependency smoke tests; install the research group first."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def test_onnx_model_check_save_load_and_cpu_inference(tmp_path: Path) -> None:
    import onnx
    import onnxruntime as ort
    from onnx import TensorProto, helper

    graph = helper.make_graph(
        [helper.make_node("Add", ["x", "x"], ["y"])],
        "security-upgrade-smoke",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [2])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [2])],
    )
    # Match the supported ORT contract instead of ONNX's newest default IR.
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    onnx.checker.check_model(model)
    path = tmp_path / "model.onnx"
    onnx.save(model, path)
    loaded = onnx.load(path)
    onnx.checker.check_model(loaded)
    result = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]).run(
        None, {"x": np.array([1.0, -2.0], dtype=np.float32)}
    )[0]
    assert result.dtype == np.dtype("float32")
    np.testing.assert_array_equal(result, np.array([2.0, -4.0], dtype=np.float32))


def test_torch_cpu_torchscript_and_onnx_export_roundtrip(tmp_path: Path) -> None:
    import onnx
    import onnxruntime as ort
    import torch

    model = torch.nn.Linear(3, 2).eval()
    with torch.no_grad():
        model.weight.copy_(torch.tensor([[1.0, 2.0, 3.0], [-1.0, 0.0, 1.0]]))
        model.bias.copy_(torch.tensor([0.5, -0.5]))
    sample = torch.tensor([[1.0, 2.0, 3.0]], device="cpu")
    with torch.no_grad():
        expected = model(sample).numpy()
    assert sample.device.type == "cpu"
    np.testing.assert_array_equal(expected, np.array([[14.5, 1.5]], dtype=np.float32))
    scripted = torch.jit.trace(model, sample)
    script_path = tmp_path / "model.pt"
    torch.jit.save(scripted, str(script_path))
    with torch.no_grad():
        actual = torch.jit.load(str(script_path), map_location="cpu")(sample).numpy()
    np.testing.assert_array_equal(actual, expected)

    onnx_path = tmp_path / "linear.onnx"
    torch.onnx.export(model, (sample,), str(onnx_path), input_names=["x"], output_names=["y"], dynamo=True)
    onnx.checker.check_model(onnx.load(onnx_path))
    exported = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"]).run(
        None, {"x": sample.numpy()}
    )[0]
    np.testing.assert_allclose(exported, expected, rtol=1e-6, atol=1e-6)
