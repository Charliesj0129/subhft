# Numba Optimization Patterns (Ad Hoc)

## Recursive Updates for Hawkes Process
- **Problem**: Calculating Hawkes intensity with full history summation is $O(N^2)$.
- **Solution**: Use recursive formulation for exponential kernels.
  $$ \lambda(t_k) = \mu + (\lambda(t_{k-1}) - \mu) e^{-\beta(t_k - t_{k-1})} + \alpha $$
- **Result**: constant $O(1)$ time per tick update.

## Jitclass State Management
- **Pattern**: Use `@jitclass` to encapsulate strategy state instead of dictionaries or multiple lists.
- **Benefits**:
  - Typed fields (float64, int64).
  - No Python object overhead inside the hot loop.
- **Example**:
  ```python
  @jitclass([('mu', float64), ('intensity', float64)])
  class Tracker:
      ...
  ```

## Event Loop Overhead
- **Observation**: `hbt.elapse(INTERVAL)` introduces loop overhead.
- **Micro-optimization**: For pure signal calculation, direct method calls on the `Tracker` object (inside Numba) are < 1µs, whereas the full backtest loop overhead is dominant.

## Serialized Neural Inference
- **Problem**: Need to run LSTM/RNN in the hot path (< 10µs) where PyTorch/TensorFlow are not available.
- **Solution**: Manually implement the forward pass using Numba and flattened arrays.
- **Key Techniques**:
  - Store weights as `float64[:,:]` in `jitclass`.
  - Unroll matrix multiplications if dimensions are small.
  - Implement activations (Sigmoid, Tanh, Softplus) using `numpy` primitives supported by Numba.
- **Performance**: A $H=32$ LSTM step takes ~10µs.

