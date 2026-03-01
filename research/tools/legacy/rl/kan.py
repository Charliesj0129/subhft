
import torch
import torch.nn as nn
import torch.nn.functional as F
import math

class KANLinear(nn.Module):
    """
    Efficient KAN Linear Layer ( Kolmogorov-Arnold Network )
    
    Implements the formulation:
    phi(x) = w_b * act(x) + w_s * spline(x)
    
    where:
    - act(x): SiLU activation (base)
    - spline(x): B-Spline interpolation
    """
    def __init__(
        self,
        in_features,
        out_features,
        grid_size=5,
        spline_order=3,
        scale_noise=0.1,
        scale_base=1.0,
        scale_spline=1.0,
        enable_standalone_scale_spline=True,
        base_activation=torch.nn.SiLU,
        grid_eps=0.02,
        grid_range=[-1, 1],
    ):
        super(KANLinear, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.grid_size = grid_size
        self.spline_order = spline_order
        
        # h = (max - min) / grid_size
        h = (grid_range[1] - grid_range[0]) / grid_size
        
        # Grid Initialization
        # grid shape: (in_features, grid_size + 2*spline_order + 1)
        grid = (
            (
                torch.arange(-spline_order, grid_size + spline_order + 1) * h
                + grid_range[0]
            )
            .expand(in_features, -1)
            .contiguous()
        )
        self.register_buffer("grid", grid)
        
        # Base weight: (out_features, in_features)
        self.base_weight = nn.Parameter(torch.Tensor(out_features, in_features))
        
        # Spline weight: (out_features, in_features, grid_size + spline_order)
        self.spline_weight = nn.Parameter(
            torch.Tensor(out_features, in_features, grid_size + spline_order)
        )
        
        # Feature Scaler
        if enable_standalone_scale_spline:
            self.spline_scaler = nn.Parameter(
                torch.Tensor(out_features, in_features)
            )
        else:
            self.spline_scaler = None
            
        self.scale_noise = scale_noise
        self.scale_base = scale_base
        self.scale_spline = scale_spline
        self.enable_standalone_scale_spline = enable_standalone_scale_spline
        self.base_activation = base_activation()
        self.grid_eps = grid_eps
        
        self.reset_parameters()

    def reset_parameters(self):
        # Initialize Base Weights (Xavier)
        nn.init.kaiming_uniform_(self.base_weight, a=math.sqrt(5) * self.scale_base)
        
        # Initialize Spline Weights (Random Noise)
        with torch.no_grad():
            self.spline_weight.uniform_(-self.scale_noise, self.scale_noise)
            
        if self.enable_standalone_scale_spline:
            # Init scaler to scale_spline
            nn.init.constant_(self.spline_scaler, self.scale_spline)

    def b_splines(self, x: torch.Tensor):
        """
        Compute the B-spline bases for x assuming grid is uniform.
        x: (batch, in_features)
        grid: (in_features, grid_points)
        """
        assert x.dim() == 2 and x.size(1) == self.in_features
        
        grid: torch.Tensor = self.grid  # (in, grid_points)
        
        # Normalize x relative to grid start and step
        # grid[0, 0] is min (extended), grid[0, -1] is max
        # h = grid[0, 1] - grid[0, 0] (approx)
        
        # Efficient vectorization
        x = x.unsqueeze(-1) # (batch, in, 1)
        
        # Bases calculation (Recursive De Boor) is complex. 
        # Using simplified recurrence for order k
        # B_i,0(x) = 1 if t_i <= x < t_{i+1} else 0
        
        # Since grid is uniform, we can calculate bases more directly or use the expanded definition.
        # But to be robust, let's use the explicit recurrence on the grid buffer.
        
        # Expand x to match grid shape logic? No, memory intensive.
        # Let's iterate orders.
        
        grid = grid.unsqueeze(0) # (1, in, grid_len)
        
        # Order 0
        bases = ((x >= grid[:, :, :-1]) & (x < grid[:, :, 1:])).to(x.dtype)
        
        # Order 1 to k
        for k in range(1, self.spline_order + 1):
            # Term 1: (x - t_i) / (t_{i+k} - t_i)
            t_i = grid[:, :, :-(k + 1)]
            t_ipk = grid[:, :, k:-1]
            term1 = (x - t_i) / (t_ipk - t_i)
            
            # Term 2: (t_{i+k+1} - x) / (t_{i+k+1} - t_{i+1})
            t_ip1 = grid[:, :, 1:-k]
            t_ipk1 = grid[:, :, k + 1:]
            term2 = (t_ipk1 - x) / (t_ipk1 - t_ip1)
            
            bases = term1 * bases[:, :, :-1] + term2 * bases[:, :, 1:]
            
        return bases # (batch, in, grid_size + spline_order)

    def curve2coeff(self, x: torch.Tensor, y: torch.Tensor):
        """
        Compute coefficients that best fit the curve (x, y).
        Used for initialization.
        x: (grid_size, in)
        y: (grid_size, in, out)
        """
        # Simplification: we just map random noise for now.
        # Mapping true curve2coeff requires solving linear system (A'A)^-1 A'y
        # For initialization, simple mapping is sufficient.
        
        # Reshape to (out, in, grid) to match spline_weight
        return y.permute(2, 1, 0) 

    def forward(self, x: torch.Tensor):
        # x: (batch, in)
        
        # 1. Base (SiLU + Linear)
        base_output = F.linear(self.base_activation(x), self.base_weight)
        
        # 2. Spline
        # Update grid to avoid Out of Bound (Adaptive Grid logic can be added here)
        # For now, clamp x to range
        # x_clamped = torch.clamp(x, -10, 10) ? No, let's trust normalization.
        
        bases = self.b_splines(x) # (batch, in, coeff_dim)
        
        # Spline Output calculation
        # sum_i ( c_i * B_i(x) )
        # spline_weight: (out, in, coeff_dim)
        # bases: (batch, in, coeff_dim)
        
        # Apply scaler if enabled
        weight = self.spline_weight
        if self.enable_standalone_scale_spline:
            weight = weight * self.spline_scaler.unsqueeze(-1)
            
        y = torch.einsum("bid,oid->bo", bases, weight)
            
        return base_output + y

    def regularization_loss(self, regularize_activation=1.0, regularize_entropy=1.0):
        # 1. L1 norm of spline weights (Sparsity)
        l1_fake = self.spline_weight.abs().mean()
        
        # 2. Entropy of connection distributions (to prune unused inputs)
        # ...
        return l1_fake

# Helper to build a KAN Network
class KAN(nn.Module):
    def __init__(
        self,
        layers_hidden,
        grid_size=5,
        spline_order=3,
        scale_noise=0.1,
        scale_base=1.0,
        scale_spline=1.0,
        base_activation=torch.nn.SiLU,
        grid_eps=0.02,
        grid_range=[-1, 1],
    ):
        super(KAN, self).__init__()
        self.layers = nn.ModuleList()
        for in_features, out_features in zip(layers_hidden[:-1], layers_hidden[1:]):
            self.layers.append(
                KANLinear(
                    in_features,
                    out_features,
                    grid_size=grid_size,
                    spline_order=spline_order,
                    scale_noise=scale_noise,
                    scale_base=scale_base,
                    scale_spline=scale_spline,
                    base_activation=base_activation,
                    grid_eps=grid_eps,
                    grid_range=grid_range,
                )
            )

    def forward(self, x, update_grid=False):
        for layer in self.layers:
            if update_grid:
                # layer.update_grid(x) # Not implemented in concise version
                pass
            x = layer(x)
        return x
