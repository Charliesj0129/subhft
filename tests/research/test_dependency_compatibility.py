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


def test_onnx_linear_model_cpu_roundtrip(tmp_path: Path) -> None:
    import onnx
    import onnxruntime as ort
    from onnx import TensorProto, helper, numpy_helper

    sample = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
    weights = np.array([[1.0, -1.0], [2.0, 0.0], [3.0, 1.0]], dtype=np.float32)
    bias = np.array([0.5, -0.5], dtype=np.float32)
    graph = helper.make_graph(
        [
            helper.make_node("MatMul", ["x", "weights"], ["product"]),
            helper.make_node("Add", ["product", "bias"], ["y"]),
        ],
        "linear-cpu-smoke",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 2])],
        initializer=[numpy_helper.from_array(weights, name="weights"), numpy_helper.from_array(bias, name="bias")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    onnx.checker.check_model(model)
    onnx_path = tmp_path / "linear.onnx"
    onnx.save(model, onnx_path)
    onnx.checker.check_model(onnx.load(onnx_path))
    actual = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"]).run(None, {"x": sample})[0]
    np.testing.assert_array_equal(actual, np.array([[14.5, 1.5]], dtype=np.float32))
